"""
Remove (un-deploy) an observation's data from Supabase + R2.

Preserves the ``observations`` row and ``deployments`` history rows.
Wipes ``targets``, ``spectra``, ``shutters``, ``slit_regions`` for the
observation and the matching R2 prefixes (``spectra/<obs>/``,
``rgb/<obs>/``, ``sed/<obs>/``), then reconciles the ``objects`` table
for the affected field (preserving inspection state via id-reuse) and
refreshes materialized views.

Refuses to run if any target has user inspection data
(``redshift_quality > 0``, ``redshift_inspected`` set, non-zero
flags, or non-deleted comments) unless ``force=True``.

Comments and ``flag_audit_log`` rows attached to affected targets are
removed via the existing ``ON DELETE CASCADE`` foreign-key constraints.
"""

from supabase import Client

from campfire_layout import Scope, key_prefix
from campfire.deploy.supabase import (
    DeployScopeBusy,
    ScopeLease,
    heartbeat_deploy_scope,
    deploy_event_metadata,
    get_supabase_client,
    get_user_id_from_token,
    log_deploy_event,
    refresh_filter_options,
    refresh_programs_overview,
    refresh_observations_overview,
)


BATCH_SIZE = 500
PAGE_SIZE = 1000

# (display label, product_type) — the per-observation key prefixes hard-delete
# removes. The label keeps the report readable; the product_type drives the key.
R2_REMOVE_PRODUCTS = (('spectra', 'nirspec_spec'), ('rgb', 'rgb'), ('sed', 'sed'))

INSPECTION_FIELDS = (
    'target_id, id, redshift_quality, redshift_inspected, '
    'spectral_features, dq_flags, '
    'last_inspected_at, last_inspected_by'
)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _fetch_observation(sb: Client, obs_name: str) -> dict | None:
    resp = (sb.table('observations')
            .select('name, field, program_slug, latest_deployment_id')
            .eq('name', obs_name)
            .execute())
    return resp.data[0] if resp.data else None


