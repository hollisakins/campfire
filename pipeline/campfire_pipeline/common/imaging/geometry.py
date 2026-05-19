"""
geometry: shared overlap geometry for imaging-arm tile/exposure selection.

Single source of truth for "which input exposures overlap a given tile
polygon". Used by ``steps/resample.py``, ``steps/outlier.py``
(per-tile path), and ``manifest.py`` (staleness check). Keeping the
selection logic in one place ensures all three paths see the same
input set for a given tile.
"""

import warnings

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from shapely.geometry import Polygon


def select_overlapping_files(exposure_files, tile_polygon, *, in_shape=(2048, 2048)):
    """Return the subset of ``exposure_files`` whose detector footprints
    intersect ``tile_polygon`` (a ``shapely.geometry.Polygon`` in sky
    coordinates).

    Each file's footprint is computed from the SCI extension WCS via
    ``wcs_pix2world`` on the four corners of an ``in_shape`` rectangle
    (default 2048×2048 = NIRCam detector). Override ``in_shape`` for
    other instruments (e.g. MIRI imager = 1024×1024).
    """
    nx, ny = in_shape
    pixcoords = np.array(
        [[0.0, 0.0], [float(nx), 0.0],
         [float(nx), float(ny)], [0.0, float(ny)]]
    )

    selected = []
    for f in exposure_files:
        with fits.open(f, ignore_missing_simple=True) as hdul:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                wcs = WCS(hdul[1].header, naxis=2)
            worldcoords = wcs.wcs_pix2world(pixcoords, 0)
        file_polygon = Polygon(worldcoords)
        if tile_polygon.intersects(file_polygon):
            selected.append(f)
    return selected
