"""The exposure adapter: the one seam between an instrument and the mosaic code.

The shared drizzle and outlier code needs, per detector chip of an exposure:
calibrated pixels in a defined unit, a variance model, a good-pixel mask, a way
to map pixels to the sky, and a little identity and timing metadata. An
instrument module supplies those by implementing :class:`Chip`; nothing in
``campfire_pipeline.imaging`` then needs to know the instrument's file layout,
DQ bit meanings, WCS representation or units.

One ``Chip`` is one detector chip of one exposure. A NIRCam exposure file holds
one; an ACS/WFC file holds two.

Unit convention handed to the mosaic code: ``sci`` is a **surface brightness**
(MJy/sr), already sky-subtracted if the instrument subtracts sky per exposure,
and scaled with the exposure's own zeropoint. Surface brightness does not change
with the output pixel size, so the drizzle needs no flux rescaling.

Variance convention: three non-negative components in the square of the ``sci``
unit. ``var_rnoise`` is the background-limited term (read noise, sky, dark) and
sets the drizzle weight under ``weight_type='ivm'``; ``var_poisson`` is the
source term; ``var_flat`` the flat-field term. An instrument that has only one
error array splits it (see docs/hst/design.md, section 3.2). A component may
carry non-finite values where it is undefined; the drizzle masks each component
separately.
"""

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import numpy as np


@dataclass
class ChipArrays:
    """Pixel arrays of one chip, all of the chip's shape ``(ny, nx)``."""
    sci: np.ndarray           # float32 surface brightness
    var_rnoise: np.ndarray    # float32, background-limited variance
    var_poisson: np.ndarray   # float32, source variance
    var_flat: np.ndarray      # float32, flat-field variance
    good: np.ndarray          # bool, True where the pixel may be used


@runtime_checkable
class Chip(Protocol):
    """What the mosaic code needs to know about one chip of one exposure."""

    key: str                  # stable identity, unique within a mosaic's inputs
    path: str                 # file the chip lives in
    instrument: str           # token used in product names, e.g. 'acswfc'
    filter: str
    exptime: float            # seconds, weights 'exptime' drizzles
    xposure: float            # seconds counted in the exposure-time map
    shape: tuple              # (ny, nx)
    pixel_area_arcsec2: float  # nominal native pixel area

    def load(self) -> ChipArrays:
        """Read the pixel arrays. Called once per drizzle pass."""

    def pixel_to_world(self, x, y):
        """0-indexed pixel coordinates to ``(ra, dec)`` in degrees, with the
        instrument's full distortion model. Must accept arrays."""

    def provenance(self) -> dict:
        """One row of the mosaic's input table (scalars and short strings)."""

    # Optional, a static method: ``mosaic_header(chips) -> (primary, sci)``,
    # two lists of ``(keyword, value, comment)`` the mosaic writer adds to the
    # primary and the SCI header: the instrument's identity as its archive
    # names it, the times of the inputs, the photometric conversion and the
    # calibration versions (``hst/exposure.HstChip.mosaic_header``).


def is_chip(obj) -> bool:
    """True for a :class:`Chip`; False for a file path (the JWST route)."""
    return (not isinstance(obj, (str, bytes)) and hasattr(obj, 'load')
            and hasattr(obj, 'pixel_to_world'))


def chip_weight(arrays: ChipArrays, weight_type: str,
                exptime: Optional[float] = None) -> np.ndarray:
    """Per-pixel drizzle weight of a chip, zero wherever the pixel is unusable.

    ``'ivm'``: inverse of ``var_rnoise`` (the background-limited variance).
    Unlike ``stcal.resample.utils.build_driz_weight``, a pixel whose variance is
    non-finite or not positive gets weight 0, not 1: a chip adapter is expected
    to supply a real variance everywhere it marks a pixel good.

    ``'exptime'``: the chip's exposure time for every good pixel.
    """
    good = np.asarray(arrays.good, dtype=bool)
    if weight_type == 'ivm':
        var = np.asarray(arrays.var_rnoise, dtype=np.float32)
        ok = good & np.isfinite(var) & (var > 0)
        weight = np.zeros(var.shape, dtype=np.float32)
        np.divide(1.0, var, out=weight, where=ok)
        return weight
    if weight_type == 'exptime':
        if exptime is None:
            raise ValueError("weight_type='exptime' needs the chip's exptime")
        return np.where(good, np.float32(exptime), np.float32(0)).astype(np.float32)
    raise ValueError(
        f"Unknown weight_type {weight_type!r}; expected 'ivm' or 'exptime'")
