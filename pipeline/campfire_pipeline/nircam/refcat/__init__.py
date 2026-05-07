"""
NIRCam astrometric reference catalog utilities.

Three workflows are supported via ``cfpipe nircam refcat <subcmd>``:

  query    Query an external survey (Gaia, Legacy Surveys DR10, HSC SSP)
           for an absolute astrometric reference over a field.
  extract  Build a catalog from a mosaic. Useful when one filter has
           already been aligned to an absolute frame and you want to use
           it as the reference for the remaining filters.
  merge    Combine two or more catalogs with positional dedup, ordered by
           precedence (first wins).
  compare  Sky-residual diagnostic between two catalogs.

All catalogs share the same on-disk schema (ECSV with columns RA, DEC,
mag, mag_err) so they can be fed straight into JHAT via the
``[<field>.jhat.refcat_dict]`` mapping in ``fields.toml``.
"""

from .io import (
    REFCAT_COLUMNS,
    read_refcat,
    write_refcat,
    make_meta,
)

__all__ = [
    "REFCAT_COLUMNS",
    "read_refcat",
    "write_refcat",
    "make_meta",
]
