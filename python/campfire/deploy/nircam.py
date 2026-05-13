"""
NIRCam exposure deployment — push canonical exposure state to Supabase + R2.

Scans the per-filter flat directories (the new canonical layout where each
exposure is a single ``{rootname}.fits`` mutated in place through the
``CFP_*`` provenance keys), derives a ``stage`` value from the highest
completed CFP key, uploads ``*_preview.png`` thumbnails to R2, and upserts
``nircam_exposures`` while preserving any web-triage fields
(``review_status``, ``correction``, ``notes``).

Excluded exposures flagged by reviewers in the admin UI are surfaced for
copy-paste into the field's ``skip = [...]`` block in ``fields.toml``; we no
longer maintain a local ``exposures.json`` contract since nothing in the
pipeline consumes it.
"""

import os
import sys
from collections import Counter
from datetime import datetime, timezone
from glob import glob
from pathlib import Path

from astropy.io import fits

from campfire.deploy.r2 import UploadTask, upload_files_parallel
from campfire.deploy.supabase import get_supabase_client


# ---------------------------------------------------------------------------
# CFP key → stage name mapping
# ---------------------------------------------------------------------------
# Ordered from earliest to latest, matching campfire_pipeline.common.cfp.CFP_KEYS
# (kept duplicated here so the deploy package doesn't depend on the pipeline).
# Each entry: (CFP_* keyword, stage value to report).
_CFP_TO_STAGE = [
    ('CFP_DET1', 'detector1'),
    ('CFP_PERS', 'persistence'),
    ('CFP_WISP', 'wisp'),
    ('CFP_1F',   'striping'),
    ('CFP_IMG2', 'image2'),
    ('CFP_EDGE', 'edge'),
    ('CFP_SKY',  'sky'),
    ('CFP_DIAG', 'diag_striping'),
    ('CFP_VAR',  'variance'),
    ('CFP_SHFT', 'wcs_shift'),
    ('CFP_PREV', 'preview'),
    ('CFP_JHAT', 'jhat'),
    ('CFP_MASK', 'apply_mask'),
    ('CFP_BPIX', 'bad_pixel'),
    ('CFP_OUT',  'outlier'),
]


def _stage_from_header(header):
    """Return the highest-completed step name based on CFP_* keys in ``header``.

    Falls back to ``'detector1'`` if a canonical file exists but no CFP key is
    set — that's unusual but the file's mere existence implies detector1 ran.
    """
    stage = 'detector1'
    for key, name in _CFP_TO_STAGE:
        if key in header:
            stage = name
    return stage


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_campfire_root():
    root = os.environ.get('CAMPFIRE_ROOT')
    if not root:
        print("Error: $CAMPFIRE_ROOT is not set.")
        print("  export CAMPFIRE_ROOT=/path/to/campfire/data")
        sys.exit(1)
    return Path(root)


def _resolve_nircam_dirs(field):
    """Return a dict of NIRCam directory paths for a field."""
    root = _resolve_campfire_root()
    products = root / 'products' / 'nircam' / field
    return {
        'root': root,
        'products': products,
        'reference': root / 'reference' / 'nircam' / field,
        'masks': root / 'reference' / 'nircam' / field / 'masks',
    }


def _discover_filters(dirs):
    """List available filters by scanning the products directory."""
    products = dirs['products']
    if not products.exists():
        return []
    return sorted(
        d.name for d in products.iterdir()
        if d.is_dir() and not d.name.startswith('.')
    )


# ---------------------------------------------------------------------------
# Exposure discovery
# ---------------------------------------------------------------------------

def discover_exposures(dirs, filters):
    """Discover canonical exposures and derive metadata from their headers.

    Scans ``products/nircam/{field}/{filter}/jw*.fits`` (excluding mosaics and
    transient files), reads the primary header for CFP_* keys and basic
    metadata. Mosaics are named ``mosaic_*.fits`` and naturally excluded by
    the ``jw*`` glob.

    Returns
    -------
    dict
        ``{(filter, basename): info}`` where info has keys ``basename``,
        ``filter``, ``stage``, ``visit``, ``detector``, ``date_obs``,
        ``ra_center``, ``dec_center``.
    """
    exposures = {}
    for filtname in filters:
        filter_dir = dirs['products'] / filtname
        if not filter_dir.exists():
            continue
        for path in sorted(filter_dir.glob('jw*.fits')):
            name = path.name
            if name.endswith('.tmp.fits') or name.endswith('_jump.fits'):
                continue
            basename = name.removesuffix('.fits')
            info = _read_exposure_metadata(path)
            info['basename'] = basename
            info['filter'] = filtname
            exposures[(filtname, basename)] = info
    return exposures


