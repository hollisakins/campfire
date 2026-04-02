"""Progeny tracking for NIRCam mosaic tiles.

Each mosaic tile gets a JSON manifest recording the input files, their hashes,
and processing parameters used.  This enables change detection so that only
stale tiles need to be re-mosaicked when new data arrives.
"""

import hashlib
import json
import os
from datetime import datetime, timezone

from astropy.io import fits

from campfire_pipeline import __version__ as pipeline_version
from campfire_pipeline.common.io import log


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def compute_file_hash(filepath):
    """Compute a SHA-256 hash of the SCI and DQ extensions of a FITS file.

    Only hashing the science data (not padding or auxiliary HDUs) keeps this
    fast and deterministic across different astropy write orderings.

    Parameters
    ----------
    filepath : str
        Path to a FITS file with SCI and DQ extensions.

    Returns
    -------
    str
        Hex digest prefixed with ``sha256:``.
    """
    h = hashlib.sha256()
    with fits.open(filepath, memmap=True) as hdul:
        for extname in ('SCI', 'DQ'):
            try:
                data = hdul[extname].data
                if data is not None:
                    h.update(data.tobytes())
            except KeyError:
                pass
    return f'sha256:{h.hexdigest()}'


# ---------------------------------------------------------------------------
# Manifest creation / I/O
# ---------------------------------------------------------------------------

def create_manifest(mosaic_name, field, filtname, tile, pixel_scale,
                    version, input_files, stage_config):
    """Build a manifest dict for a completed mosaic tile.

    Parameters
    ----------
    mosaic_name : str
        Output mosaic product name (without ``_i2d.fits``).
    field : Field
        NIRCam field dataclass.
    filtname : str
        Filter name.
    tile : str
        Tile name.
    pixel_scale : str
        Pixel scale string (e.g. ``'60mas'``).
    version : str
        Mosaic version string (e.g. ``'v0_1'``).
    input_files : list of str
        Paths to the CRF files that were drizzled into this tile.
    stage_config : dict
        Stage-3 configuration dict.

    Returns
    -------
    dict
        Manifest dictionary ready to be written to JSON.
    """
    resample_cfg = stage_config.get('resample', {})

    inputs = []
    for f in sorted(input_files):
        basename = os.path.basename(f)
        # Extract visit and detector from JWST filename convention
        parts = basename.split('_')
        visit = parts[0] if len(parts) > 0 else ''
        detector = parts[3] if len(parts) > 3 else ''

        entry = {
            'filename': basename,
            'file_hash': compute_file_hash(f),
            'visit': visit,
            'detector': detector,
        }
        # Read DATE-OBS if available
        try:
            with fits.open(f, memmap=True) as hdul:
                date_obs = hdul[0].header.get('DATE-OBS')
                if date_obs:
                    entry['date_obs'] = date_obs
        except Exception:
            pass

        inputs.append(entry)

    # Hash the relevant processing config so we can detect config changes too
    config_str = json.dumps({
        'pixfrac': resample_cfg.get('pixfrac', 1),
        'kernel': resample_cfg.get('kernel', 'square'),
        'pixel_scale': pixel_scale,
        'background_subtract': resample_cfg.get('background_subtract', True),
    }, sort_keys=True)
    config_hash = f'sha256:{hashlib.sha256(config_str.encode()).hexdigest()}'

    return {
        'mosaic_name': mosaic_name,
        'field': field.name,
        'filter': filtname,
        'tile': tile,
        'pixel_scale': pixel_scale,
        'version': version,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'pipeline_version': pipeline_version,
        'config_hash': config_hash,
        'inputs': inputs,
        'processing': {
            'outlier_detection': True,
            'background_subtracted': resample_cfg.get('background_subtract', True),
            'pixfrac': resample_cfg.get('pixfrac', 1),
            'kernel': resample_cfg.get('kernel', 'square'),
        },
    }


def write_manifest(manifest, manifest_dir):
    """Write a manifest dict to JSON.

    Parameters
    ----------
    manifest : dict
        Manifest dictionary (from :func:`create_manifest`).
    manifest_dir : str
        Directory to write into (created if needed).
    """
    os.makedirs(manifest_dir, exist_ok=True)
    path = os.path.join(manifest_dir, f'{manifest["mosaic_name"]}_manifest.json')
    with open(path, 'w') as fp:
        json.dump(manifest, fp, indent=2)
    log(f'Wrote manifest: {os.path.basename(path)}')
    return path


def load_manifest(manifest_path):
    """Load a manifest from disk.

    Returns
    -------
    dict or None
        Manifest dict, or None if the file does not exist.
    """
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path) as fp:
        return json.load(fp)


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

