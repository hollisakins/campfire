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

import os
import sys

import click

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
from campfire.deploy.supabase import (
    get_supabase_client, upsert_programs, refresh_filter_options,
    refresh_programs_overview, get_latest_deployment_id,
    get_field_deployment_ids, set_deployment_status, get_user_id_from_token,
)


def _gate_admin(config: dict) -> None:
    """Verify the deployer is an admin THROUGH the actual write client.

    Calls ``is_admin()`` via the same Supabase client the deploy will write
    with, so the gate and the writes share one identity + target + token — a
    gate pass therefore guarantees the writes pass. (The old gate hit
    ``/auth/whoami`` with a separately-resolved token, which could pass while
    the writes were rejected.)

    Skipped for service-role / local clients: they bypass RLS (god-mode), and
    ``is_admin()`` would falsely return false there because ``auth.uid()`` is
    NULL under the service role.
    """
    from postgrest.exceptions import APIError
    from campfire.deploy.supabase import get_supabase_client

    mode = config.get('supabase', {}).get('_auth_mode')
    if mode in ('service_role', 'local'):
        return

    client = get_supabase_client(config)
    try:
        resp = client.rpc('is_admin').execute()
    except APIError as e:
        code = getattr(e, 'code', '') or ''
        message = getattr(e, 'message', None) or str(e)
        if code in ('PGRST301', 'PGRST303') or 'jwt' in message.lower():
            print("Error: Your login session is invalid or expired. "
                  "Run: campfire login")
        else:
            print(f"Error: Failed to verify admin status: {message}")
        sys.exit(1)

    if not resp.data:
        tm = config.get('supabase', {}).get('_token_manager')
        email = tm.get_user_email() if tm else None
        print("Error: Deploy requires admin privileges.")
        if email:
            print(f"  Logged in as: {email}")
        print("  Contact an administrator to request access.")
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


def _resolve_service_role(ctx) -> bool:
    """Whether the explicit service-role escape hatch is engaged.

    True when the top-level ``--service-role`` flag was passed (stored in ctx.obj)
    or ``CAMPFIRE_DEPLOY_MODE=service-role`` is set. The env var is the universal
    switch (works for every subcommand); the flag is convenience sugar for the
    main deploy command. Either way it is an explicit opt-in — a service-role key
    merely present in the environment does NOT engage it (issue #250).
    """
    if (ctx.obj or {}).get('service_role', False):
        return True
    raw = (os.environ.get('CAMPFIRE_DEPLOY_MODE') or '').strip().lower().replace('-', '_')
    return raw in ('service_role', 'servicerole')


def _announce_auth_mode(config: dict) -> None:
    """Print the resolved deploy auth mode + where uploads land (issue #250 #4).

    Makes the login/presigned vs service-role/direct choice obvious and
    non-surprising up front, instead of silently inferring it from ambient creds.
    """
    sb = config.get('supabase', {})
    mode = sb.get('_auth_mode')
    if mode == 'login':
        tm = sb.get('_token_manager')
        email = tm.get_user_email() if tm else None
        who = f" as {email}" if email else ""
        print(f"Deploy auth: login{who} → presigned uploads (no local write keys).")
    elif mode == 'service_role':
        print("Deploy auth: service-role → direct uploads via local S3 creds "
              "(RLS bypassed).")
    elif mode == 'local':
        print("Deploy auth: local Supabase → direct uploads via local S3 creds.")


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
              help='NIRSpec observation name(s) (e.g. ember_uds_p4).')
@click.option('--field', default=None, type=str,
              help='NIRCam field name (e.g. cosmos). Deploys exposures/mosaics for '
                   'the field; mutually exclusive with --obs.')
@click.option('--filter', 'filter_names', multiple=True, cls=_VariadicOption,
              help='NIRCam filter(s) to deploy with --field (default: all).')
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
@click.option('--draft', 'draft', is_flag=True,
              help='Deploy as an admin-only draft: spectra land deploy_status=draft '
                   '(invisible to users) until published via the admin UI. Requires the '
                   'B1 lifecycle to be applied to the target DB (checked up front).')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Explicit escape hatch: authenticate to Supabase with a '
                   'service-role key and upload directly with local S3 creds '
                   '(bypasses RLS + presigned URLs). For unattended / CI deploys. '
                   'Equivalent to CAMPFIRE_DEPLOY_MODE=service-role.')
