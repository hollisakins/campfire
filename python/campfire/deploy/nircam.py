"""
NIRCam exposure deployment — push intermediate products to Supabase + R2,
pull triage results to local contract file.

Push workflow:
    1. Scan local products directory for each filter
    2. Detect per-exposure stage from file existence (uncal → rate → cal → jhat → crf)
    3. Upload cal PNGs to R2
    4. Upsert nircam_exposures table (preserve review_status/correction/notes set by web triage)

Pull workflow:
    1. Fetch nircam_exposures for field from Supabase
    2. Write contract file to $CAMPFIRE_ROOT/reference/nircam/{field}/exposures.json
    3. Print summary + rsync worklist
"""

import json
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
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_campfire_root():
    """Return $CAMPFIRE_ROOT or exit with helpful error."""
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
        'stage1': products / 'stage1',
        'stage2': products / 'stage2',
        'stage3': products / 'stage3',
        'mosaics': products / 'mosaics',
        'reference': root / 'reference' / 'nircam' / field,
        'masks': root / 'reference' / 'nircam' / field / 'masks',
    }


def _discover_filters(dirs):
    """List available filters by scanning stage2 subdirectories."""
    stage2 = dirs['stage2']
    if not stage2.exists():
        return []
    return sorted(
        d.name for d in stage2.iterdir()
        if d.is_dir() and not d.name.startswith('.')
    )


# ---------------------------------------------------------------------------
# Exposure discovery
# ---------------------------------------------------------------------------

def _exposure_basename(filename):
    """Extract the exposure basename (without suffix) from a FITS filename.

    E.g. 'jw01727028001_04101_00003_nrcalong_cal.fits' → 'jw01727028001_04101_00003_nrcalong'
    """
    name = filename
    for suffix in ('_crf.fits', '_jhat.fits', '_cal.fits', '_rate.fits', '_uncal.fits'):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    # Fallback: strip last _xxx.fits
    stem = Path(name).stem
    parts = stem.rsplit('_', 1)
    return parts[0] if len(parts) > 1 else stem


def discover_exposures(dirs, filters):
    """Discover all exposures for given filters, detecting the highest stage.

    Returns a dict keyed by (filter, basename) → exposure info dict.
    """
    exposures = {}

    for filtname in filters:
        # Scan each stage directory for this filter
        stage_dirs = [
            (dirs['stage3'], '_crf.fits', 'crf'),
            (dirs['stage3'], '_jhat.fits', 'jhat'),
            (dirs['stage2'], '_cal.fits', 'cal'),
            (dirs['stage1'], '_rate.fits', 'rate'),
        ]

        for stage_dir, suffix, stage_name in stage_dirs:
            pattern = str(stage_dir / filtname / f'*{suffix}')
            for filepath in glob(pattern):
                filename = os.path.basename(filepath)
                basename = _exposure_basename(filename)
                key = (filtname, basename)

                if key not in exposures:
                    exposures[key] = {
                        'basename': basename,
                        'filter': filtname,
                        'stage': stage_name,
                        'best_file': filepath,
                    }
                # Already found at a higher stage; skip

    return exposures


def extract_exposure_metadata(filepath):
    """Read FITS headers to extract exposure metadata.

    Parameters
    ----------
    filepath : str
        Path to a FITS file (cal preferred, any stage works).

    Returns
    -------
    dict
        Keys: visit, detector, date_obs, ra_center, dec_center.
    """
    meta = {
        'visit': None,
        'detector': None,
        'date_obs': None,
        'ra_center': None,
        'dec_center': None,
    }

    basename = os.path.basename(filepath)
    # JWST naming convention: {visit}_{activity}_{exposure}_{detector}_{suffix}.fits
    parts = basename.split('_')
    if len(parts) >= 1:
        meta['visit'] = parts[0]
    if len(parts) >= 4:
        meta['detector'] = parts[3]

    try:
        with fits.open(filepath, memmap=True) as hdul:
            hdr = hdul[0].header
            meta['date_obs'] = hdr.get('DATE-OBS')
            meta['detector'] = hdr.get('DETECTOR', meta['detector'])

            # Try SCI extension for WCS center
            if len(hdul) > 1:
                sci_hdr = hdul[1].header
                ra = sci_hdr.get('CRVAL1')
                dec = sci_hdr.get('CRVAL2')
                if ra is not None and dec is not None:
                    meta['ra_center'] = float(ra)
                    meta['dec_center'] = float(dec)
    except Exception:
        pass

    return meta


