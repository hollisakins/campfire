"""crreject: cosmic-ray flags for the exposures of one visit and filter.

An ACS/WFC exposure is a single CCD read and carries every cosmic ray that hit
it (1.5 to 3 percent of pixels per 1000 s); HST visits often have only two to
four exposures per filter. This step uses the tool built for that regime,
AstroDrizzle's rejection sequence (``drizzlepac``):

    drizzle each exposure separately -> median with ``minmed`` -> blot the
    median back to each exposure -> flag pixels that exceed it (``driz_cr``)

and nothing else of AstroDrizzle: no final combination. ``minmed`` takes the
minimum instead of the median where only two or three exposures overlap and one
stands out, which is what makes two-exposure rejection work at all.

The exposures must share a WCS frame good to a small fraction of a pixel; within
one visit HST's guiding provides that. The whole sequence runs on copies in a
private directory. Only the resulting cosmic-ray bit (4096) is transferred to
the canonical files' DQ arrays, so AstroDrizzle's own sky estimate and header
edits never reach them. The flags are read from the per-chip mask files
``driz_cr`` writes: AstroDrizzle itself puts the bit into its inputs' DQ arrays
only during the final drizzle, which is not run here.

A step that reports zero flagged pixels on a real CCD exposure has failed; the
step raises in that case instead of stamping the files.

Default thresholds (``[<instrument>.crreject]``): ACS/WFC uses the values of the legacy surveys and the
AstroDrizzle defaults (SNR ``3.5 3.0``, scale ``1.2 0.7``); WFC3/IR uses looser
ones (SNR ``8.0 5.0``, scale ``2.5 0.7``), because its up-the-ramp fit has
already removed most hits and its undersampled stellar cores are easily
mistaken for cosmic rays.
"""

import os
import shutil
import sys
import tempfile
from contextlib import contextmanager

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.config import get_hst_step_config
from campfire_pipeline.hst.exposure import open_exposure
from campfire_pipeline.imaging.outlier import warn_if_suspect

CR_BIT = 4096


