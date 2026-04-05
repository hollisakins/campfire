"""
Tile orchestration and cloud operations for ``campfire deploy tiles``.

Handles the full tile workflow: generate, clean, upload, and register.
The heavy-lifting tile generation logic lives in tiles_engine.py.
"""

import gc
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

from tqdm import tqdm

logger = logging.getLogger(__name__)


from campfire.deploy.r2 import UploadTask, upload_files_parallel


def get_r2_tiles_client(config: dict):
    """Create boto3 S3 client for the tiles R2 bucket (for delete operations)."""
    import boto3
    from botocore.config import Config as BotoConfig

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


def _require_r2_tiles(config: dict) -> None:
    """Exit with helpful error if r2_tiles config is missing (needed for delete ops)."""
    if 'r2_tiles' not in config:
        print("Error: [r2_tiles] section not found in deploy config.")
        print("Direct R2 credentials are required for tile deletion.")
        print("Set CAMPFIRE_R2_TILES_* env vars or add [r2_tiles] to deploy.toml")
        sys.exit(1)


# ============================================
# Supabase
# ============================================

def _get_supabase_client(config: dict):
    from campfire.deploy.supabase import get_supabase_client
    return get_supabase_client(config)


# ============================================
# R2 Operations
# ============================================

def delete_r2_prefix(r2_client, bucket: str, prefix: str) -> int:
    """Delete all objects under a prefix in R2. Returns number deleted."""
    deleted = 0
    paginator = r2_client.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get('Contents', [])
        if not objects:
            continue
        keys = [{'Key': obj['Key']} for obj in objects]
        r2_client.delete_objects(Bucket=bucket, Delete={'Objects': keys})
        deleted += len(keys)
    return deleted


def upload_tiles(
    config: dict,
    tile_dir: Path,
    field: str,
    filter_name: str | None = None,
    max_workers: int = 12,
    zoom_range: tuple[int, int] | None = None,
    dry_run: bool = False,
) -> None:
    """Upload generated tiles to R2 public bucket."""

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
            rel_path = png_path.relative_to(tile_dir)
            r2_key = str(rel_path)
            tasks.append(UploadTask(png_path, r2_key, 'image/png'))

        if dry_run:
            print(f"  Would upload {len(tasks)} tiles for {field}/{fname}")
            continue

        print(f"\nUploading {len(tasks)} tiles for {field}/{fname}...")
        success, failed, errors = upload_files_parallel(
            config, tasks, bucket_id='tiles', max_workers=max_workers,
            desc=f"{field}/{fname}",
            cache_control='public, max-age=31536000, immutable',
        )

        if failed:
            print(f"  Warning: {failed} uploads failed:")
            for err in errors[:5]:
                print(f"    {err}")
        print(f"  Uploaded {success}/{len(tasks)} tiles for {field}/{fname}/")

        # Bump tile_version in Supabase to bust edge cache
        try:
            supabase = _get_supabase_client(config)
            supabase.rpc('increment_tile_version', {
                'p_field': field, 'p_filter': fname,
            }).execute()
            print(f"  Bumped tile_version for {field}/{fname}")
        except Exception as e:
            print(f"  Warning: Failed to bump tile_version: {e}")


# ============================================
# Clean
# ============================================

def clean_tiles(
    config: dict,
    tile_dir: Path,
    field: str,
    filter_name: str | None = None,
    dry_run: bool = False,
) -> None:
    """Delete existing R2 tiles for field/filter."""
    _require_r2_tiles(config)
    r2_client = get_r2_tiles_client(config)
    bucket = config['r2_tiles']['bucket_name']

    fields_to_clean = []
    if filter_name:
        fields_to_clean.append((field, filter_name))
    else:
        field_dir = tile_dir / field
        if field_dir.exists():
            fields_to_clean = [
                (field, d.name) for d in sorted(field_dir.iterdir()) if d.is_dir()
            ]

    for f, filt in fields_to_clean:
        prefix = f"{f}/{filt}/"
        if dry_run:
            print(f"  Would delete R2 tiles at {bucket}/{prefix}")
            continue
        print(f"Deleting existing tiles at {bucket}/{prefix}...")
        n = delete_r2_prefix(r2_client, bucket, prefix)
        print(f"  Deleted {n} objects")


# ============================================
# Register
# ============================================

def register_layers(
    config: dict,
    tile_dir: Path,
    field: str,
    filter_name: str | None = None,
    dry_run: bool = False,
) -> None:
    """Register tile layers in Supabase map_layers table."""
    _require_r2_tiles(config)
    r2_config = config['r2_tiles']
    public_url_base = r2_config['public_url_base'].rstrip('/')
    supabase = _get_supabase_client(config)

    # Find stats files
    if filter_name:
        stats_files = [tile_dir / field / filter_name / 'stats.json']
    else:
        field_dir = tile_dir / field
        stats_files = sorted(field_dir.glob('*/stats.json'))

    if not stats_files:
        print("Error: No stats.json files found. Generate tiles first.")
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

        if dry_run:
            print(f"  Would register {stats['field']}/{fname}: "
                  f"zoom {stats['min_zoom']}-{stats['max_zoom']}, "
                  f"{stats['total_tiles']} tiles")
            continue

        supabase.table('map_layers').upsert(
            row, on_conflict='field,filter'
        ).execute()

        print(f"  Registered {stats['field']}/{fname}: "
              f"zoom {stats['min_zoom']}-{stats['max_zoom']}, "
              f"{stats['total_tiles']} tiles")


