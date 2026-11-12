"""HST imaging reduction: ACS/WFC and WFC3/IR mosaics on the CAMPFIRE tile grids.

Design: docs/hst/design.md. This package holds what is specific to HST
(file layout, units, DQ meanings, distortion bookkeeping, calibration with
``hstcal``); alignment and mosaicking are shared code in
``campfire_pipeline.imaging``.

HST-only dependencies (``stwcs``, ``drizzlepac``, ``acstools``, ``wfc3tools``)
are imported lazily inside the functions that need them.
"""
