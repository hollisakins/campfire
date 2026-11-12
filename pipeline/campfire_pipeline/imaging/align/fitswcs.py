"""A FITS-WCS corrector whose tangent plane is in arcseconds.

The align solver and its matcher were built around ``tweakwcs``'
``JWSTWCSCorrector``, whose tangent-plane coordinates are arcseconds: search
radii, convergence thresholds and reported shifts are all in arcsec.
``tweakwcs.correctors.FITSWCSCorrector``, the corrector for WCS stored the HST
way (FITS keywords with SIP and lookup-table distortion), uses *pixels* of the
undistorted image plane instead.

:class:`ArcsecFITSWCSCorrector` multiplies that plane by the WCS's pixel scale,
so an HST chip can be handed to the same solver with every arcsec-valued setting
meaning what it says. Nothing about the fit changes: a linear transformation
``M x + t`` in pixels is ``M x' + s t`` in the scaled plane, and the parent
class does all of its tangent-plane arithmetic through the four coordinate
methods overridden here.
"""

import numpy as np
from astropy.wcs.utils import proj_plane_pixel_scales
from tweakwcs.correctors import FITSWCSCorrector


class ArcsecFITSWCSCorrector(FITSWCSCorrector):
    """``FITSWCSCorrector`` with tangent-plane coordinates in arcsec.

    ``wcs`` is an ``astropy.wcs.WCS`` with ``pixel_shape`` set (e.g. built from
    an HST ``SCI`` header with ``fobj=hdulist`` so the lookup-table distortions
    are attached). After a fit, :attr:`wcs` carries the corrected ``CRVAL`` and
    ``CD``; the distortion terms are untouched.
    """

    units = 'arcsec'

    def __init__(self, wcs, meta=None, tp_scale=None):
        super().__init__(wcs, meta=meta)
        if tp_scale is None:
            # arcsec per pixel of the linear (distortion-free) part, fixed at
            # construction so the plane does not move under later corrections
            tp_scale = float(np.mean(proj_plane_pixel_scales(self._wcslin))) * 3600.0
        self._tp_scale = float(tp_scale)

    @property
    def tp_scale(self):
        """Arcseconds per tangent-plane pixel."""
        return self._tp_scale

    def det_to_tanp(self, x, y):
        tx, ty = super().det_to_tanp(x, y)
        return np.multiply(tx, self._tp_scale), np.multiply(ty, self._tp_scale)

    def tanp_to_det(self, x, y):
        return super().tanp_to_det(np.divide(x, self._tp_scale),
                                   np.divide(y, self._tp_scale))

    def world_to_tanp(self, ra, dec):
        tx, ty = super().world_to_tanp(ra, dec)
        return np.multiply(tx, self._tp_scale), np.multiply(ty, self._tp_scale)

    def tanp_to_world(self, x, y):
        return super().tanp_to_world(np.divide(x, self._tp_scale),
                                     np.divide(y, self._tp_scale))

    def set_correction(self, matrix=[[1, 0], [0, 1]], shift=[0, 0],
                       ref_tpwcs=None, meta=None, **kwargs):
        if ref_tpwcs is None:
            # The parent would build a pixel-unit reference here; the shift it
            # is given is in arcsec, so the reference plane must be too.
            ref_tpwcs = ArcsecFITSWCSCorrector(self._wcs.deepcopy(),
                                               tp_scale=self._tp_scale)
        super().set_correction(matrix=matrix, shift=shift, ref_tpwcs=ref_tpwcs,
                               meta=meta, **kwargs)
