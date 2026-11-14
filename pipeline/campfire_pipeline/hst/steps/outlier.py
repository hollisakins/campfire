"""outlier: field-level outlier flags for all exposures of one detector and filter.

The per-visit ``crreject`` step sees two to four exposures taken minutes apart.
Once every visit is on one astrometric frame, this step compares each exposure
with **everything** that overlaps it in the same filter, whatever the visit or
program, and flags what the visit could not: a cosmic ray at the same place in
both exposures of a two-exposure visit, residual trails, persistence that moves
with the dither. The method lives in ``campfire_pipeline/imaging/outlier.py``
(single drizzles, a median that falls back to the minimum where few exposures
overlap, blot, a noise-plus-gradient test) and is shared with any instrument
that provides chips.

Inputs must be aligned: every file needs a solved ``CFP_ALGN``. An exposure
left ``NOT_ALIGNED`` would be compared with a sky it does not sit on, and its
real sources would be flagged; the step refuses the whole set instead.

The flags go into an image extension, ``CFOUT`` (one per chip, same ``EXTVER``
as its ``SCI``; 1 at an outlier), not into ``DQ``. ``DQ`` bit 4096 belongs to
``crreject``, which clears and rewrites it on a re-run; a separate extension
lets either step be repeated without touching the other's result. The adapter
(``hst/exposure.py``) treats a marked pixel as bad. ``CFP_OUT`` is stamped on
the primary header.

Pixels ``crreject`` or the calibration already flagged are not used in the
comparison and are not flagged again; this step only adds.

Default thresholds are those of ``crreject`` for each detector: ACS/WFC SNR
``3.5 3.0`` and scale ``1.2 0.7``; WFC3/IR SNR ``8.0 5.0`` and scale
``2.5 0.7``.

**Stars that moved.** Exposures years apart show a star with a proper motion
at different places, and the comparison would remove its core from the
minority and leave the star too faint. Given the reference catalog the visits
were aligned to, the step leaves alone a circle of ``protect_radius`` arcsec
around every catalog star that moved by ``protect_min_motion`` native pixels or
more between the first and the last exposure containing it, at the star's
position on each exposure's date. Only stars the catalog gives a proper motion
for can be protected (``cfpipe nircam refcat extract --stars gaia`` and the
Gaia queries write them); without a catalog, or with one that has no proper
motions, nothing is protected. A cosmic ray inside such a circle is left to
``crreject``. ``protect_stars = false`` turns this off.

Defaults. WFC3/IR: ``protect_radius = 0.4``, ``protect_min_motion = 0``, that
is, every catalog star with a proper motion is protected, moved or not. On the
18 F105W exposures of pg004 (4 from 2016, 14 from 2022) the flags on stars lie
within 2.5 native pixels (0.32 arcsec) of the star. Stars that moved more than
40 mas (0.3 pixel) lose their cores in the minority epoch; with a threshold of
0.15 pixel those are all recovered, but 4 of 76 unsaturated Gaia stars still
lose 1 to 10 percent to single flags on their peak pixel, in either epoch, and
the single-epoch filters show the same (4 to 9 such flags in four exposures):
the peak pixel of a star 1.1 pixels wide changes with the sub-pixel position
by more than the test allows. With every catalog star protected none of the 76
changes. Cosmic rays on this detector are removed up the ramp before this
step, so little is given up. ACS/WFC: ``protect_radius = 0.25`` (scaled by the
narrower PSF), ``protect_min_motion = 0.15``, so that in single-epoch data,
where a visit of one exposure has only this step to find its cosmic rays,
stars stay in the comparison. Neither ACS value has been measured on two-epoch
data.
"""

import os
import shutil

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.hst.exposure import OUTLIER_EXT, open_exposure

DEFAULTS = {
    'acswfc': {'snr': (3.5, 3.0), 'scale': (1.2, 0.7),
               'protect_radius': 0.25, 'protect_min_motion': 0.15},
    'wfc3ir': {'snr': (8.0, 5.0), 'scale': (2.5, 0.7),
               'protect_radius': 0.4, 'protect_min_motion': 0.0},
}
_COMMON = {'grow': 1, 'pixfrac': 1.0, 'kernel': 'square',
           'minmed_nsigma': (4.0, 3.0), 'minmed_max_n': 3,
           'min_contributors': 2, 'protect_stars': True}
_PROTECT_KEYS = ('protect_stars', 'protect_radius', 'protect_min_motion')


def _pair(value):
    """``'3.5 3.0'`` or ``[3.5, 3.0]`` to a tuple of two floats."""
    if isinstance(value, str):
        value = value.split()
    a, b = value
    return float(a), float(b)