# ============================================
# Generate
# ============================================

def _save_stats(output_dir: Path, stats) -> Path:
    """Save TileStats to JSON sidecar."""
    stats_path = output_dir / stats.field / stats.filter_name / 'stats.json'
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
    return stats_path


def generate_tiles(
    tile_dir: Path,
    field: str,
    filter_name: str | None = None,
    pixel_scale: float | None = None,
    workers: int = 4,
    overwrite: bool = False,
    dry_run: bool = False,
    imaging_config_path: Path | None = None,
    preview: bool = False,
    preview_ra: float | None = None,
    preview_dec: float | None = None,
) -> None:
    """
    Generate tile pyramids from FITS mosaics.

    Wraps tiles_engine functions: load config, compute field grid, generate.
    """
    from campfire.deploy.tiles_engine import (
        compute_field_grid,
        estimate_tiles_for_filter,
        estimate_tiles_for_rgb,
        generate_rgb_preview,
        generate_tiles_for_filter,
        generate_tiles_for_rgb,
        get_rgb_configs,
        get_tile_configs,
        load_imaging_config,
        save_field_grid,
    )

    if imaging_config_path is None:
        from campfire.deploy.config import resolve_imaging_config
        imaging_config_path = resolve_imaging_config()

    imaging_config = load_imaging_config(imaging_config_path)

    # Override pixel scale if specified
    if pixel_scale is not None:
        imaging_config.setdefault('defaults', {})['output_pixel_scale_arcsec'] = pixel_scale

    # Override output_dir so tiles_engine writes to tile_dir
    imaging_config.setdefault('defaults', {})['output_dir'] = str(tile_dir)
    output_dir = tile_dir

    fields = [field]
    is_rgb = filter_name == 'rgb'
    filters = [filter_name] if filter_name and not is_rgb else None

    if is_rgb:
        _generate_rgb(
            imaging_config, output_dir, fields, workers, overwrite,
            dry_run, preview, preview_ra, preview_dec,
            compute_field_grid, save_field_grid, get_tile_configs,
            get_rgb_configs, estimate_tiles_for_rgb,
            generate_tiles_for_rgb, generate_rgb_preview,
        )
    else:
        _generate_single(
            imaging_config, output_dir, fields, filters, workers, overwrite,
            dry_run,
            compute_field_grid, save_field_grid, get_tile_configs,
            estimate_tiles_for_filter, generate_tiles_for_filter,
        )

    gc.collect()


def _generate_single(
    imaging_config, output_dir, fields, filters, workers, overwrite,
    dry_run,
    compute_field_grid, save_field_grid, get_tile_configs,
    estimate_tiles_for_filter, generate_tiles_for_filter,
):
    """Generate single-filter tiles."""
    tile_configs = get_tile_configs(
        imaging_config, fields=fields, filters=filters
    )

    if not tile_configs:
        print("Error: No matching field/filter configurations found.")
        sys.exit(1)

    print(f"Found {len(tile_configs)} field/filter combination(s) to process")

    # Group by field
    configs_by_field = defaultdict(list)
    for config in tile_configs:
        configs_by_field[config.field].append(config)

    # Compute unified field grids (from ALL filters, not just requested)
    field_grids = {}
    for field in configs_by_field:
        all_field_configs = get_tile_configs(imaging_config, fields=[field])
        all_filter_files = {
            c.filter_name: c.input_files for c in all_field_configs
        }
        pixel_scale = configs_by_field[field][0].output_pixel_scale_arcsec
        field_grid = compute_field_grid(all_filter_files, pixel_scale)
        save_field_grid(output_dir, field, field_grid)
        field_grids[field] = field_grid
        print(f"Unified grid for {field}: "
              f"{field_grid.naxis1} x {field_grid.naxis2} px "
              f"(from {len(all_filter_files)} filter(s))")

    if dry_run:
        print("\n--- DRY RUN ---\n")
        for config in tile_configs:
            est = estimate_tiles_for_filter(
                config, output_grid=field_grids[config.field],
            )
            print(f"  {est['field']}/{est['filter']}:")
            print(f"    Input files:     {est['input_files']}")
            print(f"    Output size:     "
                  f"{est['output_width']} x {est['output_height']} px")
            print(f"    Pixel scale:     "
                  f"{est['pixel_scale_arcsec']:.3f}\"/px")
            print(f"    Zoom range:      "
                  f"{est['min_zoom']} - {est['max_zoom']}")
            print(f"    Estimated tiles: "
                  f"~{est['estimated_tiles']:,}")
            print(f"    Estimated size:  "
                  f"~{est['estimated_size_mb']:.0f} MB "
                  f"({est['estimated_size_gb']:.2f} GB)")
            print()
        return

    for config in tile_configs:
        stats = generate_tiles_for_filter(
            config, n_workers=workers,
            overwrite=overwrite,
            output_grid=field_grids[config.field],
        )
        stats_path = _save_stats(output_dir, stats)
        print(f"\nGenerated {stats.total_tiles} tiles "
              f"({stats.total_size_bytes / (1024 * 1024):.1f} MB) "
              f"for {stats.field}/{stats.filter_name}")
        print(f"  Stats saved to {stats_path}")


