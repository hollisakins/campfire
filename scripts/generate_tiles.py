#!/usr/bin/env python3
"""
CAMPFIRE Tile Generation Script

Generates PNG tile pyramids from NIRCam FITS mosaics for the map viewer.

Usage:
    # Generate tiles for a specific field and filter
    python scripts/generate_tiles.py --generate --field cosmos --filter f444w

    # Generate tiles for all filters in a field
    python scripts/generate_tiles.py --generate --field cosmos

    # Dry run (compute tile counts and storage estimates)
    python scripts/generate_tiles.py --generate --field cosmos --dry-run

    # Upload generated tiles to R2 public bucket
    python scripts/generate_tiles.py --upload --field cosmos

    # Register layers in Supabase map_layers table
    python scripts/generate_tiles.py --register --field cosmos

    # Full pipeline: generate + upload + register
    python scripts/generate_tiles.py --generate --upload --register --field cosmos
"""

import argparse
import json
import logging
import sys
from collections import defaultdict, namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable, **kwargs):
        return iterable

logger = logging.getLogger(__name__)

# ============================================
# Config Loading (follows deploy.py pattern)
# ============================================

def load_toml(path: Path) -> dict:
    with open(path, 'rb') as f:
        return tomllib.load(f)


def load_config(scripts_dir: Path) -> dict:
    """Load deployment config (R2 credentials, Supabase)."""
    config_path = scripts_dir / 'config.toml'
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        sys.exit(1)
    return load_toml(config_path)


# ============================================
# R2 Upload (follows deploy.py pattern)
# ============================================

UploadTask = namedtuple('UploadTask', ['local_path', 'r2_key', 'content_type'])


def get_r2_tiles_client(config: dict):
    """Create boto3 S3 client for the tiles R2 bucket."""
    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        print("Error: boto3 required for upload. Install with: pip install boto3")
        sys.exit(1)

    r2_config = config['r2_tiles']
    return boto3.client(
        's3',
        endpoint_url=f"https://{r2_config['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=r2_config['access_key_id'],
        aws_secret_access_key=r2_config['secret_access_key'],
        config=BotoConfig(
            signature_version='s3v4',
            max_pool_connections=50,
        ),
        region_name='auto',
    )


def upload_files_parallel(
    r2_client,
    bucket: str,
    tasks: list,
    max_workers: int = 12,
    desc: str = "Uploading",
) -> tuple[int, int, list[str]]:
    """Upload multiple files in parallel. Returns (success, failed, errors)."""
    success = 0
    failed = 0
    errors = []

    def upload_one(task):
        try:
            extra_args = {}
            if task.content_type:
                extra_args['ContentType'] = task.content_type
            extra_args['CacheControl'] = 'public, max-age=31536000, immutable'
            r2_client.upload_file(
                str(task.local_path),
                bucket,
                task.r2_key,
                ExtraArgs=extra_args,
            )
            return True, None
        except Exception as e:
            return False, f"{task.r2_key}: {e}"

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(upload_one, t): t for t in tasks}
        for future in tqdm(
            as_completed(futures),
            total=len(futures),
            desc=desc,
            unit="tile",
        ):
            ok, err = future.result()
            if ok:
                success += 1
            else:
                failed += 1
                errors.append(err)

    return success, failed, errors


