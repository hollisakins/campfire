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

The same verbs carry the per-observation NIRSpec reducer-decision lists
(``reference/nirspec/<obs>/stuck_closed_shutters.toml`` and
``nodded_background_overrides.toml``, the ``--flags`` kind; admin only), by
the same three-way rule applied to each (exposure root, source) entry instead
of a section — see :mod:`campfire.deploy.nirspec_flags`. The NIRCam reviewer
masks and exposure triage status (``reference/nircam/<field>/``, the ``--masks``
kind; admin only) sync by the same rule, per exposure — see
:mod:`campfire.deploy.nircam_review`.

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
    f = click.option('--flags', 'kind_flags', is_flag=True,
                     help='Only the NIRSpec stuck-shutter / background-override '
                          'lists (admin).')(f)
    f = click.option('--masks', 'kind_masks', is_flag=True,
                     help='Only the NIRCam reviewer masks and exposure triage '
                          'status (admin).')(f)
    f = click.option('--obs', multiple=True,
                     help='Specific observation(s): their observations.toml '
                          'section and their lists.')(f)
    f = click.option('--field', multiple=True,
                     help='Specific field(s): their fields.toml section and '
                          'their masks / triage status.')(f)
    return f


_NO_FLAGS = False


def _resolve_scope(kind_programs, kind_observations, kind_fields, obs, field,
                   kind_flags=False, kind_masks=False):
    """``({kind: names-or-None}, flags)`` — None means every section in scope.

    ``flags`` is the scope of the NIRSpec lists: ``_NO_FLAGS`` (not selected),
    None (every observation) or a list of observations. With no flags at all,
    all three kinds and the lists are selected in full. ``--obs X`` selects X's
    observations.toml section and X's lists; add ``--observations`` or
    ``--flags`` to take only one of the two. ``--field X --masks`` takes only X's
    masks (see ``_resolve_masks``), not its fields.toml section."""
    scope: dict[str, list | None] = {}
    flags = _NO_FLAGS
    if obs:
        if kind_observations or not kind_flags:
            scope['observations'] = list(obs)
        if kind_flags or not kind_observations:
            flags = list(obs)
    else:
        if kind_observations:
            scope['observations'] = None
        if kind_flags:
            flags = None
    if field:
        if kind_fields or not kind_masks:
            scope['fields'] = list(field)
    elif kind_fields:
        scope['fields'] = None
    if kind_programs:
        scope['programs'] = None
    if not scope and flags is _NO_FLAGS and not kind_masks:
        scope = {k: None for k in cs.KINDS}
        flags = None
    # Stable order: programs first (observations FK programs.slug).
    return {k: scope[k] for k in cs.KINDS if k in scope}, flags


def _resolve_masks(kind_programs, kind_observations, kind_fields, obs, field,
                   kind_flags=False, kind_masks=False):
    """The scope of the NIRCam masks / triage status: ``_NO_FLAGS`` (not selected),
    None (every field) or a list of fields. ``--field X`` selects X's fields.toml
    section and X's masks; ``--masks`` alone every field's; no selector at all
    selects everything, masks included."""
    if field:
        return list(field) if (kind_masks or not kind_fields) else _NO_FLAGS
    if kind_masks:
        return None
    if not any((kind_programs, kind_observations, kind_fields, kind_flags, obs)):
        return None
    return _NO_FLAGS


def _masks_ready(config, sb, masks, explicit: bool) -> bool:
    """Whether the NIRCam masks / triage status take part in this command: admin
    only (nircam_exposures is admin-only), and reading ``.reg`` files needs
    ``regions`` (the deploy profile)."""
    if masks is _NO_FLAGS:
        return False
    from . import nircam_review as nr
    if not nr.can_sync(config, sb):
        if explicit:
            print("masks: skipped — the NIRCam masks and triage status are admin-only")
        return False
    try:
        import regions  # noqa: F401
    except ImportError:
        if explicit:
            print("masks: skipped — reading .reg files needs `regions` "
                  "(install the deploy profile)")
        return False
    return True


def _flags_ready(config, sb, flags, explicit: bool) -> bool:
    """Whether the NIRSpec lists take part in this command. They are admin-only
    and need the pipeline's TOML reader/writer; where either is missing they are
    skipped, quietly unless ``--flags`` / ``--obs`` asked for them by name."""
    if flags is _NO_FLAGS:
        return False
    from . import nirspec_flags as nf
    if not nf.can_sync(config, sb):
        if explicit:
            print("flags: skipped — the NIRSpec stuck-shutter / background-override "
                  "lists are admin-only")
        return False
    try:
        import campfire_pipeline  # noqa: F401
    except ImportError:
        if explicit:
            print("flags: skipped — syncing the NIRSpec lists needs the pipeline "
                  "(install the deploy profile)")
        return False
    return True


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
@click.option('--prune', is_flag=True,
              help='Also remove from the cloud the NIRSpec list entries your files '
                   'no longer have (asked interactively otherwise).')
