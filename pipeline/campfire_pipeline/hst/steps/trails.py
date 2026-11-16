"""trails: satellite trails on ACS/WFC exposures, masked without review.

Per-exposure step on a canonical ACS/WFC file, between ``sky`` and
``crreject``. Each chip goes through the STScI trail finder
(``acstools.findsat_mrt``: a median Radon transform of the binned,
background-subtracted chip, in which a straight streak is a point; Stark et
al. 2022, ACS ISR 2022-08), and the pixels of every trail it is confident of
get DQ bit 16384, the bit ``acstools`` itself uses for satellite trails. No
tolerated-bits set includes it, so the cosmic-ray step, the alignment, the
field-level outlier pass and the drizzle all leave those pixels out.

Why here and not left to the cosmic-ray steps: a trail is tens of pixels wide
with faint wings, the per-visit rejection removes its core only, and an
exposure alone in its visit has nothing to be compared with.

**What is masked.** At its own defaults the finder also passes things that are
not satellites, and each would cost a stripe across the whole chip. On 40
COSMOS F814W exposures (80 chips) it reports 11 trails. Six are real (five
satellites, one crossing both chips): persistence, the share of the streak's
length along which it is detected, is 0.86 to 1, and the signal-to-noise 10
to 660. Five are not (three diffraction spikes of bright stars, a line through
two galaxies, a chord of a scattered-light arc): persistence 0.56 to 0.67,
signal-to-noise 6 to 9 (docs/hst/design.md, section 8b). A mask nobody reviews
has to be right, so only a candidate with at least ``min_persistence`` and
``min_snr`` is masked.

**What is not.** A streak that does not run the length of the chip (a
satellite that flares, or enters or leaves during the exposure) has a low
persistence and is not masked here, at these cuts or at the finder's own. The
cosmic-ray step removes its core like any other sharp feature: on the one case
in those exposures (signal-to-noise 40, persistence 0.42) about half the
pixels within 2 pixels of the line are flagged and the rest carry 0.1 to 0.5
sigma per pixel. What is left of such a streak, and any trail too faint for
the cuts, is for the outlier pass and a hand-drawn mask (``apply_masks``),
like any other artifact.

**Cost.** About 2.5 core-minutes per exposure (the transform of two chips at
``binsize`` 2). Binning by 4 halves that and loses the faintest of the six.

**Undoing one.** ``ignore`` in ``[<field>.acswfc.trails]`` lists rootnames
whose trails are found and logged but not masked.

The step is ACS/WFC only. On WFC3/IR a satellite crosses during a few reads
of the ramp and ``calwf3`` rejects those reads as it does a cosmic ray.

Stamps ``CFP_TRL = 'findsat n=<trails masked> px=<pixels>'``. A re-run clears
bit 16384 first, so the flags are replaced, not accumulated.
"""

import contextlib
import logging
import os
import shutil
import tempfile
import warnings

import numpy as np
from astropy.io import fits
from scipy.ndimage import binary_dilation

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.config import get_hst_step_config

TRAIL_BIT = 16384
_FINDER_LOGGERS = ('findsat_mrt', 'utils_findsat_mrt')


@contextlib.contextmanager
def _quiet_finder():
    """The finder logs every fit of every candidate at INFO; keep it to
    warnings while it runs."""
    loggers = [logging.getLogger(n) for n in _FINDER_LOGGERS]
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for lg, level in zip(loggers, levels):
            lg.setLevel(level)


def _expand(mask, shape):
    """A mask made on the binned chip, back at the chip's own shape."""
    fy, fx = shape[0] // mask.shape[0], shape[1] // mask.shape[1]
    if fy != fx or fy < 1:
        raise ValueError(f'trail mask {mask.shape} does not bin into {shape}')
    big = np.repeat(np.repeat(mask, fy, axis=0), fx, axis=1)
    out = np.zeros(shape, dtype=bool)
    ny, nx = min(shape[0], big.shape[0]), min(shape[1], big.shape[1])
    out[:ny, :nx] = big[:ny, :nx]
    # rows / columns the binning dropped take the value of their neighbor
    out[ny:, :nx] = out[ny - 1, :nx]
    out[:, nx:] = out[:, nx - 1:nx]
    return out


