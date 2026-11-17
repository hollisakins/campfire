"""NIRCam exposure adapter for the instrument-neutral mosaic code.

``imaging/exposure.py`` defines what the shared outlier code needs of one
detector chip of one exposure (:class:`~campfire_pipeline.imaging.exposure.Chip`).
A NIRCam calibrated exposure is one chip: a JWST ``ImageModel`` file. This
module wraps it, so that NIRCam's outlier pass runs the code the HST
instruments run (``imaging/outlier.py``) instead of a second implementation.

What the adapter hands over:

* **sci**: ``SCI`` as it is, MJy/sr. The per-exposure ``bkg`` step has
  subtracted the background, which is what the shared comparison assumes.
* **Variance**: the three JWST components as they are. ``VAR_RNOISE`` is the
  background-limited term and sets the drizzle weight; the ``bkg`` step
  scales it to the measured scatter of blank background, taken over block
  sums (``oneoverf.variance_rescale``). Where neighboring pixels are
  correlated that is above the pixel-to-pixel scatter (1.14 times in the
  F150W exposures of macs0647, 1.01 in F444W), so the outlier threshold is
  that much stricter than its nominal value.
* **Good pixels**: the ``good_bits`` of ``[nircam.outlier]`` (``~DO_NOT_USE``
  by default) through the mask builder the drizzle uses
  (``stcal.resample.utils.build_mask``), and a finite ``SCI``.
* **Sky positions**: the exposure's gwcs, with its full distortion model and
  no bounding box.

The drizzle of the final mosaic still takes NIRCam files directly (the JWST
route of ``imaging/drizzle.py``); only the outlier pass uses this adapter.
"""

import os
from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.imaging.exposure import ChipArrays


@dataclass
class NircamChip:
    """One calibrated NIRCam exposure (one detector)."""
    path: str
    key: str
    filter: str
    detector: str
    exptime: float
    xposure: float
    shape: tuple
    pixel_area_arcsec2: float
    good_bits: str = '~DO_NOT_USE'
    mid_mjd: float = float('nan')       # exposure mid-point, for star motion
    program: str = ''
    visit: str = ''
    instrument: str = 'nircam'
    _wcs: object = field(default=None, repr=False)

    def load(self):
        from jwst.datamodels.dqflags import pixel as pixel_flags
        from stcal.resample.utils import build_mask
        from stdatamodels.jwst.datamodels import ImageModel

        with ImageModel(self.path, memmap=False) as model:
            sci = np.array(model.data, dtype=np.float32)
            good = build_mask(np.asarray(model.dq), self.good_bits,
                              flag_name_map=pixel_flags).astype(bool)
            good &= np.isfinite(sci)
            return ChipArrays(
                sci=sci,
                var_rnoise=np.array(model.var_rnoise, dtype=np.float32),
                var_poisson=np.array(model.var_poisson, dtype=np.float32),
                var_flat=np.array(model.var_flat, dtype=np.float32),
                good=good,
            )

    def pixel_to_world(self, x, y):
        return self._wcs(x, y, with_bounding_box=False)

    def provenance(self):
        return {
            'key': self.key, 'instrument': self.instrument,
            'filter': self.filter, 'detector': self.detector,
            'program': self.program, 'visit': self.visit,
            'exptime': self.exptime, 'mid_mjd': self.mid_mjd,
        }


def open_exposure(path, good_bits='~DO_NOT_USE'):
    """The :class:`NircamChip` of a calibrated NIRCam exposure file."""
    from copy import deepcopy

    from stdatamodels.jwst.datamodels import ImageModel

    with ImageModel(path, memmap=False) as model:
        exposure = model.meta.exposure
        exptime = float(exposure.exposure_time)
        xposure = exposure.effective_exposure_time
        mid = exposure.mid_time
        return NircamChip(
            path=str(path),
            key=os.path.basename(str(path)).removesuffix('.fits'),
            filter=str(model.meta.instrument.filter or '').lower(),
            detector=str(model.meta.instrument.detector or '').lower(),
            exptime=exptime,
            xposure=float(xposure if xposure is not None else exptime),
            shape=tuple(model.data.shape),
            pixel_area_arcsec2=float(model.meta.photometry.pixelarea_arcsecsq),
            good_bits=good_bits,
            mid_mjd=float(mid) if mid is not None else float('nan'),
            program=str(model.meta.observation.program_number or ''),
            visit=str(model.meta.observation.visit_id or ''),
            _wcs=deepcopy(model.meta.wcs),
        )
