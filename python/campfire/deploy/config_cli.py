"""`campfire config` — the config plane of the sync story (issue #303).

The storage plane moves bytes with ``campfire push / pull / status``; this
group moves the *data-management config* (programs.toml / observations.toml /
fields.toml) with the same verbs:

    campfire config push   local TOML sections -> cloud registry (admin)
    campfire config pull   cloud registry -> local TOML sections (any user)
    campfire config diff   three-way divergence report (any user)

Reconciliation is three-way, per section, against the last-synced hash
recorded in ``$CAMPFIRE_ROOT/meta/config_sync_state.json`` (the *base*):
push refuses to clobber a cloud section someone else changed, pull refuses to
clobber local hand-edits — each names the conflict and how to resolve it.

`campfire deploy` keeps its narrow automatic upsert of the config it deploys
(programs + observation, or field); this group is the explicit/bulk path. The
old ``deploy sync-programs`` / ``deploy sync-fields`` commands are hidden
delegating aliases now.
"""

from __future__ import annotations

import socket
import sys

import click

from .config import load_config
from . import config_sync as cs
from .supabase import (
    get_supabase_client,
    get_user_id_from_token,
    log_deploy_event,
    refresh_programs_overview,
)


def _scope_options(f):
    """Shared scoping flags: kind selectors + per-name narrowing."""
    f = click.option('--programs', 'kind_programs', is_flag=True,
                     help='Only programs.toml.')(f)
    f = click.option('--observations', 'kind_observations', is_flag=True,
                     help='Only observations.toml.')(f)
    f = click.option('--fields', 'kind_fields', is_flag=True,
                     help='Only fields.toml.')(f)
    f = click.option('--obs', multiple=True,
                     help='Specific observation(s) (implies --observations).')(f)
    f = click.option('--field', multiple=True,
                     help='Specific field(s) (implies --fields).')(f)
    return f


def _resolve_scope(kind_programs, kind_observations, kind_fields, obs, field):
    """``{kind: names-or-None}`` — None means every section in scope. With no
    flags at all, all three kinds are selected in full."""
    scope: dict[str, list | None] = {}
    if obs:
        scope['observations'] = list(obs)
    elif kind_observations:
        scope['observations'] = None
    if field:
        scope['fields'] = list(field)
    elif kind_fields:
        scope['fields'] = None
    if kind_programs:
        scope['programs'] = None
    if not scope:
        scope = {k: None for k in cs.KINDS}
    # Stable order: programs first (observations FK programs.slug).
    return {k: scope[k] for k in cs.KINDS if k in scope}


def _client(config_path, local, service_role):
    config = load_config(config_path, local=local, service_role=service_role)
    return config, get_supabase_client(config)


@click.group('config')
def config_group():
    """Sync data-management config (programs / observations / fields) with the cloud.

    The cloud registry is the source of truth for the three TOMLs in
    $CAMPFIRE_ROOT/config/. `push` publishes your local definitions, `pull`
    brings the cloud's current state into your local files (preserving
    comments outside changed sections), `diff` shows where the two diverge.
    """


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

@config_group.command('push')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@_scope_options
@click.option('--dry-run', is_flag=True, help='Show what would happen without writing.')
@click.option('--force', is_flag=True,
              help='Overwrite cloud sections that changed since your last sync.')