@click.option('--local', is_flag=True, help='Use local Supabase (127.0.0.1:54321).')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Service-role auth for unattended runs (bypasses RLS).')
def push_cmd(config_path, kind_programs, kind_observations, kind_fields,
             kind_flags, kind_masks, obs, field, dry_run, force, prune, local, service_role):
    """Upsert local TOML sections into the cloud registry (admin).

    Defaults to everything local: all programs, all observations, every field
    that already has deployed data (matching the old sync-fields scope; name
    one explicitly with --field to push it regardless), and the NIRSpec
    stuck-shutter / background-override lists of every observation. A cloud
    section (or list entry) that changed since your last sync is refused, not
    clobbered — pull or diff first, or --force. An entry you removed from a
    list is removed from the cloud only when you confirm it (or --prune), and a
    detection is never sent over an entry a person wrote. The NIRCam masks and
    triage status (--masks) follow the same rules, per exposure.
    """
    sel = (kind_programs, kind_observations, kind_fields, obs, field, kind_flags, kind_masks)
    scope, flags = _resolve_scope(*sel)
    masks = _resolve_masks(*sel)
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

    if _flags_ready(config, sb, flags, explicit=bool(kind_flags or obs)):
        from . import nirspec_flags as nf
        print("flags: pushing the lists of "
              + ("every local observation" if flags is None
                 else f"{len(flags)} observation(s)"))
        res = nf.sync_push(sb, flags, nf.backend_state(state, config), force=force,
                           prune=prune, dry_run=dry_run, interactive=sys.stdin.isatty())
        totals['flags'] = res['pushed']
        if not (res['pushed'] or res['pending'] or res['errors']):
            print("  in sync")

    if _masks_ready(config, sb, masks, explicit=bool(kind_masks or field)):
        from . import nircam_review as nr
        print("masks: pushing the masks and triage status of "
              + ("every local field" if masks is None else f"{len(masks)} field(s)"))
        res = nr.sync_push(sb, masks, nr.backend_state(state, config), force=force,
                           prune=prune, dry_run=dry_run, interactive=sys.stdin.isatty())
        totals['masks'] = res['pushed']
        if not (res['pushed'] or res['pending'] or res['errors']):
            print("  in sync")

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
    n_flags = totals.pop('flags', 0)
    n_masks = totals.pop('masks', 0)
    print(f"\nDone. Pushed {sum(totals.values())} section(s)"
          + (f", {n_flags} list entr{'y' if n_flags == 1 else 'ies'}" if n_flags else "")
          + (f", {n_masks} mask / status entr{'y' if n_masks == 1 else 'ies'}" if n_masks else "")
          + ".")


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
@click.option('--prune', is_flag=True,
              help='Also delete the NIRSpec list entries the cloud removed '
                   '(asked interactively otherwise).')
