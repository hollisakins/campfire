"""
geometry: shared overlap geometry for NIRCam tile/exposure selection.

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


def compute_footprints(exposure_files, *, in_shape=(2048, 2048)):
    """Return ``{file: shapely Polygon}`` of each exposure's sky footprint.

    Each footprint is computed from the SCI extension WCS via
    ``wcs_pix2world`` on the four corners of an ``in_shape`` rectangle
    (default 2048×2048 = NIRCam detector). NIRCam detectors are all
    2048², so the default is correct for the pipeline; the parameter
    exists so this helper can be reused for other instruments.

    Footprints are tile-invariant, so callers selecting against many tiles
    should call this **once** per filter and pass the result to
    ``select_overlapping`` per tile — instead of re-opening every exposure
    inside the tile loop (the old per-tile ``select_overlapping_files``).
    """
    nx, ny = in_shape
    pixcoords = np.array(
        [[0.0, 0.0], [float(nx), 0.0],
         [float(nx), float(ny)], [0.0, float(ny)]]
    )

    footprints = {}
    for f in exposure_files:
        with fits.open(f, ignore_missing_simple=True, memmap=False) as hdul:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                wcs = WCS(hdul[1].header, naxis=2)
            worldcoords = wcs.wcs_pix2world(pixcoords, 0)
        footprints[f] = Polygon(worldcoords)
    return footprints


def select_overlapping(footprints, tile_polygon):
    """Return files whose footprint intersects ``tile_polygon`` — no I/O.

    ``footprints`` is a ``{file: Polygon}`` mapping from
    ``compute_footprints``; ``tile_polygon`` is a ``shapely`` Polygon in sky
    coordinates. Iteration follows the mapping's insertion order (the
    original file order), so the selected list matches the legacy
    ``select_overlapping_files`` ordering exactly.
    """
    return [f for f, poly in footprints.items() if tile_polygon.intersects(poly)]


def select_overlapping_files(exposure_files, tile_polygon, *, in_shape=(2048, 2048)):
    """Footprint ``exposure_files`` then return those intersecting ``tile_polygon``.

    Thin wrapper over ``compute_footprints`` + ``select_overlapping`` for
    single-tile callers. When selecting against multiple tiles, call those
    two directly so the per-file opens happen once, not once per tile.
    """
    footprints = compute_footprints(exposure_files, in_shape=in_shape)
    return select_overlapping(footprints, tile_polygon)
