"""
Supabase database operations.

Handles upserts for programs, objects, and spectra tables, plus
slit geometry deployment and filter cache refresh.
"""

from supabase import create_client, Client


class AutoRefreshClient:
    """Wraps a Supabase Client to auto-refresh the JWT before each operation.

    Deployments can run for hours, but Supabase JWTs expire after ~1 hour.
    This wrapper checks token expiry before every ``table()`` or ``rpc()``
    call and transparently refreshes via the stored ``TokenManager``.
    """

    def __init__(self, client: Client, token_manager):
        self._client = client
        self._token_manager = token_manager

    def _ensure_valid_token(self):
        if self._token_manager and self._token_manager.needs_refresh():
            new_token = self._token_manager.get_supabase_token(auto_refresh=True)
            if new_token:
                self._client.postgrest.auth(new_token)

    def table(self, *args, **kwargs):
        self._ensure_valid_token()
        return self._client.table(*args, **kwargs)

    def rpc(self, *args, **kwargs):
        self._ensure_valid_token()
        return self._client.rpc(*args, **kwargs)


def get_supabase_client(config: dict):
    """Create a Supabase client from deploy config.

    Two authentication paths:

    1. **Service role** (``config['supabase']['service_role_key']``) — used
       for ``--local`` deploys and env-var-driven CI. Bypasses RLS; no
       refresh needed.
    2. **User JWT** (``config['supabase']['supabase_token']`` +
       ``anon_key``) — used for remote deploys authenticated via
       ``campfire login``. Operates through RLS policies. When a
       ``_token_manager`` is present, wraps the client in
       ``AutoRefreshClient`` so long-running deploys survive the ~1 hour
       JWT expiry.
    """
    url = config['supabase']['url']
    service_role_key = config['supabase'].get('service_role_key')
    supabase_token = config['supabase'].get('supabase_token')
    anon_key = config['supabase'].get('anon_key')

    if service_role_key:
        return create_client(url, service_role_key)

    if supabase_token and anon_key:
        client = create_client(url, anon_key)
        client.postgrest.auth(supabase_token)

        token_manager = config['supabase'].get('_token_manager')
        if token_manager:
            return AutoRefreshClient(client, token_manager)
        return client

    raise ValueError(
        "No Supabase credentials available. "
        "Run 'campfire login' to authenticate, or pass --local for a "
        "local Supabase instance."
    )


def get_user_id_from_token(config: dict) -> str | None:
    """Extract user_id (sub claim) from the stored Supabase token."""
    token = config.get('supabase', {}).get('supabase_token')
    if not token:
        return None
    try:
        import json
        import base64
        # Decode JWT payload (second segment) without verification
        payload_b64 = token.split('.')[1]
        # Add padding
        payload_b64 += '=' * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get('sub')
    except Exception:
        return None


def insert_deployment(
    client: Client,
    observation: str,
    deployed_by: str | None,
    *,
    cfpipe_version: str | None = None,
    jwst_version: str | None = None,
    crds_context: str | None = None,
    reduction_version: str | None = None,
    config_snapshot: dict | None = None,
    stuck_shutters: dict | None = None,
    reduced_at: str | None = None,
    n_targets: int | None = None,
    n_spectra: int | None = None,
    n_new_targets: int | None = None,
    force_overwrite: bool = False,
    source_ids_filter: list[int] | None = None,
    supabase_only: bool = False,
) -> int | None:
    """
    Insert a deployment record and return its ID.

    Returns None if the insert fails (e.g. deployed_by is not set).
    """
    if not deployed_by:
        print("  Warning: No user_id available, skipping deployment record")
        return None

    data = {
        'observation': observation,
        'deployed_by': deployed_by,
        'force_overwrite': force_overwrite,
        'supabase_only': supabase_only,
    }
    if cfpipe_version:
        data['cfpipe_version'] = cfpipe_version
    if jwst_version:
        data['jwst_version'] = jwst_version
    if crds_context:
        data['crds_context'] = crds_context
    if reduction_version:
        data['reduction_version'] = reduction_version
    if config_snapshot is not None:
        data['config_snapshot'] = config_snapshot
    if stuck_shutters is not None:
        data['stuck_shutters'] = stuck_shutters
    if reduced_at:
        data['reduced_at'] = reduced_at
    if n_targets is not None:
        data['n_targets'] = n_targets
    if n_spectra is not None:
        data['n_spectra'] = n_spectra
    if n_new_targets is not None:
        data['n_new_targets'] = n_new_targets
    if source_ids_filter:
        data['source_ids_filter'] = source_ids_filter

    resp = client.table('deployments').insert(data).execute()
    if resp.data and len(resp.data) > 0:
        return resp.data[0]['id']
    return None