@click.pass_context
def deploy_group(ctx, config_path, obs, field, filter_names, dry_run, source_ids,
                 supabase_only, force_overwrite, auto_approve, rgb, no_sed,
                 no_shutters, no_photometry, skip_astrometry, draft, local,
                 service_role):
    """Deploy CAMPFIRE pipeline products to Supabase + object storage.

    NIRSpec: `campfire deploy --obs <obs>`.  NIRCam:
    `campfire deploy --field <field> [--filter f1 --filter f2] [--draft]` —
    first-class parity, recording a field-scoped deployment (published by default;
    --draft holds it admin-only for review, then `deploy publish --field`).
    """
    ctx.ensure_object(dict)
    ctx.obj['local'] = local
    # Propagate the flag to subcommands via the env switch load_config reads, so
    # ``campfire deploy --service-role <sub>`` behaves like the env var everywhere.
    if service_role:
        os.environ['CAMPFIRE_DEPLOY_MODE'] = 'service-role'
    ctx.obj['service_role'] = _resolve_service_role(ctx)
    if not local:
        # Gate once here (the group callback runs before every subcommand and
        # subgroup), through the actual write client. Skipped for service-role
        # (it bypasses RLS; the gate no-ops for that mode anyway).
        _gate_admin(load_config(config_path, local=local,
                                service_role=ctx.obj['service_role']))

    # When invoked without a subcommand: NIRCam field deploy (--field) or NIRSpec
    # observation deploy (--obs).
    if ctx.invoked_subcommand is None:
        if field and obs:
            print("Error: --field (NIRCam) and --obs (NIRSpec) are mutually exclusive.")
            sys.exit(1)
        if field:
            config = load_config(config_path, local=local,
                                 service_role=ctx.obj['service_role'])
            _announce_auth_mode(config)
            from campfire.deploy.nircam import deploy_nircam
            deploy_nircam(field, config,
                          filters=list(filter_names) if filter_names else None,
                          dry_run=dry_run, draft=draft)
            return
        if not obs:
            print("Error: --obs (NIRSpec) or --field (NIRCam) is required for full deployment.")
            print("Usage: campfire deploy --obs <observation_name>")
            print("       campfire deploy --field <field> [--filter <f> ...] [--draft]")
            sys.exit(1)

        config = load_config(config_path, local=local,
                             service_role=ctx.obj['service_role'])
        _announce_auth_mode(config)
        multi = len(obs) > 1
        if multi and config.get('supabase', {}).get('_auth_mode') == 'login':
            print(f"  Note: deploying {len(obs)} observations on a user login "
                  f"(the token auto-refreshes, so long batches are fine). For "
                  f"unattended batches, --service-role is available.")
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
                draft=draft,
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


def _lifecycle_transition(ctx, config_path, obs, dry_run, local, *, to_status, verb,
                          field=None):
    """Shared body for the publish/revoke subcommands (epic #210, B2 / #261).

    Resolves each observation's (or a NIRCam field's) latest deployment and calls
    set_deployment_status, which flips the deployment + its spectra / nircam_images
    + writes audit rows server-side.
    """
    config = load_config(config_path, local=_resolve_local(ctx, local))
    sb = get_supabase_client(config)
    actor = get_user_id_from_token(config)
    if field:
        # Flip EVERY deployment the field has, not just the latest: a --filter
        # subset re-deploy spreads a field's objects across deployments, so flipping
        # only the newest would partially publish (or leave part of a revoked field
        # public). set_deployment_status per deployment also flips its nircam_images.
        dep_ids = get_field_deployment_ids(sb, field)
        if not dep_ids:
            print(f"  field {field}: no deployment found, skipping")
            return
        if dry_run:
            print(f"  [dry-run] {verb} field {field} ({len(dep_ids)} deployment(s): "
                  f"{', '.join('#'+str(d) for d in dep_ids)})")
            return
        n_ok = 0
        for dep_id in dep_ids:
            if set_deployment_status(sb, dep_id, to_status, actor=actor) is not None:
                n_ok += 1
        print(f"  {verb}ed field {field}: {n_ok}/{len(dep_ids)} deployment(s) -> {to_status}")
        return
    for obs_name in obs:
        dep_id = get_latest_deployment_id(sb, obs_name)
        if dep_id is None:
            print(f"  {obs_name}: no deployment found, skipping")
            continue
        if dry_run:
            print(f"  [dry-run] {verb} {obs_name} (deployment #{dep_id})")
            continue
        result = set_deployment_status(sb, dep_id, to_status, actor=actor)
        if result is None:
            print(f"  {obs_name}: {verb.lower()} failed")
            continue
        spectra = (result.get('spectra') or {})
        print(f"  {verb}ed {obs_name} (deployment #{dep_id}): "
              f"{spectra.get('updated', 0)} spectra -> {to_status}")


