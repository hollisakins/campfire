"""sky: per-chip sky level, ACS amplifier offsets, and the variance scale.

Per-exposure step on a canonical HST file. For each chip it

1. builds a source mask (iterative clipping above the sky, grown by a few
   pixels) on the good pixels;
2. for a full-frame ACS/WFC exposure, measures the sky step between the two
   amplifiers that read the chip and **removes it from the pixels** — a
   detector effect calacs leaves in (ACS Data Handbook 4.2: a random offset of a
   few tenths of a DN per quadrant and exposure), and the one thing this step
   changes in ``SCI``;
3. records the chip's sky level as ``CFSKY`` (native ``SCI`` units). The sky is
   **not** subtracted from the file: the adapter subtracts it when it hands the
   chip to the mosaic code, and the cosmic-ray step needs the true counts;
4. compares the measured scatter of blank sky with the background variance the
   adapter derives from ``ERR`` and records the ratio as ``CFVARSC``. For
   ACS/WFC the adapter multiplies its background variance by it, so drizzle
   weights and the mosaic ``ERR`` follow the noise that is actually in the
   frame: on CTE-corrected ACS data ``ERR`` is low by tens of percent in
   variance. For WFC3/IR the ratio is recorded and not applied, because
   neighboring pixels are correlated there and the pixel scatter understates
   the noise in an aperture (``hst/instruments.py``).

Header keywords written to each chip's ``SCI`` header: ``CFSKY``, ``CFSKYSIG``
(measured sky scatter per pixel, native units), ``CFVARSC``, and for ACS
``CFAMPO1`` / ``CFAMPO2`` (the offset removed from each amplifier half, native
units). ``CFP_SKY`` is stamped on the primary header.

Re-running with ``overwrite`` measures again on the already-corrected pixels;
any further amplifier offset is removed and added to the recorded total.
"""

import os
import shutil

import numpy as np
from astropy.io import fits
from astropy.stats import mad_std
from scipy.ndimage import binary_dilation, uniform_filter

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.hst.exposure import SKY_KEY, VARSCALE_KEY, HstChip, open_exposure

SKYSIG_KEY = 'CFSKYSIG'
AMP_KEYS = ('CFAMPO1', 'CFAMPO2')


def source_mask(sci, good, *, nsigma=3.0, grow=3, smooth=5, maxiters=3):
    """True where a pixel must not enter sky statistics.

    Sources are found on a box-smoothed copy of the image (``smooth`` pixels),
    thresholded at ``nsigma`` robust sigmas of the smoothed sky, so that a
    connected source triggers but an isolated noise peak does not. Sharp spikes
    (cosmic rays, hot pixels) are caught separately at 5 sigma of the unsmoothed
    sky. Both are grown by ``grow`` pixels. Thresholding single pixels at a low
    sigma and growing them would mask a large, noise-selected share of blank
    sky and bias both the level and the scatter measured from the rest.

    The mask only grows from one iteration to the next, and stops when it no
    longer changes. Bad pixels are always masked.
    """
    use = good.copy()
    for _ in range(maxiters):
        if use.sum() < 10:
            break
        med = float(np.median(sci[use]))
        sig = float(mad_std(sci[use]))
        if not np.isfinite(sig) or sig <= 0:
            break
        work = np.where(use, sci, med)           # masked pixels sit at the sky
        sm = uniform_filter(work, size=int(smooth), mode='nearest')
        sm_sig = float(mad_std(sm[use]))
        if not np.isfinite(sm_sig) or sm_sig <= 0:
            break
        flagged = (sm > med + nsigma * sm_sig) | (good & (np.abs(sci - med) > 5.0 * sig))
        if grow:
            flagged = binary_dilation(flagged, iterations=int(grow))
        new_use = use & ~flagged
        if new_use.sum() == use.sum():
            break
        use = new_use
    return ~use


