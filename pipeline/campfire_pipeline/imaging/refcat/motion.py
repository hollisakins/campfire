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

from campfire_pipeline.imaging.refcat.io import has_proper_motion

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


def attach_gaia_motion(refcat, gaia, epoch, *, match_radius=0.2):
    """Give the stars of a position-only catalog their Gaia proper motions.

    ``refcat`` holds positions measured at ``epoch`` (a Julian year): sources
    extracted from a mosaic observed then, or a survey catalog at its own
    epoch. ``gaia`` is a Gaia table with ``RA``/``DEC``/``ref_epoch``/``pmra``/
    ``pmdec`` (the output of ``query.query_gaia``). Without this step such a
    catalog holds every star where it was at ``epoch``, and an exposure taken
    years earlier or later is pulled toward positions the stars did not have
    (pg004: stars moving a median 4.6 mas/yr put a 2016 HST visit 17 mas from
    its galaxies when it was aligned to a 2025 NIRCam catalog;
    docs/hst/design.md, section 8b).

    Gaia is moved to ``epoch`` and matched to the catalog: the median offset
    between the two is removed first, then a pair is kept when each is the
    other's nearest neighbour within ``match_radius`` arcsec. A matched row
    keeps its own position, which is in the catalog's frame, and gains
    ``ref_epoch = epoch`` with the star's ``pmra``/``pmdec`` (and ``source_id``,
    ``parallax``, ``pmra_err``, ``pmdec_err`` where Gaia has them), so
    :func:`propagate_to_epoch` moves it from the catalog's epoch to any
    exposure's. Rows without a match, and matches to a Gaia star that has no
    proper motion, are left stationary (NaN). A row that already carries a
    finite proper motion is not touched.

    Returns ``(table, info)``; ``info`` has the counts and the median offset
    (catalog minus Gaia at ``epoch``, mas).
    """
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from astropy.time import Time

    epoch = float(epoch)
    if not _EPOCH_MIN_JYEAR < epoch < _EPOCH_MAX_JYEAR:
        raise ValueError(
            f"epoch {epoch!r} is outside the plausible range ({_EPOCH_MIN_JYEAR:g}, "
            f"{_EPOCH_MAX_JYEAR:g}); it must be a Julian year (e.g. 2025.76), not MJD.")
    if not has_proper_motion(gaia):
        raise ValueError("the Gaia table has no ref_epoch/pmra/pmdec columns")

    out = refcat.copy()
    n = len(out)
    for name in ('ref_epoch', 'pmra', 'pmdec', 'parallax', 'pmra_err', 'pmdec_err'):
        out[name] = _float_col(out, name) if name in out.colnames else np.full(n, np.nan)
    if 'source_id' not in out.colnames:
        out['source_id'] = np.zeros(n, dtype=np.int64)
    info = {'epoch': epoch, 'n_catalog': n, 'n_gaia': len(gaia), 'n_matched': 0,
            'n_with_motion': 0, 'match_radius_arcsec': float(match_radius),
            'offset_mas': [float('nan'), float('nan')]}
    if n == 0 or len(gaia) == 0:
        return out, info

    moved = propagate_to_epoch(gaia, Time(epoch, format='jyear').mjd)
    cat = SkyCoord(_float_col(out, 'RA') * u.deg, _float_col(out, 'DEC') * u.deg)
    g_ra, g_dec = _float_col(moved, 'RA'), _float_col(moved, 'DEC')
    cosd = np.cos(np.radians(np.nanmedian(g_dec)))

    # Median offset between the two frames, from every Gaia star with a catalog
    # source within 1"; a catalog tied to another survey can sit tens of mas
    # from Gaia, which would otherwise eat into the match radius.
    idx, d2d, _ = SkyCoord(g_ra * u.deg, g_dec * u.deg).match_to_catalog_sky(cat)
    near = d2d.arcsec < 1.0
    d_ra = d_dec = 0.0
    if near.sum() >= 5:
        d_ra = float(np.median((cat.ra.deg[idx[near]] - g_ra[near]) * cosd))
        d_dec = float(np.median(cat.dec.deg[idx[near]] - g_dec[near]))
        info['offset_mas'] = [d_ra * 3.6e6, d_dec * 3.6e6]
    gsky = SkyCoord((g_ra + d_ra / cosd) * u.deg, (g_dec + d_dec) * u.deg)

    g2c, d2d, _ = gsky.match_to_catalog_sky(cat)
    c2g, _, _ = cat.match_to_catalog_sky(gsky)
    pair = (c2g[g2c] == np.arange(len(gsky))) & (d2d.arcsec <= float(match_radius))
    info['n_matched'] = int(pair.sum())

    pmra, pmdec = _float_col(gaia, 'pmra'), _float_col(gaia, 'pmdec')
    src = np.flatnonzero(pair & np.isfinite(pmra) & np.isfinite(pmdec))
    rows = g2c[src]
    free = ~(np.isfinite(out['pmra'][rows]) & np.isfinite(out['pmdec'][rows]))
    rows, src = rows[free], src[free]
    out['ref_epoch'][rows] = epoch
    out['pmra'][rows] = pmra[src]
    out['pmdec'][rows] = pmdec[src]
    for name in ('parallax', 'pmra_err', 'pmdec_err'):
        if name in gaia.colnames:
            out[name][rows] = _float_col(gaia, name)[src]
    if 'source_id' in gaia.colnames:
        out['source_id'][rows] = np.asarray(gaia['source_id'])[src]
    info['n_with_motion'] = int(len(rows))
    return out, info
