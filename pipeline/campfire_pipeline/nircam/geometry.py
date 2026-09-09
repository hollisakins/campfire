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
from shapely.ops import unary_union

# Buffer (deg) applied to a tile polygon when gating whole exposures by their
# approximate ``S_REGION`` footprint (see ``filter_exposures_to_tiles``). The
# SDP-derived ``S_REGION`` is a guide-star-attitude footprint — good to ~1", not
# distortion-solved — so a small dilation keeps the coarse pre-filter a
# conservative SUPERSET of the precise per-tile selection ``resample`` still
# does with the SCI WCS. ~11 arcsec: negligible vs a tile (arcmin-scale) but
# comfortably above ``S_REGION``'s error.
DEFAULT_TILE_BUFFER_DEG = 0.003


def exposure_footprints(exposure_files, *, in_shape=(2048, 2048)):
    """One SCI-WCS footprint ``Polygon`` per file, as ``[(file, Polygon)]``.

    The footprint construction of :func:`select_overlapping_files` (four
    ``in_shape`` corners through ``wcs_pix2world``), factored out so a caller
    testing many tiles pays the per-file header reads ONCE and each tile's
    selection is then pure polygon math — instead of every tile re-opening
    every exposure.
    """
    nx, ny = in_shape
    pixcoords = np.array(
        [[0.0, 0.0], [float(nx), 0.0],
         [float(nx), float(ny)], [0.0, float(ny)]]
    )

    footprints = []
    for f in exposure_files:
        with fits.open(f, ignore_missing_simple=True, memmap=False) as hdul:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                wcs = WCS(hdul[1].header, naxis=2)
            worldcoords = wcs.wcs_pix2world(pixcoords, 0)
        footprints.append((f, Polygon(worldcoords)))
    return footprints


def select_overlapping_files(exposure_files, tile_polygon, *,
                             in_shape=(2048, 2048), footprints=None):
    """Return the subset of ``exposure_files`` whose detector footprints
    intersect ``tile_polygon`` (a ``shapely.geometry.Polygon`` in sky
    coordinates).

    Each file's footprint is computed from the SCI extension WCS via
    ``wcs_pix2world`` on the four corners of an ``in_shape`` rectangle
    (default 2048×2048 = NIRCam detector). NIRCam detectors are all
    2048², so the default is correct for the pipeline; the parameter
    exists so this helper can be reused for other instruments.

    ``footprints`` (from :func:`exposure_footprints` over the same files)
    skips the per-file reads; selection order and membership are identical
    either way.
    """
    if footprints is None:
        footprints = exposure_footprints(exposure_files, in_shape=in_shape)
    return [f for f, file_polygon in footprints
            if tile_polygon.intersects(file_polygon)]


def polygon_from_sregion(s_region):
    """Parse an ``S_REGION`` string into a footprint ``Polygon`` (or ``None``).

    Handles the standard ``'POLYGON ICRS ra1 dec1 ra2 dec2 ...'`` form (the
    leading two tokens are the shape + frame; RA/Dec alternate thereafter, the
    same layout ``expmap._parse_sregion`` reads). Returns ``None`` for anything
    unparseable or with fewer than three vertices — callers treat ``None`` as
    "footprint unknown" and fail open rather than dropping the exposure.
    """
    if not s_region:
        return None
    try:
        parts = s_region.split()
        ra = [float(x) for x in parts[2::2]]
        dec = [float(x) for x in parts[3::2]]
    except (ValueError, IndexError):
        return None
    if len(ra) < 3 or len(ra) != len(dec):
        return None
    return Polygon(zip(ra, dec))


# Per-phase memo of ``read_sregion_polygon`` keyed by absolute path. A single
# ``process``/``align``/``combine`` invocation calls the tile pre-filter once per
# step (~10 ``get_exposure_files(tiles=)`` calls in ``run_process`` alone), each
# re-deriving the same footprints. ``S_REGION`` is a ground-system keyword that
# never changes for a given file within a run, so memoizing by path collapses
# those N scans into one. Reset at each phase entry (see ``reset_sregion_cache``)
# to bound growth and stay robust to re-invocation in a long-lived process.
_SREGION_CACHE = {}
_CACHE_MISS = object()


def reset_sregion_cache():
    """Clear the per-phase ``S_REGION`` footprint memo. Called at phase entry."""
    _SREGION_CACHE.clear()


