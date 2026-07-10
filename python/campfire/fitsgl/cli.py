"""Click CLI for `campfire fitsgl` (epic #337).

Registered lazily by ``campfire.cli`` (like ``deploy``) so the base client never
imports the FitsGL producer. Phase 2 ships ``build``; Phase 3 adds ``deploy``.

Usage:
    campfire fitsgl build  --field cosmos
    campfire fitsgl build  --field cosmos --tile PRIMER      # single-tile / off-grid
    campfire fitsgl deploy --field cosmos                    # composite → campfire-tiles
    campfire fitsgl deploy --field cosmos --tile PRIMER      # single tile
"""

import os

import click

from campfire.fitsgl import build as _build


@click.group()
def fitsgl_group():
    """Build & deploy FitsGL tile-pyramid datasets for the map/cutout service."""
    pass


@fitsgl_group.command('build')
@click.option('--field', required=True, help='NIRCam field name (e.g. cosmos).')
@click.option('--tile', default=None,
              help='Build a single-tile standalone dataset instead of the fiducial '
                   'composite (the only path for off-grid tiles like PRIMER).')
@click.option('--pixel-scale', default='30mas', show_default=True,
              help='Mosaic pixel scale to build. All bands must share it to co-grid.')
@click.option('--processes', type=int, default=None,
              help='Per-level worker pool size (default: auto, one per level up to CPU count).')
@click.option('--out-dir', default=None,
              help='Dataset output root (default: $CAMPFIRE_ROOT/fitsgl).')
@click.option('--overwrite', is_flag=True,
              help='Rebuild every band from scratch. Pass this after a mosaic\'s '
                   'pixels change (band reuse keys on presence, not content) or after '
                   'changing a [build] knob.')
def build_cmd(field, tile, pixel_scale, processes, out_dir, overwrite):
    """Build a field's FitsGL dataset from native (rotated) mosaics — no reproject.

    The default builds the fiducial composite (each filter is one band; the filters
    co-grid so they RGB-composite). CAMPFIRE generates the fitsgl.toml; you never
    edit it. The display pyramid is RICE Q=8 (~0.03% lossy) — for photometry read
    the raw lossless mosaic, not the pyramid.
    """
    _build.run_build(
        field, pixel_scale=pixel_scale, tile=tile, processes=processes,
        overwrite=overwrite, out_dir=out_dir,
    )


@fitsgl_group.command('deploy')
@click.option('--field', required=True, help='NIRCam field name (e.g. cosmos).')
@click.option('--tile', default=None,
              help='Deploy a single-tile standalone dataset instead of the composite.')
@click.option('--pixel-scale', default='30mas', show_default=True,
              help='Pixel scale of the built dataset (recorded on the fitsgl_datasets row).')
@click.option('--viewer-origin', default='*', show_default=True,
              help='CORS Allow-Origin set on the tiles bucket. Default "*" (public, '
                   'derived data; also covers preview deploys with dynamic origins).')
@click.option('--out-dir', default=None,
              help='Dataset output root (default: $CAMPFIRE_ROOT/fitsgl).')
@click.option('--dry-run', is_flag=True,
              help='Show FitsGL\'s upload/delete/purge plan without writing anything.')
@click.option('--config', 'config_path', default=None,
              help='Deploy config TOML (default: $CAMPFIRE_ROOT/config/config.toml).')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321) + local CAMPFIRE_S3_TILES_* creds.')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Authenticate to Supabase with a service-role key and use local '
                   'CAMPFIRE_S3_TILES_* creds (unattended / CI). Equivalent to '
                   'CAMPFIRE_DEPLOY_MODE=service-role.')
def deploy_cmd(field, tile, pixel_scale, viewer_origin, out_dir, dry_run,
               config_path, local, service_role):
    """Deploy a built FitsGL dataset to campfire-tiles + upsert its fitsgl_datasets row.

    In the default `login` mode the CLI fetches the R2 tiles credentials from an
    admin-gated web endpoint (you must `campfire login` as an admin) and hands them to
    FitsGL. The dataset row's visibility derives from the backing mosaics'
    deploy_status, so deploying against draft mosaics surfaces nothing publicly until
    they publish. Re-deploy of unchanged data is an incremental no-op.
    """
    from campfire.deploy.cli import _gate_admin
    from campfire.deploy.config import load_config
    from campfire.deploy.supabase import get_supabase_client
    from campfire.fitsgl import deploy as _deploy

    if service_role:
        os.environ['CAMPFIRE_DEPLOY_MODE'] = 'service-role'
    config = load_config(config_path, local=local, service_role=service_role)
    if not local:
        _gate_admin(config)  # no-ops under service-role / local (they bypass RLS)
    client = get_supabase_client(config)

    _deploy.run_deploy(
        field, config=config, client=client, tile=tile, pixel_scale=pixel_scale,
        viewer_origin=viewer_origin, dry_run=dry_run, out_dir=out_dir,
    )