def update_latest_deployment(
    client: Client,
    observation: str,
    deployment_id: int,
) -> None:
    """Update observations.latest_deployment_id after a successful deploy."""
    client.table('observations').update(
        {'latest_deployment_id': deployment_id}
    ).eq('name', observation).execute()


def check_existing_objects(client: Client, target_ids: list[str]) -> dict[str, dict]:
    """
    Return existing target_ids as a dict keyed by target_id.

    Phase D: targets carry no inspection state any more, so this collapses to a
    membership check used by batch_upsert_objects to route each row to the
    insert vs. update path.
    """
    if not target_ids:
        return {}

    existing = {}
    batch_size = 500
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        resp = client.table('targets').select('target_id').in_('target_id', batch).execute()
        for row in resp.data:
            existing[row['target_id']] = row
    return existing


def upsert_programs(
    client: Client,
    program_slugs: list[str],
    programs_config: dict[str, dict],
) -> None:
    """Upsert program records."""
    for slug in program_slugs:
        info = programs_config.get(slug, {})
        data = {
            'slug': slug,
            'program_name': info.get('program_name', slug),
            'pi_name': info.get('pi_name', ''),
            'description': info.get('description', ''),
            'is_public': info.get('is_public', False),
            'cycle': info.get('cycle'),
        }
        client.table('programs').upsert(data, on_conflict='slug').execute()
        print(f"  + {slug} ({data['program_name']})")


def upsert_observation(
    client: Client,
    obs_name: str,
    program_slug: str,
    jwst_program_id: int,
    field: str,
    file_globs: list[str] | None = None,
    gratings: list[str] | None = None,
    data_subdir: str | None = None,
) -> None:
    """Upsert an observation record."""
    data = {
        'name': obs_name,
        'program_slug': program_slug,
        'jwst_program_id': jwst_program_id,
        'field': field,
    }
    if file_globs is not None:
        data['file_globs'] = file_globs
    if gratings is not None:
        data['gratings'] = gratings
    if data_subdir is not None:
        data['data_subdir'] = data_subdir
    client.table('observations').upsert(data, on_conflict='name').execute()


def update_observation_pointings(
    client: Client,
    obs_name: str,
    pointings: list[dict],
) -> int:
    """Write the JSONB pointings array for an observation.

    Returns the number of rows updated (0 if no observation row matches).
    """
    response = (
        client.table('observations')
        .update({'pointings': pointings})
        .eq('name', obs_name)
        .execute()
    )
    return len(response.data or [])


