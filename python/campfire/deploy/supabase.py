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

    Requires user JWT authentication via ``campfire login``. The caller's
    Supabase-compatible JWT (from the OAuth device flow) and anon key are
    used to create a client that operates through RLS policies.

    When a ``TokenManager`` is available (from ``campfire login``), the
    returned client auto-refreshes the JWT before it expires so that
    long-running deployments don't fail with ``PGRST303 JWT expired``.

    The config dict should contain::

        config['supabase']['url']                  # always required
        config['supabase']['anon_key']             # from campfire login
        config['supabase']['supabase_token']       # from campfire login
        config['supabase']['_token_manager']       # optional, enables auto-refresh
    """
    url = config['supabase']['url']
    supabase_token = config['supabase'].get('supabase_token')
    anon_key = config['supabase'].get('anon_key')

    if supabase_token and anon_key:
        client = create_client(url, anon_key)
        client.postgrest.auth(supabase_token)

        token_manager = config['supabase'].get('_token_manager')
        if token_manager:
            return AutoRefreshClient(client, token_manager)
        return client

    raise ValueError(
        "No Supabase credentials available. "
        "Run 'campfire login' to authenticate."
    )


REDSHIFT_DRIFT_THRESHOLD = 0.03


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
    Return existing targets as a dict keyed by target_id.

    Each value contains inspection-relevant fields needed for the
    redshift drift check during upsert.
    """
    if not target_ids:
        return {}

    fields = 'target_id, redshift_auto, redshift_inspected, redshift_quality'
    existing = {}
    batch_size = 500
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        resp = client.table('targets').select(fields).in_('target_id', batch).execute()
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
    n_quality_reset = 0

    for obj in objects:
        oid = obj['object_id']
        is_existing = oid in existing
        has_sed = oid in objects_with_sed

        if is_existing and not force_overwrite:
            # Update pipeline fields only
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

            # Reset Secure quality if redshift_auto drifted and there's
            # no manual override anchoring the redshift
            old = existing[oid]
            if (
                old['redshift_quality'] == 4
                and old['redshift_inspected'] is None
                and old['redshift_auto'] is not None
                and obj['redshift_best'] is not None
                and abs(float(old['redshift_auto']) - float(obj['redshift_best'])) > REDSHIFT_DRIFT_THRESHOLD
            ):
                data['redshift_quality'] = 0
                data['last_inspected_at'] = None
                data['last_inspected_by'] = None
                n_quality_reset += 1
                print(f"    quality reset: {oid} z_auto {old['redshift_auto']:.4f} → {obj['redshift_best']:.4f}")

            update_records.append(data)
        elif is_existing and force_overwrite:
            # Reset everything
            data = {
                'target_id': oid,
                'program_slug': obj['program_slug'],
                'observation': obj['observation'],
                'field': field,
                'ra': obj['ra'],
                'dec': obj['dec'],
                'redshift_auto': obj['redshift_best'],
                'has_sed_plot': has_sed,
                'redshift_inspected': None,
                'redshift_quality': 0,
                'spectral_features': 0,

                'dq_flags': 0,
                'last_inspected_at': None,
                'last_inspected_by': None,
            }
            update_records.append(data)
        else:
            # New target
            data = {
                'target_id': oid,
                'program_slug': obj['program_slug'],
                'observation': obj['observation'],
                'field': field,
                'ra': obj['ra'],
                'dec': obj['dec'],
                'redshift_auto': obj['redshift_best'],
                'has_sed_plot': has_sed,
                'redshift_inspected': None,
                'redshift_quality': 0,
                'spectral_features': 0,

                'dq_flags': 0,
                'last_inspected_at': None,
                'last_inspected_by': None,
            }
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
    return len(objects), new_ids, n_quality_reset


def batch_upsert_spectra(
    client: Client,
    spectra: list[dict],
    batch_size: int = 50,
) -> int:
    """
    Upsert spectra in batches, keyed on the UNIQUE constraint (target_id, grating).

    Uses PostgreSQL ON CONFLICT (target_id, grating) for a single-pass upsert,
    eliminating the need to pre-fetch existing records.

    Note: batch_size is kept small (default 50) because each row includes
    inline SVG thumbnails that inflate payload size.

    Args:
        spectra: List of dicts from summary.get_spectra_records(), optionally
                 enriched with thumbnail_svg_fnu / thumbnail_svg_flambda.
        batch_size: Records per batch

    Returns:
        Number of spectra upserted
    """
    if not spectra:
        return 0

    for i in range(0, len(spectra), batch_size):
        batch = spectra[i:i + batch_size]
        client.table('spectra').upsert(
            batch, on_conflict='target_id,grating'
        ).execute()

    return len(spectra)


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


def propagate_crossmatches(
    client: Client,
    target_ids: list[str],
    batch_size: int = 500,
) -> int:
    """
    Check new targets against existing inspected cross-matches.

    For each new target (quality=0), calls the DB function to check if
    a nearby Secure (quality=4) target with matching redshift exists.
    If so, the new target is automatically marked Secure.

    Args:
        target_ids: String target_ids of newly inserted targets
        batch_size: Records per batch for ID lookups

    Returns:
        Number of targets auto-secured
    """
    if not target_ids:
        return 0

    # Batch-fetch integer IDs for the new targets
    id_map: dict[str, int] = {}
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        resp = client.table('targets').select('id, target_id').in_('target_id', batch).execute()
        for row in resp.data:
            id_map[row['target_id']] = row['id']

    total = 0
    for oid in target_ids:
        db_id = id_map.get(oid)
        if db_id is None:
            continue
        try:
            result = client.rpc('propagate_crossmatch_inspection', {
                'p_target_id': db_id,
            }).execute()
            total += result.data or 0
        except Exception as e:
            print(f"  Warning: cross-match check failed for {oid}: {e}")

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
