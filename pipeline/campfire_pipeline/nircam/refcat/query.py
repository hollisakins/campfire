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
              to ``brick_primary=1``, ``maskbits=0``, ``type='PSF'``.
    hsc_ssp   Not implemented as an automated query — use the HSC SSP
              web UI to export a CSV/FITS, then incorporate it via
              ``cfpipe nircam refcat merge``. The CAS API needs an
              account and async job polling that is out of scope here.

All backends are dispatched by name through :func:`query`.
"""

import warnings

import numpy as np
from astropy.table import Table


SUPPORTED_BACKENDS = ("gaia", "ls_dr10", "hsc_ssp")


def query(backend, center, radius_deg, **kwargs):
    """Dispatch to a named backend. ``center`` is ``(ra_deg, dec_deg)``."""
    if backend == "gaia":
        return query_gaia(center, radius_deg, **kwargs)
    if backend == "ls_dr10":
        return query_ls_dr10(center, radius_deg, **kwargs)
    if backend == "hsc_ssp":
        raise NotImplementedError(
            "Automated HSC SSP queries are not supported here — they "
            "require an HSC SSP account and async CAS job polling. "
            "Export a catalog from https://hsc-release.mtk.nao.ac.jp/ "
            "and combine it with `cfpipe nircam refcat merge`."
        )
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
        SELECT ra, dec, {mag_col}, {flux_col}, {flux_err_col}
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

    flux = np.asarray(result[flux_col], dtype=float)
    flux_err = np.asarray(result[flux_err_col], dtype=float)
    mag = np.asarray(result[mag_col], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = np.where(flux_err > 0, flux / flux_err, np.nan)
        mag_err = 2.5 / np.log(10) / snr

    keep = np.isfinite(mag) & np.isfinite(mag_err) & (mag_err > 0)
    out = Table({
        "RA": np.asarray(result["ra"], dtype=float)[keep],
        "DEC": np.asarray(result["dec"], dtype=float)[keep],
        "mag": mag[keep],
        "mag_err": mag_err[keep],
    })
    return out


# ---------------------------------------------------------------------------
# Legacy Surveys DR10 (NOIRLab Data Lab TAP)
# ---------------------------------------------------------------------------

def query_ls_dr10(center, radius_deg, *, mag_band="i", point_sources=True,
                  brick_primary=True, maskbits_zero=True, mag_max=None):
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
        Apply ``type = 'PSF'``.
    brick_primary : bool
        Apply ``brick_primary = 1`` (drop duplicate rows on brick edges).
    maskbits_zero : bool
        Apply ``maskbits = 0`` (drop sources flagged by the LS bitmask).
    mag_max : float, optional
        Upper magnitude cut on the chosen band.
    """
    import pyvo

    band = mag_band.lower()
    if band not in ("g", "r", "i", "z"):
        raise ValueError(f"mag_band must be g/r/i/z (got {mag_band!r})")

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
        SELECT ra, dec, mag_{band}, flux_{band}, flux_ivar_{band}
        FROM ls_dr10.tractor
        WHERE {' AND '.join(where)}
    """
    tap = pyvo.dal.TAPService("https://datalab.noirlab.edu/tap")
    result = tap.search(adql).to_table()

    flux = np.asarray(result[f"flux_{band}"], dtype=float)
    ivar = np.asarray(result[f"flux_ivar_{band}"], dtype=float)
    mag = np.asarray(result[f"mag_{band}"], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        snr = flux * np.sqrt(np.where(ivar > 0, ivar, np.nan))
        mag_err = 2.5 / np.log(10) / snr

    keep = np.isfinite(mag) & np.isfinite(mag_err) & (mag_err > 0)
    out = Table({
        "RA": np.asarray(result["ra"], dtype=float)[keep],
        "DEC": np.asarray(result["dec"], dtype=float)[keep],
        "mag": mag[keep],
        "mag_err": mag_err[keep],
    })
    return out
