"""
Click CLI for CAMPFIRE deployment.

Usage (as subcommand of campfire):
    campfire deploy --obs ember_uds_p4
    campfire deploy --obs ember_uds_p4 --dry-run
    campfire deploy --obs ember_uds_p4 --supabase-only
    campfire deploy --obs ember_uds_p4 --force-overwrite --auto-approve
    campfire deploy --obs ember_uds_p4 --rgb
    campfire deploy --obs ember_uds_p4 --no-sed
    campfire deploy --obs ember_uds_p4 --no-shutters
    campfire deploy --obs ember_uds_p4 --skip-astrometry
    campfire deploy --obs ember_uds_p4 --source-ids 12345 67890

    campfire deploy rgb   --obs ember_uds_p4
    campfire deploy sed   --obs ember_uds_p4
    campfire deploy json  --obs ember_uds_p4 --source-ids 12345
    campfire deploy zfit  --obs ember_uds_p4 --force-overwrite
    campfire deploy thumbnails --obs ember_uds_p4
    campfire deploy slits --obs ember_uds_p4
    campfire deploy remove --obs ember_uds_p4 --dry-run
    campfire deploy fetch-config --obs ember_uds_p4 --output-dir ./config

    campfire deploy objects                    # reconcile (default)
    campfire deploy objects reconcile --field cosmos
    campfire deploy objects rebuild --field cosmos --force  # escape hatch

Multiple observations are processed serially:
    campfire deploy --obs ember_uds_p4 ember_uds_p5 ember_uds_p6
    campfire deploy rgb --obs ember_uds_p4 ember_uds_p5
"""

import sys

import click
import requests

from campfire.deploy.config import load_config, load_programs, resolve_imaging_config, resolve_photometry_config, resolve_tiles_dir


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


def _parse_source_ids(ctx, param, value):
    """Convert space-separated, comma-separated, or repeated flag values to int tuple."""
    if not value:
        return None
    result = []
    for item in value:
        for part in item.replace(',', ' ').split():
            try:
                result.append(int(part))
            except ValueError:
                raise click.BadParameter(f"'{part}' is not a valid integer.")
    return tuple(result) if result else None


from campfire.deploy.deploy import (
    deploy_json,
    deploy_observation,
    deploy_pointings,
    deploy_rgb,
    deploy_sed,
    deploy_shutters,
    deploy_slits,
    deploy_thumbnails,
    deploy_zfit,
)
from campfire.deploy.supabase import get_supabase_client, upsert_programs, refresh_filter_options, refresh_programs_overview


def _check_admin() -> None:
    """Verify the logged-in user has admin privileges. Exits on failure."""
    from campfire.api.session import resolve_base_url
    from campfire.auth.tokens import TokenManager

    base_url = resolve_base_url()
    try:
        tm = TokenManager(base_url=base_url)
        token = tm.get_valid_token(auto_refresh=True)
    except Exception:
        print("Error: Not logged in. Run: campfire login")
        sys.exit(1)

    try:
        resp = requests.get(
            f"{base_url}/auth/whoami",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"Error: Failed to verify admin status: {e}")
        sys.exit(1)

    if not data.get("is_admin"):
        print(f"Error: Deploy requires admin privileges.")
        print(f"  Logged in as: {data.get('email', 'unknown')}")
        print(f"  Contact an administrator to request access.")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Shared option decorators for subcommands
# ---------------------------------------------------------------------------

def shared_options(f):
    """Decorator: --config, --obs, --dry-run, --local."""
    f = click.option('--config', 'config_path', default=None,
                     help='Path to deploy config TOML.')(f)
    f = click.option('--obs', required=True, multiple=True, type=str,
                     cls=_VariadicOption,
                     help='Observation name(s) (e.g. ember_uds_p4).')(f)
    f = click.option('--dry-run', is_flag=True,
                     help='Show what would happen without making changes.')(f)
    f = click.option('--local', is_flag=True,
                     help='Use local Supabase (127.0.0.1:54321).')(f)
    return f