def _read_exposure_metadata(path):
    """Read header → stage + metadata in a single open."""
    info = {
        'stage': 'detector1',
        'visit': None,
        'detector': None,
        'date_obs': None,
        'ra_center': None,
        'dec_center': None,
    }
    # JWST naming convention: {visit}_{activity}_{exposure}_{detector}.fits
    parts = path.stem.split('_')
    if len(parts) >= 1:
        info['visit'] = parts[0]
    if len(parts) >= 4:
        info['detector'] = parts[3]

    try:
        with fits.open(path, memmap=True) as hdul:
            hdr0 = hdul[0].header
            info['stage'] = _stage_from_header(hdr0)
            info['date_obs'] = hdr0.get('DATE-OBS')
            info['detector'] = hdr0.get('DETECTOR', info['detector'])
            if len(hdul) > 1:
                sci_hdr = hdul[1].header
                ra = sci_hdr.get('CRVAL1')
                dec = sci_hdr.get('CRVAL2')
                if ra is not None and dec is not None:
                    info['ra_center'] = float(ra)
                    info['dec_center'] = float(dec)
    except Exception:
        pass
    return info


def _detect_masking(dirs, basename, filtname):
    """Return 'done' if a .reg mask file exists, else None.

    Pipeline reads masks from ``masks/<filter>/<rootname>.reg`` (no ``_cal``
    suffix in the new canonical layout).
    """
    reg_path = dirs['masks'] / filtname / f'{basename}.reg'
    return 'done' if reg_path.exists() else None


# ---------------------------------------------------------------------------
# Preview PNG discovery
# ---------------------------------------------------------------------------

def build_upload_tasks(dirs, field, filters):
    """Build R2 upload tasks for per-exposure preview PNGs.

    The ``preview`` pipeline step writes two PNGs per exposure:
    ``{rootname}_preview.png`` (downsampled thumbnail) and
    ``{rootname}_full.png`` (native resolution, used by the in-browser mask
    editor). Both are uploaded so the table view stays fast while the
    editor renders at exposure-pixel resolution.

    Returns list of (UploadTask, basename, filter, kind) tuples where
    ``kind`` is ``'thumb'`` or ``'full'``.
    """
    tasks = []
    for filtname in filters:
        png_dir = dirs['products'] / filtname
        if not png_dir.exists():
            continue
        for png_path in sorted(png_dir.glob('jw*_preview.png')):
            basename = png_path.name.removesuffix('_preview.png')
            r2_key = f'nircam/exposures/{field}/{filtname}/{png_path.name}'
            tasks.append((
                UploadTask(local_path=png_path, r2_key=r2_key,
                           content_type='image/png'),
                basename, filtname, 'thumb',
            ))
        for png_path in sorted(png_dir.glob('jw*_full.png')):
            basename = png_path.name.removesuffix('_full.png')
            r2_key = f'nircam/exposures/{field}/{filtname}/{png_path.name}'
            tasks.append((
                UploadTask(local_path=png_path, r2_key=r2_key,
                           content_type='image/png'),
                basename, filtname, 'full',
            ))
    return tasks


def _read_full_png_dimensions(png_path):
    """Return (width, height) of a PNG without decoding pixel data."""
    # PNG IHDR: 8-byte signature, 4-byte length, 4-byte 'IHDR',
    # then width (4 BE) and height (4 BE).
    import struct
    with open(png_path, 'rb') as f:
        f.seek(16)
        width, height = struct.unpack('>II', f.read(8))
    return width, height


# ---------------------------------------------------------------------------
# Deploy (push)
# ---------------------------------------------------------------------------

