"""Output tile grids as plain FITS tangent-plane WCS.

A tile is fully specified in ``fields.toml`` by a tangent point, a rotation, and
``crpix`` / ``naxis`` at a pixel scale (see ``Field.get_tile_wcs``). The NIRCam
route turns those numbers into a gwcs through ``stcal`` using a JWST exposure as
a template. :class:`TileGrid` builds the same tangent plane as an
``astropy.wcs.WCS`` from the numbers alone, so an instrument without JWST
datamodels can drizzle onto the identical grid.

The rotation convention is the one ``Field._compute_corners_from_wcs`` documents
and the JWST resample step uses: position angle of the output +y axis, measured
east of north.
"""

from dataclasses import dataclass
from functools import cached_property

import numpy as np


@dataclass(frozen=True)
class TileGrid:
    """One output tile at one pixel scale.

    ``crpix`` is **0-indexed** (what ``Field.get_tile_wcs`` returns), ``shape``
    is ``(nx, ny)``, ``pixel_scale`` is in arcsec, ``rotation`` in degrees.
    """
    crval: tuple
    crpix: tuple
    shape: tuple
    rotation: float
    pixel_scale: float

    @property
    def array_shape(self):
        nx, ny = self.shape
        return (int(ny), int(nx))

    @cached_property
    def wcs(self):
        from astropy.wcs import WCS

        scale = float(self.pixel_scale) / 3600.0
        theta = np.radians(float(self.rotation))
        cos_t, sin_t = np.cos(theta), np.sin(theta)
        w = WCS(naxis=2)
        w.wcs.crpix = [self.crpix[0] + 1.0, self.crpix[1] + 1.0]
        w.wcs.crval = [float(self.crval[0]), float(self.crval[1])]
        w.wcs.ctype = ['RA---TAN', 'DEC--TAN']
        w.wcs.cunit = ['deg', 'deg']
        w.wcs.cd = np.array([[-scale * cos_t, scale * sin_t],
                             [scale * sin_t, scale * cos_t]])
        w.wcs.radesys = 'ICRS'
        nx, ny = self.shape
        w.pixel_shape = (int(nx), int(ny))
        return w

    def world_to_pixel(self, ra, dec):
        """``(ra, dec)`` in degrees to 0-indexed output pixel ``(x, y)``."""
        return self.wcs.all_world2pix(ra, dec, 0)

    def pixel_to_world(self, x, y):
        """0-indexed output pixel ``(x, y)`` to ``(ra, dec)`` in degrees."""
        return self.wcs.all_pix2world(x, y, 0)

    def header(self):
        """FITS WCS cards of the grid (CD form, 1-indexed CRPIX)."""
        return self.wcs.to_header()

    def footprint_sregion(self):
        """``S_REGION``-style polygon of the tile outline."""
        nx, ny = self.shape
        xs = np.array([-0.5, nx - 0.5, nx - 0.5, -0.5])
        ys = np.array([-0.5, -0.5, ny - 0.5, ny - 0.5])
        ra, dec = self.pixel_to_world(xs, ys)
        return 'POLYGON ICRS ' + ' '.join(
            f'{a:.9f} {d:.9f}' for a, d in zip(ra, dec))