def _resolve_local(ctx, local: bool) -> bool:
    """Let top-level ``deploy --local`` propagate into subcommands.

    Accepts either ``campfire deploy --local <sub>`` (stored in ctx.obj)
    or ``campfire deploy <sub> --local`` (subcommand flag).
    """
    return bool(local) or bool((ctx.obj or {}).get('local', False))


def source_ids_option(f):
    """Decorator: --source-ids."""
    f = click.option('--source-ids', multiple=True, type=str, default=None,
                     cls=_VariadicOption, callback=_parse_source_ids,
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
@click.option('--source-ids', multiple=True, type=str, default=None,
              cls=_VariadicOption, callback=_parse_source_ids,
              help='Deploy only specific source IDs.')
@click.option('--supabase-only', is_flag=True, help='Skip R2 uploads, only update Supabase.')
@click.option('--force-overwrite', is_flag=True, help='Reset inspection data for existing objects.')
@click.option('--auto-approve', is_flag=True, help='Skip confirmation prompts.')
@click.option('--rgb', is_flag=True, help='Include RGB image deployment (skipped by default).')
@click.option('--no-sed', is_flag=True, help='Skip SED plot deployment.')
@click.option('--no-shutters', is_flag=True, help='Skip shutter deployment.')
@click.option('--no-photometry', is_flag=True,
              help='Skip photometry upsert after objects reconcile.')
@click.option('--skip-astrometry', is_flag=True,
              help='Skip astrometric correction for shutters (deploy raw MSA positions).')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def deploy_group(ctx, config_path, obs, dry_run, source_ids, supabase_only,
                 force_overwrite, auto_approve, rgb, no_sed, no_shutters,
                 no_photometry, skip_astrometry, local):
    """Deploy CAMPFIRE pipeline products to Supabase + R2."""
    ctx.ensure_object(dict)
    ctx.obj['local'] = local
    if not local:
        _check_admin()

    # When invoked without a subcommand, --obs is required
    if ctx.invoked_subcommand is None:
        if not obs:
            print("Error: --obs is required for full deployment.")
            print("Usage: campfire deploy --obs <observation_name>")
            sys.exit(1)

        config = load_config(config_path, local=local)
        multi = len(obs) > 1
        fields_needing_rebuild: set[str] = set()

        for obs_name in obs:
            result = deploy_observation(
                obs_name,
                config,
                dry_run=dry_run,
                supabase_only=supabase_only,
                force_overwrite=force_overwrite,
                include_rgb=rgb,
                include_sed=not no_sed,
                include_shutters=not no_shutters,
                include_photometry=not no_photometry,
                skip_astrometry=skip_astrometry,
                source_ids=list(source_ids) if source_ids else None,
                auto_approve=auto_approve,
                defer_rebuild=multi,
            )
            if result and result.get('needs_reconcile'):
                fields_needing_rebuild.add(result['field'])

        if multi and not dry_run and fields_needing_rebuild:
            # Deferred multi-obs path: run reconcile (and photometry) once per
            # field at the end. Trade-off: changed_hashes from each observation's
            # upsert aren't threaded through here, so 'reprocessed' staleness
            # won't be detected for multi-obs deploys. Acceptable — per-obs
            # deploys (the common case) get full detection via deploy_observation.
            from campfire.deploy.reconcile import reconcile_field_objects

            sb = get_supabase_client(config)
            phot_path = None
            if not no_photometry:
                from campfire.deploy.config import resolve_photometry_config
                phot_path = resolve_photometry_config(None)

            for field in sorted(fields_needing_rebuild):
                print(f"\nReconciling objects for field '{field}'...")
                _, _, changed_ids = reconcile_field_objects(
                    sb, field, abort_on_split_merge=True,
                )

                if phot_path is not None and changed_ids:
                    from campfire.deploy.photometry import deploy_field_photometry
                    print(f"\nDeploying photometry for {len(changed_ids)} "
                          f"changed objects in '{field}'...")
                    deploy_field_photometry(
                        sb, field, phot_path, config,
                        restrict_to_object_db_ids=changed_ids,
                    )

            print()
            refresh_filter_options(sb)
            refresh_programs_overview(sb)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

@deploy_group.command()
@shared_options
@source_ids_option
@click.option('--overwrite', is_flag=True, help='Regenerate files even if they exist.')
@click.pass_context
def rgb(ctx, config_path, obs, dry_run, local, source_ids, overwrite):
    """Generate and deploy RGB images to R2."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_rgb(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
            overwrite=overwrite,
        )


@deploy_group.command()
@shared_options
@source_ids_option
@click.option('--overwrite', is_flag=True, help='Regenerate files even if they exist.')
@click.pass_context
def sed(ctx, config_path, obs, dry_run, local, source_ids, overwrite):
    """Generate and deploy SED plots to R2 and update has_sed_plot."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_sed(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
            overwrite=overwrite,
        )


