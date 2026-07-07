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

    ra = np.asarray(refcat['RA'], dtype=float)
    dec = np.asarray(refcat['DEC'], dtype=float)
    pmra = np.asarray(refcat['pmra'], dtype=float)
    pmdec = np.asarray(refcat['pmdec'], dtype=float)
    ref_epoch = np.asarray(refcat['ref_epoch'], dtype=float)

    movable = (np.isfinite(pmra) & np.isfinite(pmdec) & np.isfinite(ref_epoch)
               & ((pmra != 0.0) | (pmdec != 0.0)))
    if not movable.any():
        return refcat

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
