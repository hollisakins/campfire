"""
geometry: shared overlap geometry for NIRCam tile/exposure selection.

Single source of truth for "which input exposures overlap a given tile
polygon". Used by ``steps/resample.py``, ``steps/outlier.py``
(per-tile path), and ``manifest.py`` (staleness check). Keeping the
selection logic in one place ensures all three paths see the same
input set for a given tile.
"""

import warnings
from collections import namedtuple

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from shapely.geometry import Polygon


# Per-exposure geometry read once from the SCI header: the detector-corner
# footprint polygon (for tile-overlap selection) and the S_REGION string (for
# the drizzle output-WCS build). Both come from the same single open.
InputGeometry = namedtuple('InputGeometry', ['footprint', 's_region'])


def compute_input_geometry(exposure_files, *, in_shape=(2048, 2048)):
    """Return ``{file: InputGeometry(footprint, s_region)}`` — one open each.

    The footprint is computed from the SCI extension WCS via ``wcs_pix2world``
    on the four corners of an ``in_shape`` rectangle (default 2048×2048 =
    NIRCam detector; all NIRCam detectors are 2048², the parameter exists so
    this can be reused for other instruments). The ``s_region`` is that same
    extension's ``S_REGION`` keyword, read from the same open.

    Both are tile-invariant, so callers selecting against many tiles call this
    **once** per filter and reuse it: ``select_overlapping`` consumes the
    footprints per tile, and the resample drizzle consumes the S_REGION
    strings for its output-WCS build — neither re-opens the inputs per tile.
    """
    nx, ny = in_shape
    pixcoords = np.array(
        [[0.0, 0.0], [float(nx), 0.0],
         [float(nx), float(ny)], [0.0, float(ny)]]
    )

    geometry = {}
    for f in exposure_files:
        with fits.open(f, ignore_missing_simple=True, memmap=False) as hdul:
            hdr = hdul[1].header
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                wcs = WCS(hdr, naxis=2)
            worldcoords = wcs.wcs_pix2world(pixcoords, 0)
            s_region = hdr.get('S_REGION')
        geometry[f] = InputGeometry(Polygon(worldcoords), s_region)
    return geometry


def select_overlapping(geometry, tile_polygon):
    """Return files whose footprint intersects ``tile_polygon`` — no I/O.

    ``geometry`` is a ``{file: InputGeometry}`` mapping from
    ``compute_input_geometry``; ``tile_polygon`` is a ``shapely`` Polygon in
    sky coordinates. Iteration follows the mapping's insertion order (the
    original file order), so the selected list matches the legacy
    ``select_overlapping_files`` ordering exactly.
    """
    return [
        f for f, geom in geometry.items()
        if tile_polygon.intersects(geom.footprint)
    ]


def select_overlapping_files(exposure_files, tile_polygon, *, in_shape=(2048, 2048)):
    """Footprint ``exposure_files`` then return those intersecting ``tile_polygon``.

    Thin wrapper over ``compute_input_geometry`` + ``select_overlapping`` for
    single-tile callers. When selecting against multiple tiles, call those two
    directly so the per-file opens happen once, not once per tile.
    """
    geometry = compute_input_geometry(exposure_files, in_shape=in_shape)
    return select_overlapping(geometry, tile_polygon)
