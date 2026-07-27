"""Progeny tracking for NIRCam mosaic tiles.

Each mosaic tile gets a JSON manifest recording the input files, their hashes,
and processing parameters used.  This enables change detection so that only
stale tiles need to be re-mosaicked when new data arrives.
"""

import hashlib
import json
import os
import re
import warnings
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

    This is the *pixel* half of an input's identity. It says nothing about
    where those pixels sit on the sky — see :func:`compute_wcs_hash`, which
    :func:`file_unchanged` compares alongside it.

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
    # do_not_scale_image_data: hash the raw stored bytes regardless of
    # BZERO/BSCALE/BLANK. memmap=False: the arrays are read in full anyway,
    # and one sequential read beats memmap's page-faulted small reads on NFS.
    with fits.open(filepath, memmap=False, do_not_scale_image_data=True) as hdul:
        for extname in ('SCI', 'DQ'):
            try:
                data = hdul[extname].data
                if data is not None:
                    h.update(data.tobytes())
            except KeyError:
                pass
    return f'sha256:{h.hexdigest()}'


# The header cards that define an input's sky mapping. A whitelist rather than
# a denylist: the digest must move when the astrometry moves and never on a
# plain re-save (a re-save that looks "modified" costs a needless re-drizzle of
# every tile the file touches). Mirrored client-side in
# ``campfire.storage.hashing`` — the client cannot import the pipeline, so the
# recipe is written out in both places and the two must stay in step.
_WCS_KEY_RE = re.compile(
    r'^(?:'
    r'WCSAXES|RADESYS|EQUINOX|LONPOLE|LATPOLE'
    r'|C(?:TYPE|UNIT|RPIX|RVAL|DELT|ROTA)\d+'
    r'|CD\d+_\d+|PC\d+_\d+'
    r'|[AB]P?_ORDER|[AB]P?_DMAX|[AB]P?_\d+_\d+'
    r'|V[23]_REF|VPARITY|VA_SCALE|ROLL_REF|RA_REF|DEC_REF'
    r'|S_REGION'
    r')$'
)

_WCS_DIGEST_VERSION = b'campfire-wcs-1\n'
_WCS_HEADER_EXTS = (0, 'SCI')