def _fetch_targets(sb: Client, obs_name: str) -> list[dict]:
    """All targets for the observation with inspection-relevant fields."""
    targets: list[dict] = []
    offset = 0
    while True:
        resp = (sb.table('targets')
                .select(INSPECTION_FIELDS)
                .eq('observation', obs_name)
                .range(offset, offset + PAGE_SIZE - 1)
                .execute())
        if not resp.data:
            break
        targets.extend(resp.data)
        if len(resp.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return targets


def _count_spectra(sb: Client, target_ids: list[str]) -> int:
    if not target_ids:
        return 0
    total = 0
    for i in range(0, len(target_ids), BATCH_SIZE):
        batch = target_ids[i:i + BATCH_SIZE]
        resp = (sb.table('spectra')
                .select('id', count='exact')
                .in_('target_id', batch)
                .execute())
        total += resp.count or 0
    return total


def _count_by_obs(sb: Client, table: str, obs_name: str) -> int:
    resp = (sb.table(table)
            .select('id', count='exact')
            .eq('observation', obs_name)
            .execute())
    return resp.count or 0


def _count_comments(sb: Client, target_db_ids: list[int]) -> int:
    if not target_db_ids:
        return 0
    total = 0
    for i in range(0, len(target_db_ids), BATCH_SIZE):
        batch = target_db_ids[i:i + BATCH_SIZE]
        resp = (sb.table('comments')
                .select('id', count='exact')
                .in_('target_id', batch)
                .eq('is_deleted', False)
                .execute())
        total += resp.count or 0
    return total


def _is_inspected(row: dict) -> bool:
    return (
        (row.get('redshift_quality') or 0) > 0
        or row.get('redshift_inspected') is not None
        or (row.get('spectral_features') or 0) != 0
        or (row.get('dq_flags') or 0) != 0
    )


def _inspection_summary(row: dict) -> str:
    parts = []
    q = row.get('redshift_quality') or 0
    if q > 0:
        parts.append(f"quality={q}")
    zi = row.get('redshift_inspected')
    if zi is not None:
        parts.append(f"z={zi}")
    sf = row.get('spectral_features') or 0
    if sf:
        parts.append(f"spectral_features=0x{sf:x}")
    dq = row.get('dq_flags') or 0
    if dq:
        parts.append(f"dq_flags=0x{dq:x}")
    return ', '.join(parts)


# ---------------------------------------------------------------------------
# Delete helpers
# ---------------------------------------------------------------------------

def _delete_db_rows(sb: Client, obs_name: str, target_ids: list[str], check=lambda step: None) -> None:
    """Delete child rows, then targets; null latest_deployment_id.

    Order matters: spectra have a plain FK (no CASCADE) on targets.target_id,
    shutters/slit_regions have plain FKs on observations.name. Targets have
    CASCADE from comments.target_id and flag_audit_log.target_id, which
    take care of attached inspection artifacts automatically.

    *check(step)* runs before every request and raises to stop the delete
    (the removal's lease re-check, Codex on #602).
    """
    for i in range(0, len(target_ids), BATCH_SIZE):
        batch = target_ids[i:i + BATCH_SIZE]
        check(f'spectra batch {i // BATCH_SIZE + 1}')
        sb.table('spectra').delete().in_('target_id', batch).execute()

    check('shutters')
    sb.table('shutters').delete().eq('observation', obs_name).execute()
    check('slit regions')
    sb.table('slit_regions').delete().eq('observation', obs_name).execute()

    for i in range(0, len(target_ids), BATCH_SIZE):
        batch = target_ids[i:i + BATCH_SIZE]
        check(f'targets batch {i // BATCH_SIZE + 1}')
        sb.table('targets').delete().in_('target_id', batch).execute()

    check('the observation update')
    (sb.table('observations')
     .update({'latest_deployment_id': None})
     .eq('name', obs_name)
     .execute())


def _delete_r2_prefixes(config: dict, obs_name: str, check=lambda step: None) -> dict[str, int]:
    from campfire.deploy.backend import resolve_backend
    from campfire.deploy.r2 import get_r2_client
    from campfire.deploy.tiles import delete_r2_prefix

    client = get_r2_client(config)
    bucket = resolve_backend(config, 'data').bucket
    scope = Scope(obs=obs_name)
    counts: dict[str, int] = {}
    for label, product_type in R2_REMOVE_PRODUCTS:
        full = f"{key_prefix(product_type, scope)}/"
        # before EVERY delete page, not once per prefix (Codex on #602)
        counts[label] = delete_r2_prefix(client, bucket, full,
                                         before_page=lambda n, p=full: check(f'prefix {p} page {n}'))
    return counts


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def remove_observation(
    obs_name: str,
    config: dict,
    *,
    dry_run: bool = False,
    force: bool = False,
    supabase_only: bool = False,
    auto_approve: bool = False,
    skip_rebuild: bool = False,
) -> None:
    """Un-deploy ``obs_name``. See module docstring for semantics."""
    sb = get_supabase_client(config)

    obs = _fetch_observation(sb, obs_name)
    if obs is None:
        print(f"Observation '{obs_name}' not found. Nothing to do.")
        return

    # Data releases (P4d): a published release keeps its observations live,
    # and an unpublished one needs the live bytes until its byte pass has
    # archived them. The check runs UNDER the observation's deploy lease, held
    # until the deletes are done: begin_deploy_scope refuses while a freeze
    # runs, a live lease makes a freeze and dr_publish refuse, and both sides
    # take the same lock -- so no release can start depending on this
    # observation between the check and the deletes (Codex on #602). Fails
    # closed: without the lease or the check, nothing is deleted. A dry run
    # checks without leasing.
    lease = None
    if not dry_run:
        lease = ScopeLease(sb, 'observation', obs_name, actor=get_user_id_from_token(config))
        try:
            lease.begin()
        except DeployScopeBusy as e:
            print(f"\n  REFUSED: {e}")
            if e.holder:
                print("  " + e.describe().replace('\n', '\n  '))
            raise RemovalRefused(f"{obs_name}: {e}") from e
        if not lease.leased:
            print(f"\n  REFUSED: could not take the deploy lease on {obs_name}; "
                  f"a removal never runs without it.")
            raise RemovalRefused(f"{obs_name}: the deploy lease could not be taken")
    def still_held() -> bool:
        # Checked right before each destructive step (Codex on #602): a lease
        # stolen or taken over meanwhile means another deploy may be writing
        # this observation, so nothing more is deleted. Unknown = not held.
        if lease is None:
            return True
        return heartbeat_deploy_scope(sb, 'observation', obs_name, actor=lease.actor, host=lease.host,
                                      version=lease.version_at_start) is True
    try:
        removed = _remove_checked(sb, obs, obs_name, config, dry_run=dry_run, force=force,
                                  supabase_only=supabase_only, auto_approve=auto_approve,
                                  skip_rebuild=skip_rebuild, still_held=still_held)
        if lease is not None and removed:
            result = lease.claim()     # the data changed: bump the scope version
            if result.get('conflict'):
                raise RemovalLeaseLost(
                    f"another deploy took the lease on {obs_name} while it was being removed; "
                    f"re-check the observation (its rows or bytes may have been re-deployed)")
            if result.get('error') or not result.get('claimed'):
                # indeterminate (claim_deploy_scope turns RPC errors into a
                # nominal success): the version may not be bumped and the lease
                # may stay live until it goes stale (Codex on #602)
                raise RemovalLeaseLost(
                    f"{obs_name} was removed, but the final lease claim could not be confirmed "
                    f"({result.get('error') or 'not claimed'}); check `campfire dr status` / the deploy "
                    f"lease before deploying it again")
    finally:
        if lease is not None and lease.leased:
            lease.release()


class RemovalLeaseLost(RuntimeError):
    """The deploy lease of a removal was lost (stolen or taken over) mid-way."""


class RemovalRefused(RuntimeError):
    """A removal refused before deleting anything: a data release needs the
    observation, the release check could not run, or the deploy lease could
    not be taken. The CLI exits non-zero (Codex on #602: automation must not
    read a refused removal as a successful one)."""


def _release_blockers_refuse(sb, obs_name: str) -> bool:
    """True (after printing why) when a data release forbids removing *obs_name*."""
    from campfire.dr import format_references, observation_references
    try:
        refs = observation_references(sb, obs_name)
    except Exception as e:  # noqa: BLE001 - a missing RPC or an error both refuse
        print(f"\n  REFUSED: could not check the data releases that reference {obs_name} "
              f"({getattr(e, 'message', None) or e}).")
        return True
    if any(r.get('blocks') for r in refs):
        print()
        print("  REFUSED: " + format_references(obs_name, refs).replace('\n', '\n  '))
        print("  A published release is withdrawn with `campfire dr withdraw`; an unpublished one "
              "finishes `campfire dr bytes` or is purged with `campfire dr purge`.")
        return True
    return False


def _remove_checked(sb, obs: dict, obs_name: str, config: dict, *, dry_run: bool, force: bool,
                    supabase_only: bool, auto_approve: bool, skip_rebuild: bool,
                    still_held=lambda: True) -> bool:
    """The removal proper (the caller holds the lease). True once rows were deleted."""
    field = obs['field']
    if _release_blockers_refuse(sb, obs_name):
        raise RemovalRefused(f"{obs_name}: a data release still needs it (see above)")

    targets = _fetch_targets(sb, obs_name)
    target_ids = [t['target_id'] for t in targets]
    target_db_ids = [t['id'] for t in targets if t.get('id') is not None]

    n_spectra = _count_spectra(sb, target_ids)
    n_shutters = _count_by_obs(sb, 'shutters', obs_name)
    n_slits = _count_by_obs(sb, 'slit_regions', obs_name)
    n_comments = _count_comments(sb, target_db_ids)
    inspected = [t for t in targets if _is_inspected(t)]

    print(f"\n{'='*60}")
    print(f"Remove observation: {obs_name}")
    print(f"{'='*60}")
    print(f"  Field:             {field}")
    print(f"  Program:           {obs.get('program_slug', '?')}")
    print(f"  Targets:           {len(targets)}")
    print(f"  Spectra:           {n_spectra}")
    print(f"  Shutters:          {n_shutters}")
    print(f"  Slit regions:      {n_slits}")
    print(f"  Comments:          {n_comments}")
    print(f"  Inspected targets: {len(inspected)}")
    if obs.get('latest_deployment_id') is None:
        print(f"  Latest deployment: <unset>")
    else:
        print(f"  Latest deployment: id={obs['latest_deployment_id']} "
              f"(will be cleared; deployments history preserved)")

    if inspected and not force:
        print()
        print(f"  REFUSED: {len(inspected)} target(s) have user inspection data.")
        print(f"  Sample:")
        for row in inspected[:5]:
            print(f"    {row['target_id']}: {_inspection_summary(row)}")
        if len(inspected) > 5:
            print(f"    ... and {len(inspected) - 5} more")
        print(f"  Pass --force to proceed anyway.")
        return False

    if not targets and not n_shutters and not n_slits:
        print("\nNo data to remove.")
        return False

    if dry_run:
        print("\nDry run — no changes made.")
        return False

    if not auto_approve:
        summary = (
            f"{len(targets)} targets, {n_spectra} spectra, "
            f"{n_shutters} shutters, {n_slits} slit regions"
        )
        if n_comments:
            summary += f", {n_comments} comments"
        prompt = f"\nProceed with deleting {summary}"
        if not supabase_only:
            scope = Scope(obs=obs_name)
            prefixes = ', '.join(
                f"{key_prefix(pt, scope)}/" for _label, pt in R2_REMOVE_PRODUCTS
            )
            prompt += f"\n  + R2 prefixes: {prefixes}"
        prompt += "? [y/N] "
        response = input(prompt)
        if response.lower() != 'y':
            print("Aborted.")
            return False

    done: list[str] = []

    def check(step: str) -> None:
        # before EVERY destructive request (Codex on #602): a lease lost
        # mid-way stops the delete at the next batch
        if not still_held():
            pages = [d for d in done if d.startswith('prefix')]
            if not done:
                what = 'nothing was deleted'
            elif not step.startswith('prefix'):
                what = (f"deleted so far: {', '.join(done)}; stopped before {step}; "
                        f"the bucket prefixes were NOT deleted")
            else:
                what = (f"its rows were deleted; bucket pages deleted: {', '.join(pages) or 'none'}; "
                        f"stopped before bucket {step} (another deploy may be uploading it)")
            raise RemovalLeaseLost(f"the deploy lease on {obs_name} was lost during the removal: {what}")
        done.append(step)

    print(f"\nDeleting DB rows...")
    _delete_db_rows(sb, obs_name, target_ids, check=check)
    print(f"  Done.")

    n_r2_deleted = 0
    if not supabase_only:
        print(f"\nDeleting R2 prefixes...")
        counts = _delete_r2_prefixes(config, obs_name, check=check)
        for prefix, n in counts.items():
            print(f"  {prefix}/{obs_name}/: {n} object(s)")
        n_r2_deleted = sum(counts.values())

    # Audit the un-deploy (audit B3): a hard delete was previously silent — no
    # deploy_events row, so the ledger drifted from the bucket. Best-effort.
    log_deploy_event(
        sb, action='delete', actor=get_user_id_from_token(config),
        observation=obs_name, affected_count=n_spectra,
        metadata=deploy_event_metadata(
            'nirspec', observation=obs_name, items=len(targets),
            supabase_only=supabase_only,
            targets=len(targets), spectra=n_spectra, shutters=n_shutters,
            slit_regions=n_slits, comments=n_comments,
            r2_objects_deleted=n_r2_deleted))

    if not skip_rebuild:
        from campfire.deploy.reconcile import reconcile_field_objects
        print(f"\nReconciling objects for field '{field}'...")
        n_clusters, _stats, _changed_ids = reconcile_field_objects(
            sb, field, abort_on_split_merge=True,
        )
        print(f"  {n_clusters} clusters")

    print(f"\nRefreshing materialized views...")
    refresh_filter_options(sb)
    refresh_programs_overview(sb)
    refresh_observations_overview(sb)

    print(f"\nRemoved observation '{obs_name}'.")
    return True
