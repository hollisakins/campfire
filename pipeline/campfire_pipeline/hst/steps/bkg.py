"""bkg: the per-exposure background step, the same chain as NIRCam's.

Per-exposure step on a canonical HST file, after the cosmic-ray flags are in.
For each chip it runs the chain of ``imaging/bkg_chain.py``, the loop the
NIRCam ``bkg`` step runs (``nircam/steps/bkg.py`` gives the reasons for each
stage)::

    for _ in range(n_iterations):
        mask    = source mask (SubtractBackground, mask only), on the residual
        detrend = fit-only coarse 2-D structure model (zero median)
        ped     = level of each amplifier on (residual - detrend)
        b2d     = applied smooth 2-D background (subtract_2d)
        residual -= ped + b2d                        # detrend not subtracted
    variance scale on (residual - detrend), final mask

and **subtracts the result from ``SCI``**, as the NIRCam step does. A light
pattern that belongs to one exposure (scattered earthlight on the left of the
WFC3/IR detector, a gradient across an ACS chip) is removed here, where it is
still one exposure's; the mosaic sees exposures on a common zero level.

What is adapted to the HST detectors, and only that:

* **No readout pattern terms.** NIRCam's column and amplifier-row 1/f terms
  model the noise of its readout. calacs and calwf3 deliver exposures without
  such a pattern (bias striping is removed by calacs, the WFC3/IR reference
  pixels by calwf3), so the chain runs without them and the fit order of the
  applied 2-D model has nothing to be ordered against.
* **Amplifiers.** The pedestal is one level per amplifier, as on NIRCam. A
  full-frame ACS/WFC chip is read through two amplifiers, one per half of its
  4096 columns, and calacs leaves a random offset of a few tenths of a DN
  between them (ACS Data Handbook 4.2). WFC3/IR is treated as one region.
* **Pixel scale.** Every length in the mask and the fits is an angular scale
  written in pixels of 30 mas, as in ``[nircam.bkg]``, and is multiplied by
  ``pixel_scale_factor`` = 30 mas / (the detector's pixel).
* **Units and variance.** ``SCI`` stays in the detector's native units
  (electrons, or electrons per second). HST has one ``ERR`` array, which is
  left alone: it carries the Poisson noise of the background that was there.
  The variance rule is NIRCam's (``variance_rescale``: the scatter of blank
  background in blocks, against the modelled background variance); the ratio
  is recorded as ``CFVARSC`` and the adapter (``hst/exposure.py``) multiplies
  its background variance by it, where NIRCam rescales ``VAR_RNOISE`` in the
  file.
* **Cosmic rays.** The cosmic-ray step of a CCD needs the counts with their
  background (its noise model is Poisson). It runs before this step; when it
  is run again on a subtracted file it adds the recorded level back to its
  own working copy (``hst/steps/crreject.py``).

Written per chip: the subtracted ``SCI``; ``CFBKG`` in the ``SCI`` header, the
mean level removed over blank pixels in native units (summed over runs, so the
original level stays known); ``CFVARSC``; a ``SRCMASK`` image extension, the
final source mask. ``CFP_BKG`` is stamped on the primary header.
"""

import os
import shutil

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.config import get_hst_step_config
from campfire_pipeline.hst.exposure import (
    BKG_KEY, VARSCALE_KEY, HstChip, open_exposure,
)
from campfire_pipeline.imaging.bkg_chain import (
    region_pedestal, run_chain, scale_mask_config, variance_rescale,
)
from campfire_pipeline.imaging.bkgsub import SubtractBackground

SRCMASK_EXT = 'SRCMASK'
# Cards of the step this one replaced (a constant level per chip, recorded and
# not subtracted). Removed when this step writes, so a file says one thing.
_RETIRED_SCI_KEYS = ('CFSKY', 'CFSKYSIG', 'CFAMPO1', 'CFAMPO2')
_RETIRED_STAMP = 'CFP_SKY'