def chip_trails(exposure_file, extver, params, processes=1):
    """Find the satellite trails of one chip.

    Returns ``(mask, trails)``: ``mask`` is True on the pixels of the trails
    that pass ``min_snr`` and ``min_persistence``, at the chip's shape and
    grown by ``grow`` pixels; ``trails`` lists every candidate the finder kept
    at its own thresholds, each a dict with ``snr``, ``persistence``,
    ``width`` (native pixels), ``theta`` (degrees) and ``masked``.
    """
    import acstools.findsat_mrt as findsat
    from acstools.utils_findsat_mrt import create_mask

    with fits.open(exposure_file, memmap=False) as hdul:
        index = hdul.index_of(('SCI', extver))
        dq_index = hdul.index_of(('DQ', extver))
        shape = (hdul[index].header['NAXIS2'], hdul[index].header['NAXIS1'])
    if index not in (1, 4) or dq_index != index + 2:
        raise ValueError(
            f'trails: {os.path.basename(exposure_file)} does not have the '
            f'standard ACS/WFC extension order ([SCI,{extver}] is HDU {index}, '
            f'[DQ,{extver}] is HDU {dq_index}); the trail finder reads the '
            f'chips by position')

    binsize = int(params['binsize'])
    with tempfile.TemporaryDirectory() as tmp, _quiet_finder(), \
            warnings.catch_warnings():
        # medians over rows of the transform that hold no pixel
        warnings.simplefilter('ignore', RuntimeWarning)
        finder = findsat.WfcWrapper(
            exposure_file, extension=index, binsize=binsize,
            processes=int(processes), execute=False, output_dir=tmp,
            threshold=float(params['detect_threshold']),
            max_width=float(params['max_width']),
            min_length=float(params['min_length']),
            check_persistence=True,
            min_persistence=float(params['min_persistence']),
            save_catalog=False, save_diagnostic=False, save_mask=False,
            save_mrt=False)
        # the module sets its loggers to INFO when it is imported, and that
        # may have happened inside the constructor
        for name in _FINDER_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
        finder.run_mrt()
        finder.find_mrt_sources()
        finder.filter_sources()

        mask = np.zeros(shape, dtype=bool)
        trails = []
        src = finder.source_list
        if src is not None and len(src):
            def column(name):
                return np.array([float(getattr(v, 'value', v)) for v in src[name]])

            status = column('status')
            snr = column('snr')
            keep = (status == 2) & (snr >= float(params['min_snr']))
            for i in np.flatnonzero(status == 2):
                trails.append({
                    'snr': float(snr[i]),
                    'persistence': float(column('persistence')[i]),
                    'width': float(column('width')[i]) * binsize,
                    'theta': float(column('theta')[i]),
                    'masked': bool(keep[i]),
                })
            if keep.any():
                _segment, binned = create_mask(
                    finder.image, src['id'][keep], src['endpoints'][keep],
                    src['width'][keep])
                mask = _expand(np.asarray(binned) != 0, shape)
                grow = int(params['grow'])
                if grow > 0:
                    mask = binary_dilation(mask, iterations=grow)
    return mask, trails


def _write_flags(canonical, masks, stamp):
    """Set bit 16384 exactly where ``masks`` say so (clearing it elsewhere) and
    stamp the file, in one atomic replace."""
    tmp = f'{canonical}.trails.tmp'
    try:
        shutil.copyfile(canonical, tmp)
        with fits.open(tmp, mode='update', memmap=False) as dst:
            for v, new in masks.items():
                dq = np.asarray(dst['DQ', v].data)
                bit = np.asarray(TRAIL_BIT, dtype=dq.dtype)
                dst['DQ', v].data = (dq & ~bit) | (new.astype(dq.dtype) * bit)
            for key, val in cfp.format(keyset=cfp.HST, CFP_TRL=stamp).items():
                dst[0].header[key] = val
        os.replace(tmp, canonical)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def trails_step(exposure_file, step_config=None, overwrite=False, processes=1):
    """Find and flag satellite trails on one canonical ACS/WFC exposure.

    Returns the list of trails per chip (``{extver: [trail, ...]}``), or
    ``None`` when the file already carries ``CFP_TRL`` and ``overwrite`` is not
    set. Raises ``ValueError`` for a file of another detector.
    """
    rootname = os.path.basename(exposure_file).split('_')[0]
    if cfp.should_skip(exposure_file, 'CFP_TRL', rootname, 'trails', None,
                       overwrite, keyset=cfp.HST):
        return None
    from campfire_pipeline.hst.instruments import detector_for

    primary = fits.getheader(exposure_file, 0)
    det = detector_for(primary.get('INSTRUME'), primary.get('DETECTOR'))
    if det.token != 'acswfc':
        raise ValueError(
            f'trails: {os.path.basename(exposure_file)} is a {det.token} '
            f'exposure; the step is for ACS/WFC (calwf3 rejects the reads a '
            f'satellite crosses)')
    params = get_hst_step_config('trails', det.token, overrides=step_config)
    ignored = rootname.lower() in {str(r).strip().lower()
                                   for r in params.get('ignore', [])}

    found, masks = {}, {}
    for v in det.sci_extvers:
        mask, trails = chip_trails(exposure_file, v, params, processes=processes)
        found[v] = trails
        masks[v] = np.zeros_like(mask) if ignored else mask
        for t in trails:
            what = ('masked' if t['masked'] and not ignored else
                    'NOT masked (listed in ignore)' if t['masked'] else
                    'not masked (below min_snr)')
            log(f"trails: {rootname} [SCI,{v}] S/N {t['snr']:.1f}, persistence "
                f"{t['persistence']:.2f}, width {t['width']:.0f} px, angle "
                f"{t['theta']:.1f} deg: {what}")
    n = sum(1 for ts in found.values() for t in ts if t['masked'])
    npix = int(sum(m.sum() for m in masks.values()))
    stamp = f'findsat n={0 if ignored else n} px={npix}'
    if ignored and n:
        stamp += f' ignored={n}'
    _write_flags(exposure_file, masks, stamp)
    log(f'trails: {rootname} {n} satellite trail(s)'
        + (f', {npix} pixels flagged' if npix else '')
        + (' (ignored by config)' if ignored and n else ''))
    return found
