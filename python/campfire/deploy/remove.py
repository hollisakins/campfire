"""
Remove (un-deploy) an observation's data from Supabase + R2.

Preserves the ``observations`` row and ``deployments`` history rows.
Wipes ``targets``, ``spectra``, ``shutters``, ``slit_regions`` for the
observation and the matching R2 prefixes (``spectra/<obs>/``,
``rgb/<obs>/``, ``sed/<obs>/``), then rebuilds the ``objects`` table
for the affected field and refreshes materialized views.

Refuses to run if any target has user inspection data
(``redshift_quality > 0``, ``redshift_inspected`` set, non-zero
flags, or non-deleted comments) unless ``force=True``.

Comments and ``flag_audit_log`` rows attached to affected targets are
removed via the existing ``ON DELETE CASCADE`` foreign-key constraints.
"""

from supabase import Client

from campfire.deploy.supabase import (
    get_supabase_client,
    refresh_filter_options,
    refresh_programs_overview,
)


BATCH_SIZE = 500
PAGE_SIZE = 1000

R2_PREFIXES = ('spectra', 'rgb', 'sed')

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

def _delete_db_rows(sb: Client, obs_name: str, target_ids: list[str]) -> None:
    """Delete child rows, then targets; null latest_deployment_id.

    Order matters: spectra have a plain FK (no CASCADE) on targets.target_id,
    shutters/slit_regions have plain FKs on observations.name. Targets have
    CASCADE from comments.target_id and flag_audit_log.target_id, which
    take care of attached inspection artifacts automatically.
    """
    for i in range(0, len(target_ids), BATCH_SIZE):
        batch = target_ids[i:i + BATCH_SIZE]
        sb.table('spectra').delete().in_('target_id', batch).execute()

    sb.table('shutters').delete().eq('observation', obs_name).execute()
    sb.table('slit_regions').delete().eq('observation', obs_name).execute()

    for i in range(0, len(target_ids), BATCH_SIZE):
        batch = target_ids[i:i + BATCH_SIZE]
        sb.table('targets').delete().in_('target_id', batch).execute()

    (sb.table('observations')
     .update({'latest_deployment_id': None})
     .eq('name', obs_name)
     .execute())


def _delete_r2_prefixes(config: dict, obs_name: str) -> dict[str, int]:
    from campfire.deploy.r2 import get_r2_client
    from campfire.deploy.tiles import delete_r2_prefix

    client = get_r2_client(config)
    bucket = config['r2']['bucket_name']
    counts: dict[str, int] = {}
    for prefix in R2_PREFIXES:
        full = f"{prefix}/{obs_name}/"
        counts[prefix] = delete_r2_prefix(client, bucket, full)
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

    field = obs['field']
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
        return

    if not targets and not n_shutters and not n_slits:
        print("\nNo data to remove.")
        return

    if dry_run:
        print("\nDry run — no changes made.")
        return

    if not auto_approve:
        summary = (
            f"{len(targets)} targets, {n_spectra} spectra, "
            f"{n_shutters} shutters, {n_slits} slit regions"
        )
        if n_comments:
            summary += f", {n_comments} comments"
        prompt = f"\nProceed with deleting {summary}"
        if not supabase_only:
            prompt += (
                f"\n  + R2 prefixes: "
                f"spectra/{obs_name}/, rgb/{obs_name}/, sed/{obs_name}/"
            )
        prompt += "? [y/N] "
        response = input(prompt)
        if response.lower() != 'y':
            print("Aborted.")
            return

    print(f"\nDeleting DB rows...")
    _delete_db_rows(sb, obs_name, target_ids)
    print(f"  Done.")

    if not supabase_only:
        print(f"\nDeleting R2 prefixes...")
        counts = _delete_r2_prefixes(config, obs_name)
        for prefix, n in counts.items():
            print(f"  {prefix}/{obs_name}/: {n} object(s)")

    if not skip_rebuild:
        from campfire.deploy.objects import rebuild_field_objects
        print(f"\nRebuilding objects for field '{field}'...")
        n_obj, n_multi = rebuild_field_objects(sb, field)
        print(f"  {n_obj} objects ({n_multi} multi-target)")

    print(f"\nRefreshing materialized views...")
    refresh_filter_options(sb)
    refresh_programs_overview(sb)

    print(f"\nRemoved observation '{obs_name}'.")
