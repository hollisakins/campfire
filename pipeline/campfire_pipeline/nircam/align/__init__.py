"""NIRCam astrometric ``align`` subsystem.

The field-level alignment algorithm that replaces the JHAT-based ``jhat`` /
``wcs_shift`` steps: matches a pooled per-exposure source catalog (SEP
segmentation, mirroring the refcat build) to a Gaia-tied reference catalog via
the JHAT-ported offset-histogram consensus matcher (``histmatch.py``) and fits
one shared shift+rotation per pool via ``tweakwcs`` (SIAF distortion fixed),
freeing a per-detector fit only where residuals demand it.

This package exports the source detector, the per-pool solve, and the exposure
I/O layer. The exposure-grouping layer lives at ``nircam/association.py`` (a
general primitive, not align-specific).
"""

from campfire_pipeline.nircam.align.apply import align_exposure_group
from campfire_pipeline.nircam.align.detect import (
    detect_in_exposure,
    detect_sources,
)
from campfire_pipeline.nircam.align.solve import (
    DetectorInput,
    DetectorSolution,
    GroupSolution,
    solve_exposure_group,
)

__all__ = [
    'detect_sources',
    'detect_in_exposure',
    'solve_exposure_group',
    'DetectorInput',
    'DetectorSolution',
    'GroupSolution',
    'align_exposure_group',
]
