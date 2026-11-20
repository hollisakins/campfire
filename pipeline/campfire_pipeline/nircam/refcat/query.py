"""
External-catalog query backends.

Each backend takes a sky center, radius, and backend-specific kwargs, and
returns an astropy ``Table`` with the standard refcat columns
(``RA``, ``DEC``, ``mag``, ``mag_err``).

Backends:
    gaia      Gaia DR3 via astroquery.gaia. Defaults to filtering for
              point sources with finite magnitude errors.
    ls_dr10   DESI Legacy Surveys DR10 ``tractor`` table via the NOIRLab
              Astro Data Lab TAP service (``ls_dr10.tractor``). Defaults
              to ``brick_primary=1``, ``maskbits=0``, ``type='PSF'``, and
              ``stars='gaia'``: Gaia stars carry their proper motion and
              recognizable stars without one are dropped.
    hsc_ssp   HSC SSP PDR3 cone search via the async catalog_jobs API.
              Requires an HSC SSP account; credentials are resolved from
              the ``user``/``password`` kwargs, the
              ``HSC_SSP_USER``/``HSC_SSP_PASSWORD`` env vars, or a
              ``~/.netrc`` entry for ``hsc-release.mtk.nao.ac.jp``.

All backends are dispatched by name through :func:`query`.
"""

import warnings

import numpy as np
from astropy.table import Table, vstack

from campfire_pipeline.common.io import log


SUPPORTED_BACKENDS = ("gaia", "ls_dr10", "hsc_ssp")


def _float_col(result, name):
    """Return column *name* as a float ndarray, masked entries -> NaN.

    Query backends return masked tables; a masked value cast straight to float
    yields the fill sentinel (garbage), not NaN — which would make a NULL proper
    motion look like a real one. Fill masked entries with NaN first.
    """
    col = result[name]
    filled = col.filled(np.nan) if hasattr(col, "filled") else col
    return np.asarray(filled, dtype=float)


def query(backend, center, radius_deg, **kwargs):
    """Dispatch to a named backend. ``center`` is ``(ra_deg, dec_deg)``."""
    if backend == "gaia":
        return query_gaia(center, radius_deg, **kwargs)
    if backend == "ls_dr10":
        return query_ls_dr10(center, radius_deg, **kwargs)
    if backend == "hsc_ssp":
        return query_hsc_ssp(center, radius_deg, **kwargs)
    raise ValueError(
        f"Unknown backend {backend!r}. Supported: {SUPPORTED_BACKENDS!r}"
    )


# ---------------------------------------------------------------------------
# Gaia DR3
# ---------------------------------------------------------------------------

