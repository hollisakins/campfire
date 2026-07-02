"""
NIRCam exposure deployment — push canonical exposure state to OSN + Supabase.

Scans the per-filter flat directories (the canonical layout where each exposure
is a single ``{rootname}.fits`` mutated in place through the ``CFP_*`` provenance
keys), derives a ``stage`` value from the highest completed CFP key, and:

* uploads the **canonical exposure FITS** (and any field ``expmap`` coverage
  files) to **OSN** under ``campfire-layout`` canonical keys, content-hash-
  deduped so an unchanged exposure is not re-uploaded (epic #261, N1 / D1);
* records a **field-scoped deployment** (provenance + lifecycle anchor) and
  registers those objects in ``storage_objects`` (``nircam_exposure`` /
  ``nircam_expmap``) tagged with its ``deployment_id`` — visibility rides
  ``deployment.status`` via the storage gate: ``published`` is public to everyone
  (NIRCam fields span multiple programs, so there is no per-program scope),
  ``draft`` is admin-only for review;
* uploads ``*_preview.png`` / ``*_full.png`` thumbnails (unchanged from before —
  still on R2 legacy keys; their migration to canonical OSN keys rides N5 with
  the inspection-UI rework that retires ``*_full.png``);
* upserts ``nircam_exposures`` while preserving web-triage fields
  (``review_status``, ``correction``, ``notes``).

A normal deploy publishes immediately; ``--draft`` holds the field admin-only for
portal inspection, then ``campfire deploy publish --field`` makes it public.

Excluded exposures flagged by reviewers in the admin UI are surfaced for
copy-paste into the field's ``skip = [...]`` block in ``fields.toml``; we no
longer maintain a local ``exposures.json`` contract since nothing in the
pipeline consumes it.
"""

import hashlib
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from astropy.io import fits

from campfire_layout import KeyScheme, Scope, storage_key
from campfire.deploy.r2 import UploadTask, upload_files_parallel
from campfire.deploy.supabase import (
    claim_deploy_scope, get_deploy_scope_version, get_supabase_client,
    get_user_id_from_token, insert_deployment, log_deploy_event,
)

# The OSN data backend NIRCam canonical intermediates are homed on (epic #210/#216,
# same as NIRSpec canonical spectrum-exposures). Preview PNGs stay on the legacy R2
# 'data' keys until N5 migrates them alongside the inspection-UI rework.
_CANONICAL_BACKEND = 'osn'
_FITS_CONTENT_TYPE = 'application/fits'


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


def _sci_dq_hash(path):
    """Return ``'sha256:<hex>'`` over just the SCI+DQ arrays, or None.

    The science-only change-detection digest for the deploy dedup (epic #261, D1).
    Reproduces ``campfire_pipeline.nircam.manifest.compute_file_hash`` deploy-side
    (deploy must not import the pipeline): ``do_not_scale_image_data=True`` hashes
    the raw stored bytes regardless of BZERO/BSCALE, ``memmap=False`` forces a real
    read. Whole-file hashes churn on every pipeline re-save (header timestamps), so
    they can't drive dedup; the SCI+DQ digest is stable across a science-identical
    re-save. Returns None if neither array is present (never dedup on an empty hash).
    """
    h = hashlib.sha256()
    hashed_any = False
    try:
        with fits.open(path, memmap=False, do_not_scale_image_data=True) as hdul:
            for extname in ('SCI', 'DQ'):
                if extname not in hdul:
                    continue
                data = hdul[extname].data
                if data is not None:
                    h.update(data.tobytes())
                    hashed_any = True
    except Exception:
        return None
    return f'sha256:{h.hexdigest()}' if hashed_any else None


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
        ``filter``, ``path`` (the canonical FITS Path), ``stage``, ``visit``,
        ``detector``, ``date_obs``, ``ra_center``, ``dec_center``.
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
            info['path'] = path
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
            r2_key = storage_key('nircam_exposure_preview', Scope(field=field, filt=filtname), png_path.name)
            tasks.append((
                UploadTask(local_path=png_path, r2_key=r2_key,
                           content_type='image/png'),
                basename, filtname, 'thumb',
            ))
        for png_path in sorted(png_dir.glob('jw*_full.png')):
            basename = png_path.name.removesuffix('_full.png')
            r2_key = storage_key('nircam_exposure_full', Scope(field=field, filt=filtname), png_path.name)
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
# Canonical FITS + expmap discovery (epic #261, N1)
# ---------------------------------------------------------------------------