def compute_wcs_hash(filepath):
    """SHA-256 over a FITS file's WCS-defining header cards, or ``None``.

    The astrometric half of an input's identity. ``align`` and ``wcs_shift``
    rewrite an exposure's WCS *without touching a science pixel*, so
    :func:`compute_file_hash` cannot see a re-alignment — a CRF regenerated with
    corrected astrometry but identical SCI/DQ hashes the same as before, and the
    tile that consumed it is judged up to date while its inputs have moved on
    the sky.

    ``None`` means the file carries no WCS cards at all (or could not be read);
    :func:`file_unchanged` treats that as "no astrometric component", never as
    a match.

    Digests the FITS (SIP-approximated) WCS that ``update_fits_wcsinfo`` writes
    from the authoritative gwcs on every solve, not the gwcs in the embedded
    ASDF extension — those bytes carry a save timestamp and would churn on every
    re-save, defeating the point of a partial digest. The two disagree only when
    ``update_fits_wcsinfo`` fails, which ``align`` logs.
    """
    h = hashlib.sha256()
    h.update(_WCS_DIGEST_VERSION)
    found = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with fits.open(filepath, memmap=False, lazy_load_hdus=True) as hdul:
                for ext in _WCS_HEADER_EXTS:
                    try:
                        header = hdul[ext].header
                    except (KeyError, IndexError):
                        continue
                    # Sorted, so card order in the file cannot move the digest;
                    # repr() round-trips a float's full precision exactly, so a
                    # re-save writing the same numbers hashes the same.
                    for key in sorted(k for k in header if _WCS_KEY_RE.match(k)):
                        h.update(
                            f'{ext}|{key}={header[key]!r}\n'.encode('utf-8', 'replace'))
                        found = True
    except Exception:
        return None
    return f'sha256:{h.hexdigest()}' if found else None


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

    The astrometric digest is compared **only when the old entry carries one**.
    Manifests written before ``wcs_hash`` existed therefore keep their current
    verdicts instead of declaring every input modified — a recipe change that
    invalidated every manifest at once would re-drizzle every field's every tile
    on the next run. Those entries pick up the digest the next time the manifest
    is rewritten, and ``--overwrite`` forces the rebuild in the meantime.
    """
    size = old_entry.get('size')
    mtime_ns = old_entry.get('mtime_ns')
    if size is not None and mtime_ns is not None:
        cur_size, cur_mtime = file_stat(filepath)
        if cur_size == size and cur_mtime == mtime_ns:
            return True
    if compute_file_hash(filepath) != old_entry.get('file_hash'):
        return False
    old_wcs = old_entry.get('wcs_hash')
    if old_wcs and compute_wcs_hash(filepath) != old_wcs:
        return False
    return True


def input_entry(filepath, extra=None):
    """Build a manifest input record with hash + fast-path stat fields."""
    size, mtime_ns = file_stat(filepath)
    entry = {
        'filename': os.path.basename(filepath),
        'file_hash': compute_file_hash(filepath),
        # Astrometric digest: without it a re-aligned input (same pixels, new
        # WCS) reads as unchanged and its tiles are never rebuilt.
        'wcs_hash': compute_wcs_hash(filepath),
        'size': size,
        'mtime_ns': mtime_ns,
    }
    if extra:
        entry.update(extra)
    return entry


# ---------------------------------------------------------------------------
# Mosaic naming (epic #261, N2 / D3 — the version axis is retired)
# ---------------------------------------------------------------------------

# One logical mosaic per (field, filter, tile, pixel_scale, extension); no
# version segment. The single builder below is the sole authority for the
# basename, shared by the resample step and the staleness check so the two can
# never disagree.
DEFAULT_MOSAIC_NAME = 'mosaic_nircam_[filter]_[field_name]_[pixel_scale]_[tile]'


def build_mosaic_name(filtname, field_name, pixel_scale, tile, epoch=None,
                      template=None):
    """Version-free mosaic basename (without ``_i2d.fits``).

    Expands the ``[filter]`` / ``[field_name]`` / ``[pixel_scale]`` / ``[tile]``
    placeholders. A ``template`` override (from ``resample.mosaic_name`` config)
    may omit some placeholders but must NOT reintroduce ``[version]`` — that axis
    is retired (D3).

    ``epoch`` (optional) appends a trailing ``_<epoch>`` segment, marking a
    mosaic built from an exposure subset (fields.toml ``[<field>.epochs.<name>]``).
    An empty/None epoch yields today's version-free full-field name unchanged, so
    normal mosaics and their deploy identity stay byte-for-byte compatible.
    """
    tmpl = template or DEFAULT_MOSAIC_NAME
    name = (tmpl
            .replace('[filter]', filtname)
            .replace('[field_name]', field_name)
            .replace('[pixel_scale]', pixel_scale)
            .replace('[tile]', tile))
    if epoch:
        name = f'{name}_{epoch}'
    return name


def _resample_config_hash(resample_cfg, pixel_scale):
    """Hash the resample config fields that affect mosaic pixels.

    Single source of truth for :func:`create_manifest` and
    :func:`check_config_changed` so the two can never drift. The bg_reject
    keys are folded in **only when the guard is enabled** (non-default):
    existing tiles keep their historical hash (no spurious global rebuild),
    while flipping ``bg_reject`` on a field hashes distinctly so
    ``get_stale_tiles`` rebuilds its tiles. ``wht_aware`` follows the same
    non-default-only pattern (folded in only when *disabled*).
    """
    cfg = {
        'pixfrac': resample_cfg.get('pixfrac', 1),
        'kernel': resample_cfg.get('kernel', 'square'),
        'pixel_scale': pixel_scale,
        'background_subtract': resample_cfg.get('background_subtract', True),
    }
    if not resample_cfg.get('wht_aware', True):
        cfg['wht_aware'] = False
    if resample_cfg.get('bg_reject', False):
        cfg['bg_reject'] = True
        cfg['bg_reject_sigma_hi'] = resample_cfg.get('bg_reject_sigma_hi', 4.0)
        cfg['bg_reject_sigma_lo'] = resample_cfg.get('bg_reject_sigma_lo', 3.0)
        cfg['bg_reject_percentile'] = resample_cfg.get(
            'bg_reject_percentile', 60.0)
        cfg['bg_reject_dilate'] = resample_cfg.get('bg_reject_dilate', 40.0)
    config_str = json.dumps(cfg, sort_keys=True)
    return f'sha256:{hashlib.sha256(config_str.encode()).hexdigest()}'


# ---------------------------------------------------------------------------
# Manifest creation / I/O
# ---------------------------------------------------------------------------

def create_manifest(mosaic_name, field, filtname, tile, pixel_scale,
                    input_files, stage_config, epoch=None):
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
    input_files : list of str
        Paths to the CRF files that were drizzled into this tile.
    stage_config : dict
        Stage-3 configuration dict.
    epoch : str, optional
        Epoch name for a subset mosaic, or ``None``/``''`` for the full field.
        Recorded verbatim (empty string when absent) so deploy can key on it.

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
            with fits.open(f, memmap=False) as hdul:
                date_obs = hdul[0].header.get('DATE-OBS')
                if date_obs:
                    extra['date_obs'] = date_obs
        except Exception:
            pass
        inputs.append(input_entry(f, extra=extra))

    # Hash the relevant processing config so we can detect config changes too
    config_hash = _resample_config_hash(resample_cfg, pixel_scale)

    return {
        'mosaic_name': mosaic_name,
        'field': field.name,
        'filter': filtname,
        'tile': tile,
        'pixel_scale': pixel_scale,
        'epoch': epoch or '',
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
    current_hash = _resample_config_hash(resample_cfg, pixel_scale)

    return current_hash != manifest.get('config_hash')


def get_stale_tiles(field, filtname, stage_config, tiles=None, epoch=None):
    """Identify tiles that need re-mosaicking.

    Parameters
    ----------
    field : Field
        NIRCam field dataclass (workspace must be set up).
    filtname : str
        Filter name.
    stage_config : dict
        Stage-3 configuration dict.
    tiles : str, list of str, or None
        Tile name(s) to probe. ``None`` (the default) checks every tile in
        the field.
    epoch : str, optional
        Probe the named epoch's mosaics (subset inputs + epoch-labelled
        manifest name), matching what ``resample --epoch`` builds. ``None``
        (the default) checks the full-field mosaics.

    Returns
    -------
    list of dict
        One entry per tile with keys: ``tile``, ``stale`` (bool),
        ``reasons`` (list of str), ``manifest_path``.
    """
    from shapely.geometry import Polygon

    from campfire_pipeline.nircam.geometry import select_overlapping_files

    resample_cfg = stage_config.get('resample', {})
    pixel_scale = resample_cfg.get('pixel_scale', '60mas')
    if isinstance(pixel_scale, (float, int)):
        if pixel_scale > 1:
            pixel_scale = f'{int(pixel_scale)}mas'
        else:
            pixel_scale = f'{int(pixel_scale * 1000)}mas'

    if tiles is None:
        tiles = list(field.tiles.keys())
    elif isinstance(tiles, str):
        tiles = [tiles]

    files_to_skip = stage_config.get('files_to_skip', [])
    # Resample's input source: the combine working copies whose outlier
    # detection has finished (CFP_OUT keyword stamped). CFP_OUT lives on the
    # working copies, never the frozen canonical, so this must match the set
    # resample_step actually drizzles (work=True).
    candidate_files = field.get_exposure_files(
        filtname,
        skip=files_to_skip if files_to_skip else None,
        with_step='CFP_OUT',
        work=True,
        epoch=epoch,
    )

    results = []
    for tile in tiles:
        mosaic_name = build_mosaic_name(
            filtname, field.name, pixel_scale, tile, epoch=epoch,
            template=resample_cfg.get('mosaic_name'),
        )

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