def deploy_nircam(field, config, filters=None, dry_run=False):
    """Push NIRCam exposure state: upload PNGs to R2, upsert nircam_exposures.

    Parameters
    ----------
    field : str
        Field name.
    config : dict
        Deploy config (Supabase + R2 credentials).
    filters : list of str, optional
        Filters to deploy. If None, discovers every directory under
        ``products/nircam/{field}/``.
    dry_run : bool
        If True, print what would be done without making changes.
    """
    dirs = _resolve_nircam_dirs(field)

    if not dirs['products'].exists():
        print(f"Error: Products directory not found: {dirs['products']}")
        sys.exit(1)

    available = _discover_filters(dirs)
    if filters:
        missing = [f for f in filters if f not in available]
        if missing:
            print(f"Warning: No products for filters: {', '.join(missing)}")
        filters = [f for f in filters if f in available]
    else:
        filters = available

    if not filters:
        print("No filters found to deploy.")
        return

    print(f"Field: {field}")
    print(f"Filters: {', '.join(filters)}")

    exposures = discover_exposures(dirs, filters)
    print(f"Discovered {len(exposures)} canonical exposures")
    if not exposures:
        return

    png_tasks = build_upload_tasks(dirs, field, filters)
    thumb_r2_keys = {
        (filtname, basename): task.r2_key
        for task, basename, filtname, kind in png_tasks if kind == 'thumb'
    }
    full_r2_keys = {
        (filtname, basename): task.r2_key
        for task, basename, filtname, kind in png_tasks if kind == 'full'
    }
    # Image dimensions come from the full-res PNG (= native exposure shape),
    # so the web canvas can place mask polygons in DS9 image coords without
    # ever needing the FITS file.
    full_dims = {
        (filtname, basename): _read_full_png_dimensions(task.local_path)
        for task, basename, filtname, kind in png_tasks if kind == 'full'
    }
    print(f"Preview PNGs to upload: {len(png_tasks)} "
          f"({len(thumb_r2_keys)} thumb, {len(full_r2_keys)} full)")

    records = []
    for (filtname, basename), info in sorted(exposures.items()):
        masking = _detect_masking(dirs, basename, filtname)
        dims = full_dims.get((filtname, basename))
        record = {
            'field': field,
            'filter': filtname,
            'detector': info['detector'] or 'unknown',
            'filename': basename,
            'visit': info['visit'],
            'date_obs': info['date_obs'],
            'ra_center': info['ra_center'],
            'dec_center': info['dec_center'],
            'stage': info['stage'],
            'png_path': thumb_r2_keys.get((filtname, basename)),
            'full_png_path': full_r2_keys.get((filtname, basename)),
            'image_width': dims[0] if dims else None,
            'image_height': dims[1] if dims else None,
        }
        if masking:
            record['masking'] = masking
        records.append(record)

    if dry_run:
        stage_counts = Counter(r['stage'] for r in records)
        print("\nStage breakdown:")
        for _, stage in _CFP_TO_STAGE:
            if stage in stage_counts:
                print(f"  {stage}: {stage_counts[stage]}")
        mask_count = sum(1 for r in records if r.get('masking') == 'done')
        if mask_count:
            print(f"  with masks: {mask_count}")
        print("\nDry run — no changes made.")
        return

    if png_tasks:
        print("\nUploading PNGs to R2...")
        upload_task_list = [t[0] for t in png_tasks]
        success, failed, failures = upload_files_parallel(
            config, upload_task_list,
            desc='Uploading PNGs',
        )
        print(f"  Uploaded: {success}, Failed: {failed}")
        for msg in failures:
            print(f"  Error: {msg}")

    print("\nUpserting exposures to Supabase...")
    client = get_supabase_client(config)
    _upsert_exposures(client, records)
    print(f"  Upserted {len(records)} exposures")


def _upsert_exposures(client, records, batch_size=500):
    """Upsert exposure records, preserving web-triage fields for existing rows.

    New rows get default ``review_status='pending'``, ``masking='none'``,
    ``correction='none'``. Existing rows: update pipeline-derived columns
    only (``stage``, ``png_path``, metadata, ``masking='done'`` when a
    ``.reg`` file is present). Preserve ``review_status``, ``correction``,
    ``notes``.
    """
    if not records:
        return

    filenames = [r['filename'] for r in records]
    existing = set()
    for i in range(0, len(filenames), batch_size):
        batch = filenames[i:i + batch_size]
        resp = (client.table('nircam_exposures')
                .select('filename, field, filter')
                .in_('filename', batch)
                .execute())
        for row in resp.data:
            existing.add((row['field'], row['filter'], row['filename']))

    new_records = []
    update_records = []
    now = datetime.now(timezone.utc).isoformat()

    for r in records:
        key = (r['field'], r['filter'], r['filename'])
        if key in existing:
            update = {
                'field': r['field'],
                'filter': r['filter'],
                'filename': r['filename'],
                'stage': r['stage'],
                'detector': r['detector'],
                'updated_at': now,
            }
            if r.get('png_path'):
                update['png_path'] = r['png_path']
            if r.get('full_png_path'):
                update['full_png_path'] = r['full_png_path']
            if r.get('image_width') is not None:
                update['image_width'] = r['image_width']
            if r.get('image_height') is not None:
                update['image_height'] = r['image_height']
            if r.get('visit'):
                update['visit'] = r['visit']
            if r.get('date_obs'):
                update['date_obs'] = r['date_obs']
            if r.get('ra_center') is not None:
                update['ra_center'] = r['ra_center']
            if r.get('dec_center') is not None:
                update['dec_center'] = r['dec_center']
            if r.get('masking') == 'done':
                update['masking'] = 'done'
            update_records.append(update)
        else:
            new = {
                **r,
                'review_status': 'pending',
                'correction': 'none',
                'created_at': now,
                'updated_at': now,
            }
            if 'masking' not in new:
                new['masking'] = 'none'
            new_records.append(new)

    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        client.table('nircam_exposures').insert(batch).execute()

    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        client.table('nircam_exposures').upsert(
            batch,
            on_conflict='field,filter,filename',
        ).execute()
