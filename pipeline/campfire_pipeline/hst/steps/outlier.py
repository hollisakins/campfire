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

Default thresholds (``[<instrument>.outlier]`` in the default config, the
only place they are set). ACS/WFC: those of ``crreject``, SNR ``3.5 3.0`` and scale
``1.2 0.7``. WFC3/IR: SNR ``8.0 5.0`` as in ``crreject``, and scale
``5.0 0.7``, twice the first value ``crreject`` uses. The scale multiplies the
gradient of the model image and is the allowance for a steep source. A WFC3/IR
star is 1.1 native pixels wide, and its peak pixel changes with the sub-pixel
position by more than a scale of 2.5 allows: on the 30 exposures of pg004 that
value flags the peak of about one star image in a hundred, stars that did not
move included, and 4 of 76 unsaturated Gaia stars lose 1 to 10 percent in the
F105W mosaic. Flags within 3 pixels of a catalog star that is not protected,
in F105W, F125W, F140W and F160W: 6, 4, 5 and 9 at scale 2.5; 1, 1, 2 and 0 at
3.5; none at 5, where no star's flux changes. Flags on flat background do not
depend on the scale.

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

Defaults. A star is protected only where the catalog predicts a motion that
matters for the comparison: ``protect_min_motion = 0.15`` native pixel between
the first and the last exposure that contains it, on both detectors (Hollis,
2026-11-14). Exposures of one epoch therefore protect nothing and every star
stays in the comparison. WFC3/IR: ``protect_radius = 0.4``. On the 18 F105W
exposures of pg004 (4 from 2016, 14 from 2022) the flags on stars lie within
2.5 native pixels (0.32 arcsec) of the star, and stars lose their cores in the
minority epoch once they moved more than 40 mas (0.3 pixel), so the threshold
sits at half the motion that showed a loss. With it 58 of 90 catalog stars are
protected and all 76 unsaturated Gaia stars keep their flux to 1 percent
(together with the scale above, which deals with the peak pixels of stars
that did not move). ``protect_min_motion = 0`` protects every catalog star
with a proper motion. ACS/WFC: ``protect_radius = 0.5`` and the same 0.15
pixel. Measured on COSMOS F814W of 2004 and 2011-2012 (seven catalog stars on
both epochs, ``acs_star_protection.py``): the four that moved 1.6 to 6.7
pixels lost 36 to 61 percent of the flux within 0.2 arcsec in one epoch
without protection and none with it; one that moved 0.66 and one that moved
0.61 pixel lost nothing either way. Around those four (three of them
saturated) 10 to 12 percent of pixels were flagged at 0.25 to 0.4 arcsec and
3 to 6 percent at 0.4 to 0.6, beyond the first radius of 0.25, hence 0.5.
"""

import os
import shutil

import numpy as np
from astropy.io import fits

from campfire_pipeline.common import cfp
from campfire_pipeline.common.io import log
from campfire_pipeline.config import get_hst_step_config
from campfire_pipeline.hst.exposure import OUTLIER_EXT, open_exposure

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
    from campfire_pipeline.imaging.outlier import (
        detect_outliers, stack_grid, star_protection,
    )

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

    params = get_hst_step_config('outlier', token, overrides=step_config)
    for name in ('snr', 'scale', 'minmed_nsigma'):
        params[name] = _pair(params[name])

    # One grid at the native scale covers every chip given. Refuse a set whose
    # grid would not fit in memory, before any pixel is read.
    limit = float(params.pop('max_grid_megapixels'))
    nx, ny = stack_grid(chips, pixel_scale=params.get('pixel_scale')).shape
    if nx * ny > limit * 1e6:
        raise ValueError(
            f'outlier: the {len(files)} overlapping {token} {next(iter(filters))} '
            f'exposures span a {nx} x {ny} pixel grid ({nx * ny / 1e6:.0f} '
            f'megapixels), above [{token}.outlier].max_grid_megapixels = '
            f'{limit:g}. A contiguous mosaic of this size needs the pass run '
            f'per visit against its neighbors, which is not built; raise the '
            f'limit if the machine has the memory (about 14 bytes per pixel).')

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