def amplifier_regions(chip, primary_header):
    """``(row slice, column slice)`` of each amplifier's part of a chip.

    A full-frame ACS/WFC chip is read through two amplifiers, one per half of
    the 4096 columns (``CCDAMP = 'ABCD'``). Subarrays and WFC3/IR are one
    region.
    """
    ny, nx = chip.shape
    amps = str(primary_header.get('CCDAMP', '')).strip().upper()
    if chip.detector.token == 'acswfc' and len(amps) == 4:
        return [(slice(0, ny), slice(0, nx // 2)),
                (slice(0, ny), slice(nx // 2, nx))]
    return [(slice(0, ny), slice(0, nx))]


def _fitters(cfg):
    """The source-mask / applied-fit object and the detrend fitter of a config,
    with every length scaled to the detector's pixel."""
    f = float(cfg['pixel_scale_factor'])
    mcfg = scale_mask_config(dict(cfg['mask']), f)
    subtract_2d = bool(cfg['subtract_2d'])
    b2d = cfg['bkg2d']
    b2d_box = max(1, int(round(b2d['box_size'] * f)))
    b2d_dilate = float(b2d['extra_dilate']) * f
    if subtract_2d:
        mcfg.update(
            bg_box_size=b2d_box,
            bg_filter_size=b2d['filter_size'],
            bg_sigma=b2d['sigma'],
            bg_exclude_percentile=b2d['exclude_percentile'],
            bg_reject=b2d['reject'],
            bg_reject_sigma_hi=b2d['reject_sigma_hi'],
            bg_reject_sigma_lo=b2d['reject_sigma_lo'],
            bg_reject_percentile=b2d['reject_percentile'],
            bg_reject_dilate=float(b2d['reject_dilate']) * f,
        )
    sb = SubtractBackground.from_config(mcfg)

    det = cfg['detrend']
    sb_det, det_box = None, 0
    if bool(det['enabled']):
        det_box = max(1, int(round(det['box_size'] * f)))
        det_filter = det['filter_size']
        if isinstance(det_filter, list):
            det_filter = tuple(det_filter)
        sb_det = SubtractBackground(
            bg_box_size=det_box, bg_filter_size=det_filter,
            bg_sigma=det['sigma'],
            bg_exclude_percentile=det['exclude_percentile'],
            bg_reject=False,
        )
    return sb, sb_det, {'subtract_2d': subtract_2d, 'b2d_box': b2d_box,
                        'b2d_dilate': b2d_dilate, 'det_box': det_box}


def background_of_chip(chip: HstChip, sci, err, dq, time, regions, cfg,
                       components_out=None):
    """Run the chain on one chip. ``sci`` is not modified.

    Returns a dict: ``correction`` (what to subtract from ``SCI``, native
    units), ``srcmask``, ``level`` (mean of the correction over blank
    pixels), ``levels`` (the last iteration's pedestal per amplifier),
    ``var_scale`` and the scaled fit sizes.
    """
    bits = (chip.detector.tolerated_bits if chip.tolerated_bits is None
            else chip.tolerated_bits)
    good = ((dq.astype(np.int64) & ~np.int64(bits)) == 0) & np.isfinite(sci) \
        & np.isfinite(err) & (err > 0)
    if time is not None:
        good &= time > 0
    if good.sum() < 100:
        raise ValueError(f'{chip.key}: too few usable pixels ({int(good.sum())})')

    sb, sb_det, sizes = _fitters(cfg)
    ped_cfg = cfg['pedestal']
    scope = str(ped_cfg['scope'])
    if scope not in ('per_amp', 'frame'):
        raise ValueError(f"[{chip.instrument}.bkg.pedestal].scope={scope!r} "
                         f"is not 'per_amp' or 'frame'")
    if scope == 'frame':
        ny, nx = sci.shape
        regions = [(slice(0, ny), slice(0, nx))]
    levels = []

    def _pedestal(image, mask):
        ped, per_region = region_pedestal(
            image, mask, regions, sigma=float(ped_cfg['sigma']),
            maxiters=int(ped_cfg['maxiters']))
        levels[:] = per_region
        return ped

    # The source mask is seeded with the unusable pixels the way NIRCam's is
    # with DO_NOT_USE (bit 1 of the JWST convention the mask code reads).
    seed = (~good).astype(np.uint32)
    chain = run_chain(
        sci, err, seed, ~good, sb=sb, sb_det=sb_det,
        n_iter=int(cfg['n_iterations']), remask=True,
        pedestal_fn=_pedestal, striping_fn=None,
        subtract_2d=sizes['subtract_2d'], b2d_order='last',
        b2d_extra_dilate=sizes['b2d_dilate'], components_out=components_out)
    correction, srcmask = chain['correction'], chain['srcmask']

    # Variance: NIRCam's rule, on the conditioned residual, against the
    # background variance the adapter derives from ERR.
    resid = chain['resid'] - chain['det_struct']
    rate, var_rate, t_eff = chip.to_rate(resid, err.astype(np.float64), time, 0.0)
    ok = good & (t_eff > 0)
    var_bkg = HstChip._background_variance(rate, var_rate, t_eff, ok)
    var_bkg = np.where(np.isfinite(var_bkg), var_bkg, 0.0)
    var_scale = variance_rescale(rate, var_bkg, srcmask | ~ok,
                                 int(cfg['variance']['block_size']))

    blank = good & ~srcmask & np.isfinite(correction)
    level = float(np.mean(correction[blank])) if blank.any() else 0.0
    if components_out is not None:
        components_out['before'] = sci.copy()
        components_out['srcmask'] = srcmask.copy()
        components_out['good'] = good
    return {'correction': correction, 'srcmask': srcmask, 'level': level,
            'levels': list(levels), 'var_scale': float(var_scale),
            'blank_fraction': float(blank.mean()), 'scope': scope, **sizes}


def bkg_step(exposure_file, step_config=None, overwrite=False,
             components_out=None):
    """Run the background step on one canonical HST exposure.

    Returns per-chip results, or ``None`` when the file already carries
    ``CFP_BKG`` and ``overwrite`` is not set. ``components_out``: optional
    dict, filled per chip key with the chain's component images, for
    diagnostics; no effect on processing.
    """
    rootname = os.path.basename(exposure_file).split('_')[0]
    if cfp.should_skip(exposure_file, 'CFP_BKG', rootname, 'bkg', None, overwrite,
                       keyset=cfp.HST):
        return None

    chips = open_exposure(exposure_file)
    cfg = get_hst_step_config('bkg', chips[0].instrument, overrides=step_config)
    tmp = f'{exposure_file}.bkg.tmp'
    results, before, after = {}, [], []
    try:
        shutil.copyfile(exposure_file, tmp)
        with fits.open(tmp, mode='update', memmap=False) as hdul:
            primary = hdul[0].header
            for chip in chips:
                v = chip.extver
                sci_hdu = hdul['SCI', v]
                sci = np.asarray(sci_hdu.data, dtype=np.float64)
                err = np.asarray(hdul['ERR', v].data, dtype=np.float64)
                dq = np.asarray(hdul['DQ', v].data)
                time = None
                if chip.is_rate and ('TIME', v) in hdul:
                    time = np.asarray(hdul['TIME', v].data, dtype=np.float64)
                comp = None
                if components_out is not None:
                    comp = components_out.setdefault(chip.key, {})
                res = background_of_chip(
                    chip, sci, err, dq, time, amplifier_regions(chip, primary),
                    cfg, components_out=comp)

                out = sci - res['correction']
                out = np.where(np.isfinite(sci), out, sci)
                sci_hdu.data = out.astype(sci_hdu.data.dtype)
                h = sci_hdu.header
                total = float(h.get(BKG_KEY, 0.0)) + res['level']
                h[BKG_KEY] = (total, 'campfire: background level removed (native)')
                h[VARSCALE_KEY] = (res['var_scale'],
                                   'campfire: measured / ERR-model sky variance')
                for key in _RETIRED_SCI_KEYS:
                    if key in h:
                        del h[key]
                mask_hdu = fits.ImageHDU(res['srcmask'].astype('uint8'),
                                         name=SRCMASK_EXT, ver=v)
                if (SRCMASK_EXT, v) in hdul:
                    hdul[SRCMASK_EXT, v] = mask_hdu
                else:
                    hdul.append(mask_hdu)
                res['total_level'] = total
                results[chip.key] = res
                before.append(sci)
                after.append(out)
                if comp is not None:
                    comp['after'] = out.copy()
                amps = ', '.join(f'{x:+.3f}' for x in res['levels'])
                log(f"bkg: {chip.key} level={res['level']:.4f} "
                    f"(total {total:.4f}) amplifiers=[{amps}] "
                    f"var_scale={res['var_scale']:.3f}")

            first = next(iter(results.values()))
            levels = '/'.join(f"{r['total_level']:.4g}" for r in results.values())
            factors = '/'.join(f"{r['var_scale']:.3f}" for r in results.values())
            value = (f"n_iter={int(cfg['n_iterations'])}, level={levels}, "
                     f"ped_scope={first['scope']}, var_factor={factors}, "
                     f"detrend={'box%d' % first['det_box'] if first['det_box'] else 'off'}, "
                     f"subtract_2d={first['subtract_2d']}")
            if first['subtract_2d']:
                value += (f", bkg2d_box={first['b2d_box']}, "
                          f"bkg2d_dilate={first['b2d_dilate']:.0f}")
            if _RETIRED_STAMP in primary:
                del primary[_RETIRED_STAMP]
            for key, val in cfp.format(keyset=cfp.HST, CFP_BKG=value).items():
                primary[key] = val
        os.replace(tmp, exposure_file)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise

    if cfg.get('plot', True):
        try:
            from campfire_pipeline.nircam.steps._plots import plot_two
            pdf = os.path.join(os.path.dirname(exposure_file),
                               f'{os.path.basename(exposure_file)[:-5]}_bkg.pdf')
            width = max(a.shape[1] for a in before)

            def stack(arrays):
                return np.vstack([np.pad(a, ((0, 0), (0, width - a.shape[1])))
                                  for a in arrays])
            level = float(np.mean([r['level'] for r in results.values()]))
            plot_two(stack(before) - level, stack(after),
                     title1=f'{rootname}: before (level removed)',
                     title2='after bkg', save_file=pdf, scaling=1)
            log(f'Saved {os.path.basename(pdf)}')
        except Exception as e:  # plotting must never fail the step
            log(f'bkg plot failed for {rootname}: {e}')
    return results