@click.option('--local', is_flag=True, help='Use local Supabase (127.0.0.1:54321).')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Service-role auth for unattended runs (bypasses RLS).')
def push_cmd(config_path, kind_programs, kind_observations, kind_fields, obs,
             field, dry_run, force, local, service_role):
    """Upsert local TOML sections into the cloud registry (admin).

    Defaults to everything local: all programs, all observations, and every
    field that already has deployed data (matching the old sync-fields scope;
    name one explicitly with --field to push it regardless). A cloud section
    that changed since your last sync is refused, not clobbered — pull or
    diff first, or --force.
    """
    scope = _resolve_scope(kind_programs, kind_observations, kind_fields, obs, field)
    config, sb = _client(config_path, local, service_role)

    from .cli import _gate_admin
    if not local:
        _gate_admin(config)

    state = cs.load_state()
    programs_config = cs.load_sections('programs')
    totals = {}
    for kind, names in scope.items():
        sections = cs.load_sections(kind)
        if not sections:
            print(f"{kind}: no local {kind}.toml (or empty) — skipping")
            continue
        if kind == 'fields' and names is None:
            # Deployed-data scope (same as the old sync-fields). An empty
            # intersection must stay [] — push_kind pushes nothing for [], but
            # everything for None, and a fresh DB has no deployed fields.
            from .fields import deployed_field_names
            names = [n for n in deployed_field_names(sb) if n in sections]
            if not names:
                print("fields: no deployed fields overlap fields.toml — "
                      "nothing to push (use --field X to push one anyway)")
                continue
        print(f"{kind}: pushing "
              + (f"all {len(sections)} local section(s)" if names is None
                 else f"{len(names)} section(s)"))
        n, pushed = cs.push_kind(
            sb, kind, sections, names,
            programs_config=programs_config if kind == 'observations' else None,
            base=state.get('base', {}).get(kind, {}),
            force=force, dry_run=dry_run,
        )
        totals[kind] = n
        if not dry_run:
            cs.record_base(state, kind, pushed)

    if dry_run:
        print("\nDry run — no changes made.")
        return
    cs.save_state(state)
    if totals.get('programs'):
        refresh_programs_overview(sb)
    if any(totals.values()):
        log_deploy_event(
            sb, action='config_sync', actor=get_user_id_from_token(config),
            affected_count=sum(totals.values()), host=socket.gethostname(),
            metadata={'scope': 'config_push', 'counts': totals},
        )
    print(f"\nDone. Pushed {sum(totals.values())} section(s).")


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def _pull_decision(lhash, chash, base):
    """Three-way merge verdict for one section.

    Returns one of 'in-sync', 'new', 'fast-forward', 'local-ahead', 'conflict'.
    """
    if chash == lhash:
        return 'in-sync'
    if lhash is None:
        return 'new'
    if base == lhash:
        return 'fast-forward'      # only the cloud moved
    if base == chash:
        return 'local-ahead'       # only local moved — push it, don't clobber
    return 'conflict'              # both moved (or never synced)


@config_group.command('pull')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@_scope_options
@click.option('--dry-run', is_flag=True, help='Show what would happen without writing.')
@click.option('--theirs', is_flag=True,
              help='Resolve conflicts by taking the cloud version (no prompt).')
@click.option('--local', is_flag=True, help='Use local Supabase (127.0.0.1:54321).')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Service-role auth for unattended runs (bypasses RLS).')
def pull_cmd(config_path, kind_programs, kind_observations, kind_fields, obs,
             field, dry_run, theirs, local, service_role):
    """Bring cloud config into your local TOMLs (works for any logged-in user).

    Pulls every section your program access lets you read, converging the
    local files on the cloud state: sections are replaced in place with
    comments outside them preserved; local-only sections are left alone. A
    section you edited locally is never clobbered silently — local-ahead
    sections are skipped (push them), and true conflicts prompt (or take the
    cloud with --theirs).
    """
    scope = _resolve_scope(kind_programs, kind_observations, kind_fields, obs, field)
    _, sb = _client(config_path, local, service_role)
    from .toml_surgery import set_section

    state = cs.load_state()
    interactive = sys.stdin.isatty() and not theirs
    counts = {'applied': 0, 'skipped': 0}
    for kind, names in scope.items():
        cloud = cs.fetch_cloud_rows(sb, kind, names)
        if names:
            for miss in [n for n in names if n not in cloud]:
                print(f"  ! {kind}/{miss}: not in cloud (or not accessible)")
        local_sections = cs.load_sections(kind)
        path = cs.toml_path(kind)
        print(f"{kind}: {len(cloud)} cloud section(s) in scope")
        for name in sorted(cloud):
            row = cloud[name]
            if row.get('retired_at'):
                print(f"  - {name}: retired in cloud — skipping")
                continue
            if row.get('config') is None:
                print(f"  ! {name}: cloud row has no mirrored config yet "
                      f"(pushed by an old client) — skipping")
                counts['skipped'] += 1
                continue
            chash = cs.cloud_hash(row)
            lsec = local_sections.get(name)
            # A local section that can't ride jsonb can't be hashed, so it
            # can't be three-way compared — and without a hash we can't tell
            # local-ahead from stale, so overwriting it is never safe. Skip
            # explicitly (push refuses these sections for the same reason).
            bad = cs.find_unjsonable(lsec) if lsec is not None else []
            if bad:
                print(f"  ! {name}: local section has bare TOML datetime(s) "
                      f"at {', '.join(bad)} — can't compare; keeping yours "
                      f"(quote as ISO strings to sync)")
                counts['skipped'] += 1
                continue
            lhash = cs.config_hash(lsec) if lsec is not None else None
            base = state.get('base', {}).get(kind, {}).get(name)
            verdict = _pull_decision(lhash, chash, base)
            if verdict == 'in-sync':
                cs.record_base(state, kind, {name: chash})
                continue
            if verdict == 'local-ahead':
                print(f"  ! {name}: local edits are ahead of the cloud — "
                      f"keeping yours (run `campfire config push`)")
                counts['skipped'] += 1
                continue
            if verdict == 'conflict':
                take = theirs
                if interactive:
                    take = click.confirm(
                        f"  {name}: local AND cloud both changed since your "
                        f"last sync. Take the cloud version?", default=False)
                if not take:
                    print(f"  ! {name}: conflict — keeping local "
                          f"(--theirs to take cloud, or push --force yours)")
                    counts['skipped'] += 1
                    continue
            if dry_run:
                print(f"  would {'add' if verdict == 'new' else 'update'} {name}")
                continue
            set_section(path, name, row['config'])
            cs.record_base(state, kind, {name: chash})
            counts['applied'] += 1
            print(f"  {'+' if verdict == 'new' else '~'} {name}")
    if dry_run:
        print("\nDry run — no changes made.")
        return
    cs.save_state(state)
    print(f"\nDone. Applied {counts['applied']} section(s), "
          f"skipped {counts['skipped']}.")


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