def upload_tiles(
    config: dict,
    tile_dir: Path,
    field: str,
    filter_name: str | None = None,
    max_workers: int = 12,
    zoom_range: tuple[int, int] | None = None,
) -> None:
    """Upload generated tiles to R2 public bucket."""
    r2_config = config['r2_tiles']
    bucket = r2_config['bucket_name']
    r2_client = get_r2_tiles_client(config)

    # Discover tiles to upload
    if filter_name:
        search_dir = tile_dir / field / filter_name
        filters_to_upload = [(filter_name, search_dir)]
    else:
        field_dir = tile_dir / field
        if not field_dir.exists():
            print(f"Error: No tiles found at {field_dir}")
            sys.exit(1)
        filters_to_upload = [
            (d.name, d) for d in sorted(field_dir.iterdir()) if d.is_dir()
        ]

    for fname, fdir in filters_to_upload:
        png_files = sorted(fdir.rglob('*.png'))

        # Filter by zoom range if specified
        if zoom_range is not None:
            z_min, z_max = zoom_range
            filtered = []
            for p in png_files:
                # Path structure: .../filter/{z}/{x}/{y}.png
                try:
                    z = int(p.relative_to(fdir).parts[0])
                    if z_min <= z <= z_max:
                        filtered.append(p)
                except (ValueError, IndexError):
                    continue
            png_files = filtered
        if not png_files:
            print(f"  No tiles found for {field}/{fname}")
            continue

        tasks = []
        for png_path in png_files:
            # R2 key: {field}/{filter}/{z}/{x}/{y}.png
            rel_path = png_path.relative_to(tile_dir)
            r2_key = str(rel_path)
            tasks.append(UploadTask(png_path, r2_key, 'image/png'))

        print(f"\nUploading {len(tasks)} tiles for {field}/{fname}...")
        success, failed, errors = upload_files_parallel(
            r2_client, bucket, tasks, max_workers=max_workers,
            desc=f"{field}/{fname}",
        )

        if failed:
            print(f"  Warning: {failed} uploads failed:")
            for err in errors[:5]:
                print(f"    {err}")
        print(f"  Uploaded {success}/{len(tasks)} tiles to {bucket}/{field}/{fname}/")

        # Bump tile_version in Supabase to bust edge cache
        try:
            supabase = get_supabase_client(config)
            supabase.rpc('increment_tile_version', {
                'p_field': field, 'p_filter': fname,
            }).execute()
            print(f"  Bumped tile_version for {field}/{fname}")
        except Exception as e:
            print(f"  Warning: Failed to bump tile_version: {e}")


# ============================================
# Supabase Registration
# ============================================

def get_supabase_client(config: dict):
    """Create Supabase client using service role key."""
    try:
        from supabase import create_client
    except ImportError:
        print("Error: supabase-py required. Install with: pip install supabase")
        sys.exit(1)

    return create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key'],
    )


def register_layers(
    config: dict,
    tile_dir: Path,
    field: str,
    filter_name: str | None = None,
) -> None:
    """Register tile layers in Supabase map_layers table."""
    r2_config = config['r2_tiles']
    public_url_base = r2_config['public_url_base'].rstrip('/')
    supabase = get_supabase_client(config)

    # Find stats files (generated alongside tiles)
    if filter_name:
        stats_files = [tile_dir / field / filter_name / 'stats.json']
    else:
        field_dir = tile_dir / field
        stats_files = sorted(field_dir.glob('*/stats.json'))

    if not stats_files:
        print(f"Error: No stats.json files found. Generate tiles first.")
        sys.exit(1)

    for stats_path in stats_files:
        if not stats_path.exists():
            print(f"  Skipping {stats_path}: not found")
            continue

        with open(stats_path) as f:
            stats = json.load(f)

        fname = stats['filter_name']
        tile_base_url = f"{public_url_base}/{stats['field']}/{fname}"

        row = {
            'field': stats['field'],
            'filter': fname,
            'tile_base_url': tile_base_url,
            'min_zoom': stats['min_zoom'],
            'max_zoom': stats['max_zoom'],
            'tile_size': 256,
            'ra_min': stats['ra_min'],
            'ra_max': stats['ra_max'],
            'dec_min': stats['dec_min'],
            'dec_max': stats['dec_max'],
            'wcs_params': stats['wcs_params'],
            'image_width': stats['output_grid_size'][0],
            'image_height': stats['output_grid_size'][1],
            'total_tiles': stats['total_tiles'],
            'total_size_bytes': stats['total_size_bytes'],
        }

        # Upsert (update if exists, insert if not)
        result = supabase.table('map_layers').upsert(
            row, on_conflict='field,filter'
        ).execute()

        print(f"  Registered {stats['field']}/{fname}: "
              f"zoom {stats['min_zoom']}-{stats['max_zoom']}, "
              f"{stats['total_tiles']} tiles")


# ============================================
# Main
# ============================================