def check_inputs_changed(manifest_path, current_input_files):
    """Compare current input files against a stored manifest.

    Parameters
    ----------
    manifest_path : str
        Path to the manifest JSON file.
    current_input_files : list of str
        Paths to the CRF files that *would* go into this tile now.

    Returns
    -------
    changed : bool
        True if the inputs have changed (new, removed, or modified files).
    reasons : list of str
        Human-readable reasons describing what changed.
    """
    manifest = load_manifest(manifest_path)
    if manifest is None:
        return True, ['no existing manifest']

    old_by_name = {inp['filename']: inp for inp in manifest['inputs']}
    new_names = {os.path.basename(f) for f in current_input_files}
    old_names = set(old_by_name.keys())

    reasons = []

    added = new_names - old_names
    if added:
        reasons.append(f'{len(added)} new file(s): {", ".join(sorted(added))}')

    removed = old_names - new_names
    if removed:
        reasons.append(f'{len(removed)} removed file(s): {", ".join(sorted(removed))}')

    # Check hashes for files present in both
    for f in sorted(current_input_files):
        basename = os.path.basename(f)
        if basename in old_by_name:
            current_hash = compute_file_hash(f)
            if current_hash != old_by_name[basename]['file_hash']:
                reasons.append(f'modified: {basename}')

    changed = len(reasons) > 0
    return changed, reasons


def check_config_changed(manifest_path, stage_config, pixel_scale):
    """Check whether processing config has changed since the manifest was written.

    Parameters
    ----------
    manifest_path : str
        Path to the manifest JSON file.
    stage_config : dict
        Current stage-3 configuration dict.
    pixel_scale : str
        Current pixel scale string.

    Returns
    -------
    changed : bool
    """
    manifest = load_manifest(manifest_path)
    if manifest is None:
        return True

    resample_cfg = stage_config.get('resample', {})
    config_str = json.dumps({
        'pixfrac': resample_cfg.get('pixfrac', 1),
        'kernel': resample_cfg.get('kernel', 'square'),
        'pixel_scale': pixel_scale,
        'background_subtract': resample_cfg.get('background_subtract', True),
    }, sort_keys=True)
    current_hash = f'sha256:{hashlib.sha256(config_str.encode()).hexdigest()}'

    return current_hash != manifest.get('config_hash')


def get_stale_tiles(field, filtname, stage_config):
    """Identify tiles that need re-mosaicking.

    Parameters
    ----------
    field : Field
        NIRCam field dataclass (workspace must be set up).
    filtname : str
        Filter name.
    stage_config : dict
        Stage-3 configuration dict.

    Returns
    -------
    list of dict
        One entry per tile with keys: ``tile``, ``stale`` (bool),
        ``reasons`` (list of str), ``manifest_path``.
    """
    from shapely.geometry import Polygon

    resample_cfg = stage_config.get('resample', {})
    version = resample_cfg.get('version', 'v0_1')
    pixel_scale = resample_cfg.get('pixel_scale', '60mas')
    if isinstance(pixel_scale, (float, int)):
        if pixel_scale > 1:
            pixel_scale = f'{int(pixel_scale)}mas'
        else:
            pixel_scale = f'{int(pixel_scale * 1000)}mas'

    tiles = resample_cfg.get('tile', None)
    if tiles is None:
        tiles = list(field.tiles.keys())
    if isinstance(tiles, str):
        tiles = [tiles]

    files_to_skip = stage_config.get('files_to_skip', [])
    crf_files = field.get_crf_files(filtname, skip=files_to_skip if files_to_skip else None)

    results = []
    for tile in tiles:
        mosaic_name = resample_cfg.get(
            'mosaic_name',
            'mosaic_nircam_[filter]_[field_name]_[pixel_scale]_[version]_[tile]',
        )
        mosaic_name = mosaic_name.replace('[filter]', filtname)
        mosaic_name = mosaic_name.replace('[field_name]', field.name)
        mosaic_name = mosaic_name.replace('[pixel_scale]', pixel_scale)
        mosaic_name = mosaic_name.replace('[version]', version)
        mosaic_name = mosaic_name.replace('[tile]', tile)

        manifest_dir = os.path.join(field.mosaic_dir, filtname, 'manifests')
        manifest_path = os.path.join(manifest_dir, f'{mosaic_name}_manifest.json')

        # Find which CRF files overlap this tile
        tile_polygon = Polygon(field.get_tile_corners(tile))
        selected = _select_overlapping_files(crf_files, tile_polygon)

        changed, reasons = check_inputs_changed(manifest_path, selected)

        if not changed:
            if check_config_changed(manifest_path, stage_config, pixel_scale):
                changed = True
                reasons = ['processing config changed']

        results.append({
            'tile': tile,
            'stale': changed,
            'reasons': reasons,
            'manifest_path': manifest_path,
            'n_inputs': len(selected),
        })

    return results


def _select_overlapping_files(crf_files, tile_polygon):
    """Return the subset of CRF files whose footprints overlap a tile polygon.

    This duplicates the selection logic in resample_step so that ``check``
    gives the same answer as an actual reduction run.
    """
    import warnings

    import numpy as np
    from astropy.wcs import WCS
    from shapely.geometry import Polygon as ShapelyPolygon

    selected = []
    for f in crf_files:
        with fits.open(f, memmap=True) as hdul:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                wcs = WCS(hdul[1].header, naxis=2)
            pixcoords = np.array([[0., 0.], [2048., 0.], [2048., 2048.], [0., 2048.]])
            worldcoords = wcs.wcs_pix2world(pixcoords, 0)
            file_polygon = ShapelyPolygon(worldcoords)
            if tile_polygon.intersects(file_polygon):
                selected.append(f)
    return selected