def read_sregion_polygon(path):
    """Footprint ``Polygon`` from a file's ``S_REGION`` keyword, or ``None``.

    Reads ``S_REGION`` from the SCI extension (falling back to the primary
    header), mirroring ``expmap._read_metadata``. Because ``S_REGION`` is
    written by the ground system it is present on raw ``_uncal`` files — before
    any WCS is assigned — as well as on canonical exposures, so this is the one
    overlap source usable at every pipeline phase. ``None`` when the keyword is
    missing/blank or the file can't be opened.

    Results are memoized by path for the current phase (see
    ``reset_sregion_cache``). Reads reach the SCI header by name so astropy
    stops after the first extension: slicing ``hdul[1:]`` would enumerate every
    HDU, seeking past all ~9 data units — ~4x more NFS round-trips on a cold
    mount for a keyword that lives in the first extension.
    """
    hit = _SREGION_CACHE.get(path, _CACHE_MISS)
    if hit is not _CACHE_MISS:
        return hit
    try:
        with fits.open(path, memmap=False) as hdul:
            try:
                sci_hdr = hdul['SCI'].header
            except KeyError:
                sci_hdr = hdul[1].header if len(hdul) > 1 else None
            s_region = sci_hdr.get('S_REGION') if sci_hdr is not None else None
            if s_region is None:
                s_region = hdul[0].header.get('S_REGION')
    except (OSError, KeyError):
        # Unreadable / transient — fail open (kept by callers) but do NOT cache,
        # so a later readable pass on the same path can still resolve it.
        return None
    poly = polygon_from_sregion(s_region)
    _SREGION_CACHE[path] = poly
    return poly


def select_overlapping_by_sregion(files, tile_polygon, *, key_fn=None):
    """Subset of *files* whose ``S_REGION`` footprint intersects *tile_polygon*.

    Unlike :func:`select_overlapping_files` (which derives the footprint from a
    live SCI WCS, canonical-only), this uses the ``S_REGION`` keyword, so it
    works before ``assign_wcs`` has run (raw uncals). **Fails open**: a file
    whose footprint can't be determined is kept, never silently dropped.

    With *key_fn* (e.g. :func:`association.exposure_key`), selection is by
    GROUP — every file sharing a key is kept when *any* member of that key
    overlaps (or is fail-open). This keeps a whole dither together when it
    straddles a tile edge, so the align phase pools its full detector set.
    """
    overlaps = {}
    for f in files:
        poly = read_sregion_polygon(f)
        overlaps[f] = (poly is None) or tile_polygon.intersects(poly)

    if key_fn is None:
        return [f for f in files if overlaps[f]]

    keep_keys = {key_fn(f) for f in files if overlaps[f]}
    return [f for f in files if key_fn(f) in keep_keys]


def tiles_union_polygon(field, tiles, *, buffer_deg=DEFAULT_TILE_BUFFER_DEG):
    """Buffered sky-polygon union of the named *tiles*.

    Each tile's corners come from :meth:`Field.get_tile_corners`; the small
    *buffer_deg* dilation keeps the ``S_REGION`` gate conservative. Propagates
    ``get_tile_corners``'s ``ValueError`` on an unknown tile name.
    """
    if isinstance(tiles, str):
        tiles = [tiles]
    polys = [Polygon(field.get_tile_corners(t)) for t in tiles]
    union = unary_union(polys)
    if buffer_deg:
        union = union.buffer(buffer_deg)
    return union


def filter_exposures_to_tiles(field, files, tiles, *,
                              buffer_deg=DEFAULT_TILE_BUFFER_DEG):
    """Restrict *files* to exposures overlapping the named *tiles*.

    A no-op when *tiles* is falsy. Gates whole exposures (dithers) by their
    approximate ``S_REGION`` footprint against the buffered tile union — so it
    applies uniformly at every phase, including ``detector1`` on raw uncals
    (before any WCS exists). Fail-open on unknown footprints.
    """
    if not tiles:
        return files
    from campfire_pipeline.nircam.association import exposure_key
    tile_polygon = tiles_union_polygon(field, tiles, buffer_deg=buffer_deg)
    return select_overlapping_by_sregion(files, tile_polygon,
                                         key_fn=exposure_key)
