"""
Click CLI for CAMPFIRE deployment.

Usage:
    cfdeploy --obs ember_uds_p4
    cfdeploy --obs ember_uds_p4 --dry-run
    cfdeploy --obs ember_uds_p4 --supabase-only
    cfdeploy --obs ember_uds_p4 --force-overwrite --auto-approve
    cfdeploy --obs ember_uds_p4 --no-rgb --no-sed
    cfdeploy --obs ember_uds_p4 --source-ids 12345 67890

    cfdeploy rgb   --obs ember_uds_p4
    cfdeploy sed   --obs ember_uds_p4
    cfdeploy json  --obs ember_uds_p4 --source-ids 12345
    cfdeploy zfit  --obs ember_uds_p4 --force-overwrite
    cfdeploy thumbnails --obs ember_uds_p4
    cfdeploy slits --obs ember_uds_p4

Multiple observations are processed serially:
    cfdeploy --obs ember_uds_p4 ember_uds_p5 ember_uds_p6
    cfdeploy rgb --obs ember_uds_p4 ember_uds_p5
"""

import sys

import click

from campfire_deploy.config import load_config, load_programs, resolve_imaging_config, resolve_tiles_dir


class _VariadicOption(click.Option):
    """Click option that consumes multiple space-separated values after a single flag."""

    def add_to_parser(self, parser, ctx):
        super().add_to_parser(parser, ctx)
        name = self.opts[-1]
        opt = parser._long_opt.get(name)
        if opt is None:
            return
        original_process = opt.process

        def _eat_remaining(value, state):
            original_process(value, state)
            while state.rargs and not state.rargs[0].startswith('-'):
                original_process(state.rargs.pop(0), state)

        opt.process = _eat_remaining
from campfire_deploy.deploy import (
    deploy_json,
    deploy_observation,
    deploy_rgb,
    deploy_sed,
    deploy_shutters,
    deploy_slits,
    deploy_thumbnails,
    deploy_zfit,
)
from campfire_deploy.supabase import get_supabase_client, upsert_programs, refresh_programs_overview


# ---------------------------------------------------------------------------
# Shared option decorators for subcommands
# ---------------------------------------------------------------------------

def shared_options(f):
    """Decorator: --config, --obs, --dry-run."""
    f = click.option('--config', 'config_path', default=None,
                     help='Path to deploy config TOML.')(f)
    f = click.option('--obs', required=True, multiple=True, type=str,
                     cls=_VariadicOption,
                     help='Observation name(s) (e.g. ember_uds_p4).')(f)
    f = click.option('--dry-run', is_flag=True,
                     help='Show what would happen without making changes.')(f)
    return f


def source_ids_option(f):
    """Decorator: --source-ids."""
    f = click.option('--source-ids', multiple=True, type=int, default=None,
                     help='Deploy only specific source IDs.')(f)
    return f


# ---------------------------------------------------------------------------
# CLI group (invoke_without_command=True for full deployment)
# ---------------------------------------------------------------------------

@click.group(invoke_without_command=True)
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@click.option('--obs', default=None, multiple=True, type=str, cls=_VariadicOption,
              help='Observation name(s) (e.g. ember_uds_p4).')
@click.option('--dry-run', is_flag=True, help='Show what would happen without making changes.')
@click.option('--source-ids', multiple=True, type=int, default=None, help='Deploy only specific source IDs.')
@click.option('--supabase-only', is_flag=True, help='Skip R2 uploads, only update Supabase.')
@click.option('--force-overwrite', is_flag=True, help='Reset inspection data for existing objects.')
@click.option('--auto-approve', is_flag=True, help='Skip confirmation prompts.')
@click.option('--no-rgb', is_flag=True, help='Skip RGB image deployment.')
@click.option('--no-sed', is_flag=True, help='Skip SED plot deployment.')
@click.pass_context
def main(ctx, config_path, obs, dry_run, source_ids, supabase_only,
         force_overwrite, auto_approve, no_rgb, no_sed):
    """Deploy CAMPFIRE pipeline products to Supabase + R2."""
    ctx.ensure_object(dict)

    # When invoked without a subcommand, --obs is required
    if ctx.invoked_subcommand is None:
        if not obs:
            print("Error: --obs is required for full deployment.")
            print("Usage: cfdeploy --obs <observation_name>")
            sys.exit(1)

        config = load_config(config_path)
        for obs_name in obs:
            deploy_observation(
                obs_name,
                config,
                dry_run=dry_run,
                supabase_only=supabase_only,
                force_overwrite=force_overwrite,
                include_rgb=not no_rgb,
                include_sed=not no_sed,
                source_ids=list(source_ids) if source_ids else None,
                auto_approve=auto_approve,
            )


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