def build_fits_upload_tasks(field, exposures):
    """Build canonical-key OSN upload tasks for the exposure FITS.

    One ``nircam_exposure`` task per discovered exposure, keyed under the
    ``campfire-layout`` CANONICAL scheme (``data/products/nircam/<field>/<filter>/
    <rootname>.fits``). Returns a list of ``UploadTask``; content-hash dedup is
    applied by the caller (unchanged exposures are dropped before upload).
    """
    tasks = []
    for (filtname, basename), info in sorted(exposures.items()):
        path = info['path']
        key = storage_key('nircam_exposure', Scope(field=field, filt=filtname),
                          path.name, scheme=KeyScheme.CANONICAL)
        tasks.append(UploadTask(local_path=path, r2_key=key,
                                content_type=_FITS_CONTENT_TYPE))
    return tasks


def discover_expmap_tasks(dirs, field):
    """Build canonical-key OSN upload tasks for any field expmap coverage files.

    Expmaps are per-``(field, filter)`` coverage maps written under
    ``products/nircam/<field>/expmaps/``. They are produced at combine time, so a
    process-only field may have none yet — deploy is idempotent and picks them up
    on a later run. Returns a list of ``UploadTask`` (empty if the dir is absent).
    """
    expmap_dir = dirs['products'] / 'expmaps'
    if not expmap_dir.exists():
        return []
    tasks = []
    for path in sorted(expmap_dir.glob('*.fits')):
        key = storage_key('nircam_expmap', Scope(field=field), path.name,
                          scheme=KeyScheme.CANONICAL)
        tasks.append(UploadTask(local_path=path, r2_key=key,
                                content_type=_FITS_CONTENT_TYPE))
    return tasks


# ---------------------------------------------------------------------------
# Deploy (push)
# ---------------------------------------------------------------------------