def _generate_rgb(
    imaging_config, output_dir, fields, workers, overwrite, dry_run,
    preview, preview_ra, preview_dec,
    compute_field_grid, save_field_grid, get_tile_configs,
    get_rgb_configs, estimate_tiles_for_rgb,
    generate_tiles_for_rgb, generate_rgb_preview,
):
    """Generate RGB composite tiles."""
    rgb_configs = get_rgb_configs(imaging_config, fields=fields)

    if not rgb_configs:
        print("Error: No [field.rgb] configurations found.")
        sys.exit(1)

    print(f"Found {len(rgb_configs)} RGB configuration(s)")

    for rgb_config in rgb_configs:
        # Compute unified field grid from ALL filters
        all_field_configs = get_tile_configs(
            imaging_config, fields=[rgb_config.field]
        )
        all_filter_files = {
            c.filter_name: c.input_files for c in all_field_configs
        }
        field_grid = compute_field_grid(
            all_filter_files, rgb_config.output_pixel_scale_arcsec
        )
        save_field_grid(output_dir, rgb_config.field, field_grid)
        print(f"Unified grid for {rgb_config.field}: "
              f"{field_grid.naxis1} x {field_grid.naxis2} px")

        if preview:
            preview_path = generate_rgb_preview(
                rgb_config, field_grid,
                center_ra=preview_ra,
                center_dec=preview_dec,
            )
            print(f"Preview saved to {preview_path}")
            continue

        if dry_run:
            est = estimate_tiles_for_rgb(
                rgb_config, output_grid=field_grid,
            )
            print(f"\n--- RGB DRY RUN: {est['field']} ---\n")
            print(f"  Filters:           {est['num_filters']}")
            print(f"  Input files:       {est['input_files']}")
            print(f"  Output size:       "
                  f"{est['output_width']} x {est['output_height']} px")
            print(f"  Pixel scale:       "
                  f"{est['pixel_scale_arcsec']:.3f}\"/px")
            print(f"  Zoom range:        "
                  f"{est['min_zoom']} - {est['max_zoom']}")
            print(f"  Estimated tiles:   ~{est['estimated_tiles']:,}")
            print(f"  Estimated size:    "
                  f"~{est['estimated_size_mb']:.0f} MB "
                  f"({est['estimated_size_gb']:.2f} GB)")
            print()
            continue

        stats = generate_tiles_for_rgb(
            rgb_config, n_workers=workers,
            overwrite=overwrite, output_grid=field_grid,
        )
        stats_path = _save_stats(output_dir, stats)
        print(f"\nGenerated {stats.total_tiles} RGB tiles "
              f"({stats.total_size_bytes / (1024 * 1024):.1f} MB) "
              f"for {stats.field}")
        print(f"  Stats saved to {stats_path}")


# ============================================
# Top-level Orchestrator
# ============================================

def deploy_tiles(
    config: dict,
    tile_dir: Path,
    field: str,
    filter_name: str | None = None,
    pixel_scale: float | None = None,
    workers: int = 4,
    overwrite: bool = False,
    dry_run: bool = False,
    imaging_config_path: Path | None = None,
    generate: bool = True,
    upload: bool = True,
    register: bool = True,
    clean: bool = False,
    zoom_range: tuple[int, int] | None = None,
    preview: bool = False,
    preview_ra: float | None = None,
    preview_dec: float | None = None,
) -> None:
    """Full tile workflow: generate -> clean -> upload -> register."""
    if generate:
        generate_tiles(
            tile_dir, field,
            filter_name=filter_name,
            pixel_scale=pixel_scale,
            workers=workers,
            overwrite=overwrite,
            dry_run=dry_run,
            imaging_config_path=imaging_config_path,
            preview=preview,
            preview_ra=preview_ra,
            preview_dec=preview_dec,
        )

    if clean:
        clean_tiles(config, tile_dir, field,
                    filter_name=filter_name, dry_run=dry_run)

    if upload:
        upload_tiles(config, tile_dir, field,
                     filter_name=filter_name,
                     max_workers=workers,
                     zoom_range=zoom_range,
                     dry_run=dry_run)

    if register:
        register_layers(config, tile_dir, field,
                        filter_name=filter_name, dry_run=dry_run)

    print("\nDone!")