@main.command()
@shared_options
@source_ids_option
@click.option('--overwrite', is_flag=True, help='Regenerate files even if they exist.')
def rgb(config_path, obs, dry_run, source_ids, overwrite):
    """Generate and deploy RGB images to R2."""
    config = load_config(config_path)
    for obs_name in obs:
        deploy_rgb(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
            overwrite=overwrite,
        )


@main.command()
@shared_options
@source_ids_option
@click.option('--overwrite', is_flag=True, help='Regenerate files even if they exist.')
def sed(config_path, obs, dry_run, source_ids, overwrite):
    """Generate and deploy SED plots to R2 and update has_sed_plot."""
    config = load_config(config_path)
    for obs_name in obs:
        deploy_sed(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
            overwrite=overwrite,
        )


@main.command('json')
@shared_options
@source_ids_option
def json_cmd(config_path, obs, dry_run, source_ids):
    """Regenerate and upload spectrum JSON files."""
    config = load_config(config_path)
    for obs_name in obs:
        deploy_json(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
        )


@main.command()
@shared_options
@source_ids_option
@click.option('--force-overwrite', is_flag=True, help='Reset inspection data.')
@click.option('--auto-approve', is_flag=True, help='Skip confirmation prompts.')
def zfit(config_path, obs, dry_run, source_ids, force_overwrite, auto_approve):
    """Deploy zfit JSON files and update redshift_auto."""
    config = load_config(config_path)
    for obs_name in obs:
        deploy_zfit(
            obs_name, config,
            dry_run=dry_run,
            force_overwrite=force_overwrite,
            source_ids=list(source_ids) if source_ids else None,
            auto_approve=auto_approve,
        )


@main.command()
@shared_options
@source_ids_option
def thumbnails(config_path, obs, dry_run, source_ids):
    """Regenerate spectrum thumbnail SVGs in Supabase."""
    config = load_config(config_path)
    for obs_name in obs:
        deploy_thumbnails(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
        )


@main.command()
@shared_options
def slits(config_path, obs, dry_run):
    """Deploy slit geometry data to Supabase (legacy)."""
    config = load_config(config_path)
    for obs_name in obs:
        deploy_slits(obs_name, config, dry_run=dry_run)


@main.command()
@shared_options
@click.option('--skip-astrometry', is_flag=True,
              help='Skip astrometric correction (deploy raw MSA positions).')
def shutters(config_path, obs, dry_run, skip_astrometry):
    """Deploy shutters ECSV data to Supabase."""
    config = load_config(config_path)
    for obs_name in obs:
        deploy_shutters(obs_name, config, dry_run=dry_run,
                        skip_astrometry=skip_astrometry)


# ---------------------------------------------------------------------------
# sync-programs subcommand
# ---------------------------------------------------------------------------

@main.command('sync-programs')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@click.option('--dry-run', is_flag=True, help='Show what would happen without making changes.')
def sync_programs(config_path, dry_run):
    """Upsert all programs from $CAMPFIRE_ROOT/config/programs.toml."""
    programs_config = load_programs()
    program_slugs = list(programs_config.keys())

    print(f"Found {len(program_slugs)} programs in programs.toml")
    for slug, info in programs_config.items():
        print(f"  {slug}: {info.get('program_name', '?')} (cycle {info.get('cycle', '?')})")

    if dry_run:
        print("\nDry run — no changes made.")
        return

    config = load_config(config_path)
    sb = get_supabase_client(config)

    print("\nUpserting programs...")
    upsert_programs(sb, program_slugs, programs_config)

    print("\nRefreshing materialized view...")
    refresh_programs_overview(sb)

    print("Done.")


