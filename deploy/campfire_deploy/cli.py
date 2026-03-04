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
"""

import sys

import click

from campfire_deploy.config import load_config
from campfire_deploy.deploy import (
    deploy_json,
    deploy_observation,
    deploy_rgb,
    deploy_sed,
    deploy_slits,
    deploy_thumbnails,
    deploy_zfit,
)


# ---------------------------------------------------------------------------
# Shared option decorators for subcommands
# ---------------------------------------------------------------------------

def shared_options(f):
    """Decorator: --config, --obs, --dry-run."""
    f = click.option('--config', 'config_path', default=None,
                     help='Path to deploy config TOML.')(f)
    f = click.option('--obs', required=True,
                     help='Observation name (e.g. ember_uds_p4).')(f)
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
@click.option('--obs', default=None, help='Observation name (e.g. ember_uds_p4).')
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
        deploy_observation(
            obs,
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
    deploy_rgb(
        obs, config,
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
    deploy_sed(
        obs, config,
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
    deploy_json(
        obs, config,
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
    deploy_zfit(
        obs, config,
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
    deploy_thumbnails(
        obs, config,
        dry_run=dry_run,
        source_ids=list(source_ids) if source_ids else None,
    )


@main.command()
@shared_options
def slits(config_path, obs, dry_run):
    """Deploy slit geometry data to Supabase."""
    config = load_config(config_path)
    deploy_slits(obs, config, dry_run=dry_run)