def deploy_nircam(field, config, filters=None, dry_run=False, draft=False):
    """Push NIRCam exposure state to OSN + Supabase (epic #261, N1).

    Records a **field-scoped deployment** (provenance + the draft->published
    lifecycle), uploads the canonical exposure FITS (+ any field expmaps) to OSN
    under ``campfire-layout`` canonical keys (content-hash deduped on
    ``sha256(SCI+DQ)``), uploads preview PNGs to R2, upserts ``nircam_exposures``,
    and registers every landed object in ``storage_objects`` tagged with that
    ``deployment_id``.

    Visibility rides the deployment (not a bespoke admin-only special case): a
    ``published`` field deployment is public to everyone (NIRCam fields span
    multiple programs, so there is no per-program scope); ``--draft`` keeps it
    admin-only for portal inspection until ``campfire deploy publish --field``.

    Parameters
    ----------
    field : str
        Field name.
    config : dict
        Deploy config (Supabase + R2/OSN credentials).
    filters : list of str, optional
        Filters to deploy. If None, discovers every directory under
        ``products/nircam/{field}/``.
    dry_run : bool
        If True, print what would be done without making changes.
    draft : bool
        If True, record the deployment as ``draft`` (admin-only) for review;
        otherwise ``published`` (public) immediately.
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

    fits_tasks = build_fits_upload_tasks(field, exposures)
    expmap_tasks = discover_expmap_tasks(dirs, field)
    print(f"Canonical FITS: {len(fits_tasks)} exposure(s), {len(expmap_tasks)} expmap(s) → OSN")

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
        print(f"\nWould record a {'draft' if draft else 'published'} field "
              f"deployment and upload {len(fits_tasks)} canonical FITS (subject to "
              f"SCI+DQ content-hash dedup) + {len(expmap_tasks)} expmap(s) to OSN, "
              f"and {len(png_tasks)} preview PNG(s) to R2.")
        print("Dry run — no changes made.")
        return

    from campfire.deploy.registry import (
        build_registry_rows, fetch_active_sci_dq_hashes, resolve_backend_label,
        set_active_deployment, upsert_storage_objects,
    )

    client = get_supabase_client(config)
    user_id = get_user_id_from_token(config)

    # Read the field's optimistic-concurrency version BEFORE any work so a
    # concurrent same-field deploy is detected at finalize (epic #210, B4 / D12).
    scope_version = get_deploy_scope_version(client, 'field', field)

    # Record the field-scoped deployment up front — it is the provenance anchor and
    # the draft/published visibility gate every registered object hangs off.
    deployment_id = insert_deployment(
        client, field=field, deployed_by=user_id,
        status='draft' if draft else 'published', n_targets=len(records))
    print(f"Deployment #{deployment_id} recorded "
          f"({'draft — admin-only' if draft else 'published — public'})")

    # --- Content-hash dedup for the canonical FITS (D1) ---------------------
    # Compare each exposure's local sha256(SCI+DQ) to the digest already stored in
    # the registry; skip the upload when the science is unchanged (a pipeline
    # re-save with fresh header timestamps shifts the whole-file hash but not this).
    local_sci_dq = {t.r2_key: _sci_dq_hash(t.local_path) for t in fits_tasks}
    existing_sci_dq = fetch_active_sci_dq_hashes(client, [t.r2_key for t in fits_tasks])
    fits_to_upload = [
        t for t in fits_tasks
        if not (local_sci_dq.get(t.r2_key)
                and existing_sci_dq.get(t.r2_key) == local_sci_dq[t.r2_key])
    ]
    n_skipped = len(fits_tasks) - len(fits_to_upload)
    if n_skipped:
        print(f"\nSkipping {n_skipped} unchanged exposure(s) (SCI+DQ hash match)")

    # --- Upload canonical FITS + expmaps to OSN ----------------------------
    osn_tasks = fits_to_upload + expmap_tasks
    osn_uploaded: set[str] = set()
    if osn_tasks:
        print(f"\nUploading {len(osn_tasks)} canonical FITS/expmap(s) to OSN...")
        success, failed, failures = upload_files_parallel(
            config, osn_tasks, desc='OSN uploads',
            succeeded_out=osn_uploaded, backend=_CANONICAL_BACKEND)
        print(f"  Uploaded: {success}, Failed: {failed}")
        for msg in failures:
            print(f"  Error: {msg}")

    # --- Upload preview PNGs to R2 (legacy keys — migrated in N5) -----------
    png_upload_list = [t[0] for t in png_tasks]
    png_uploaded: set[str] = set()
    if png_upload_list:
        print("\nUploading preview PNGs to R2...")
        success, failed, failures = upload_files_parallel(
            config, png_upload_list, desc='Uploading PNGs',
            succeeded_out=png_uploaded)
        print(f"  Uploaded: {success}, Failed: {failed}")
        for msg in failures:
            print(f"  Error: {msg}")

    print("\nUpserting exposures to Supabase...")
    _upsert_exposures(client, records)
    print(f"  Upserted {len(records)} exposures")

    # --- Register storage objects, tagged with the field deployment ---------
    # Visibility rides deployment.status via the storage_objects gate: published ->
    # public to everyone, draft -> admin-only. The FITS carry a science-only
    # sci_dq_hash for change detection; content_hash stays the authoritative
    # whole-file digest for download/copy verification.
    n_reg = 0
    if osn_uploaded:
        reg_rows = build_registry_rows(
            osn_tasks, backend=_CANONICAL_BACKEND, deployment_id=deployment_id,
            uploaded_by=user_id, succeeded_keys=osn_uploaded, sci_dq_hashes=local_sci_dq)
        n_reg += upsert_storage_objects(client, reg_rows)
    if png_uploaded:
        reg_rows = build_registry_rows(
            png_upload_list, backend=resolve_backend_label(config),
            deployment_id=deployment_id, uploaded_by=user_id, succeeded_keys=png_uploaded)
        n_reg += upsert_storage_objects(client, reg_rows)
    if n_reg:
        print(f"  Registered {n_reg} storage object(s)")

    # Re-point dedup-skipped exposures (unchanged bytes, not re-registered) to this
    # deployment so a publish / re-deploy flips the whole field's visibility.
    skipped_keys = ([t.r2_key for t in fits_tasks if t.r2_key not in osn_uploaded]
                    if deployment_id else [])
    if skipped_keys:
        n_moved = set_active_deployment(client, skipped_keys, deployment_id)
        if n_moved:
            print(f"  Re-pointed {n_moved} unchanged exposure(s) to deployment #{deployment_id}")

    # --- Multi-reducer scope claim + audit (epic #210, B4 / D12) -----------
    claim = claim_deploy_scope(client, 'field', field, scope_version, actor=user_id)
    if claim.get('conflict'):
        print(f"⚠  CONCURRENT DEPLOY DETECTED for field '{field}': another reducer "
              f"advanced this field while this deploy was running. Re-run to reconcile.")
    log_deploy_event(
        client, action='upload', actor=user_id, deployment_id=deployment_id,
        observation=None, affected_count=len(records),
        metadata={'nircam': True, 'field': field, 'filters': filters, 'draft': draft,
                  'exposures': len(records), 'fits_uploaded': len(osn_uploaded),
                  'fits_skipped': n_skipped})


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
