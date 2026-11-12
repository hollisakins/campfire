"""
Astrometric reference catalog utilities, shared by every imaging instrument.

Library half of the reference-catalog tooling: querying an external survey
(Gaia, Legacy Surveys DR10, HSC SSP), extracting a catalog from a mosaic,
merging catalogs with positional dedup, comparing two catalogs, and moving
sources to an exposure epoch.

All catalogs share one on-disk schema (ECSV with columns RA, DEC, mag,
mag_err), so a catalog made from a NIRCam mosaic can be the alignment reference
for any other exposure set of the same field.

The command-line group (``cfpipe nircam refcat <subcmd>``) stays in
``campfire_pipeline.nircam.refcat.cli``.
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
