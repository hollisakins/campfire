#!/usr/bin/env python3
"""
CAMPFIRE NIRCam Deployment Script

Discovers NIRCam mosaic images and upserts metadata to Supabase.

Usage:
    python scripts/deploy_nircam.py --dry-run
    python scripts/deploy_nircam.py
    python scripts/deploy_nircam.py --data-dir /custom/path/to/data/nircam

Environment Variables:
    SUPABASE_URL          - Supabase project URL
    SUPABASE_SERVICE_KEY  - Supabase service role key (not anon key)

File Naming Convention:
    mosaic_nircam_{filter}_{field}_{pixel_scale}_{version}_{tile}_{extension}.fits.gz

    Example: mosaic_nircam_f090w_cosmos_30mas_v0p7_A1_sci.fits.gz
    - filter: f090w
    - field: cosmos
    - pixel_scale: 30mas
    - version: v0p7
    - tile: A1
    - extension: sci
"""

import argparse
import os
import re
import sys
from pathlib import Path


def get_supabase_client():
    """Create Supabase client from environment variables."""
    try:
        from supabase import create_client
    except ImportError:
        print("Error: supabase-py not installed. Install with: pip install supabase")
        sys.exit(1)

    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_KEY')

    if not url or not key:
        print("Error: Missing environment variables.")
        print("Please set SUPABASE_URL and SUPABASE_SERVICE_KEY")
        sys.exit(1)

    return create_client(url, key)


def parse_nircam_filename(filename: str) -> dict | None:
    """
    Parse a NIRCam mosaic filename to extract metadata.

    Expected pattern: mosaic_nircam_{filter}_{field}_{pixel_scale}_{version}_{tile}_{extension}.fits.gz
    Example: mosaic_nircam_f090w_cosmos_30mas_v0p7_A1_sci.fits.gz

    Returns dict with parsed values or None if filename doesn't match pattern.
    """
    # Remove .fits.gz suffix
    base = filename
    if base.endswith('.fits.gz'):
        base = base[:-8]
    elif base.endswith('.fits'):
        base = base[:-5]
    else:
        return None

    # Expected pattern: mosaic_nircam_{filter}_{field}_{pixel_scale}_{version}_{tile}_{extension}
    # Split and validate
    parts = base.split('_')

    # Should have at least 8 parts: mosaic, nircam, filter, field, pixel_scale, version, tile, extension
    if len(parts) < 8:
        return None

    if parts[0] != 'mosaic' or parts[1] != 'nircam':
        return None

    # Parse from known positions
    # mosaic_nircam_{filter}_{field}_{pixel_scale}_{version}_{tile}_{extension}
    # 0      1       2        3       4            5         6      7

    try:
        return {
            'filter': parts[2],
            'field': parts[3],
            'pixel_scale': parts[4],
            'version': parts[5],
            'tile': parts[6],
            'extension': parts[7],
        }
    except IndexError:
        return None


def discover_nircam_files(data_dir: Path) -> list[dict]:
    """
    Discover all NIRCam FITS files and parse their metadata.

    Returns list of dicts with file info and parsed metadata.
    """
    files = []

    # Look for .fits.gz files in subdirectories (one per field)
    for fits_path in sorted(data_dir.glob('**/*.fits.gz')):
        parsed = parse_nircam_filename(fits_path.name)

        if parsed is None:
            print(f"  Warning: Could not parse filename: {fits_path.name}")
            continue

        # Get file size
        file_size = fits_path.stat().st_size

        # Construct relative path for CDN (field/filename)
        # The file_path stored should be relative to the CDN base URL
        relative_path = f"{parsed['field']}/{fits_path.name}"

        files.append({
            'local_path': fits_path,
            'file_path': relative_path,
            'file_size': file_size,
            **parsed,
        })

    return files


