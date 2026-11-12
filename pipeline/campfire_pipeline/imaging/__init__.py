"""Instrument-neutral imaging machinery shared by the NIRCam and HST modules.

Tile and footprint geometry, mosaic manifests and naming, exposure maps and
mosaic background subtraction live here. Nothing in this package may import
from ``campfire_pipeline.nircam`` or ``campfire_pipeline.hst`` at module top:
instruments depend on ``imaging``, never the reverse.

Modules that used to live under ``campfire_pipeline.nircam`` keep their old
import path as an alias of the module here (the same module object), so
existing imports and monkeypatches are unaffected. See docs/hst/design.md,
section 3.
"""