@deploy_group.command('json')
@shared_options
@source_ids_option
@click.pass_context
def json_cmd(ctx, config_path, obs, dry_run, local, source_ids):
    """Regenerate and upload spectrum JSON files."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_json(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
        )


@deploy_group.command()
@shared_options
@source_ids_option
@click.option('--force-overwrite', is_flag=True, help='Reset inspection data.')
@click.option('--auto-approve', is_flag=True, help='Skip confirmation prompts.')
@click.pass_context
def zfit(ctx, config_path, obs, dry_run, local, source_ids, force_overwrite, auto_approve):
    """Deploy zfit JSON files and update redshift_auto."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_zfit(
            obs_name, config,
            dry_run=dry_run,
            force_overwrite=force_overwrite,
            source_ids=list(source_ids) if source_ids else None,
            auto_approve=auto_approve,
        )


@deploy_group.command()
@shared_options
@source_ids_option
@click.pass_context
def thumbnails(ctx, config_path, obs, dry_run, local, source_ids):
    """Regenerate spectrum thumbnail SVGs in Supabase."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_thumbnails(
            obs_name, config,
            dry_run=dry_run,
            source_ids=list(source_ids) if source_ids else None,
        )


@deploy_group.command()
@shared_options
@click.pass_context
def slits(ctx, config_path, obs, dry_run, local):
    """Deploy slit geometry data to Supabase (legacy)."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_slits(obs_name, config, dry_run=dry_run)


@deploy_group.command()
@shared_options
@click.option('--skip-astrometry', is_flag=True,
              help='Skip astrometric correction (deploy raw MSA positions).')
@click.pass_context
def shutters(ctx, config_path, obs, dry_run, local, skip_astrometry):
    """Deploy shutters ECSV data to Supabase."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_shutters(obs_name, config, dry_run=dry_run,
                        skip_astrometry=skip_astrometry)


@deploy_group.command()
@shared_options
@click.pass_context
def pointings(ctx, config_path, obs, dry_run, local):
    """Deploy pointings ECSV to observations.pointings (JSONB).

    Backfills an existing observation row with pointing metadata from
    {obs}_pointings.ecsv without rerunning a full `campfire deploy`.
    """
    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        deploy_pointings(obs_name, config, dry_run=dry_run)


# ---------------------------------------------------------------------------
# remove subcommand
# ---------------------------------------------------------------------------

@deploy_group.command()
@shared_options
@click.option('--force', is_flag=True,
              help='Proceed even if targets have user inspection data.')
@click.option('--supabase-only', is_flag=True,
              help='Skip R2 deletion, only clean up Supabase.')
@click.option('--auto-approve', is_flag=True,
              help='Skip confirmation prompts.')
@click.option('--skip-rebuild', is_flag=True,
              help='Skip objects table rebuild after deletion.')
@click.pass_context
def remove(ctx, config_path, obs, dry_run, local, force, supabase_only,
           auto_approve, skip_rebuild):
    """Un-deploy observation data from Supabase + R2.

    Wipes targets, spectra, shutters, slit_regions for the observation and
    the matching R2 prefixes (spectra/, rgb/, sed/), then reconciles the
    objects table for the affected field (preserving inspection state).
    Preserves the observations row and deployments history.

    Refuses if any target has user inspection data unless --force.
    """
    from campfire.deploy.remove import remove_observation

    config = load_config(config_path, local=_resolve_local(ctx, local))
    for obs_name in obs:
        remove_observation(
            obs_name, config,
            dry_run=dry_run,
            force=force,
            supabase_only=supabase_only,
            auto_approve=auto_approve,
            skip_rebuild=skip_rebuild,
        )


# ---------------------------------------------------------------------------
# objects subgroup (Phase C: persistent reconciliation)
# ---------------------------------------------------------------------------

@deploy_group.group(invoke_without_command=True)
@click.pass_context
def objects(ctx):
    """Manage the objects table (reconcile / rebuild).

    Bare `campfire deploy objects` defaults to `reconcile`.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(objects_reconcile)