def batch_upsert_objects(
    client: Client,
    objects: list[dict],
    field: str,
    force_overwrite: bool,
    objects_with_sed: set[str] | None = None,
    batch_size: int = 500,
) -> int:
    """
    Upsert objects in batches.

    Three branches:
      - New objects: full insert with defaults
      - Existing (normal): update pipeline fields only, preserve inspection data
      - Existing (force_overwrite): reset all fields including inspection data

    Args:
        objects: List of dicts from summary.get_unique_objects()
        field: Field name
        force_overwrite: Whether to reset inspection data
        objects_with_sed: Set of object_ids that have SED plots
        batch_size: Records per batch

    Returns:
        Tuple of (number of objects upserted, list of new object_ids)
    """
    if not objects:
        return 0, []
    if objects_with_sed is None:
        objects_with_sed = set()

    target_ids = [o['object_id'] for o in objects]
    existing = check_existing_objects(client, target_ids)

    new_records = []
    update_records = []

    # Phase D: targets are stateless provenance now. Only write pipeline-derived
    # fields here — inspection state lives on the parent object and is owned by
    # the user, never the deploy pipeline. force_overwrite is preserved as a
    # signal but no longer carries any field-level effect at the target level
    # (the legacy escape hatch wipes object inspection state in
    # rebuild_field_objects, not here).
    for obj in objects:
        oid = obj['object_id']
        is_existing = oid in existing
        has_sed = oid in objects_with_sed

        data = {
            'target_id': oid,
            'program_slug': obj['program_slug'],
            'observation': obj['observation'],
            'field': field,
            'ra': obj['ra'],
            'dec': obj['dec'],
            'redshift_auto': obj['redshift_best'],
            'has_sed_plot': has_sed,
        }

        if is_existing:
            update_records.append(data)
        else:
            new_records.append(data)

    # Batch insert new records
    for i in range(0, len(new_records), batch_size):
        batch = new_records[i:i + batch_size]
        client.table('targets').insert(batch).execute()

    # Batch upsert updates
    for i in range(0, len(update_records), batch_size):
        batch = update_records[i:i + batch_size]
        client.table('targets').upsert(batch, on_conflict='target_id').execute()

    new_ids = [r['target_id'] for r in new_records]
    return len(objects), new_ids, 0


def batch_upsert_spectra(
    client: Client,
    spectra: list[dict],
    batch_size: int = 100,
) -> tuple[int, set[tuple[str, str]]]:
    """
    Upsert spectra in batches, keyed on the UNIQUE constraint (target_id, grating).

    Uses PostgreSQL ON CONFLICT (target_id, grating) for a single-pass upsert,
    eliminating the need to pre-fetch existing records.

    Phase C: also returns the set of (target_id, grating) pairs whose
    `file_hash` differs from the existing DB row — i.e. spectra that were
    re-reduced or re-uploaded. `reconcile_field_objects()` uses this to set
    `staleness_reason='reprocessed'` on affected objects. New rows (no
    existing hash) are NOT included; their parent object instead picks up
    the membership-based staleness signal.

    Args:
        spectra: List of dicts from summary.get_spectra_records(), optionally
                 enriched with thumbnail_svg_fnu / thumbnail_svg_flambda.
        batch_size: Records per batch

    Returns:
        Tuple of (n_upserted, changed_hash_pairs).
    """
    if not spectra:
        return 0, set()

    new_hashes: dict[tuple[str, str], str | None] = {
        (s['target_id'], s['grating']): s.get('file_hash') for s in spectra
    }

    # Pre-fetch existing file_hashes for these (target_id, grating) pairs.
    # PostgREST can't filter on tuples, so we fetch by target_id IN (...) and
    # filter Python-side. Gratings per target are few (<6), so the over-fetch
    # is small.
    target_ids = sorted({tid for (tid, _) in new_hashes.keys()})
    existing_hashes: dict[tuple[str, str], str | None] = {}
    fetch_batch = 200
    for i in range(0, len(target_ids), fetch_batch):
        batch = target_ids[i:i + fetch_batch]
        resp = (
            client.table('spectra')
            .select('target_id, grating, file_hash')
            .in_('target_id', batch)
            .execute()
        )
        for row in resp.data or []:
            key = (row['target_id'], row['grating'])
            if key in new_hashes:
                existing_hashes[key] = row['file_hash']

    for i in range(0, len(spectra), batch_size):
        batch = spectra[i:i + batch_size]
        client.table('spectra').upsert(
            batch, on_conflict='target_id,grating'
        ).execute()

    changed: set[tuple[str, str]] = set()
    for key, new_hash in new_hashes.items():
        if key not in existing_hashes:
            # Brand-new row. Membership signal, not a reprocessing signal.
            continue
        old_hash = existing_hashes[key]
        # Flag any delta — including NULL→hash, which occurs on the first
        # upload after the file_hash field was added. Without this branch,
        # pre-hash-rollout rows are silently treated as clean on their first
        # re-upload, losing a real "data changed" signal.
        if old_hash != new_hash:
            changed.add(key)

    return len(spectra), changed