# ---------------------------------------------------------------------------
# Tiles subcommand (per-field, does NOT use @shared_options)
# ---------------------------------------------------------------------------

def _parse_zoom(ctx, param, value):
    """Click callback to parse zoom range string like '5-8' or '5'."""
    if value is None:
        return None
    if '-' in value:
        parts = value.split('-')
        return (int(parts[0]), int(parts[1]))
    z = int(value)
    return (z, z)


@main.command()
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--field', required=True,
              help='Field name (e.g. cosmos).')
@click.option('--filter', 'filter_names', multiple=True, cls=_VariadicOption,
              help='Filter(s) to process (e.g. --filter f444w f150w). Default: all.')
@click.option('--dry-run', is_flag=True,
              help='Show estimates without making changes.')
@click.option('--generate-only', is_flag=True,
              help='Generate tiles only (no cloud operations).')
@click.option('--upload-only', is_flag=True,
              help='Upload existing tiles and register (skip generation).')
@click.option('--register-only', is_flag=True,
              help='Register layers in Supabase only.')
@click.option('--no-register', is_flag=True,
              help='Skip registration after upload.')
@click.option('--clean', is_flag=True,
              help='Delete stale R2 tiles before uploading.')
@click.option('--pixel-scale', type=float, default=None,
              help='Override output pixel scale (arcsec).')
@click.option('--zoom', callback=_parse_zoom, default=None,
              help='Zoom range for upload (e.g. "5-8", "5").')
@click.option('--workers', type=int, default=4,
              help='Parallel workers (default: 4).')
@click.option('--overwrite', is_flag=True,
              help='Regenerate existing tiles.')
@click.option('--tile-dir', default=None,
              help='Custom tile output directory.')
@click.option('--imaging-config', default=None,
              help='Path to imaging.toml.')
@click.option('--preview', is_flag=True,
              help='Generate RGB preview only (use with --filter rgb).')
@click.option('--preview-ra', type=float, default=None,
              help='RA for preview center (degrees).')
@click.option('--preview-dec', type=float, default=None,
              help='Dec for preview center (degrees).')
@click.option('--verbose', '-v', is_flag=True,
              help='Enable debug logging.')
def tiles(config_path, field, filter_names, dry_run, generate_only,
          upload_only, register_only, no_register, clean, pixel_scale, zoom,
          workers, overwrite, tile_dir, imaging_config, preview, preview_ra,
          preview_dec, verbose):
    """Generate, upload, and register map tiles for a field."""
    import logging

    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format='[%(asctime)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )

    from campfire_deploy.tiles import deploy_tiles

    tile_dir_path = resolve_tiles_dir(tile_dir)
    imaging_config_path = resolve_imaging_config(imaging_config)

    # Determine which phases to run
    if generate_only:
        do_generate, do_upload, do_register = True, False, False
    elif upload_only:
        do_generate, do_upload, do_register = False, True, not no_register
    elif register_only:
        do_generate, do_upload, do_register = False, False, True
    else:
        # Default: all phases
        do_generate, do_upload, do_register = True, True, True

    # Only load deploy config if we need cloud operations
    if do_upload or do_register or clean:
        config = load_config(config_path)
    else:
        config = {}

    # None means all filters; single filter passed directly
    filters = list(filter_names) if filter_names else [None]

    for filter_name in filters:
        deploy_tiles(
            config=config,
            tile_dir=tile_dir_path,
            field=field,
            filter_name=filter_name,
            pixel_scale=pixel_scale,
            workers=workers,
            overwrite=overwrite,
            dry_run=dry_run,
            imaging_config_path=imaging_config_path,
            generate=do_generate,
            upload=do_upload,
            register=do_register,
            clean=clean,
            zoom_range=zoom,
            preview=preview,
            preview_ra=preview_ra,
            preview_dec=preview_dec,
        )
