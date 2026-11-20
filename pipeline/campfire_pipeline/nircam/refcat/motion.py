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


def use_gaia_for_stars(refcat, gaia, *, match_radius=0.3, epoch_hint=None,
                       size_column='rhalf', nsigma=3.0, min_stars=8):
    """Make a catalog safe to use at any epoch: stars come from Gaia, point
    sources Gaia cannot vouch for are dropped, galaxies stay.

    A catalog extracted from a mosaic holds every star where it was when the
    mosaic's exposures were taken, which may span years. An exposure from
    another epoch aligned to it is pulled toward positions the stars did not
    have then (pg004, a low-latitude field: a 2016 HST visit landed 20 mas from
    its galaxies against a 2025 NIRCam catalog). After this step:

    * a source matched to a Gaia star with a proper motion carries **Gaia's**
      position, ``ref_epoch`` and ``pmra``/``pmdec`` (and ``source_id``,
      ``parallax``, the errors), so :func:`propagate_to_epoch` moves it from
      Gaia's epoch to each exposure's own. The mosaic's epoch is not used;
    * a source matched to a Gaia star without a proper motion is dropped;
    * an unmatched source as compact as the Gaia stars is dropped: it is a
      star (or something as small) that moves by an unknown amount. "As compact"
      means ``size_column`` at or below the stars' median plus ``nsigma`` times
      their scatter. This needs the size column (``extract`` writes ``rhalf``,
      the half-light radius) and at least ``min_stars`` matched stars to say
      what a point source looks like in this catalog; without either, nothing
      is dropped on size, and ``info`` says so;
    * everything else (extended sources) is kept where it is.

    ``gaia`` is a Gaia table (``RA``/``DEC``/``ref_epoch``/``pmra``/``pmdec``;
    the output of ``query.query_gaia``). Matching is mutual nearest neighbour
    within ``match_radius`` arcsec. ``epoch_hint`` (a Julian year, the rough
    date of the catalog's data) only helps the matching: Gaia is moved there
    first, so fast stars still pair up. It is never written to the catalog.

    Returns ``(table, info)``.
    """
    import astropy.units as u
    from astropy.coordinates import SkyCoord
    from astropy.time import Time

    if not has_proper_motion(gaia):
        raise ValueError("the Gaia table has no ref_epoch/pmra/pmdec columns")
    n = len(refcat)
    out = refcat.copy()
    for name in ('ref_epoch', 'pmra', 'pmdec', 'parallax', 'pmra_err', 'pmdec_err'):
        out[name] = _float_col(out, name) if name in out.colnames else np.full(n, np.nan)
    if 'source_id' not in out.colnames:
        out['source_id'] = np.zeros(n, dtype=np.int64)
    info = {'n_catalog': n, 'n_gaia': len(gaia), 'match_radius_arcsec': float(match_radius),
            'epoch_hint': None if epoch_hint is None else float(epoch_hint),
            'n_gaia_stars': 0, 'n_dropped_no_motion': 0, 'n_dropped_compact': 0,
            'size_column': None, 'size_limit': None, 'n_kept': n}
    if n == 0 or len(gaia) == 0:
        return out, info

    at = gaia if epoch_hint is None else propagate_to_epoch(
        gaia, Time(float(epoch_hint), format='jyear').mjd)
    cat = SkyCoord(_float_col(out, 'RA') * u.deg, _float_col(out, 'DEC') * u.deg)
    gsky = SkyCoord(_float_col(at, 'RA') * u.deg, _float_col(at, 'DEC') * u.deg)
    g2c, d2d, _ = gsky.match_to_catalog_sky(cat)
    c2g, _, _ = cat.match_to_catalog_sky(gsky)
    pair = (c2g[g2c] == np.arange(len(gsky))) & (d2d.arcsec <= float(match_radius))

    pmra, pmdec = _float_col(gaia, 'pmra'), _float_col(gaia, 'pmdec')
    epoch = _float_col(gaia, 'ref_epoch')
    moving = pair & np.isfinite(pmra) & np.isfinite(pmdec) & np.isfinite(epoch)
    src, rows = np.flatnonzero(moving), g2c[moving]
    if epoch_hint is not None and len(rows) >= 5:
        # How far the catalog's own star positions sit from Gaia at the hinted
        # epoch: the catalog's frame against Gaia's (plus any error in the
        # hint). Stars take Gaia's frame below and galaxies keep the catalog's,
        # so a large value means the two halves of the result disagree.
        cosd = np.cos(np.radians(np.nanmedian(cat.dec.deg[rows])))
        info['frame_offset_mas'] = [
            float(np.median((cat.ra.deg[rows] - gsky.ra.deg[src]) * cosd) * 3.6e6),
            float(np.median(cat.dec.deg[rows] - gsky.dec.deg[src]) * 3.6e6)]
    out['RA'][rows] = _float_col(gaia, 'RA')[src]
    out['DEC'][rows] = _float_col(gaia, 'DEC')[src]
    out['ref_epoch'][rows] = epoch[src]
    out['pmra'][rows] = pmra[src]
    out['pmdec'][rows] = pmdec[src]
    for name in ('parallax', 'pmra_err', 'pmdec_err'):
        if name in gaia.colnames:
            out[name][rows] = _float_col(gaia, name)[src]
    if 'source_id' in gaia.colnames:
        out['source_id'][rows] = np.asarray(gaia['source_id'])[src]
    is_star = np.zeros(n, bool)
    is_star[rows] = True
    drop = np.zeros(n, bool)
    drop[g2c[pair & ~moving]] = True
    info['n_gaia_stars'] = int(is_star.sum())
    info['n_dropped_no_motion'] = int(drop.sum())

    if size_column in out.colnames:
        size = _float_col(out, size_column)
        ref = size[is_star & np.isfinite(size) & (size > 0)]
        if len(ref) >= int(min_stars):
            med = float(np.median(ref))
            mad = float(1.4826 * np.median(np.abs(ref - med)))
            limit = med + float(nsigma) * max(mad, 0.05 * med)
            compact = ~is_star & ~drop & np.isfinite(size) & (size <= limit)
            drop |= compact
            info.update(size_column=size_column, size_limit=limit,
                        n_dropped_compact=int(compact.sum()))
    out = out[~drop]
    info['n_kept'] = len(out)
    return out, info