@contextmanager
def _output_to(path):
    """Send everything written to standard output and error to ``path`` for
    the duration of the block, at the file-descriptor level, so that it also
    catches what a library prints from C or through its own log handlers."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    saved = os.dup(1), os.dup(2)
    try:
        with open(path, 'w') as f:
            os.dup2(f.fileno(), 1)
            os.dup2(f.fileno(), 2)
            try:
                yield
            finally:
                for stream in (sys.stdout, sys.stderr):
                    try:
                        stream.flush()
                    except Exception:
                        pass
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
    finally:
        os.close(saved[0])
        os.close(saved[1])


def _read_crmasks(work, rootname, extvers):
    """Cosmic-ray masks AstroDrizzle left in ``work`` for one exposure.

    ``driz_cr`` writes one ``*_sci<extver>_crmask.fits`` per chip, holding 1
    where the pixel passed and 0 where it was rejected. (It sets the DQ bit of
    the inputs only during the final drizzle, which this step does not run.)
    Returns ``{extver: bool array, True where a cosmic ray was found}``.
    """
    masks = {}
    for v in extvers:
        found = [n for n in os.listdir(work)
                 if n.startswith(rootname) and n.endswith(f'_sci{v}_crmask.fits')]
        if len(found) != 1:
            raise RuntimeError(
                f'crreject: expected one cosmic-ray mask for {rootname} [SCI,{v}] '
                f'in the AstroDrizzle work directory, found {found}')
        masks[v] = fits.getdata(os.path.join(work, found[0])) == 0
    return masks


def _transfer_flags(canonical, masks):
    """Set bit 4096 in ``canonical`` exactly where ``masks`` say so.

    Any 4096 bit already in the canonical file is cleared first, so a re-run
    replaces the flags instead of accumulating them. Returns the number of
    flagged pixels per chip.
    """
    tmp = f'{canonical}.cr.tmp'
    counts = []
    try:
        shutil.copyfile(canonical, tmp)
        with fits.open(tmp, mode='update', memmap=False) as dst:
            for v, new in masks.items():
                dq = np.asarray(dst['DQ', v].data)
                if new.shape != dq.shape:
                    raise RuntimeError(
                        f'crreject: mask shape {new.shape} does not match '
                        f'[DQ,{v}] {dq.shape} of {os.path.basename(canonical)}')
                bit = np.asarray(CR_BIT, dtype=dq.dtype)
                dst['DQ', v].data = (dq & ~bit) | (new.astype(dq.dtype) * bit)
                counts.append(int(new.sum()))
        os.replace(tmp, canonical)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return counts


def _restore_background(work_copy):
    """Put the background level back into a working copy of an exposure.

    The noise model of cosmic-ray rejection is Poisson: it needs the counts
    with their background. The step normally runs before ``bkg``; when it is
    run again on an exposure whose background has been subtracted, the level
    the ``bkg`` step recorded (``CFBKG``, one number per chip) is added back
    to the copy this step works on. The canonical file is not touched.
    """
    from campfire_pipeline.hst.exposure import BKG_KEY

    with fits.open(work_copy, mode='update', memmap=False) as hdul:
        for hdu in hdul:
            if hdu.name == 'SCI' and BKG_KEY in hdu.header:
                hdu.data = (hdu.data + float(hdu.header[BKG_KEY])).astype(
                    hdu.data.dtype)
                del hdu.header[BKG_KEY]


def _level_background(work_copy, bkg_config):
    """Take the shape of the background out of a working copy, keeping its level.

    The exposures of a visit are compared with their median. A light pattern
    that belongs to one exposure (scattered earthlight on a third of the
    WFC3/IR detector, up to 0.5 e-/s over a background of 0.8) stands above
    that median over its whole area, and the comparison flags pixels all
    across it: 3.5 percent of one macs0647 F140W exposure, against 0.4 percent
    of a clean exposure of the same visit. So each copy gets a coarse 2-D
    background fitted, and the fit minus its median subtracted: the copy is
    level, and still at its own counts, which the noise model needs.

    The fit is the conditioning detrend of the ``bkg`` step (``[<instrument>.
    bkg.detrend]``, a clipped estimate in large boxes), with only the unusable
    pixels masked: it has to work on a CCD exposure that is still covered in
    cosmic rays, where the source mask of the real background step would
    blank most of the frame. It is used for the comparison alone. The
    exposure itself is not touched; the ``bkg`` step runs on it afterwards,
    with the cosmic-ray flags in.
    """
    from campfire_pipeline.imaging.bkgsub import SubtractBackground

    det = bkg_config['detrend']
    if not bool(det['enabled']):
        return
    box = max(1, int(round(det['box_size'] * float(bkg_config['pixel_scale_factor']))))
    filt = det['filter_size']
    fitter = SubtractBackground(
        bg_box_size=box,
        bg_filter_size=tuple(filt) if isinstance(filt, list) else filt,
        bg_sigma=det['sigma'], bg_exclude_percentile=det['exclude_percentile'],
        bg_reject=False)
    chips = {c.extver: c for c in open_exposure(work_copy)}
    with fits.open(work_copy, mode='update', memmap=False) as hdul:
        for v, chip in chips.items():
            sci = np.asarray(hdul['SCI', v].data, dtype=np.float64)
            dq = np.asarray(hdul['DQ', v].data).astype(np.int64)
            # a cosmic-ray bit from an earlier run is not a reason to mask:
            # this run sets it again
            bad = ((dq & ~np.int64(chip.detector.tolerated_bits | CR_BIT)) != 0) \
                | ~np.isfinite(sci)
            if (~bad).sum() < 100:
                continue
            model = fitter.estimate_background(sci, bad).background
            shape = model - float(np.median(model[~bad]))
            hdul['SCI', v].data = (sci - shape).astype(hdul['SCI', v].data.dtype)


def single_exposure_masks(exposure_file, params):
    """Cosmic-ray masks of one CCD exposure from its own pixels.

    L.A.Cosmic (van Dokkum 2001; the ``astroscrappy`` implementation): a
    cosmic ray is sharper than anything the telescope can image, so a pixel is
    flagged where the Laplacian of the image stands above the noise by
    ``single_sigclip`` and above the fine structure of a real source by
    ``single_objlim``; neighbors are added down to ``single_sigfrac`` of the
    threshold. The image is in electrons with its sky, which is what the noise
    model of the method needs. Pixels the calibration already calls bad are
    handed over as a mask and are never returned. Each flagged region is then
    grown by ``single_grow`` pixels.

    Measured against the flags of four-exposure visits on the same pixels
    (two COSMOS F814W exposures, 2004 and 2015; ``lacosmic_vs_visit.py``,
    docs/hst/design.md section 8b). At the method's usual contrast limit of 5
    it flags the core of 21 of 27 and 27 of 38 stars: ACS/WFC undersamples a
    star, and its core is as sharp as a cosmic ray. At the shipped 10, with
    one pixel of growth: 0 of 27 and 3 of 38 star cores, 92 and 85 percent of
    the cosmic-ray flux caught, and 0.08 and 0.21 percent of pixels left that
    the visit flagged and that stand more than 5 sigma above the sky. A single
    exposure cannot be cleaned as well as a visit.

    Returns ``{extver: bool array}``.
    """
    import astroscrappy

    masks = {}
    chips = open_exposure(exposure_file)
    with fits.open(exposure_file, memmap=False) as hdul:
        primary = hdul[0].header
        noise = [float(primary[k]) for k in ('READNSEA', 'READNSEB', 'READNSEC',
                                             'READNSED') if primary.get(k)]
        readnoise = float(np.mean(noise)) if noise else 5.0
        for chip in chips:
            v = chip.extver
            sci = np.asarray(hdul['SCI', v].data, dtype=np.float32)
            if chip.bkg_level is not None:
                # run again after bkg: the counts with their background
                sci = sci + np.float32(chip.bkg_level)
            dq = np.asarray(hdul['DQ', v].data).astype(np.int64)
            bad = ((dq & ~np.int64(chip.detector.tolerated_bits | CR_BIT)) != 0) \
                | ~np.isfinite(sci)
            found, _ = astroscrappy.detect_cosmics(
                sci, inmask=bad, gain=1.0, readnoise=readnoise, satlevel=1e30,
                sigclip=float(params['single_sigclip']),
                sigfrac=float(params['single_sigfrac']),
                objlim=float(params['single_objlim']),
                niter=int(params['single_niter']),
                # fsmode 'median' is the method as published. The variant for
                # undersampled images ('convolve' with a Gaussian of 2.2 or 3
                # pixels) caught 3 to 7 percent of the cosmic rays on ACS/WFC
                # at any contrast limit tried (5 to 20) and is not offered.
                sepmed=True, cleantype='meanmask', fsmode='median',
                verbose=False)
            grow = int(params['single_grow'])
            if grow > 0:
                # the faint skirt of a hit, as driz_cr_grow does for a visit
                from scipy.ndimage import binary_dilation
                found = binary_dilation(found, iterations=grow)
            masks[v] = found & ~bad
    return masks


def crreject_single_step(exposure_file, step_config=None, overwrite=False):
    """Flag cosmic rays in a CCD exposure that is alone in its visit.

    There is nothing to compare it with, so the flags come from its own
    pixels (:func:`single_exposure_masks`) and go to DQ bit 4096 like those of
    :func:`crreject_step`. Where the exposure overlaps exposures of other
    visits, the field-level outlier pass compares it with them afterwards.
    WFC3/IR is refused: its ramp fit has rejected cosmic rays already.

    Returns the number of flagged pixels, or ``None`` when the file already
    carries ``CFP_CR`` and ``overwrite`` is not set.
    """
    if not overwrite and cfp.has_step(exposure_file, 'CFP_CR', keyset=cfp.HST):
        log(f'Skipping crreject on {os.path.basename(exposure_file)}: CFP_CR '
            f'already set')
        return None
    chips = open_exposure(exposure_file)
    token = chips[0].instrument
    if chips[0].is_rate:
        raise ValueError(
            f'{os.path.basename(exposure_file)} is a {token} exposure; cosmic '
            f'rays are rejected in its ramp fit, and the single-exposure '
            f'method is for CCD exposures')
    params = get_hst_step_config('crreject', token, overrides=step_config)
    masks = single_exposure_masks(exposure_file, params)
    counts = _transfer_flags(exposure_file, masks)
    npix = sum(c.shape[0] * c.shape[1] for c in chips)
    frac = sum(counts) / npix
    if frac == 0.0:
        raise RuntimeError(
            f'crreject: no cosmic rays flagged in '
            f'{os.path.basename(exposure_file)}; refusing to stamp CFP_CR on a '
            f'result that cannot be right')
    with fits.open(exposure_file, mode='update', memmap=False) as hdul:
        value = (f"lacosmic n=1 sigclip={float(params['single_sigclip']):g} "
                 f"objlim={float(params['single_objlim']):g} frac={frac:.4f}")
        for key, val in cfp.format(keyset=cfp.HST, CFP_CR=value).items():
            hdul[0].header[key] = val
    log(f'crreject: {os.path.basename(exposure_file)} alone in its visit: '
        f'flagged {sum(counts)} px ({100 * frac:.2f}%) from its own pixels')
    return sum(counts)


def crreject_step(exposure_files, step_config=None, overwrite=False,
                  bkg_config=None):
    """Flag cosmic rays in the exposures of one visit and filter.

    ``exposure_files`` are canonical files of one detector and filter that
    overlap on the sky. ``bkg_config`` is the field's ``bkg`` step table (the
    working copies are leveled with its conditioning detrend,
    :func:`_level_background`); ``None`` takes the shipped one. Returns ``{path: flagged pixel count}``, or ``None``
    when every file already carries ``CFP_CR`` and ``overwrite`` is not set.
    A single exposure cannot be treated this way and raises ``ValueError``
    (:func:`crreject_single_step` is for that).
    """
    files = sorted(exposure_files)
    if not overwrite and all(cfp.has_step(f, 'CFP_CR', keyset=cfp.HST) for f in files):
        log(f'Skipping crreject: CFP_CR already set on all {len(files)} exposures')
        return None
    if len(files) < 2:
        raise ValueError(
            'crreject needs at least two exposures; a single exposure is cleaned '
            'from its own pixels by crreject_single_step')

    chips = {f: open_exposure(f) for f in files}
    tokens = {c.instrument for cs in chips.values() for c in cs}
    filters = {c.filter for cs in chips.values() for c in cs}
    if len(tokens) != 1 or len(filters) != 1:
        raise ValueError(
            f'crreject takes one detector and filter at a time, got '
            f'{sorted(tokens)} / {sorted(filters)}')
    token = tokens.pop()
    first = chips[files[0]][0]
    params = get_hst_step_config('crreject', token, overrides=step_config)
    bkg_cfg = get_hst_step_config('bkg', token, overrides=bkg_config)
    bits = first.detector.tolerated_bits

    from drizzlepac import astrodrizzle

    work = tempfile.mkdtemp(prefix='.crrej_', dir=os.path.dirname(files[0]))
    cwd = os.getcwd()
    result = {}
    try:
        names = []
        for f in files:
            shutil.copyfile(f, os.path.join(work, os.path.basename(f)))
            names.append(os.path.basename(f))
            _restore_background(os.path.join(work, os.path.basename(f)))
            _level_background(os.path.join(work, os.path.basename(f)), bkg_cfg)
        os.chdir(work)
        # AstroDrizzle prints several hundred lines per visit. They go to a
        # file in the work directory; its end is shown only if the run fails.
        adz_log = os.path.join(work, 'astrodrizzle_output.txt')
        try:
            with _output_to(adz_log):
                astrodrizzle.AstroDrizzle(
                    # clean=False: the per-chip cosmic-ray masks are the
                    # product we want; the whole work directory is removed
                    # below.
                    names, output='crrej', build=False, preserve=False,
                    clean=False, context=False, num_cores=1, updatewcs=False,
                    resetbits=CR_BIT, crbit=CR_BIT,
                    static=True, skysub=True, skymethod='localmin',
                    driz_separate=True, driz_sep_bits=bits,
                    median=True, combine_type=params['combine_type'],
                    combine_nsigma=params['combine_nsigma'],
                    combine_grow=int(params['combine_grow']),
                    blot=True, driz_cr=True, driz_cr_corr=False,
                    driz_cr_snr=params['driz_cr_snr'],
                    driz_cr_scale=params['driz_cr_scale'],
                    driz_cr_grow=int(params['driz_cr_grow']),
                    driz_cr_ctegrow=int(params['driz_cr_ctegrow']),
                    driz_combine=False,
                )
        except Exception as e:
            tail = ''
            if os.path.exists(adz_log):
                with open(adz_log, errors='replace') as f:
                    tail = f.read()[-3000:]
            raise RuntimeError(
                f'crreject: AstroDrizzle failed on {names}: {e}\n'
                f'--- end of its output ---\n{tail}') from e
        os.chdir(cwd)

        stamp_params = f"snr={params['driz_cr_snr']} scale={params['driz_cr_scale']}"
        all_masks = {
            f: _read_crmasks(work, chips[f][0].rootname,
                             [c.extver for c in chips[f]])
            for f in files
        }
        # An ACS/WFC exposure of any length has cosmic rays. Finding none means
        # the rejection did not run as intended; say so before touching a file.
        if token == 'acswfc':
            empty = [os.path.basename(f) for f, m in all_masks.items()
                     if not any(a.any() for a in m.values())]
            if empty:
                raise RuntimeError(
                    f'crreject: no cosmic rays flagged in {empty}; refusing to '
                    f'stamp CFP_CR on a result that cannot be right')
        for f in files:
            counts = _transfer_flags(f, all_masks[f])
            npix = sum(c.shape[0] * c.shape[1] for c in chips[f])
            frac = sum(counts) / npix
            with fits.open(f, mode='update', memmap=False) as hdul:
                value = f'adz n={len(files)} {stamp_params} frac={frac:.4f}'
                for key, val in cfp.format(keyset=cfp.HST, CFP_CR=value).items():
                    hdul[0].header[key] = val
            result[f] = sum(counts)
            log(f'crreject: {os.path.basename(f)} flagged {sum(counts)} px '
                f'({100 * frac:.2f}%)')
            warn_if_suspect('crreject', os.path.basename(f), frac, log)
    finally:
        os.chdir(cwd)
        shutil.rmtree(work, ignore_errors=True)
    return result