def query_gaia(center, radius_deg, *, mag_band="G", mag_max=None,
               row_limit=-1):
    """Gaia DR3 cone search via astroquery.

    Parameters
    ----------
    center : (ra_deg, dec_deg)
    radius_deg : float
    mag_band : {'G', 'BP', 'RP'}
        Which Gaia band to expose as the standard ``mag`` column.
    mag_max : float, optional
        Upper magnitude cut applied server-side.
    row_limit : int
        Passed through to astroquery (-1 = unlimited).

    Notes
    -----
    Filters out rows with non-finite mag/mag_err (i.e. dropouts in the
    chosen band) and casts errors from the Gaia ``flux_over_error`` style
    into magnitude uncertainties via ``2.5 / ln(10) / (S/N)``.
    """
    from astroquery.gaia import Gaia

    band = mag_band.upper()
    if band not in ("G", "BP", "RP"):
        raise ValueError(f"mag_band must be G/BP/RP (got {mag_band!r})")
    flux_col = f"phot_{band.lower()}_mean_flux"
    flux_err_col = f"phot_{band.lower()}_mean_flux_error"
    mag_col = f"phot_{band.lower()}_mean_mag"

    where = ["1=1"]
    if mag_max is not None:
        where.append(f"{mag_col} < {mag_max:g}")
    where_clause = " AND ".join(where)

    ra, dec = center
    adql = f"""
        SELECT source_id, ra, dec, ref_epoch, pmra, pmdec, parallax,
               pmra_error, pmdec_error,
               {mag_col}, {flux_col}, {flux_err_col}
        FROM gaiadr3.gaia_source
        WHERE 1 = CONTAINS(
            POINT('ICRS', ra, dec),
            CIRCLE('ICRS', {ra:.6f}, {dec:.6f}, {radius_deg:g}))
          AND {where_clause}
    """
    with warnings.catch_warnings():
        # astroquery prints a UnitsWarning for the magnitude columns
        warnings.simplefilter("ignore")
        Gaia.ROW_LIMIT = row_limit
        job = Gaia.launch_job_async(adql)
        result = job.get_results()

    flux = _float_col(result, flux_col)
    flux_err = _float_col(result, flux_err_col)
    mag = _float_col(result, mag_col)
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = np.where(flux_err > 0, flux / flux_err, np.nan)
        mag_err = 2.5 / np.log(10) / snr

    keep = np.isfinite(mag) & np.isfinite(mag_err) & (mag_err > 0)
    # Proper-motion columns feed the align phase's epoch propagation. Gaia leaves
    # them NULL for sources without a 5-parameter solution; NULL -> NaN so those
    # rows are treated as stationary (see refcat.motion.propagate_to_epoch).
    out = Table({
        "source_id": np.asarray(result["source_id"])[keep],
        "RA": _float_col(result, "ra")[keep],
        "DEC": _float_col(result, "dec")[keep],
        "ref_epoch": _float_col(result, "ref_epoch")[keep],
        "pmra": _float_col(result, "pmra")[keep],
        "pmdec": _float_col(result, "pmdec")[keep],
        "parallax": _float_col(result, "parallax")[keep],
        "pmra_err": _float_col(result, "pmra_error")[keep],
        "pmdec_err": _float_col(result, "pmdec_error")[keep],
        "mag": mag[keep],
        "mag_err": mag_err[keep],
    })
    return out


# ---------------------------------------------------------------------------
# HSC SSP PDR3 (async catalog_jobs API)
# ---------------------------------------------------------------------------

_HSC_BANDS = ("g", "r", "i", "z", "y")
_HSC_SCHEMA_DUD = "pdr3_dud_rev"
_HSC_SCHEMA_WIDE = "pdr3_wide"


def query_hsc_ssp(center, radius_deg, *, mag_band="i", mag_max=None,
                  point_sources=True, release="auto",
                  user=None, password=None, max_wait_s=600):
    """HSC SSP PDR3 cone search using cmodel magnitudes.

    Parameters
    ----------
    center : (ra_deg, dec_deg)
    radius_deg : float
    mag_band : {'g', 'r', 'i', 'z', 'y'}
        Band whose ``{band}_cmodel_mag`` is exposed as the standard
        ``mag`` column. JHAT defaults to ``i`` for NIRCam alignment.
    mag_max : float, optional
        Upper magnitude cut applied server-side.
    point_sources : bool
        Apply ``{band}_extendedness_value < 0.5`` (HSC's stellar proxy,
        analogous to ``type='PSF'`` in Legacy Surveys).
    release : {'auto', 'wide', 'dud'}
        Which PDR3 schema to hit. ``'auto'`` queries ``public.skymap``
        to identify which tracts the cone overlaps, then routes DUD
        tracts to ``pdr3_dud_rev.summary`` and the rest to
        ``pdr3_wide.summary``. ``'wide'`` and ``'dud'`` force one or
        the other.
    user, password : str, optional
        HSC SSP credentials. Falls back to ``HSC_SSP_USER`` /
        ``HSC_SSP_PASSWORD`` env vars, then ``~/.netrc``.
    max_wait_s : int
        Job-polling timeout per submitted query.
    """
    from . import hsc

    band = mag_band.lower()
    if band not in _HSC_BANDS:
        raise ValueError(
            f"mag_band must be one of {_HSC_BANDS!r} (got {mag_band!r})"
        )
    if release not in ("auto", "wide", "dud"):
        raise ValueError(
            f"release must be 'auto', 'wide', or 'dud' (got {release!r})"
        )

    user, password = hsc.resolve_credentials(user, password)
    creds = {"user": user, "password": password}

    targets = _resolve_query_targets(center, radius_deg, release)
    log(f"hsc_ssp: query targets = "
        f"{[(t['schema'], t.get('dud_field')) for t in targets]}")

    pieces = []
    for target in targets:
        pieces.append(_hsc_cone_table(
            target, center, radius_deg, band,
            mag_max=mag_max, point_sources=point_sources,
            creds=creds, max_wait_s=max_wait_s,
        ))

    if not pieces:
        return Table({"RA": [], "DEC": [], "mag": [], "mag_err": []})
    out = pieces[0] if len(pieces) == 1 else vstack(pieces,
                                                    metadata_conflicts="silent")
    return out