_DIFF_LABEL = {
    'in-sync': '=',
    'new': '< cloud-only',
    'fast-forward': '< cloud-modified',
    'local-ahead': '> local-modified',
    'conflict': '! diverged',
}


@config_group.command('diff')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@_scope_options
@click.option('--all', 'show_all', is_flag=True, help='Also list in-sync sections.')
@click.option('--local', is_flag=True, help='Use local Supabase (127.0.0.1:54321).')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Service-role auth for unattended runs (bypasses RLS).')
def diff_cmd(config_path, kind_programs, kind_observations, kind_fields, obs,
             field, show_all, local, service_role):
    """Show where local TOMLs and the cloud registry diverge (read-only).

    `<` means pull would apply the cloud's version, `>` means push would
    publish yours, `!` means both sides changed since your last sync.
    """
    scope = _resolve_scope(kind_programs, kind_observations, kind_fields, obs, field)
    _, sb = _client(config_path, local, service_role)

    state = cs.load_state()
    clean = True
    for kind, names in scope.items():
        cloud = cs.fetch_cloud_rows(sb, kind, names)
        local_sections = cs.load_sections(kind)
        every = sorted(set(cloud) | set(local_sections)
                       if not names else names)
        lines = []
        for name in every:
            row = cloud.get(name)
            if row and row.get('retired_at'):
                lines.append(f"  - {name}: retired in cloud")
                continue
            chash = cs.cloud_hash(row)
            lsec = local_sections.get(name)
            # diff is exactly where a user looks to see why push/pull skipped
            # a section — report unjsonable local sections, don't crash on them.
            bad = cs.find_unjsonable(lsec) if lsec is not None else []
            if bad:
                lines.append(f"  ! {name}: bare TOML datetime(s) at "
                             f"{', '.join(bad)} — quote as ISO strings to sync")
                continue
            lhash = cs.config_hash(lsec) if lsec is not None else None
            base = state.get('base', {}).get(kind, {}).get(name)
            if chash is None:
                lines.append(f"  > {name}: local-only (push to publish)")
                continue
            verdict = _pull_decision(lhash, chash, base)
            if verdict == 'in-sync' and not show_all:
                continue
            lines.append(f"  {_DIFF_LABEL[verdict]:<18} {name}")
        print(f"{kind}: "
              + (f"{len(lines)} difference(s)" if lines else "in sync"))
        for line in lines:
            print(line)
        clean = clean and not lines
    if clean:
        print("\nLocal config and cloud registry are in sync.")