@objects.command('reconcile')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--field', type=str, default=None,
              help='Reconcile objects for a single field.')
@click.option('--all', 'all_fields', is_flag=True,
              help='Reconcile objects for all fields.')
@click.option('--dry-run', is_flag=True,
              help='Show plan without making changes.')
@click.option('--radius', type=float, default=0.2,
              help='FoF clustering radius in arcseconds (default: 0.2).')
@click.option('--yes', is_flag=True,
              help='Skip interactive confirmation for splits/merges.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def objects_reconcile(ctx, config_path, field, all_fields, dry_run, radius, yes, local):
    """Incrementally reconcile the objects table (Phase C).

    Preserves inspection state, comments, list memberships, and photometry
    on existing objects. Inserts new objects for new clusters, soft-deletes
    orphaned objects (is_active=false), and surfaces splits/merges for
    interactive confirmation. This is the default behavior on every deploy.
    """
    if not field and not all_fields:
        raise click.UsageError("Specify --field <name> or --all.")

    from campfire.deploy.objects import fetch_distinct_fields
    from campfire.deploy.reconcile import reconcile_field_objects

    config = load_config(config_path, local=_resolve_local(ctx, local))
    sb = get_supabase_client(config)

    if all_fields:
        fields = fetch_distinct_fields(sb)
        print(f"Found {len(fields)} fields: {', '.join(fields)}")
    else:
        fields = [field]

    for f in fields:
        print(f"\nReconciling objects for field '{f}'...")
        reconcile_field_objects(
            sb, f, radius=radius, dry_run=dry_run, yes=yes,
        )  # standalone reconcile: changed_ids discarded; photometry refresh
        # is operator-driven via `campfire deploy photometry` if needed.

    if not dry_run:
        print()
        refresh_filter_options(sb)
        refresh_programs_overview(sb)

    print("Done.")


@objects.command('split')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--object', 'object_ref', required=True, type=str,
              help='Object to split (IAU object_id or integer DB id).')
@click.option('--move', 'move_target_ids', multiple=True, type=str,
              cls=_VariadicOption, required=True,
              help='Target ID(s) to move to a new object (repeat or space-separate).')
@click.option('--dry-run', is_flag=True, help='Show plan without making changes.')
@click.option('--yes', is_flag=True, help='Skip interactive confirmation.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def objects_split(ctx, config_path, object_ref, move_target_ids, dry_run, yes, local):
    """Manually split an object by moving a subset of its targets to a new row.

    The original object keeps its DB id, inspection state, comments, and list
    memberships; the moved targets get a fresh object with a coordinate-
    derived IAU name. Photometry is re-linked by proximity to the closer
    centroid.

    Example:

        campfire deploy objects split --object J100033.42+022054.8 \\
            --move 12345 67890
    """
    from campfire.deploy.reconcile import split_object

    config = load_config(config_path, local=_resolve_local(ctx, local))
    sb = get_supabase_client(config)
    split_object(
        sb, object_ref, list(move_target_ids),
        dry_run=dry_run, yes=yes,
    )


@objects.command('merge')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--into', 'survivor_ref', required=True, type=str,
              help='Survivor object (IAU object_id or integer DB id).')
@click.option('--from', 'source_refs', multiple=True, type=str,
              cls=_VariadicOption, required=True,
              help='Source object(s) to fold in (repeat or space-separate).')