def format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def upsert_nircam_images(supabase, images: list[dict], dry_run: bool = False) -> None:
    """
    Upsert NIRCam image records to Supabase.

    Uses upsert with on_conflict to handle both inserts and updates.
    """
    if not images:
        print("No images to upsert.")
        return

    # Prepare records for upsert
    records = []
    for img in images:
        records.append({
            'field': img['field'],
            'tile': img['tile'],
            'filter': img['filter'],
            'pixel_scale': img['pixel_scale'],
            'version': img['version'],
            'extension': img['extension'],
            'file_path': img['file_path'],
            'file_size': img['file_size'],
        })

    if dry_run:
        print(f"\nWould upsert {len(records)} records to nircam_images table")
        return

    # Upsert in batches to avoid request size limits
    batch_size = 100
    total_upserted = 0

    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]

        # Use upsert with composite unique constraint
        # Assuming the table has a unique constraint on (field, tile, filter, pixel_scale, version, extension)
        response = supabase.table('nircam_images').upsert(
            batch,
            on_conflict='field,tile,filter,pixel_scale,version,extension'
        ).execute()

        total_upserted += len(batch)
        print(f"  Upserted {total_upserted}/{len(records)} records...")

    print(f"\n✓ Successfully upserted {total_upserted} records")


def main():
    parser = argparse.ArgumentParser(
        description='Deploy NIRCam image metadata to Supabase',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
    SUPABASE_URL          - Supabase project URL
    SUPABASE_SERVICE_KEY  - Supabase service role key

Examples:
    # Dry run (show what would be uploaded)
    python scripts/deploy_nircam.py --dry-run

    # Full deployment
    python scripts/deploy_nircam.py

    # Custom data directory
    python scripts/deploy_nircam.py --data-dir /path/to/data/nircam
        """
    )

    parser.add_argument(
        '--data-dir',
        type=Path,
        default=Path('data/nircam'),
        help='Path to NIRCam data directory (default: data/nircam)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be deployed without making changes'
    )

    args = parser.parse_args()

    # Resolve data directory
    data_dir = args.data_dir
    if not data_dir.is_absolute():
        # If relative, try relative to script location first, then current directory
        script_dir = Path(__file__).parent.parent
        if (script_dir / data_dir).exists():
            data_dir = script_dir / data_dir
        elif not data_dir.exists():
            print(f"Error: Data directory not found: {data_dir}")
            print(f"  Also tried: {script_dir / args.data_dir}")
            sys.exit(1)

    if not data_dir.exists():
        print(f"Error: Data directory not found: {data_dir}")
        sys.exit(1)

    print(f"NIRCam Deployment Script")
    print(f"========================")
    print(f"Data directory: {data_dir}")
    print()

    # Discover files
    print("Discovering NIRCam files...")
    images = discover_nircam_files(data_dir)

    if not images:
        print("No NIRCam files found!")
        sys.exit(1)

    # Summary statistics
    fields = sorted(set(img['field'] for img in images))
    filters = sorted(set(img['filter'] for img in images))
    tiles = sorted(set(img['tile'] for img in images))
    versions = sorted(set(img['version'] for img in images))
    extensions = sorted(set(img['extension'] for img in images))
    total_size = sum(img['file_size'] for img in images)

    print(f"\nFound {len(images)} files:")
    print(f"  Fields: {', '.join(fields)}")
    print(f"  Filters: {', '.join(filters[:5])}{'...' if len(filters) > 5 else ''} ({len(filters)} total)")
    print(f"  Tiles: {', '.join(tiles[:5])}{'...' if len(tiles) > 5 else ''} ({len(tiles)} total)")
    print(f"  Versions: {', '.join(versions)}")
    print(f"  Extensions: {', '.join(extensions)}")
    print(f"  Total size: {format_file_size(total_size)}")

    if args.dry_run:
        print("\n=== DRY RUN MODE ===")
        print("\nSample records:")
        for img in images[:5]:
            print(f"  - {img['file_path']} ({format_file_size(img['file_size'])})")
        if len(images) > 5:
            print(f"  ... and {len(images) - 5} more")

        upsert_nircam_images(None, images, dry_run=True)
        return

    # Connect to Supabase and upsert
    print("\nConnecting to Supabase...")
    supabase = get_supabase_client()

    print("Upserting records...")
    upsert_nircam_images(supabase, images, dry_run=False)


if __name__ == '__main__':
    main()