def recompute_target_aggregates(
    client: Client,
    target_ids: list[str],
    batch_size: int = 500,
) -> int:
    """
    Bulk-recompute max_snr and max_exposure_time on targets from spectra.

    Replaces the old per-row triggers which caused statement timeouts
    on large batch upserts.

    Args:
        target_ids: List of target_id strings to recompute
        batch_size: IDs per RPC call

    Returns:
        Number of targets updated
    """
    if not target_ids:
        return 0

    total = 0
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        result = client.rpc('recompute_target_aggregates', {
            'p_target_ids': batch,
        }).execute()
        total += result.data or 0

    return total


def update_has_sed_plot(
    client: Client,
    target_ids: set[str],
    batch_size: int = 500,
) -> int:
    """Set has_sed_plot = true for the given target IDs."""
    if not target_ids:
        return 0

    id_list = list(target_ids)
    for i in range(0, len(id_list), batch_size):
        batch = id_list[i:i + batch_size]
        client.table('targets').update({'has_sed_plot': True}).in_('target_id', batch).execute()

    return len(id_list)


def deploy_slits(
    client: Client,
    obs_name: str,
    slits_data: list[dict],
    batch_size: int = 500,
) -> int:
    """
    Deploy slit geometry: delete existing rows for the observation,
    then bulk insert new rows.
    """
    client.table('slit_regions').delete().eq('observation', obs_name).execute()

    total = 0
    for i in range(0, len(slits_data), batch_size):
        batch = slits_data[i:i + batch_size]
        client.table('slit_regions').insert(batch).execute()
        total += len(batch)

    return total


def deploy_shutters(
    client: Client,
    obs_name: str,
    shutters_data: list[dict],
    batch_size: int = 500,
) -> int:
    """
    Deploy shutters: delete existing rows for the observation,
    then bulk insert new rows.
    """
    client.table('shutters').delete().eq('observation', obs_name).execute()

    total = 0
    for i in range(0, len(shutters_data), batch_size):
        batch = shutters_data[i:i + batch_size]
        client.table('shutters').insert(batch).execute()
        total += len(batch)

    return total


def fetch_deployment_config(client: Client, obs_name: str) -> dict | None:
    """
    Fetch observation metadata and latest deployment for config reconstruction.

    Returns a dict with 'observation' and 'deployment' keys, or None if
    the observation is not found or has no deployment record.
    """
    # Query observation
    obs_resp = client.table('observations').select(
        'name, program_slug, jwst_program_id, field, '
        'file_globs, gratings, data_subdir, latest_deployment_id'
    ).eq('name', obs_name).execute()

    if not obs_resp.data:
        return None

    obs_row = obs_resp.data[0]
    dep_id = obs_row.get('latest_deployment_id')
    if not dep_id:
        return None

    # Query latest deployment
    dep_resp = client.table('deployments').select(
        'id, config_snapshot, stuck_shutters, deployed_at, reduced_at, '
        'deployed_by, cfpipe_version, jwst_version, crds_context, reduction_version'
    ).eq('id', dep_id).execute()

    if not dep_resp.data:
        return None

    return {
        'observation': {
            'name': obs_row['name'],
            'program_slug': obs_row['program_slug'],
            'field': obs_row['field'],
            'file_globs': obs_row.get('file_globs', []),
            'gratings': obs_row.get('gratings', []),
            'data_subdir': obs_row.get('data_subdir'),
        },
        'deployment': dep_resp.data[0],
    }


def refresh_filter_options(client: Client) -> None:
    """Refresh the filter options materialized view."""
    print("  Refreshing filter options cache...")
    try:
        client.rpc('refresh_filter_options').execute()
        print("  Done")
    except Exception as e:
        print(f"  Warning: Failed to refresh filter options: {e}")
        print("  Run manually: SELECT refresh_filter_options();")


def refresh_programs_overview(client: Client) -> None:
    """Refresh the programs overview materialized view."""
    print("  Refreshing programs overview cache...")
    try:
        client.rpc('refresh_programs_overview').execute()
        print("  Done")
    except Exception as e:
        print(f"  Warning: Failed to refresh programs overview: {e}")
        print("  Run manually: SELECT refresh_programs_overview();")
