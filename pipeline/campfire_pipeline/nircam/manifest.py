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
    # do_not_scale_image_data lets memmap work even when extensions carry
    # BZERO/BSCALE/BLANK; the raw stored bytes are a fine fingerprint.
    with fits.open(filepath, memmap=True, do_not_scale_image_data=True) as hdul:
        for extname in ('SCI', 'DQ'):
            try:
                data = hdul[extname].data
                if data is not None:
                    h.update(data.tobytes())
            except KeyError:
                pass
    return f'sha256:{h.hexdigest()}'


def file_stat(filepath):
    """Return ``(size, mtime_ns)`` for fast change detection."""
    st = os.stat(filepath)
    return st.st_size, st.st_mtime_ns


def file_unchanged(filepath, old_entry):
    """True if *filepath* still matches the recorded *old_entry*.

    Fast path: when the manifest carries ``size`` and ``mtime_ns`` AND they
    match the current stat, the file hasn't been rewritten — skip the
    SHA-256 read entirely. Otherwise fall back to recomputing the content
    hash and comparing.
    """
    size = old_entry.get('size')
    mtime_ns = old_entry.get('mtime_ns')
    if size is not None and mtime_ns is not None:
        cur_size, cur_mtime = file_stat(filepath)
        if cur_size == size and cur_mtime == mtime_ns:
            return True
    return compute_file_hash(filepath) == old_entry.get('file_hash')


def input_entry(filepath, extra=None):
    """Build a manifest input record with hash + fast-path stat fields."""
    size, mtime_ns = file_stat(filepath)
    entry = {
        'filename': os.path.basename(filepath),
        'file_hash': compute_file_hash(filepath),
        'size': size,
        'mtime_ns': mtime_ns,
    }
    if extra:
        entry.update(extra)
    return entry


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
        parts = os.path.basename(f).split('_')
        extra = {
            'visit': parts[0] if len(parts) > 0 else '',
            'detector': parts[3] if len(parts) > 3 else '',
        }
        try:
            with fits.open(f, memmap=True) as hdul:
                date_obs = hdul[0].header.get('DATE-OBS')
                if date_obs:
                    extra['date_obs'] = date_obs
        except Exception:
            pass
        inputs.append(input_entry(f, extra=extra))

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


def write_manifest(manifest, manifest_dir_or_path):
    """Write a manifest dict to JSON.

    Parameters
    ----------
    manifest : dict
        Manifest dictionary.
    manifest_dir_or_path : str
        If this ends with ``.json``, treated as the full output path.
        Otherwise treated as a directory and the filename is derived from
        ``manifest["mosaic_name"]``.
    """
    if manifest_dir_or_path.endswith('.json'):
        path = manifest_dir_or_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
    else:
        os.makedirs(manifest_dir_or_path, exist_ok=True)
        path = os.path.join(manifest_dir_or_path, f'{manifest["mosaic_name"]}_manifest.json')
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

    for f in sorted(current_input_files):
        basename = os.path.basename(f)
        if basename in old_by_name and not file_unchanged(f, old_by_name[basename]):
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

    from campfire_pipeline.nircam.geometry import select_overlapping_files

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
    # Resample's input source: canonical exposures whose outlier detection
    # has finished (CFP_OUT keyword stamped).
    candidate_files = field.get_exposure_files(
        filtname,
        skip=files_to_skip if files_to_skip else None,
        with_step='CFP_OUT',
    )

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

        manifest_path = os.path.join(
            field.filter_dir(filtname), f'{mosaic_name}_manifest.json',
        )

        # Find which canonical exposures overlap this tile
        tile_polygon = Polygon(field.get_tile_corners(tile))
        selected = select_overlapping_files(candidate_files, tile_polygon)

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