def amplifier_slices(chip, primary_header):
    """Column slices of the amplifier halves of a chip, or one full slice.

    A full-frame ACS/WFC chip is read through two amplifiers, one per half of
    the 4096 columns (``CCDAMP = 'ABCD'``). Subarrays and WFC3/IR are treated as
    a single region.
    """
    ny, nx = chip.shape
    amps = str(primary_header.get('CCDAMP', '')).strip().upper()
    if chip.detector.token == 'acswfc' and len(amps) == 4:
        return [slice(0, nx // 2), slice(nx // 2, nx)]
    return [slice(0, nx)]


def measure_chip(chip: HstChip, sci, err, dq, time, slices, *, nsigma=3.0, grow=3):
    """Sky, amplifier offsets, sky scatter and variance scale of one chip.

    All returned levels are in the chip's native ``SCI`` units. ``sci`` is not
    modified; the offsets are returned for the caller to subtract.
    """
    bits = (chip.detector.tolerated_bits if chip.tolerated_bits is None
            else chip.tolerated_bits)
    good = ((dq.astype(np.int64) & ~np.int64(bits)) == 0) & np.isfinite(sci) \
        & np.isfinite(err) & (err > 0)
    mask = source_mask(sci, good, nsigma=nsigma, grow=grow)
    blank = ~mask
    if blank.sum() < 100:
        raise ValueError(f'{chip.key}: too few blank-sky pixels ({int(blank.sum())})')

    sky = float(np.median(sci[blank]))
    offsets = []
    corrected = sci.astype(np.float64, copy=True)
    for sl in slices:
        part = blank[:, sl]
        off = float(np.median(sci[:, sl][part]) - sky) if part.sum() >= 100 else 0.0
        offsets.append(off)
        corrected[:, sl] -= off
    sky = float(np.median(corrected[blank]))
    sigma = float(mad_std(corrected[blank]))

    # Model background variance exactly as the adapter derives it (unscaled).
    rate, var_rate, t_eff = chip.to_rate(corrected, err.astype(np.float64),
                                         time, sky)
    ok = good & (t_eff > 0)
    var_bkg = HstChip._background_variance(rate, var_rate, t_eff, ok)
    sel = blank & ok & np.isfinite(var_bkg)
    measured = float(mad_std(rate[sel])) ** 2
    modelled = float(np.median(var_bkg[sel]))
    scale = measured / modelled if modelled > 0 else 1.0
    return {'sky': sky, 'offsets': offsets, 'sigma': sigma,
            'var_scale': float(scale), 'blank_fraction': float(blank.mean())}


def sky_step(exposure_file, step_config=None, overwrite=False):
    """Run the sky step on one canonical HST exposure. Returns per-chip results,
    or ``None`` when the file already carries ``CFP_SKY`` and ``overwrite`` is
    not set."""
    cfg = step_config or {}
    rootname = os.path.basename(exposure_file).split('_')[0]
    if cfp.should_skip(exposure_file, 'CFP_SKY', rootname, 'sky', None, overwrite,
                       keyset=cfp.HST):
        return None

    chips = open_exposure(exposure_file)
    tmp = f'{exposure_file}.sky.tmp'
    results = {}
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
                slices = amplifier_slices(chip, primary)
                res = measure_chip(chip, sci, err, dq, time, slices,
                                   nsigma=float(cfg.get('mask_nsigma', 3.0)),
                                   grow=int(cfg.get('mask_grow', 3)))
                h = sci_hdu.header
                if len(slices) > 1:
                    for sl, off in zip(slices, res['offsets']):
                        sci[:, sl] -= off
                    sci_hdu.data = sci.astype(sci_hdu.data.dtype)
                    for key, off in zip(AMP_KEYS, res['offsets']):
                        total = float(h.get(key, 0.0)) + off
                        h[key] = (total, 'campfire: amplifier offset removed')
                h[SKY_KEY] = (res['sky'], 'campfire: chip sky level (not subtracted)')
                h[SKYSIG_KEY] = (res['sigma'], 'campfire: measured sky scatter / pixel')
                h[VARSCALE_KEY] = (res['var_scale'],
                                   'campfire: measured / ERR-model sky variance')
                results[chip.key] = res
                offs = ', '.join(f'{o:+.3f}' for o in res['offsets'])
                log(f"sky: {chip.key} sky={res['sky']:.3f} sigma={res['sigma']:.3f} "
                    f"amp offsets=[{offs}] var_scale={res['var_scale']:.3f}")
            summary = ' '.join(f"{r['sky']:.2f}/{r['var_scale']:.2f}"
                               for r in results.values())
            for key, val in cfp.format(keyset=cfp.HST,
                                       CFP_SKY=f'sky/varscale {summary}').items():
                primary[key] = val
        os.replace(tmp, exposure_file)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return results