@click.option('--dry-run', is_flag=True, help='Show plan without making changes.')
@click.option('--yes', is_flag=True, help='Skip interactive confirmation.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def objects_merge(ctx, config_path, survivor_ref, source_refs, dry_run, yes, local):
    """Manually merge one or more source objects into a survivor.

    The survivor keeps its DB id and all inspection state. Each source's
    comments, list memberships, and photometry are absorbed; its targets
    re-point to the survivor; and the source is soft-deleted (is_active=false).

    Example:

        campfire deploy objects merge --into J100033.42+022054.8 \\
            --from J100033.43+022054.9
    """
    from campfire.deploy.reconcile import merge_objects

    config = load_config(config_path, local=_resolve_local(ctx, local))
    sb = get_supabase_client(config)
    merge_objects(
        sb, survivor_ref, list(source_refs),
        dry_run=dry_run, yes=yes,
    )


@objects.command('rebuild')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--field', type=str, default=None,
              help='Rebuild objects for a single field.')
@click.option('--all', 'all_fields', is_flag=True,
              help='Rebuild objects for all fields.')
@click.option('--dry-run', is_flag=True,
              help='Show stats without making changes.')
@click.option('--radius', type=float, default=0.2,
              help='Cross-match radius in arcseconds (default: 0.2).')
@click.option('--force', is_flag=True,
              help='Required to actually run; this WIPES inspection state.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def objects_rebuild(ctx, config_path, field, all_fields, dry_run, radius, force, local):
    """Legacy wipe-and-rebuild escape hatch — destroys inspection state.

    Use only when reconcile produces structurally wrong results that
    warrant starting over. Requires --force AND a typed confirmation.
    """
    if not field and not all_fields:
        raise click.UsageError("Specify --field <name> or --all.")

    from campfire.deploy.objects import fetch_distinct_fields, rebuild_field_objects

    config = load_config(config_path, local=_resolve_local(ctx, local))
    sb = get_supabase_client(config)

    if all_fields:
        fields = fetch_distinct_fields(sb)
        print(f"Found {len(fields)} fields: {', '.join(fields)}")
    else:
        fields = [field]

    if not dry_run:
        if not force:
            raise click.UsageError(
                "--force is required for rebuild. This WIPES all object-level "
                "inspection state (redshift_inspected, redshift_quality, "
                "last_inspected_*) unrecoverably; comments, list memberships, "
                "and photometry are re-linked by spatial proximity (0.3\") "
                "with possible loss.  Use `campfire deploy objects reconcile` "
                "instead unless you have a specific reason to start over."
            )
        click.echo(
            f"\nWARNING: about to wipe and rebuild objects for "
            f"{len(fields)} field(s): {', '.join(fields)}"
        )
        click.echo("Inspection state (redshift, quality) will be LOST — not re-linked.")
        click.echo("Comments, list memberships, and photometry will be re-linked by")
        click.echo("spatial proximity (0.3\"); anything farther is orphaned/soft-deleted.")
        click.echo("Type DESTROY to confirm.")
        if click.prompt("> ", type=str) != "DESTROY":
            click.echo("Aborted.")
            sys.exit(1)

    for f in fields:
        print(f"\nRebuilding objects for field '{f}'...")
        n_obj, n_multi = rebuild_field_objects(
            sb, f, radius=radius, dry_run=dry_run,
        )
        if not dry_run:
            print(f"  {n_obj} objects ({n_multi} multi-target)")

    if not dry_run:
        print()
        refresh_filter_options(sb)
        refresh_programs_overview(sb)

    print("Done.")


# ---------------------------------------------------------------------------
# photometry subcommand
# ---------------------------------------------------------------------------

@deploy_group.command()
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--field', required=True, type=str,
              help='Field name (e.g. cosmos).')
@click.option('--photometry-config', default=None,
              help='Path to photometry.toml.')
@click.option('--dry-run', is_flag=True,
              help='Show stats without making changes.')
@click.option('--no-photoz', is_flag=True,
              help='Skip photo-z extraction and P(z) sidecar upload.')
