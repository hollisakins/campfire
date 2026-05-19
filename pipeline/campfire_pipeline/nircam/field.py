"""
NircamField: NIRCam-specific imaging field, subclass of ``ImagingField``.

Adds NIRCam-only reference subdirectories (bad-pixel masks, wisp templates)
and the SW/LW filter classifier. All generic field machinery (TOML loading,
workspace setup, per-filter file globs, tile WCS) lives on
``common.imaging.field.ImagingField``.
"""

import os
from dataclasses import dataclass
from typing import ClassVar, Optional

from campfire_pipeline.common.imaging.field import ImagingField
from campfire_pipeline.nircam.constants import LW_FILTERS, SW_FILTERS


# Known per-step keys for the canonical-exposure NIRCam pipeline. Used by
# ``ImagingField.load()`` to recognize per-field step overrides and exclude
# them from the tile-detection loop.
_NIRCAM_KNOWN_STEPS = frozenset({
    'detector1', 'persistence', 'wisp', 'striping',
    'image2', 'diag_striping', 'edge', 'sky', 'variance',
    'wcs_shift', 'jhat',
    'apply_mask', 'bad_pixel', 'outlier', 'resample',
})


@dataclass
class NircamField(ImagingField):
    """NIRCam imaging field with NIRCam-specific reference subdirectories."""

    INSTRUMENT_NAME: ClassVar[str] = 'nircam'
    DETECTOR_SHAPE: ClassVar[tuple] = (2048, 2048)
    KNOWN_STEPS: ClassVar[frozenset] = _NIRCAM_KNOWN_STEPS

    # NIRCam-specific reference subdirectories
    bad_pixel_dir: Optional[str] = None
    wisp_dir: Optional[str] = None

    def _setup_instrument_workspace(self):
        self.bad_pixel_dir = os.path.join(self.reference_dir, 'bad_pixels')
        self.wisp_dir = os.path.join(self.reference_dir, 'wisps')
        for d in [self.bad_pixel_dir, self.wisp_dir]:
            os.makedirs(d, exist_ok=True)

    def is_sw_filter(self, filter_name):
        """Check if a filter is short-wavelength."""
        return filter_name.lower() in SW_FILTERS

    def is_lw_filter(self, filter_name):
        """Check if a filter is long-wavelength."""
        return filter_name.lower() in LW_FILTERS


# Backward-compat alias — existing import sites use ``Field``.
Field = NircamField
