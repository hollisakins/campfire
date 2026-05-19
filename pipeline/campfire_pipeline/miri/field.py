"""
MiriField: MIRI-specific imaging field, subclass of ``ImagingField``.

Minimal v1: sets the instrument path component, detector shape, and the
list of step names that ``ImagingField.load()`` should treat as per-field
step overrides rather than tile definitions.

Additional MIRI-specific reference subdirectories (warm-pixel masks,
persistence caches) will be added by overriding
``_setup_instrument_workspace()`` once those steps land.
"""

from dataclasses import dataclass
from typing import ClassVar

from campfire_pipeline.common.imaging.field import ImagingField
from campfire_pipeline.miri.constants import MIRI_DETECTOR_SHAPE


# Known per-step keys for the MIRI imaging pipeline. Populate as steps
# land. Used by ``ImagingField.load()`` to split per-field step overrides
# from tile definitions in fields.toml.
_MIRI_KNOWN_STEPS: frozenset = frozenset()


@dataclass
class MiriField(ImagingField):
    """MIRI imaging field. v1 stub — adds no new dataclass fields."""

    INSTRUMENT_NAME: ClassVar[str] = 'miri'
    DETECTOR_SHAPE: ClassVar[tuple] = MIRI_DETECTOR_SHAPE
    KNOWN_STEPS: ClassVar[frozenset] = _MIRI_KNOWN_STEPS