def _write_flags(path, masks, stamp):
    """Replace the ``CFOUT`` extensions of ``path`` with ``masks``
    (``{extver: bool array}``) and stamp ``CFP_OUT``. All or nothing."""
    tmp = f'{path}.out.tmp'
    try:
        shutil.copyfile(path, tmp)
        with fits.open(tmp, mode='update', memmap=False) as hdul:
            for i in reversed(range(len(hdul))):
                if hdul[i].name == OUTLIER_EXT:
                    del hdul[i]
            for extver, mask in sorted(masks.items()):
                shape = hdul['SCI', extver].data.shape
                if mask.shape != shape:
                    raise RuntimeError(
                        f'outlier: mask shape {mask.shape} does not match '
                        f'[SCI,{extver}] {shape} of {os.path.basename(path)}')
                hdu = fits.ImageHDU(data=mask.astype(np.uint8), name=OUTLIER_EXT,
                                    ver=extver)
                hdu.header['COMMENT'] = (
                    'campfire: 1 where the field-level outlier pass rejects '
                    'the pixel')
                hdul.append(hdu)
            for key, val in cfp.format(keyset=cfp.HST, CFP_OUT=stamp).items():
                hdul[0].header[key] = val
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def outlier_step(exposure_files, step_config=None, overwrite=False, refcat=None):
    """Flag outliers across all given exposures of one detector and filter.

    ``refcat`` is the reference catalog table the exposures were aligned to;
    its stars with a proper motion are protected where they moved (see the
    module docstring). Without it no star is protected.

    Returns ``{path: flagged pixel count}``, or ``None`` when every file
    already carries ``CFP_OUT`` and ``overwrite`` is not set. Raises
    ``ValueError`` for mixed detectors or filters, for fewer than two
    exposures, and for any exposure without a solved alignment.
    """
    from campfire_pipeline.hst.steps.align import _mid_mjd
    from campfire_pipeline.imaging.outlier import detect_outliers, star_protection

    files = sorted(exposure_files)
    if not overwrite and all(cfp.has_step(f, 'CFP_OUT', keyset=cfp.HST)
                             for f in files):
        log(f'Skipping outlier: CFP_OUT already set on all {len(files)} exposures')
        return None
    if len(files) < 2:
        raise ValueError('outlier needs at least two exposures to compare')
    unaligned = []
    for f in files:
        value = cfp.step_value(f, 'CFP_ALGN', keyset=cfp.HST)
        if value is None or value == cfp.NOT_ALIGNED:
            unaligned.append(os.path.basename(f))
    if unaligned:
        raise ValueError(
            f'outlier: these exposures have no solved alignment and cannot be '
            f'compared with the others: {unaligned}')

    by_file = {f: open_exposure(f, use_outlier_flags=False) for f in files}
    chips = [c for f in files for c in by_file[f]]
    tokens = {c.instrument for c in chips}
    filters = {c.filter for c in chips}
    if len(tokens) != 1 or len(filters) != 1:
        raise ValueError(
            f'outlier takes one detector and filter at a time, got '
            f'{sorted(tokens)} / {sorted(filters)}')
    token = tokens.pop()

    params = dict(_COMMON)
    params.update(DEFAULTS[token])
    params.update(step_config or {})
    for name in ('snr', 'scale', 'minmed_nsigma'):
        params[name] = _pair(params[name])

    protect_cfg = {k: params.pop(k) for k in _PROTECT_KEYS}
    protect, prot = None, 'off'
    if not protect_cfg['protect_stars']:
        log('outlier: star protection is turned off')
    elif refcat is None:
        log('outlier: no reference catalog given, so no star is protected')
        prot = 'nocat'
    else:
        dates = {f: _mid_mjd(f) for f in files}
        missing = [os.path.basename(f) for f in files if dates[f] is None]
        if missing:
            raise ValueError(
                f'outlier: no exposure date in {missing}; stars cannot be '
                f'placed at their position on that date')
        protect, info = star_protection(
            chips, refcat, {c.key: dates[f] for f in files for c in by_file[f]},
            radius=float(protect_cfg['protect_radius']),
            min_motion=float(protect_cfg['protect_min_motion']))
        prot = str(info['n_protected'])
        log(f"outlier: {info['n_stars']} catalog stars with a proper motion on "
            f"the exposures; {info['n_protected']} moved "
            f"{protect_cfg['protect_min_motion']:g} px or more and are protected "
            f"within {protect_cfg['protect_radius']:g} arcsec")

    result = detect_outliers(chips, workdir=os.path.dirname(files[0]),
                             protect=protect, **params)

    counts = {}
    for f in files:
        masks = {c.extver: result['flags'][c.key] for c in by_file[f]}
        n = sum(int(m.sum()) for m in masks.values())
        npix = sum(m.size for m in masks.values())
        stamp = (f"field n={len(files)} snr={params['snr'][0]:g} {params['snr'][1]:g} "
                 f"scale={params['scale'][0]:g} {params['scale'][1]:g} "
                 f"prot={prot} frac={n / npix:.5f}")
        _write_flags(f, masks, stamp)
        counts[f] = n
        log(f'outlier: {os.path.basename(f)} flagged {n} px ({100 * n / npix:.3f}%)')
    return counts