@deploy_group.command()
@shared_options
@click.option('--field', default=None, help='NIRCam field to publish (instead of --obs).')
@click.pass_context
def publish(ctx, config_path, obs, dry_run, local, field):
    """Publish draft observation(s) or a NIRCam field: make products public."""
    _lifecycle_transition(ctx, config_path, obs, dry_run, local,
                          to_status='published', verb='Publish', field=field)


@deploy_group.command()
@shared_options
@click.option('--field', default=None, help='NIRCam field to revoke (instead of --obs).')
@click.pass_context
def revoke(ctx, config_path, obs, dry_run, local, field):
    """Revoke published observation(s) or a NIRCam field: hide products (bytes retained)."""
    _lifecycle_transition(ctx, config_path, obs, dry_run, local,
                          to_status='revoked', verb='Revoke', field=field)


@deploy_group.command('delete-local')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@click.option('--obs', required=True, multiple=True, cls=_VariadicOption,
              help='Observation name(s) whose local product files to delete.')
@click.option('--verify', is_flag=True,
              help='Hash each local file and require it to match the registry sha256 '
                   'before deleting (default: trust the registry row exists + has a hash).')
@click.option('--yes', is_flag=True, help='Actually delete (default is a dry-run preview).')
@click.option('--local', is_flag=True, help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def delete_local(ctx, config_path, obs, verify, yes, local):
    """Delete local product files that are verified-present in cloud storage (B4).

    The verified-in-cloud interlock: only files that have an *active registry row
    carrying a sha256 hash* are candidates, so a local file is never deleted
    unless a verified cloud copy exists. --verify additionally requires the local
    file to hash-match the cloud copy. Files with no registry row are never
    touched. Dry-run by default — pass --yes to unlink. The delete half of the
    reduce -> deploy -> delete-local -> re-download restore loop (intermediate
    resume-set fetch lands with the canonical-exposure upload follow-up).
    """
    from campfire.deploy import registry as reg
    from campfire.config import products_dir

    config = load_config(config_path, local=_resolve_local(ctx, local))
    _gate_admin(config)
    sb = get_supabase_client(config)
    proot = products_dir()

    grand_deleted = 0
    grand_bytes = 0
    for obs_name in obs:
        plan = reg.plan_delete_local(sb, obs_name, proot, verify=verify)
        print(f"\n{obs_name}: {len(plan.deletable)} deletable "
              f"({_fmt_bytes(plan.total_bytes)}), {len(plan.skipped)} skipped, "
              f"{len(plan.absent)} registered-but-absent")
        for local_path, _key, reason in plan.skipped:
            print(f"  skip {local_path.name}: {reason}")
        if not yes:
            for local_path, _key, _size in plan.deletable[:10]:
                print(f"  [dry-run] would delete {local_path}")
            if len(plan.deletable) > 10:
                print(f"  ... and {len(plan.deletable) - 10} more")
            continue
        for local_path, _key, size in plan.deletable:
            try:
                local_path.unlink()
                grand_deleted += 1
                grand_bytes += size
            except OSError as e:
                print(f"  ! failed to delete {local_path}: {e}")

    if yes:
        print(f"\nDeleted {grand_deleted} files ({_fmt_bytes(grand_bytes)} freed). "
              f"Restore with: campfire download --obs <name>")
    else:
        print(f"\nDry run — pass --yes to delete. Interlock: only registered, "
              f"sha256-hashed{'+verified' if verify else ''} files are candidates.")


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
# registry subgroup (storage_objects shadow index, #214)
# ---------------------------------------------------------------------------

@deploy_group.group(invoke_without_command=True)
@click.pass_context
def registry(ctx):
    """Manage the storage_objects registry (backfill / reconcile / budget).

    Bare `campfire deploy registry` defaults to `reconcile`.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(registry_reconcile)


def _fmt_bytes(n: int | float) -> str:
    """Human-readable byte count."""
    n = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB', 'PB'):
        if abs(n) < 1024 or unit == 'PB':
            return f"{n:.2f} {unit}" if unit != 'B' else f"{int(n)} B"
        n /= 1024
    return f"{n:.2f} PB"


@registry.command('backfill')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--dry-run', is_flag=True,
              help='Compute rows without writing them.')
@click.option('--orphans/--no-orphans', default=True,
              help='Adopt data-bucket objects with no DB pointer via LIST (needs storage creds).')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def registry_backfill(ctx, config_path, dry_run, orphans, local):
    """Backfill storage_objects from existing pointers + bucket orphans.

    spectra rows reuse the stored sha256 (file_hash) + size — no bucket access.
    NIRCam pointers and orphans are HEAD'd for size + a provisional 'etag:' hash.
    Idempotent (upsert by key); safe to re-run.
    """
    from campfire_layout import is_known_key, parse_key, LayoutError
    from campfire.deploy import registry as reg

    config = load_config(config_path, local=_resolve_local(ctx, local))
    _gate_admin(config)
    sb = get_supabase_client(config)
    backend = reg.resolve_backend_label(config)

    # Already-migrated objects (canonical keys on OSN). Backfill must NOT re-register
    # their retained R2 legacy keys as duplicate r2 rows.
    migrated = reg.active_osn_canonical_keys(sb)

    def _registerable_orphan(k: str) -> bool:
        """A bucket orphan worth adopting: known, not a dead product, not migrated."""
        if not is_known_key(k):
            return False
        try:
            pk = parse_key(k)
        except LayoutError:
            return False
        if pk.product_type in reg.UNREGISTERED_PRODUCT_TYPES:
            return False  # dead rgb/sed — never registered
        try:
            if reg.canonical_key_for(k) in migrated:
                return False  # already on OSN — don't resurrect a legacy duplicate
        except LayoutError:
            return False
        return True

    # 1. spectra finals — authoritative sha256 from the DB (skips already-migrated).
    n_spec, skipped, already = reg.backfill_spectra(
        sb, backend=backend, dry_run=dry_run, migrated_keys=migrated)
    print(f"spectra: {n_spec} rows{' (dry-run)' if dry_run else ''}"
          + (f", {skipped} skipped (no stored hash/size)" if skipped else "")
          + (f", {already} already on OSN" if already else ""))

    # 2. NIRCam pointers — no stored hash; HEAD for size + etag.
    try:
        pointers = reg.live_pointers(sb)
        nircam = pointers['nircam_images'] + pointers['nircam_exposures']
        if nircam:
            n_nc, failed_nc = reg.backfill_via_head(
                sb, config, nircam, backend=backend,
                content_type='image/png', dry_run=dry_run,
            )
            print(f"nircam: {n_nc} rows"
                  + (f", {failed_nc} failed (HEAD error / unknown key)" if failed_nc else ""))
    except Exception as e:
        print(f"nircam backfill skipped (no storage credentials for HEAD?): {e}")

    # 3. Orphans — bucket objects with no registry row, adopted via LIST. Excludes
    #    dead products (rgb/sed) and objects already migrated to OSN.
    if orphans:
        try:
            existing = set(reg.registry_keys(sb))
            orphan_keys = [k for k in reg.list_bucket_keys(config) if k not in existing]
            adoptable = [k for k in orphan_keys if _registerable_orphan(k)]
            unadoptable = len(orphan_keys) - len(adoptable)
            if adoptable:
                n_orph, failed_orph = reg.backfill_via_head(
                    sb, config, adoptable, backend=backend, dry_run=dry_run,
                )
                print(f"orphans: {n_orph} adopted"
                      + (f", {failed_orph} failed" if failed_orph else "")
                      + (f", {unadoptable} skipped (unknown/dead/already-migrated)" if unadoptable else ""))
            else:
                print(f"orphans: none adoptable"
                      + (f" ({unadoptable} skipped)" if unadoptable else ""))
        except Exception as e:
            print(f"orphan adoption skipped (no storage credentials for LIST?): {e}")

    print("Backfill complete.")


@registry.command('reconcile')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--no-bucket', is_flag=True,
              help='Skip the bucket LIST (coverage only; no dangling/orphan detection).')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def registry_reconcile(ctx, config_path, no_bucket, local):
    """Report coverage: live pointers vs registry vs bucket (read-only).

    The F1 gate: every live fits_path/file_path/png_path must have a registry
    row (missing == 0) before any consumer may treat the registry as
    authoritative. Also reports dangling rows and unadopted bucket orphans.
    """
    from campfire.deploy import registry as reg

    config = load_config(config_path, local=_resolve_local(ctx, local))
    _gate_admin(config)
    sb = get_supabase_client(config)

    pointers = reg.live_pointers(sb)
    live = pointers['spectra'] + pointers['nircam_images'] + pointers['nircam_exposures']
    reg_keys = reg.registry_keys(sb)

    bucket_keys = None
    danglable_keys = None
    if not no_bucket:
        try:
            bucket_keys = list(reg.list_bucket_keys(config))
            # The bucket LIST only enumerates the data backend (R2 in F1); an
            # OSN-native object (NIRCam FITS/expmaps, #261/N1) has no R2 twin, so
            # scope dangling to registry rows homed on the LISTed backend, else
            # every deployed NIRCam object is reported spuriously dangling.
            danglable_keys = reg.registry_keys(sb, backend=reg.resolve_backend_label(config))
        except Exception as e:
            print(f"(bucket LIST unavailable — coverage only: {e})")

    report = reg.compute_reconcile(live, reg_keys, bucket_keys, danglable_keys=danglable_keys)
    print(f"live pointers: {len(set(live))}  registry rows: {len(set(reg_keys))}")
    print(report.summary())
    if report.missing:
        print(f"\n  COVERAGE GAP — {len(report.missing)} live pointer(s) have no registry row.")
        for k in sorted(report.missing)[:10]:
            print(f"    missing: {k}")
        if len(report.missing) > 10:
            print(f"    ... and {len(report.missing) - 10} more")
    if report.dangling:
        for k in sorted(report.dangling)[:10]:
            print(f"    dangling: {k}")
    if report.orphans:
        print(f"  {len(report.orphans)} orphan bucket object(s) "
              f"({len(report.adoptable)} adoptable) — run `registry backfill`.")
    if report.covered and not report.dangling:
        print("\n  Coverage gate: PASS (registry covers all live pointers).")


@registry.command('budget')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def registry_budget(ctx, config_path, local):
    """Show bytes-at-rest against the 20 TB cap (via get_storage_budget RPC)."""
    config = load_config(config_path, local=_resolve_local(ctx, local))
    _gate_admin(config)
    sb = get_supabase_client(config)

    resp = sb.rpc('get_storage_budget').execute()
    b = resp.data or {}
    total = b.get('total_bytes', 0)
    cap = b.get('cap_bytes', 0)
    print(f"Storage budget: {_fmt_bytes(total)} / {_fmt_bytes(cap)} "
          f"({b.get('pct_used', 0)}%)")
    print(f"  registry (data): {_fmt_bytes(b.get('registry_bytes', 0))}")
    print(f"  tiles (map_layers): {_fmt_bytes(b.get('tile_bytes', 0))}")
    by_pt = b.get('by_product_type') or {}
    if by_pt:
        print("  by product_type:")
        for pt, n in sorted(by_pt.items(), key=lambda kv: -kv[1]):
            print(f"    {pt:28} {_fmt_bytes(n)}")


@registry.command('copy')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--obs', 'observations', default=None, multiple=True, type=str,
              cls=_VariadicOption, help='Limit to these observation(s).')
@click.option('--field', 'fields', default=None, multiple=True, type=str,
              cls=_VariadicOption, help='Limit to these field(s).')
@click.option('--product-type', 'product_types', default=None, multiple=True, type=str,
              cls=_VariadicOption,
              help='Override the migrated product types (default excludes dead rgb/sed).')
@click.option('--limit', type=int, default=None,
              help='Migrate at most N objects (piloting).')
@click.option('--execute', is_flag=True,
              help='Actually copy + relocate. Without this, only a dry-run plan is printed.')
@click.option('--verify-readback/--no-verify-readback', default=True,
              help='Re-download from OSN and re-hash to verify (default on). '
                   '--no-verify-readback only checks the uploaded size.')
@click.option('--tmp-dir', default=None,
              help='Scratch dir for streamed copies (default: system temp).')
@click.option('--max-workers', type=int, default=8,
              help='Parallel copy workers (default 8). Each does GET+PUT+readback.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def registry_copy(ctx, config_path, observations, fields, product_types, limit,
                  execute, verify_readback, tmp_dir, max_workers, local):
    """Copy data objects R2->OSN, re-key legacy->canonical, verify, relocate (#215).

    Reads each active backend='r2' registry row, GETs it from R2, PUTs it to OSN
    under its canonical key, verifies by sha256 (upgrading provisional 'etag:'
    hashes), then flips the registry row in place to osn+canonical+sha256. R2 bytes
    are retained (rollback = read R2). Idempotent/resumable: already-migrated rows
    are skipped. Dry-run by default; pass --execute to transfer.
    """
    from campfire.deploy import registry as reg

    config = load_config(config_path, local=_resolve_local(ctx, local))
    _gate_admin(config)
    sb = get_supabase_client(config)

    obs = list(observations) or None
    flds = list(fields) or None
    types = list(product_types) or None

    # Size the S3 connection pools for the worker fan-out (each worker does a GET
    # from R2 + a PUT and a readback GET to OSN concurrently).
    pool = max(max_workers * 2, 8)

    # Resolve the OSN destination client up front so a missing [r2_osn] section
    # fails fast with a clear message (not mid-transfer).
    try:
        dst_client, dst_bucket, dst_backend = reg._osn_client_and_bucket(
            config, max_pool_connections=pool)
    except Exception as e:
        raise click.UsageError(
            f"OSN destination not configured: {e}\n"
            f"Set CAMPFIRE_S3_OSN_* env vars (or a [r2_osn] block in deploy.toml)."
        )
    src_client, src_bucket, _ = reg._data_client_and_bucket(
        config, max_pool_connections=pool)

    # G1 freeze guard: warn if any selected obs already has osn-canonical rows that
    # collide with fresh r2-legacy rows (a re-deploy during the shadow window).
    conflicts = reg.find_migration_conflicts(sb, observations=obs)
    if conflicts:
        print(f"  WARNING: {len(conflicts)} object(s) already have an active OSN copy "
              f"but a fresh R2-legacy row exists (re-deploy during shadow?).")
        for k in conflicts[:5]:
            print(f"    conflict: {k}")
        print("    These would create duplicate active rows — resolve before --execute.\n")

    if not execute:
        report = reg.copy_objects(
            sb, src_client=src_client, src_bucket=src_bucket,
            dst_client=dst_client, dst_bucket=dst_bucket, dst_backend=dst_backend,
            observations=obs, fields=flds, product_types=types, limit=limit,
            dry_run=True,
        )
        print(f"DRY RUN — {report.summary()}")
        for legacy, canonical, _sz in report.planned[:10]:
            print(f"    {legacy}  ->  {canonical}")
        if len(report.planned) > 10:
            print(f"    ... and {len(report.planned) - 10} more")
        if report.skipped:
            print(f"  {len(report.skipped)} unmappable key(s) would be skipped:")
            for legacy, reason in report.skipped[:5]:
                print(f"    skip: {legacy} ({reason})")
        print(f"\n  Plan: {len(report.planned)} object(s), {_fmt_bytes(report.bytes_planned)}. "
              f"Re-run with --execute to copy.")
        return

    print(f"Copying R2 -> OSN ({dst_backend}, bucket {dst_bucket})"
          + (f", readback verify" if verify_readback else ", size-check only")
          + f", {max_workers} workers ...")
    report = reg.copy_objects(
        sb, src_client=src_client, src_bucket=src_bucket,
        dst_client=dst_client, dst_bucket=dst_bucket, dst_backend=dst_backend,
        observations=obs, fields=flds, product_types=types, limit=limit,
        dry_run=False, verify_readback=verify_readback, tmp_dir=tmp_dir,
        progress=True, max_workers=max_workers,
    )
    print(f"\n{report.summary()}  ({_fmt_bytes(report.bytes_copied)} copied)")
    if report.skipped:
        print(f"  {len(report.skipped)} skipped (unmappable key):")
        for legacy, reason in report.skipped[:5]:
            print(f"    skip: {legacy} ({reason})")
    if report.failed:
        print(f"  {len(report.failed)} FAILED (left on R2, not relocated):")
        for legacy, reason in report.failed[:10]:
            print(f"    fail: {legacy} ({reason})")
        ctx.exit(1)
    print("Copy complete.")


@registry.command('prune')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--duplicates/--no-duplicates', default=True,
              help='Delete active r2 rows whose canonical key is already on OSN '
                   '(duplicates left by a migration-unaware backfill). Default on.')
@click.option('--dead-products/--no-dead-products', default=True,
              help='Delete rgb/sed rows (dead products, never served). Default on.')
@click.option('--execute', is_flag=True,
              help='Actually delete. Without this, only a dry-run count is printed.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def registry_prune(ctx, config_path, duplicates, dead_products, execute, local):
    """Delete registry rows that should never exist (#215 cleanup).

    Two categories: (1) r2 duplicates of already-migrated objects — the OSN row is
    authoritative and R2 retains the bytes, so these stale legacy rows are safe to
    delete; (2) dead rgb/sed products that are no longer served. Hard delete.
    Dry-run by default; pass --execute to delete.
    """
    from campfire.deploy import registry as reg

    config = load_config(config_path, local=_resolve_local(ctx, local))
    _gate_admin(config)
    sb = get_supabase_client(config)

    ids: set[int] = set()
    if duplicates:
        dup = reg.find_r2_duplicate_ids(sb)
        print(f"r2 duplicates of migrated objects: {len(dup)}")
        ids.update(dup)
    if dead_products:
        dead = reg.find_dead_product_ids(sb)
        print(f"dead-product rows (rgb/sed):       {len(dead)}")
        ids.update(dead)

    if not ids:
        print("Nothing to prune.")
        return
    if not execute:
        print(f"\nDRY RUN — would delete {len(ids)} row(s). Re-run with --execute.")
        return
    n = reg.delete_objects(sb, list(ids))
    print(f"\nDeleted {n} row(s).")


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
    - {obs}_stuck_closed_shutters.toml (stuck shutter definitions; place at
      reference/nirspec/<obs>/stuck_closed_shutters.toml to re-reduce)
    - observations.toml (observation definition fragment)
    """
    from pathlib import Path
    from campfire.deploy.deploy import fetch_config

    config = load_config(config_path, local=_resolve_local(ctx, local))
    out = Path(output_dir) if output_dir else None
    fetch_config(obs, config, output_dir=out)


# ---------------------------------------------------------------------------
# NIRCam subcommand group (exposure tracking)
# ---------------------------------------------------------------------------

@deploy_group.group('nircam')
def nircam():
    """NIRCam mask utilities (epic #261).

    Field deploy is the top-level `campfire deploy --field <field>` (parity with
    `--obs`); `import-masks` / `pull-masks` here round-trip the DB-resident region
    masks with local reference/.../masks/*.reg files.
    """
    pass


@nircam.command('import-masks')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--field', required=True, help='Field name (e.g. cosmos).')
@click.option('--dry-run', is_flag=True,
              help='List .reg files that would be imported without writing.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def nircam_import_masks(ctx, config_path, field, dry_run, local):
    """One-time import of legacy reference/.../masks/*.reg into Supabase.

    Converts FK5/ICRS polygons to DS9 image (pixel) coords using each
    exposure's FITS WCS so the web mask editor sees them in canvas-native
    coordinates. Source .reg files are not deleted.
    """
    from campfire.deploy.nircam_masks import import_masks
    config = load_config(config_path, local=_resolve_local(ctx, local))
    import_masks(field, config, dry_run=dry_run)


@nircam.command('pull-masks')
@click.option('--config', 'config_path', default=None,
              help='Path to deploy config TOML.')
@click.option('--field', required=True, help='Field name (e.g. cosmos).')
@click.option('--dry-run', is_flag=True,
              help='Show what would be written without touching files.')
@click.option('--local', is_flag=True,
              help='Use local Supabase (127.0.0.1:54321).')
@click.pass_context
def nircam_pull_masks(ctx, config_path, field, dry_run, local):
    """Materialize Supabase mask_regions back to reference/.../masks/*.reg.

    Only writes files for exposures with a non-null mask_regions row;
    .reg files without a DB representation are left alone.
    """
    from campfire.deploy.nircam_masks import pull_masks
    config = load_config(config_path, local=_resolve_local(ctx, local))
    pull_masks(field, config, dry_run=dry_run)


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
