"""NIRCam astrometric ``align`` subsystem.

The field-level alignment algorithm that replaces the JHAT-based ``jhat`` /
``wcs_shift`` steps: triangle-matches a pooled per-exposure source catalog to a
Gaia-tied reference catalog and fits one shared shift+rotation per exposure via
``tweakwcs`` (SIAF distortion fixed), freeing a per-detector shift only where
residuals demand it. See ``pipeline/ASTROMETRY_ALIGN_HANDOFF.md``.

Modules are added phase by phase; this package currently exports the source
detector, the triangle matcher, and the per-exposure solve. The
exposure-grouping layer lives at ``nircam/association.py`` (a general primitive,
not align-specific).
"""

from campfire_pipeline.nircam.align.apply import align_exposure_group
from campfire_pipeline.nircam.align.detect import (
    detect_in_exposure,
    detect_star_centroids,
)
from campfire_pipeline.nircam.align.solve import (
    DetectorInput,
    DetectorSolution,
    GroupSolution,
    solve_exposure_group,
)

__all__ = [
    'detect_star_centroids',
    'detect_in_exposure',
    'solve_exposure_group',
    'DetectorInput',
    'DetectorSolution',
    'GroupSolution',
    'align_exposure_group',
]