def _resolve_query_targets(center, radius_deg, release):
    """Decide which schemas (and tract envelopes) to query for this cone.

    Returns a list of target dicts, each with:
        schema        -- pdr3_dud_rev | pdr3_wide
        dud_field     -- DUD field name if schema is DUD, else None
        tract_range   -- (lo, hi) inclusive tract envelope, or None to
                         skip tractSearch entirely (Wide layer)
    """
    from . import hsc

    dud_fields = hsc.dud_fields_overlapping(center, radius_deg)

    if release == "auto":
        targets = [
            {"schema": _HSC_SCHEMA_DUD, "dud_field": f,
             "tract_range": hsc.dud_tract_envelope(f)}
            for f in dud_fields
        ]
        # Wide covers everything except the DUD footprints. If the cone
        # is entirely inside one or more DUD fields, skip Wide; otherwise
        # add it (no tractSearch — Wide spans most of the sky).
        if any(hsc.cone_inside_dud_field(center, radius_deg, f)
               for f in dud_fields):
            return targets
        targets.append(
            {"schema": _HSC_SCHEMA_WIDE, "dud_field": None,
             "tract_range": None}
        )
        return targets

    if release == "dud":
        if not dud_fields:
            raise RuntimeError(
                f"hsc_ssp: release='dud' requested but cone at {center} "
                f"r={radius_deg} deg does not overlap any DUD field "
                f"({list(hsc.DUD_FIELDS)})."
            )
        return [
            {"schema": _HSC_SCHEMA_DUD, "dud_field": f,
             "tract_range": hsc.dud_tract_envelope(f)}
            for f in dud_fields
        ]

    # release == "wide"
    return [{"schema": _HSC_SCHEMA_WIDE, "dud_field": None,
             "tract_range": None}]


def _hsc_cone_table(target, center, radius_deg, band, *,
                    mag_max, point_sources, creds, max_wait_s):
    """Run one cone-search job against ``{schema}.summary``."""
    from . import hsc

    ra, dec = center
    radius_arcsec = float(radius_deg) * 3600.0
    schema = target["schema"]
    tract_range = target.get("tract_range")

    # The .summary tables are pre-filtered to primary detections, so an
    # explicit "isprimary" predicate is both unnecessary and rejected
    # ("column does not exist") — the column only lives on .forced.
    where = [
        f"coneSearch(coord, {ra:.6f}, {dec:.6f}, {radius_arcsec:g})",
        f"{band}_cmodel_mag IS NOT NULL",
        f"{band}_cmodel_magerr IS NOT NULL",
    ]
    if tract_range is not None:
        tract_min, tract_max = tract_range
        where.insert(0, f"tractSearch(object_id, {tract_min}, {tract_max})")
    if mag_max is not None:
        where.append(f"{band}_cmodel_mag < {mag_max:g}")
    if point_sources:
        where.append(f"{band}_extendedness_value < 0.5")

    mag_col = f"{band}_cmodel_mag"
    err_col = f"{band}_cmodel_magerr"
    sql = (
        f"SELECT object_id, ra, dec, {mag_col}, {err_col}\n"
        f"FROM {schema}.summary\n"
        f"WHERE " + "\n  AND ".join(where) + "\n"
    )
    where_summary = (f"tracts {tract_range[0]}..{tract_range[1]}"
                     if tract_range else "no tract pre-filter")
    log(f"hsc_ssp: querying {schema}.summary ({where_summary})")
    result = hsc.run_sql(sql, max_wait_s=max_wait_s, **creds)
    log(f"hsc_ssp: {schema}.summary returned {len(result)} rows, "
        f"columns: {result.colnames}")

    mag = np.asarray(result[mag_col], dtype=float)
    mag_err = np.asarray(result[err_col], dtype=float)
    keep = np.isfinite(mag) & np.isfinite(mag_err) & (mag_err > 0)
    return Table({
        "RA": np.asarray(result["ra"], dtype=float)[keep],
        "DEC": np.asarray(result["dec"], dtype=float)[keep],
        "mag": mag[keep],
        "mag_err": mag_err[keep],
    })