def main():
    parser = argparse.ArgumentParser(
        description='Generate and deploy map tiles for CAMPFIRE',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Dry run for one filter
  python scripts/generate_tiles.py --generate --field cosmos --filter f444w --dry-run

  # Generate tiles for all filters in a field
  python scripts/generate_tiles.py --generate --field cosmos

  # Upload + register after generating
  python scripts/generate_tiles.py --upload --register --field cosmos

  # Full pipeline
  python scripts/generate_tiles.py --generate --upload --register --field cosmos
        """,
    )

    # Action flags
    parser.add_argument('--generate', action='store_true',
                        help='Generate tile pyramids from FITS mosaics')
    parser.add_argument('--upload', action='store_true',
                        help='Upload tiles to R2 public bucket')
    parser.add_argument('--register', action='store_true',
                        help='Register layers in Supabase map_layers table')

    # Filtering
    parser.add_argument('--field', type=str, default=None,
                        help='Process only this field (e.g., cosmos)')
    parser.add_argument('--filter', type=str, default=None,
                        help='Process only this filter (e.g., f444w)')
    parser.add_argument('--zoom', type=str, default=None,
                        help='Zoom range for upload (e.g., "0-7", "5", "3-8")')

    # Options
    parser.add_argument('--dry-run', action='store_true',
                        help='Show estimates without generating')
    parser.add_argument('--pixel-scale', type=float, default=None,
                        help='Override output pixel scale (arcsec)')
    parser.add_argument('--workers', type=int, default=4,
                        help='Parallel workers for tile generation and upload (default: 4)')
    parser.add_argument('--overwrite', action='store_true',
                        help='Regenerate existing tiles (default: skip)')
    parser.add_argument('--imaging-config', type=str, default=None,
                        help='Path to imaging.toml (default: pipeline/imaging.toml)')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable debug logging')

    args = parser.parse_args()

    if not any([args.generate, args.upload, args.register]):
        parser.error("At least one action required: --generate, --upload, --register")

    # Setup logging
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format='[%(asctime)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # Load imaging config
    imaging_config_path = Path(args.imaging_config) if args.imaging_config else (
        project_root / 'pipeline' / 'imaging.toml'
    )

    # Import directly to avoid pipeline/__init__.py pulling in jwst
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "tiles", project_root / "pipeline" / "tiles.py"
    )
    tiles_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tiles_module)
    load_imaging_config = tiles_module.load_imaging_config
    get_tile_configs = tiles_module.get_tile_configs
    generate_tiles_for_filter = tiles_module.generate_tiles_for_filter
    estimate_tiles_for_filter = tiles_module.estimate_tiles_for_filter
    compute_field_grid = tiles_module.compute_field_grid
    save_field_grid = tiles_module.save_field_grid

    imaging_config = load_imaging_config(imaging_config_path)

    # Override pixel scale if specified
    if args.pixel_scale is not None:
        imaging_config.setdefault('defaults', {})['output_pixel_scale_arcsec'] = args.pixel_scale

    # Resolve output directory
    defaults = imaging_config.get('defaults', {})
    output_dir = project_root / defaults.get('output_dir', 'pipeline/tiles')

    # Get tile configs for requested field/filter
    fields = [args.field] if args.field else None
    filters = [args.filter] if args.filter else None
    tile_configs = get_tile_configs(imaging_config, fields=fields, filters=filters)

    if not tile_configs:
        print("Error: No matching field/filter configurations found.")
        if args.field:
            print(f"  Field '{args.field}' not found in {imaging_config_path}")
        sys.exit(1)

    print(f"Found {len(tile_configs)} field/filter combination(s) to process")

    # Generate tiles (two-pass: compute unified field grid, then generate per-filter)
    if args.generate:
        # Group requested configs by field
        configs_by_field = defaultdict(list)
        for config in tile_configs:
            configs_by_field[config.field].append(config)

        # Compute unified field grids (always from ALL filters, not just requested)
        field_grids = {}
        for field in configs_by_field:
            all_field_configs = get_tile_configs(imaging_config, fields=[field])
            all_filter_files = {c.filter_name: c.input_files for c in all_field_configs}
            pixel_scale = configs_by_field[field][0].output_pixel_scale_arcsec
            field_grid = compute_field_grid(all_filter_files, pixel_scale)
            save_field_grid(output_dir, field, field_grid)
            field_grids[field] = field_grid
            print(f"Unified grid for {field}: "
                  f"{field_grid.naxis1} x {field_grid.naxis2} px "
                  f"(from {len(all_filter_files)} filter(s))")

        if args.dry_run:
            print("\n--- DRY RUN ---\n")
            for config in tile_configs:
                est = estimate_tiles_for_filter(
                    config, output_grid=field_grids[config.field],
                )
                print(f"  {est['field']}/{est['filter']}:")
                print(f"    Input files:     {est['input_files']}")
                print(f"    Output size:     {est['output_width']} x {est['output_height']} px")
                print(f"    Pixel scale:     {est['pixel_scale_arcsec']:.3f}\"/px")
                print(f"    Zoom range:      {est['min_zoom']} - {est['max_zoom']}")
                print(f"    Estimated tiles: ~{est['estimated_tiles']:,}")
                print(f"    Estimated size:  ~{est['estimated_size_mb']:.0f} MB "
                      f"({est['estimated_size_gb']:.2f} GB)")
                print()
            return

        for config in tile_configs:
            stats = generate_tiles_for_filter(
                config, n_workers=args.workers, overwrite=args.overwrite,
                output_grid=field_grids[config.field],
            )

            # Save stats for upload/register steps
            stats_path = output_dir / config.field / config.filter_name / 'stats.json'
            stats_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stats_path, 'w') as f:
                json.dump({
                    'field': stats.field,
                    'filter_name': stats.filter_name,
                    'num_input_files': stats.num_input_files,
                    'output_grid_size': list(stats.output_grid_size),
                    'min_zoom': stats.min_zoom,
                    'max_zoom': stats.max_zoom,
                    'total_tiles': stats.total_tiles,
                    'total_size_bytes': stats.total_size_bytes,
                    'ra_min': stats.ra_min,
                    'ra_max': stats.ra_max,
                    'dec_min': stats.dec_min,
                    'dec_max': stats.dec_max,
                    'wcs_params': stats.wcs_params,
                }, f, indent=2)

            print(f"\nGenerated {stats.total_tiles} tiles "
                  f"({stats.total_size_bytes / (1024 * 1024):.1f} MB) "
                  f"for {stats.field}/{stats.filter_name}")
            print(f"  Stats saved to {stats_path}")

        # Free memory-mapped FITS data and numpy arrays before upload/register
        # steps to avoid segfaults in boto3's SSL threading
        import gc
        del field_grids
        gc.collect()

    # Upload to R2
    if args.upload:
        scripts_dir = project_root / 'scripts'
        config = load_config(scripts_dir)

        if 'r2_tiles' not in config:
            print("Error: [r2_tiles] section not found in scripts/config.toml")
            print("Add R2 tiles bucket credentials to scripts/config.toml")
            sys.exit(1)

        # Parse zoom range
        zoom_range = None
        if args.zoom:
            if '-' in args.zoom:
                parts = args.zoom.split('-')
                zoom_range = (int(parts[0]), int(parts[1]))
            else:
                z = int(args.zoom)
                zoom_range = (z, z)

        field = args.field
        if not field:
            for d in sorted(output_dir.iterdir()):
                if d.is_dir():
                    print(f"\n--- Uploading field: {d.name} ---")
                    upload_tiles(config, output_dir, d.name,
                                filter_name=args.filter,
                                max_workers=args.workers,
                                zoom_range=zoom_range)
        else:
            upload_tiles(config, output_dir, field,
                        filter_name=args.filter,
                        max_workers=args.workers,
                        zoom_range=zoom_range)

    # Register in Supabase
    if args.register:
        scripts_dir = project_root / 'scripts'
        config = load_config(scripts_dir)
        field = args.field
        if not field:
            for d in sorted(output_dir.iterdir()):
                if d.is_dir():
                    register_layers(config, output_dir, d.name,
                                   filter_name=args.filter)
        else:
            register_layers(config, output_dir, field,
                           filter_name=args.filter)

    print("\nDone!")


if __name__ == '__main__':
    main()
