"""
On-disk schema and provenance helpers for astrometric reference catalogs.

A refcat is an ECSV table with at least these columns (column names match
what ``jhat.align_wcs`` expects via its ``refcat_*col`` knobs):

    RA       float64   degrees, ICRS
    DEC      float64   degrees, ICRS
    mag      float32   AB magnitude in some band
    mag_err  float32   1-sigma magnitude error

Optional columns: ``source`` (str, set by ``merge`` to record provenance
per-row), plus any backend-specific extras the user wants to keep.

Provenance for the catalog as a whole lives in ``Table.meta`` (which ECSV
preserves as a YAML block at the top of the file):

    meta = {
        "schema": "campfire-refcat-v1",
        "cfpipe_version": "...",
        "field": "rj0911",
        "created": "2026-05-07T12:34:56",
        "source": "query" | "extract" | "merge",
        "params": {...},        # the kwargs used to build it
        "notes": "free-form",   # optional
    }
"""

import os
from datetime import datetime, timezone

from astropy.table import Table


SCHEMA_VERSION = "campfire-refcat-v1"
REFCAT_COLUMNS = ("RA", "DEC", "mag", "mag_err")


def make_meta(field, source, params=None, notes=None):
    """Build a standard ``meta`` dict for a fresh refcat.

    Parameters
    ----------
    field : str
        Field name (matches ``Field.name``).
    source : str
        How the catalog was made — ``'query'``, ``'extract'``, or ``'merge'``.
    params : dict, optional
        Pipeline-level kwargs used to produce the catalog (backend, query
        center+radius, mosaic path, SEP thresholds, ...). Stored verbatim
        so a future invocation can be reconstructed.
    notes : str, optional
        Free-form note shown by ``cfpipe nircam refcat info`` (TODO) and
        in the YAML header of the .ecsv file.
    """
    from campfire_pipeline import __version__ as cfpipe_version

    meta = {
        "schema": SCHEMA_VERSION,
        "cfpipe_version": cfpipe_version,
        "field": field,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "params": dict(params or {}),
    }
    if notes:
        meta["notes"] = notes
    return meta


def write_refcat(table, path, *, overwrite=False):
    """Validate columns, then write to ``path`` as ECSV.

    Casts ``mag`` and ``mag_err`` to float32 for parity with the existing
    notebook-derived catalogs and to keep the file small.
    """
    missing = [c for c in REFCAT_COLUMNS if c not in table.colnames]
    if missing:
        raise ValueError(
            f"refcat is missing required columns {missing!r}; "
            f"found {table.colnames!r}"
        )
    out = table.copy()
    # mag/mag_err to float32; RA/DEC stay float64 for sub-mas precision.
    for col in ("mag", "mag_err"):
        out[col] = out[col].astype("float32")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    out.write(path, format="ascii.ecsv", overwrite=overwrite)


def read_refcat(path):
    """Read a refcat ECSV, normalising column names to the standard schema.

    Accepts external catalogs (e.g. raw Gaia / NOIRLab TAP dumps) that
    use lowercase ``ra``/``dec`` and common magnitude column conventions
    (``phot_g_mean_mag``, ``mag_g``…). The renames are non-destructive —
    the original columns are simply renamed in place, so the file you
    write back out will use the canonical schema.
    """
    table = Table.read(path, format="ascii.ecsv")
    _canonicalize_columns(table)
    return table


# Mapping from external/legacy column names → canonical refcat schema.
# First match wins; checked only when the canonical column is absent.
_COLUMN_ALIASES = {
    "RA": ("ra", "RAJ2000", "ALPHA_J2000", "alpha_j2000", "raMean"),
    "DEC": ("dec", "DEJ2000", "DECJ2000", "DELTA_J2000", "delta_j2000",
            "decMean"),
    "mag": ("phot_g_mean_mag", "phot_bp_mean_mag", "phot_rp_mean_mag",
            "mag_g", "mag_r", "mag_i", "mag_z", "MAG_AUTO", "MAG_APER"),
    "mag_err": ("phot_g_mean_mag_error", "phot_bp_mean_mag_error",
                "phot_rp_mean_mag_error",
                "magerr_g", "magerr_r", "magerr_i", "magerr_z",
                "MAGERR_AUTO", "MAGERR_APER"),
}


def _canonicalize_columns(table):
    """Rename alias columns to the canonical schema, in place."""
    for canonical, aliases in _COLUMN_ALIASES.items():
        if canonical in table.colnames:
            continue
        for alias in aliases:
            if alias in table.colnames:
                table.rename_column(alias, canonical)
                break