# ---------------------------------------------------------------------------
# Legacy Surveys DR10 (NOIRLab Data Lab TAP)
# ---------------------------------------------------------------------------

LS_STARS_MODES = ("keep", "gaia")


def query_ls_dr10(center, radius_deg, *, mag_band="i", point_sources=True,
                  brick_primary=True, maskbits_zero=True, mag_max=None,
                  stars="gaia", star_snr_min=20.0):
    """Legacy Surveys DR10 ``tractor`` cone search.

    Mirrors the cuts in ``campfire-data/reference/nircam/rj0911/astrom_cats/
    export_rj0911.py``. The ``i`` band is a sensible default for matching
    JWST NIRCam astrometry; ``g``/``r``/``z`` are also supported.

    Parameters
    ----------
    center : (ra_deg, dec_deg)
    radius_deg : float
    mag_band : {'g', 'r', 'i', 'z'}
    point_sources : bool
        Apply ``type = 'PSF'``. Fainter than a signal-to-noise of about 20
        that type is the survey's fallback, and most such sources are
        galaxies it could not resolve (see ``stars``).
    brick_primary : bool
        Apply ``brick_primary = 1`` (drop duplicate rows on brick edges).
    maskbits_zero : bool
        Apply ``maskbits = 0`` (drop sources flagged by the LS bitmask).
    mag_max : float, optional
        Upper magnitude cut on the chosen band.
    stars : {'gaia', 'keep'}
        ``"gaia"`` (default): a catalog that holds at any epoch, by the same
        rule as ``refcat.motion.use_gaia_for_stars``. A Gaia star with a
        proper motion carries ``ref_epoch`` / ``pmra`` / ``pmdec`` (and
        ``source_id``, ``parallax``, the errors), so the align step moves it
        to each exposure's date. A point source that is recognizably one and
        has no proper motion is dropped, since it is a star moving by an
        unknown amount: a Gaia point source without a proper motion, or a
        ``type = 'PSF'`` source Gaia does not list with a signal-to-noise of
        ``star_snr_min`` or more in the band. Everything else is kept where
        it is listed.
        ``"keep"``: every selected source at its listed position. Legacy
        Surveys lists the stars it takes from Gaia at Gaia's own position and
        epoch (2016.0 in DR10), so they enter the catalog where they were
        in 2016.
    star_snr_min : float
        Signal-to-noise in ``mag_band`` at or above which a ``PSF`` source
        counts as a recognized point source. Measured in COSMOS against
        COSMOS2025: unlisted point sources
        above 20 had moved with the Gaia stars, those below 20 sat with the
        galaxies.
    """
    import pyvo

    band = mag_band.lower()
    if band not in ("g", "r", "i", "z"):
        raise ValueError(f"mag_band must be g/r/i/z (got {mag_band!r})")
    if stars not in LS_STARS_MODES:
        raise ValueError(f"stars must be one of {LS_STARS_MODES!r} (got {stars!r})")

    where = [f"'t' = q3c_radial_query(ra, dec, "
             f"{center[0]:.6f}, {center[1]:.6f}, {radius_deg:g})"]
    if brick_primary:
        where.append("brick_primary = 1")
    if maskbits_zero:
        where.append("maskbits = 0")
    if point_sources:
        where.append("type = 'PSF'")
    if mag_max is not None:
        where.append(f"mag_{band} < {mag_max:g}")

    adql = f"""
        SELECT ra, dec, mag_{band}, flux_{band}, flux_ivar_{band},
               type, ref_cat, ref_id, ref_epoch, pmra, pmdec, parallax,
               pmra_ivar, pmdec_ivar
        FROM ls_dr10.tractor
        WHERE {' AND '.join(where)}
    """
    tap = pyvo.dal.TAPService("https://datalab.noirlab.edu/tap")
    result = tap.search(adql).to_table()
    out, info = _ls_dr10_table(result, band, stars, star_snr_min)
    if stars == "gaia":
        log(f"refcat query: ls_dr10 stars=gaia: {info['n_gaia_stars']} Gaia stars carry a "
            f"proper motion; dropped {info['n_dropped_no_motion']} Gaia point sources "
            f"without one and {info['n_dropped_point']} other point sources at S/N >= "
            f"{float(star_snr_min):g}; kept {info['n_faint_point']} fainter point sources "
            f"and {info['n_extended']} extended sources")
    return out