@click.option('--prune', is_flag=True,
              help='Delete photometry rows whose (catalog_name, catalog_id) '
                   'is no longer in the catalog (cleanup after upstream '
                   'catalog regeneration).')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def photometry(ctx, config_path, field, photometry_config, dry_run, no_photoz, prune, local):
    """Deploy photometric catalog data for a field."""
    from campfire.deploy.photometry import deploy_field_photometry

    phot_config_path = resolve_photometry_config(photometry_config)
    if phot_config_path is None:
        print("Error: No photometry.toml found.")
        print("  Use --photometry-config <path> or set $CAMPFIRE_ROOT")
        sys.exit(1)

    config = load_config(config_path, local=_resolve_local(ctx, local))
    sb = get_supabase_client(config)

    print(f"\nDeploying photometry for field '{field}'...")
    result = deploy_field_photometry(
        sb, field, phot_config_path, config,
        include_photoz=not no_photoz,
        dry_run=dry_run,
        prune=prune,
    )

    print(f"\n{'='*60}")
    print(f"Photometry deployment summary")
    print(f"{'='*60}")
    print(f"  Objects in field:   {result['n_objects']}")
    print(f"  Matched to catalog: {result['n_matched']}")
    print(f"  Bands configured:   {result['n_bands']}")
    if not no_photoz:
        print(f"  P(z) sidecars:      {result['n_pz']}")
    print()

    if dry_run:
        print("Dry run — no changes made.")
    else:
        print("Done.")


# ---------------------------------------------------------------------------
# sync-programs subcommand
# ---------------------------------------------------------------------------

@deploy_group.command('sync-programs')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@click.option('--dry-run', is_flag=True, help='Show what would happen without making changes.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def sync_programs(ctx, config_path, dry_run, local):
    """Upsert all programs from $CAMPFIRE_ROOT/config/programs.toml."""
    programs_config = load_programs()
    program_slugs = list(programs_config.keys())

    print(f"Found {len(program_slugs)} programs in programs.toml")
    for slug, info in programs_config.items():
        print(f"  {slug}: {info.get('program_name', '?')} (cycle {info.get('cycle', '?')})")

    if dry_run:
        print("\nDry run — no changes made.")
        return

    config = load_config(config_path, local=_resolve_local(ctx, local))
    sb = get_supabase_client(config)

    print("\nUpserting programs...")
    upsert_programs(sb, program_slugs, programs_config)

    print("\nRefreshing materialized view...")
    refresh_programs_overview(sb)

    print("Done.")


# ---------------------------------------------------------------------------
# fetch-config subcommand
# ---------------------------------------------------------------------------

@deploy_group.command('fetch-config')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@click.option('--obs', required=True, type=str, help='Observation name.')
@click.option('--output-dir', default=None, type=click.Path(),
              help='Output directory (default: current directory).')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def fetch_config_cmd(ctx, config_path, obs, output_dir, local):
    """Fetch reduction config from the database for reproducibility.

    Retrieves the latest deployment record for the observation and writes:
    - {obs}_config.toml (effective pipeline config)
    - _{obs}_stuck_closed_shutters.toml (stuck shutter definitions)
    - observations.toml (observation definition fragment)
    """
    from pathlib import Path
    from campfire.deploy.deploy import fetch_config

    config = load_config(config_path, local=_resolve_local(ctx, local))
    out = Path(output_dir) if output_dir else None
    fetch_config(obs, config, output_dir=out)


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


@deploy_group.command()
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
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def tiles(ctx, config_path, field, filter_names, dry_run, generate_only,
          upload_only, register_only, no_register, clean, pixel_scale, zoom,
          workers, overwrite, tile_dir, imaging_config, preview, preview_ra,
          preview_dec, verbose, local):
    """Generate, upload, and register map tiles for a field."""
    import logging

    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format='[%(asctime)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        )

    from campfire.deploy.tiles import deploy_tiles

    tile_dir_path = resolve_tiles_dir(tile_dir)
    imaging_config_path = resolve_imaging_config(imaging_config)
    if imaging_config_path is None:
        print("Error: No imaging.toml found. Tiles deployment requires imaging.toml.")
        print("  Use --imaging-config <path> or set $CAMPFIRE_ROOT")
        raise SystemExit(1)

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
        config = load_config(config_path, local=_resolve_local(ctx, local))
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