def _detect_masking(dirs, basename, filtname):
    """Check if a .reg mask file exists for this exposure."""
    reg_path = dirs['masks'] / filtname / f'{basename}_cal.reg'
    return 'done' if reg_path.exists() else None


# ---------------------------------------------------------------------------
# PNG discovery
# ---------------------------------------------------------------------------

def build_upload_tasks(dirs, field, filters):
    """Build list of UploadTask for cal PNGs.

    Returns list of (UploadTask, basename, filtname) tuples.
    """
    tasks = []
    for filtname in filters:
        png_dir = dirs['stage2'] / filtname
        if not png_dir.exists():
            continue
        for png_path in sorted(png_dir.glob('*_cal.png')):
            basename = _exposure_basename(png_path.name.replace('_cal.png', '_cal.fits'))
            r2_key = f'nircam/exposures/{field}/{filtname}/{png_path.name}'
            tasks.append((
                UploadTask(local_path=png_path, r2_key=r2_key, content_type='image/png'),
                basename,
                filtname,
            ))
    return tasks


# ---------------------------------------------------------------------------
# Deploy (push)
# ---------------------------------------------------------------------------

def deploy_nircam(field, config, filters=None, dry_run=False):
    """Deploy NIRCam exposure data: upload PNGs to R2, upsert nircam_exposures.

    Parameters
    ----------
    field : str
        Field name.
    config : dict
        Deploy config (Supabase + R2 credentials).
    filters : list of str, optional
        Filters to deploy. If None, discovers all available.
    dry_run : bool
        If True, print what would be done without making changes.
    """
    dirs = _resolve_nircam_dirs(field)

    if not dirs['products'].exists():
        print(f"Error: Products directory not found: {dirs['products']}")
        sys.exit(1)

    # Discover filters
    if filters:
        available = _discover_filters(dirs)
        missing = [f for f in filters if f not in available]
        if missing:
            print(f"Warning: No stage2 products for filters: {', '.join(missing)}")
        filters = [f for f in filters if f in available]
    else:
        filters = _discover_filters(dirs)

    if not filters:
        print("No filters found to deploy.")
        return

    print(f"Field: {field}")
    print(f"Filters: {', '.join(filters)}")

    # Discover exposures
    exposures = discover_exposures(dirs, filters)
    print(f"Discovered {len(exposures)} exposures")

    if not exposures:
        return

    # Build PNG upload tasks
    png_tasks = build_upload_tasks(dirs, field, filters)
    png_r2_keys = {}  # basename → r2_key
    for task, basename, filtname in png_tasks:
        png_r2_keys[(filtname, basename)] = task.r2_key

    print(f"PNGs to upload: {len(png_tasks)}")

    # Build exposure records for upsert
    records = []
    for (filtname, basename), info in sorted(exposures.items()):
        meta = extract_exposure_metadata(info['best_file'])
        masking = _detect_masking(dirs, basename, filtname)

        record = {
            'field': field,
            'filter': filtname,
            'detector': meta['detector'] or 'unknown',
            'filename': basename,
            'visit': meta['visit'],
            'date_obs': meta['date_obs'],
            'ra_center': meta['ra_center'],
            'dec_center': meta['dec_center'],
            'stage': info['stage'],
            'png_path': png_r2_keys.get((filtname, basename)),
        }
        if masking:
            record['masking'] = masking

        records.append(record)

    if dry_run:
        stage_counts = Counter(r['stage'] for r in records)
        print("\nStage breakdown:")
        for stage in ('uncal', 'rate', 'cal', 'jhat', 'crf'):
            if stage in stage_counts:
                print(f"  {stage}: {stage_counts[stage]}")
        mask_count = sum(1 for r in records if r.get('masking') == 'done')
        if mask_count:
            print(f"  with masks: {mask_count}")
        print("\nDry run — no changes made.")
        return

    # Upload PNGs to R2
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

    # Upsert to Supabase
    print("\nUpserting exposures to Supabase...")
    client = get_supabase_client(config)
    _upsert_exposures(client, records)
    print(f"  Upserted {len(records)} exposures")


