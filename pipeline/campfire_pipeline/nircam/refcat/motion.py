"""Propagate reference-catalog positions to an exposure epoch.

When a refcat carries proper motions (see :func:`io.has_proper_motion`), the
align phase moves each star's position from the catalog epoch (``ref_epoch``) to
the exposure mid-time *before* it footprint-clips and matches — so a fast-moving
Gaia star lands where it actually was when the shutter was open, not where the
catalog recorded it years earlier. Galaxies (and any row without a finite proper
motion) are left where they are, so a stationary extragalactic anchor catalog is
a strict no-op.
"""

import warnings

import numpy as np

from campfire_pipeline.nircam.refcat.io import has_proper_motion

# Plausible Julian-year range for a catalog ``ref_epoch``. A value outside this
# almost certainly means the column is in the wrong unit (MJD, or decimal years
# since 2000) rather than a Julian year — see the range check in
# ``propagate_to_epoch``.
_EPOCH_MIN_JYEAR = 1990.0
_EPOCH_MAX_JYEAR = 2100.0


def _float_col(table, name):
    """Return column *name* as a float ndarray, masked entries -> NaN.

    Astropy ``MaskedColumn``s cast straight to float yield the fill sentinel
    (garbage) in masked slots, not NaN — which would make a NULL proper motion
    look like a real one, or a masked filler row propagate ~1e20 mas off-sky.
    Fill masked entries with NaN first (mirrors ``refcat.query._float_col``).
    """
    col = table[name]
    filled = col.filled(np.nan) if hasattr(col, "filled") else col
    return np.asarray(filled, dtype=float)


def propagate_to_epoch(refcat, epoch_mjd):
    """Return a copy of *refcat* with RA/DEC propagated to ``epoch_mjd`` (MJD).

    A no-op that returns the input unchanged when the catalog has no
    proper-motion columns, or when no row carries a finite non-zero proper
    motion — so a stationary galaxy anchor pays nothing. Only rows with finite
    ``pmra``/``pmdec``/``ref_epoch`` are moved; every other row keeps its catalog
    position (a mixed star+galaxy catalog is handled per-row).
    """
    if not has_proper_motion(refcat):
        return refcat

    # Read via .filled(np.nan) for masked columns — a masked slot cast straight
    # to float yields the fill sentinel (e.g. 1e20), not NaN, which the finite
    # guard below would then wrongly treat as a real proper motion.
    ra = _float_col(refcat, 'RA')
    dec = _float_col(refcat, 'DEC')
    pmra = _float_col(refcat, 'pmra')
    pmdec = _float_col(refcat, 'pmdec')
    ref_epoch = _float_col(refcat, 'ref_epoch')

    movable = (np.isfinite(pmra) & np.isfinite(pmdec) & np.isfinite(ref_epoch)
               & ((pmra != 0.0) | (pmdec != 0.0)))
    if not movable.any():
        return refcat

    # ``ref_epoch`` is consumed as a Julian year (``Time(..., format='jyear')``).
    # An external catalog whose epoch column is actually MJD (~57388) or decimal
    # years-since-2000 would be silently read as a year tens of thousands AD and
    # propagate stars catastrophically off-sky. Fail loud instead. (Movable rows
    # already have finite ref_epoch.)
    mov_epoch = ref_epoch[movable]
    outside = (mov_epoch < _EPOCH_MIN_JYEAR) | (mov_epoch > _EPOCH_MAX_JYEAR)
    if outside.any():
        raise ValueError(
            f"refcat ref_epoch {float(mov_epoch[outside][0])!r} is outside the "
            f"plausible range ({_EPOCH_MIN_JYEAR:g}, {_EPOCH_MAX_JYEAR:g}); "
            f"ref_epoch must be a Julian year (e.g. 2016.0 for Gaia DR3), not "
            f"MJD or years-since-2000."
        )

    from astropy.time import Time
    import astropy.units as u
    from astropy.coordinates import SkyCoord

    src = SkyCoord(
        ra=ra[movable] * u.deg, dec=dec[movable] * u.deg,
        pm_ra_cosdec=pmra[movable] * u.mas / u.yr,
        pm_dec=pmdec[movable] * u.mas / u.yr,
        obstime=Time(ref_epoch[movable], format='jyear'),
    )
    with warnings.catch_warnings():
        # apply_space_motion warns when a coord has no distance (we don't feed
        # parallax as a distance); linear proper-motion propagation is exactly
        # the astrometric correction we want, so the warning is expected noise.
        warnings.simplefilter('ignore')
        moved = src.apply_space_motion(
            new_obstime=Time(float(epoch_mjd), format='mjd'))

    out = refcat.copy()
    new_ra, new_dec = ra.copy(), dec.copy()
    new_ra[movable] = np.asarray(moved.ra.deg, dtype=float)
    new_dec[movable] = np.asarray(moved.dec.deg, dtype=float)
    out['RA'] = new_ra
    out['DEC'] = new_dec
    return out
