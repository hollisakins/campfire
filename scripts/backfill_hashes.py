#!/usr/bin/env python3
"""
One-time backfill script: compute file_hash and file_size for existing spectra rows.

Streams each FITS file from R2, computes SHA-256, and updates the spectra table.

Usage:
    python scripts/backfill_hashes.py
    python scripts/backfill_hashes.py --workers 4
    python scripts/backfill_hashes.py --dry-run
"""

import argparse
import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

try:
    from supabase import create_client
except ImportError:
    print("Error: supabase-py not installed. Install with: pip install supabase")
    sys.exit(1)

try:
    import boto3
    from botocore.config import Config
except ImportError:
    print("Error: boto3 not installed. Install with: pip install boto3")
    sys.exit(1)

from tqdm import tqdm


def load_config(scripts_dir: Path) -> dict:
    config_path = scripts_dir / 'config.toml'
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path, 'rb') as f:
        return tomllib.load(f)


def get_r2_client(config: dict):
    r2_config = config['r2']
    return boto3.client(
        's3',
        endpoint_url=f"https://{r2_config['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=r2_config['access_key_id'],
        aws_secret_access_key=r2_config['secret_access_key'],
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def backfill_single(r2_client, bucket: str, spectrum: dict) -> dict:
    """Stream a file from R2, compute hash, return result."""
    fits_path = spectrum['fits_path']
    response = r2_client.get_object(Bucket=bucket, Key=fits_path)
    file_size = response['ContentLength']

    hasher = hashlib.sha256()
    body = response['Body']
    for chunk in iter(lambda: body.read(65536), b''):
        hasher.update(chunk)
    body.close()

    file_hash = f"sha256:{hasher.hexdigest()}"

    return {
        'id': spectrum['id'],
        'file_hash': file_hash,
        'file_size': file_size,
    }


def main():
    parser = argparse.ArgumentParser(description='Backfill file_hash and file_size for spectra')
    parser.add_argument('--workers', type=int, default=6, help='Parallel workers (default: 6)')
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done without updating')
    args = parser.parse_args()

    scripts_dir = Path(__file__).parent
    config = load_config(scripts_dir)

    supabase = create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key'],
    )

    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    # Fetch all spectra missing file_hash
    print("Fetching spectra with missing file_hash...")
    all_spectra = []
    offset = 0
    batch_size = 1000
    while True:
        result = supabase.table('spectra').select('id,fits_path').is_('file_hash', 'null').range(offset, offset + batch_size - 1).execute()
        all_spectra.extend(result.data)
        if len(result.data) < batch_size:
            break
        offset += batch_size

    if not all_spectra:
        print("All spectra already have file_hash. Nothing to do.")
        return

    print(f"Found {len(all_spectra)} spectra to backfill.")

    if args.dry_run:
        print("Dry run - would process the above spectra. Exiting.")
        return

    succeeded = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_to_spec = {
            executor.submit(backfill_single, r2_client, bucket, spec): spec
            for spec in all_spectra
        }

        with tqdm(total=len(all_spectra), desc="Backfilling", unit="file") as pbar:
            for future in as_completed(future_to_spec):
                spec = future_to_spec[future]
                try:
                    result = future.result()
                    supabase.table('spectra').update({
                        'file_hash': result['file_hash'],
                        'file_size': result['file_size'],
                    }).eq('id', result['id']).execute()
                    succeeded += 1
                except Exception as e:
                    failed += 1
                    tqdm.write(f"  Failed: {spec['fits_path']}: {e}")
                pbar.update(1)

    print(f"\nBackfill complete: {succeeded} succeeded, {failed} failed")


if __name__ == '__main__':
    main()