def _upsert_exposures(client, records, batch_size=500):
    """Upsert exposure records, preserving web-triage fields for existing rows.

    New rows get default review_status='pending', masking='none', correction='none'.
    Existing rows: update stage, png_path, date_obs, coordinates, masking (if .reg detected).
    Preserve: review_status, correction, notes.
    """
    if not records:
        return

    # Check which records already exist
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
            # Update pipeline-derived fields only
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
            # New record with defaults
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

    # Insert new records
    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        client.table('nircam_exposures').insert(batch).execute()

    # Upsert existing records (on_conflict updates only the fields we set)
    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        client.table('nircam_exposures').upsert(
            batch,
            on_conflict='field,filter,filename',
        ).execute()


# ---------------------------------------------------------------------------
# Pull (contract file)
# ---------------------------------------------------------------------------

def pull_nircam(field, config, filters=None):
    """Fetch exposure triage data and write local contract file.

    Contract file: $CAMPFIRE_ROOT/reference/nircam/{field}/exposures.json

    Parameters
    ----------
    field : str
        Field name.
    config : dict
        Deploy config (Supabase credentials).
    filters : list of str, optional
        Filters to pull. If None, pulls all.
    """
    dirs = _resolve_nircam_dirs(field)

    client = get_supabase_client(config)

    # Fetch all exposures for this field
    query = (client.table('nircam_exposures')
             .select('filename, filter, review_status, masking, correction, notes')
             .eq('field', field))

    if filters:
        query = query.in_('filter', filters)

    resp = query.execute()

    if not resp.data:
        print(f"No exposures found for field '{field}' in database.")
        return

    # Build contract
    exposures = {}
    for row in resp.data:
        exposures[row['filename']] = {
            'filter': row['filter'],
            'review_status': row['review_status'],
            'masking': row['masking'],
            'correction': row['correction'],
            'notes': row['notes'],
        }

    contract = {
        'field': field,
        'pulled_at': datetime.now(timezone.utc).isoformat(),
        'exposures': exposures,
    }

    # Write contract file
    contract_dir = dirs['reference']
    os.makedirs(contract_dir, exist_ok=True)
    contract_path = contract_dir / 'exposures.json'

    with open(contract_path, 'w') as f:
        json.dump(contract, f, indent=2)

    print(f"Wrote contract: {contract_path}")

    # Print summary
    review_counts = Counter(e['review_status'] for e in exposures.values())
    masking_counts = Counter(e['masking'] for e in exposures.values())
    correction_counts = Counter(e['correction'] for e in exposures.values())

    print(f"\n{len(exposures)} exposures:")
    print(f"  Review: {review_counts.get('approved', 0)} approved, "
          f"{review_counts.get('pending', 0)} pending, "
          f"{review_counts.get('excluded', 0)} excluded")
    if masking_counts.get('needed', 0):
        print(f"  Masking: {masking_counts['needed']} needed, "
              f"{masking_counts.get('done', 0)} done")
    if correction_counts.get('needed', 0):
        print(f"  Correction: {correction_counts['needed']} needed, "
              f"{correction_counts.get('done', 0)} done")

    # Print rsync worklist
    needs_work = [
        name for name, info in exposures.items()
        if info['masking'] == 'needed' or info['correction'] == 'needed'
    ]
    if needs_work:
        print(f"\nFiles needing masking/correction ({len(needs_work)}):")
        for name in sorted(needs_work):
            info = exposures[name]
            reasons = []
            if info['masking'] == 'needed':
                reasons.append('masking')
            if info['correction'] == 'needed':
                reasons.append('correction')
            note = f" — {info['notes']}" if info.get('notes') else ''
            print(f"  {name}_cal.fits ({', '.join(reasons)}){note}")

    excluded = [name for name, info in exposures.items()
                if info['review_status'] == 'excluded']
    if excluded:
        print(f"\nExcluded ({len(excluded)}):")
        for name in sorted(excluded):
            note = f" — {exposures[name]['notes']}" if exposures[name].get('notes') else ''
            print(f"  {name}{note}")