@click.option('--local', is_flag=True, help='Use local Supabase (127.0.0.1:54321).')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Service-role auth for unattended runs (bypasses RLS).')
def pull_cmd(config_path, kind_programs, kind_observations, kind_fields,
             kind_flags, kind_masks, obs, field, dry_run, theirs, prune, local, service_role):
    """Bring cloud config into your local TOMLs (works for any logged-in user).

    Pulls every section your program access lets you read, converging the
    local files on the cloud state: sections are replaced in place with
    comments outside them preserved; local-only sections are left alone. A
    section you edited locally is never clobbered silently — local-ahead
    sections are skipped (push them), and true conflicts prompt (or take the
    cloud with --theirs).

    Admins also get the NIRSpec stuck-shutter / background-override lists
    (reference/nirspec/<obs>/), entry by entry under the same rule. An entry
    a person wrote that your file lost is put back; an entry removed in the
    cloud is removed here only when you confirm it (or --prune). The NIRCam
    masks and triage status (--masks; reference/nircam/<field>/) follow the
    same rules, per exposure.
    """
    sel = (kind_programs, kind_observations, kind_fields, obs, field, kind_flags, kind_masks)
    scope, flags = _resolve_scope(*sel)
    masks = _resolve_masks(*sel)
    config, sb = _client(config_path, local, service_role)
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
    n_flags = n_flags_kept = 0
    if _flags_ready(config, sb, flags, explicit=bool(kind_flags or obs)):
        from . import nirspec_flags as nf
        print("flags: pulling the lists of "
              + ("every observation" if flags is None else f"{len(flags)} observation(s)"))
        res = nf.sync_pull(sb, flags, nf.backend_state(state, config), theirs=theirs,
                           prune=prune, dry_run=dry_run, interactive=sys.stdin.isatty())
        n_flags, n_flags_kept = res['applied'], res['kept']
        if not (n_flags or n_flags_kept or res['errors']):
            print("  in sync")
    if _masks_ready(config, sb, masks, explicit=bool(kind_masks or field)):
        from . import nircam_review as nr
        print("masks: pulling the masks and triage status of "
              + ("every field" if masks is None else f"{len(masks)} field(s)"))
        res = nr.sync_pull(sb, masks, nr.backend_state(state, config), theirs=theirs,
                           prune=prune, dry_run=dry_run, interactive=sys.stdin.isatty())
        n_flags += res['applied']
        n_flags_kept += res['kept']
        if not (res['applied'] or res['kept'] or res['errors']):
            print("  in sync")
    if dry_run:
        print("\nDry run — no changes made.")
        return
    cs.save_state(state)
    print(f"\nDone. Applied {counts['applied']} section(s), "
          f"skipped {counts['skipped']}"
          + (f"; {n_flags} list / mask entr{'y' if n_flags == 1 else 'ies'} applied, "
             f"{n_flags_kept} kept local." if (n_flags or n_flags_kept) else "."))


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
def diff_cmd(config_path, kind_programs, kind_observations, kind_fields,
             kind_flags, kind_masks, obs, field, show_all, local, service_role):
    """Show where local TOMLs and the cloud registry diverge (read-only).

    `<` means pull would apply the cloud's version, `>` means push would
    publish yours, `!` means both sides changed since your last sync. For
    the NIRSpec lists (admins) each line is one (root, source) entry; for the
    NIRCam masks and triage status, one exposure's mask or status.
    """
    sel = (kind_programs, kind_observations, kind_fields, obs, field, kind_flags, kind_masks)
    scope, flags = _resolve_scope(*sel)
    masks = _resolve_masks(*sel)
    config, sb = _client(config_path, local, service_role)

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
    if _flags_ready(config, sb, flags, explicit=bool(kind_flags or obs)):
        from . import nirspec_flags as nf
        lines = nf.sync_diff(sb, flags, nf.backend_state(state, config), show_all=show_all)
        n_diff = sum(1 for line in lines if not line.lstrip().startswith('='))
        print("flags: " + (f"{n_diff} difference(s)" if n_diff else "in sync"))
        for line in lines:
            print(line)
        clean = clean and not n_diff
    if _masks_ready(config, sb, masks, explicit=bool(kind_masks or field)):
        from . import nircam_review as nr
        lines = nr.sync_diff(sb, masks, nr.backend_state(state, config), show_all=show_all)
        n_diff = sum(1 for line in lines if not line.lstrip().startswith('='))
        print("masks: " + (f"{n_diff} difference(s)" if n_diff else "in sync"))
        for line in lines:
            print(line)
        clean = clean and not n_diff
    if clean:
        print("\nLocal config and cloud registry are in sync.")


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------

@config_group.command('retire')
@click.argument('kind', type=click.Choice(['programs', 'observations', 'fields']))
@click.argument('names', nargs=-1, required=True)
@click.option('--undo', is_flag=True,
              help='Clear the retirement, re-activating the section for sync.')
@click.option('--config', 'config_path', default=None, help='Path to deploy config TOML.')
@click.option('--local', is_flag=True, help='Use local Supabase (127.0.0.1:54321).')
@click.option('--service-role', 'service_role', is_flag=True,
              help='Service-role auth for unattended runs (bypasses RLS).')
def retire_cmd(kind, names, undo, config_path, local, service_role):
    """Soft-retire definitions in the cloud registry (admin).

    Sync is additive by design: a section missing from someone's local TOML is
    ambiguous (two reducers hold different files), so removal is never inferred
    — it is this explicit action. A retired row keeps its data and FKs; pull
    skips it and push refuses to re-push it. A rename is retire-old + push-new.
    --undo re-activates a section (also the sanctioned way back after an
    accidental retire).
    """
    import datetime as _dt

    config, sb = _client(config_path, local, service_role)
    from .cli import _gate_admin
    if not local:
        _gate_admin(config)

    table, pk = cs._TABLES[kind], cs._PK[kind]
    value = None if undo else _dt.datetime.now(_dt.timezone.utc).isoformat()
    done = []
    for name in names:
        resp = (sb.table(table).update({'retired_at': value})
                .eq(pk, name).execute())
        if resp.data:
            done.append(name)
            print(f"  {'~' if undo else '-'} {name} "
                  f"{'re-activated' if undo else 'retired'}")
        else:
            print(f"  ! {name}: not found in cloud {kind}")
    if done:
        log_deploy_event(
            sb, action='config_sync', actor=get_user_id_from_token(config),
            affected_count=len(done), host=socket.gethostname(),
            metadata={'scope': 'config_retire', 'kind': kind,
                      'names': done, 'undo': undo},
        )
    print(f"\nDone. {'Re-activated' if undo else 'Retired'} "
          f"{len(done)} of {len(names)} section(s).")
