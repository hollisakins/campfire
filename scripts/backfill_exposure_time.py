#!/usr/bin/env python3
"""
Backfill exposure_time for existing spectra records.

Reads EFFEXPTM from FITS headers in the local pipeline/products/ directory
and updates the spectra table in Supabase.

Usage:
    python scripts/backfill_exposure_time.py [--dry-run]
"""

import argparse
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from astropy.io import fits
from supabase import create_client


def load_config(scripts_dir: Path) -> dict:
    config_path = scripts_dir / 'config.toml'
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path, 'rb') as f:
        return tomllib.load(f)


def main():
    parser = argparse.ArgumentParser(description='Backfill exposure_time from local FITS files')
    parser.add_argument('--dry-run', action='store_true', help='Print what would be updated without writing')
    args = parser.parse_args()

    scripts_dir = Path(__file__).parent
    repo_dir = scripts_dir.parent
    products_dir = repo_dir / 'pipeline' / 'products'

    if not products_dir.exists():
        print(f"Error: Products directory not found: {products_dir}")
        sys.exit(1)

    config = load_config(scripts_dir)
    supabase = create_client(config['supabase']['url'], config['supabase']['service_role_key'])

    # Fetch all spectra with NULL exposure_time (paginate to avoid PostgREST row limit)
    print("Fetching spectra with missing exposure_time...")
    rows = []
    page_size = 1000
    offset = 0
    while True:
        result = (supabase.table('spectra')
                  .select('id,object_id,grating,fits_path')
                  .is_('exposure_time', 'null')
                  .range(offset, offset + page_size - 1)
                  .execute())
        rows.extend(result.data)
        if len(result.data) < page_size:
            break
        offset += page_size

    if not rows:
        print("All spectra already have exposure_time set.")
        return

    print(f"Found {len(rows)} spectra to backfill.")

    updates = []
    missing = []

    for row in rows:
        # fits_path is like "spectra/obs_name/filename.fits"
        # Local path is "pipeline/products/obs_name/filename.fits"
        fits_path = row['fits_path']
        parts = fits_path.split('/', 2)  # ['spectra', 'obs_name', 'filename.fits']
        if len(parts) != 3:
            print(f"  Warning: unexpected fits_path format: {fits_path}")
            missing.append(fits_path)
            continue

        local_path = products_dir / parts[1] / parts[2]

        if not local_path.exists():
            missing.append(fits_path)
            continue

        try:
            with fits.open(local_path) as hdul:
                exposure_time = float(hdul['PRIMARY'].header.get('EFFEXPTM', 0))
            updates.append({
                'id': row['id'],
                'object_id': row['object_id'],
                'grating': row['grating'],
                'fits_path': row['fits_path'],
                'exposure_time': exposure_time,
            })
        except Exception as e:
            print(f"  Error reading {local_path}: {e}")
            missing.append(fits_path)

    print(f"\nResults: {len(updates)} to update, {len(missing)} missing locally")

    if missing:
        print(f"\nMissing files ({len(missing)}):")
        for p in missing[:10]:
            print(f"  {p}")
        if len(missing) > 10:
            print(f"  ... and {len(missing) - 10} more")

    if args.dry_run:
        print("\n[DRY RUN] Would update:")
        for u in updates[:5]:
            print(f"  id={u['id']} -> {u['exposure_time']:.1f} s")
        if len(updates) > 5:
            print(f"  ... and {len(updates) - 5} more")
        return

    if not updates:
        return

    # Batch upsert (includes not-null columns to satisfy constraints)
    batch_size = 500
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        supabase.table('spectra').upsert(batch, on_conflict='id').execute()
        print(f"  Updated {min(i + batch_size, len(updates))}/{len(updates)}")

    print(f"\nDone! Updated {len(updates)} spectra with exposure_time.")


if __name__ == '__main__':
    main()