def _ls_dr10_table(result, band, stars, star_snr_min=20.0):
    """The refcat table for one ``ls_dr10.tractor`` result; returns
    ``(table, info)``. Separate from the query so it can be tested offline."""
    def text(name):
        col = result[name]
        col = col.filled("") if hasattr(col, "filled") else col
        return np.array([str(v).strip() for v in col])

    flux = _float_col(result, f"flux_{band}")
    ivar = _float_col(result, f"flux_ivar_{band}")
    mag = _float_col(result, f"mag_{band}")
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = flux * np.sqrt(np.where(ivar > 0, ivar, np.nan))
        mag_err = 2.5 / np.log(10) / snr
    keep = np.isfinite(mag) & np.isfinite(mag_err) & (mag_err > 0)
    info = {"stars": stars}
    if stars == "keep":
        out = Table({
            "RA": _float_col(result, "ra")[keep],
            "DEC": _float_col(result, "dec")[keep],
            "mag": mag[keep],
            "mag_err": mag_err[keep],
        })
        return out, info

    # Sources Legacy Surveys took from Gaia carry ref_cat 'GE' (Gaia EDR3; 'G2'
    # in older releases) and Gaia's position at ref_epoch. A Gaia source with
    # no proper motion is listed with zero motion and zero inverse variance;
    # some of those are galaxies (type SER / DEV), which stay as galaxies.
    gaia = np.char.startswith(text("ref_cat"), "G")
    point = text("type") == "PSF"
    pmra = _float_col(result, "pmra")
    pmdec = _float_col(result, "pmdec")
    pmra_ivar = _float_col(result, "pmra_ivar")
    pmdec_ivar = _float_col(result, "pmdec_ivar")
    epoch = _float_col(result, "ref_epoch")
    moving = (gaia & np.isfinite(pmra) & np.isfinite(pmdec) & np.isfinite(epoch)
              & (pmra_ivar > 0) & (pmdec_ivar > 0))
    # A point source with no proper motion is dropped when it is recognizably a
    # point source: Gaia lists it, or the survey measured it well enough to
    # tell. Below that signal-to-noise 'PSF' is the fallback type.
    with np.errstate(invalid="ignore"):
        recognized = gaia | (snr >= float(star_snr_min))
    dropped = point & ~moving & recognized
    extended = ~moving & ~point
    keep &= ~dropped

    def of_stars(values):
        return np.where(moving, values, np.nan)[keep]

    with np.errstate(divide="ignore", invalid="ignore"):
        pmra_err = 1.0 / np.sqrt(pmra_ivar)
        pmdec_err = 1.0 / np.sqrt(pmdec_ivar)
    source_id = np.zeros(len(result), dtype=np.int64)
    source_id[moving] = np.asarray(result["ref_id"])[moving].astype(np.int64)
    out = Table({
        "source_id": source_id[keep],
        "RA": _float_col(result, "ra")[keep],
        "DEC": _float_col(result, "dec")[keep],
        "ref_epoch": of_stars(epoch),
        "pmra": of_stars(pmra),
        "pmdec": of_stars(pmdec),
        "parallax": of_stars(_float_col(result, "parallax")),
        "pmra_err": of_stars(pmra_err),
        "pmdec_err": of_stars(pmdec_err),
        "mag": mag[keep],
        "mag_err": mag_err[keep],
    })
    info.update(n_gaia_stars=int((moving & keep).sum()),
                n_dropped_no_motion=int((dropped & gaia).sum()),
                n_dropped_point=int((dropped & ~gaia).sum()),
                n_faint_point=int((point & ~moving & keep).sum()),
                n_extended=int((extended & keep).sum()))
    return out, info
