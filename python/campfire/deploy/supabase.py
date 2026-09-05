"""
Supabase database operations.

Handles upserts for programs, objects, and spectra tables, plus
slit geometry deployment and filter cache refresh.
"""

from supabase import create_client, Client
from supabase.client import ClientOptions

from campfire.auth._jwt import get_sub


def _make_user_client(url: str, anon_key: str, supabase_token: str) -> Client:
    """Build a Supabase client authenticated as a user (JWT) for RLS writes.

    The user JWT **must** be baked into the client options headers at
    construction. Authenticating via ``client.postgrest.auth(token)`` *after*
    construction is unreliable in supabase-py 2.x: ``Client.postgrest`` is a
    lazily-built, cached client constructed from ``options.headers``, and the
    GoTrue auth-state listener resets that cache (``self._postgrest = None``)
    and rewrites ``Authorization`` back to the anon key. When that race is
    lost the user JWT silently never reaches the wire — requests go out as
    role ``anon`` -> ``auth.uid()`` is NULL -> ``is_admin()`` is false -> admin
    writes (e.g. the ``observations`` upsert) fail intermittently with a 42501
    RLS error. Passing the token through ``options.headers`` makes every
    (re)build of the postgrest client carry the user JWT.
    """
    if not supabase_token:
        # A falsy token yields "Bearer None" — i.e. an effectively anon request.
        # Refuse rather than silently deploy as anon and fail later with 42501.
        raise ValueError(
            "Refusing to build a user Supabase client without a Supabase token "
            "(would authenticate as anon). Run 'campfire login' again."
        )
    options = ClientOptions(headers={"Authorization": f"Bearer {supabase_token}"})
    return create_client(url, anon_key, options=options)


class AutoRefreshClient:
    """Wraps a Supabase Client to auto-refresh the JWT before each operation.

    Deployments can run for hours, but Supabase JWTs expire after ~1 hour.
    This wrapper checks token expiry before every ``table()`` or ``rpc()``
    call and, when a refresh is needed, **rebuilds** the underlying client
    with the fresh token baked into its options headers (see
    ``_make_user_client`` for why a fresh build rather than
    ``postgrest.auth()``).
    """

    def __init__(self, url: str, anon_key: str, supabase_token: str, token_manager):
        self._url = url
        self._anon_key = anon_key
        self._token_manager = token_manager
        self._client = _make_user_client(url, anon_key, supabase_token)

    def _ensure_valid_token(self):
        # Decide on the Supabase JWT's OWN expiry, not the access token's, then
        # force a refresh and rebuild the client so the fresh token is actually
        # carried on the wire (a plain refresh leaves the old client in place).
        if self._token_manager and self._token_manager.supabase_token_needs_refresh():
            new_token = self._token_manager.force_refresh_supabase_token()
            if new_token:
                self._client = _make_user_client(self._url, self._anon_key, new_token)

    def table(self, *args, **kwargs):
        self._ensure_valid_token()
        return self._client.table(*args, **kwargs)

    def rpc(self, *args, **kwargs):
        self._ensure_valid_token()
        return self._client.rpc(*args, **kwargs)


def get_supabase_client(config: dict):
    """Create a Supabase client from deploy config.

    Dispatches on the auth mode resolved by ``load_config`` and tagged on
    ``config['supabase']['_auth_mode']`` (one of ``service_role``, ``local``,
    ``login``). When the tag is absent (e.g. a hand-built config in a test),
    the mode is inferred from which keys are present.

    - **service_role / local** — ``create_client(url, service_role_key)``.
      Bypasses RLS; no refresh needed.
    - **login** — user JWT from ``campfire login``. Operates through RLS. With
      a ``_token_manager`` present the client is wrapped in
      ``AutoRefreshClient`` so long-running deploys survive JWT expiry; the
      token is always baked into the client headers (never falls back to anon).
    """
    sb = config['supabase']
    url = sb['url']
    mode = sb.get('_auth_mode')

    # Infer the mode for configs that weren't tagged by load_config.
    if mode is None:
        if sb.get('service_role_key'):
            mode = 'service_role'
        elif sb.get('supabase_token') and sb.get('anon_key'):
            mode = 'login'

    if mode in ('service_role', 'local'):
        service_role_key = sb.get('service_role_key')
        if not service_role_key:
            raise ValueError(
                f"Auth mode '{mode}' requires a Supabase service_role_key."
            )
        return create_client(url, service_role_key)

    if mode == 'login':
        supabase_token = sb.get('supabase_token')
        anon_key = sb.get('anon_key')
        if not (supabase_token and anon_key):
            raise ValueError(
                "Incomplete login credentials (need supabase_token + anon_key). "
                "Run 'campfire login' again."
            )
        token_manager = sb.get('_token_manager')
        if token_manager:
            return AutoRefreshClient(url, anon_key, supabase_token, token_manager)
        # No manager (no auto-refresh) but still authenticated as the user —
        # _make_user_client bakes the JWT in, so this is never an anon client.
        return _make_user_client(url, anon_key, supabase_token)

    raise ValueError(
        "No Supabase credentials available. "
        "Run 'campfire login' to authenticate, or pass --local for a "
        "local Supabase instance."
    )


def get_user_id_from_token(config: dict) -> str | None:
    """Extract user_id (``sub`` claim) from the stored Supabase token."""
    token = config.get('supabase', {}).get('supabase_token')
    return get_sub(token) if token else None


def insert_deployment(
    client: Client,
    observation: str | None = None,
    deployed_by: str | None = None,
    *,
    field: str | None = None,
    cfpipe_version: str | None = None,
    jwst_version: str | None = None,
    crds_context: str | None = None,
    config_snapshot: dict | None = None,
    stuck_shutters: dict | None = None,
    reduced_at: str | None = None,
    n_targets: int | None = None,
    n_spectra: int | None = None,
    n_new_targets: int | None = None,
    force_overwrite: bool = False,
    source_ids_filter: list[int] | None = None,
    supabase_only: bool = False,
    status: str = 'published',
) -> int | None:
    """
    Insert a deployment record and return its ID.

    ``status`` is the deployment lifecycle (epic #210): 'published' for a normal
    deploy (stamps published_at=now), 'draft' for a draft / incomplete deploy.

    A deployment is anchored to EXACTLY ONE of a NIRSpec ``observation`` or a NIRCam
    ``field`` (epic #261; enforced by deployments_scope_check). NIRCam field deploys
    record the same provenance and drive the same draft->published->revoked lifecycle.

    The deployment record is ALWAYS written (it is the admin-review anchor for the
    draft lifecycle). On a service-role / `--local` deploy there is no user JWT, so
    ``deployed_by`` is NULL (the column is nullable as of B5); prod `login` deploys
    still record the real user. Returns the new id, or None only on insert failure.
    """
    if (observation is None) == (field is None):
        raise ValueError("insert_deployment requires exactly one of observation/field")
    data = {
        'observation': observation,
        'field': field,
        'deployed_by': deployed_by,  # may be NULL on service-role / local deploys
        'force_overwrite': force_overwrite,
        'supabase_only': supabase_only,
        'status': status,
    }
    # A normal (published) deploy stamps published_at so the lifecycle timeline is
    # complete without a separate publish step; drafts leave it NULL until
    # an admin publishes via set_deployment_status.
    if status == 'published':
        from datetime import datetime, timezone
        data['published_at'] = datetime.now(timezone.utc).isoformat()
    if cfpipe_version:
        data['cfpipe_version'] = cfpipe_version
    if jwst_version:
        data['jwst_version'] = jwst_version
    if crds_context:
        data['crds_context'] = crds_context
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


def get_lifecycle_status(client: Client) -> dict:
    """Return the target DB's intermediate-lifecycle capability (epic #210, B2).

    The marker the deploy CLI gates ``--in-prep`` on: it introspects the live
    catalog and returns ``{'enabled': bool, 'checks': {...}, 'version': int}``,
    enabled only when B1 (#217) is applied (deploy_status column + reader RPCs
    threaded with p_include_unpublished). Returns ``{'enabled': False, ...}`` when
    the RPC itself is missing (deploy code newer than the DB) so the caller can
    abort cleanly instead of crashing.
    """
    try:
        resp = client.rpc('get_lifecycle_status', {}).execute()
    except Exception as e:
        return {'enabled': False, 'error': f'get_lifecycle_status unavailable: {e}'}
    data = resp.data if resp.data is not None else {}
    if isinstance(data, dict):
        return data
    return {'enabled': False, 'error': 'unexpected get_lifecycle_status response'}


def deploy_event_metadata(
    instrument: str,
    *,
    field: str | None = None,
    observation: str | None = None,
    filters: list[str] | None = None,
    planned: int | None = None,
    succeeded: int | None = None,
    failed: int | None = None,
    skipped: int | None = None,
    items: int | None = None,
    draft: bool = False,
    supabase_only: bool = False,
    **extra,
) -> dict:
    """Build the normalized deploy_events metadata envelope (audit B5, Phase 3).

    One shape across every producer, so the consumer parses one thing:
        {instrument, scope:{field, observation, filters},
         counts:{planned, succeeded, failed, skipped, items},
         flags:{draft, partial, supabase_only}}
    ``partial`` is derived (failed > 0). Only non-None counts are included so a
    producer that doesn't track a given count leaves it absent rather than 0.
    ``extra`` folds in any producer-specific keys (e.g. exposures details).
    """
    scope = {}
    if field is not None:
        scope['field'] = field
    if observation is not None:
        scope['observation'] = observation
    if filters:
        scope['filters'] = filters

    counts = {}
    for k, v in (('planned', planned), ('succeeded', succeeded),
                 ('failed', failed), ('skipped', skipped), ('items', items)):
        if v is not None:
            counts[k] = v

    flags = {'draft': draft, 'partial': bool(failed)}
    if supabase_only:
        flags['supabase_only'] = True

    meta = {'instrument': instrument, 'scope': scope, 'counts': counts, 'flags': flags}
    meta.update(extra)
    return meta


def log_deploy_event(
    client: Client,
    *,
    action: str,
    actor: str | None = None,
    deployment_id: int | None = None,
    observation: str | None = None,
    field: str | None = None,
    affected_count: int | None = None,
    metadata: dict | None = None,
    host: str | None = None,
) -> str | None:
    """Append one row to the deploy_events audit log via the SECURITY DEFINER RPC
    (the only sanctioned write path — the table has no client INSERT policy).
    Returns the event id, or None on failure (audit is best-effort; never blocks
    the deploy)."""
    try:
        resp = client.rpc('log_deploy_event', {
            'p_action': action,
            'p_actor': actor,
            'p_deployment_id': deployment_id,
            'p_observation': observation,
            'p_field': field,
            'p_affected_count': affected_count,
            'p_metadata': metadata,
            'p_host': host,
        }).execute()
        return resp.data if resp.data else None
    except Exception as e:
        print(f"  Warning: could not write deploy_event ({action}): {e}")
        return None


def get_deploy_scope_version(client: Client, scope_type: str, scope_key: str) -> int:
    """The current optimistic-concurrency version of a deploy scope (0 if new).

    Read at the START of a deploy; passed back to claim_deploy_scope at finalize
    to detect a concurrent deploy of the same scope (epic #210, B4).
    """
    try:
        resp = client.rpc('get_deploy_scope_version', {
            'p_scope_type': scope_type, 'p_scope_key': scope_key,
        }).execute()
        return int(resp.data) if resp.data is not None else 0
    except Exception:
        # Multi-reducer detection is advisory; never block a deploy on it.
        return 0


def claim_deploy_scope(
    client: Client, scope_type: str, scope_key: str, expected_version: int,
    *, actor: str | None = None,
) -> dict:
    """Compare-and-set a deploy scope's version at finalize (epic #210, B4).

    Returns the RPC json: ``{'claimed': bool, 'conflict': bool, ...}``. A
    conflict (claimed=False) means another reducer deployed the same scope
    concurrently. Advisory — never raises into the deploy path.
    """
    try:
        resp = client.rpc('claim_deploy_scope', {
            'p_scope_type': scope_type, 'p_scope_key': scope_key,
            'p_expected_version': expected_version, 'p_actor': actor,
        }).execute()
        return resp.data if isinstance(resp.data, dict) else {'claimed': True, 'conflict': False}
    except Exception as e:
        return {'claimed': True, 'conflict': False, 'error': str(e)}


def get_latest_deployment_id(client: Client, observation: str) -> int | None:
    """The most recent deployment id for an observation (lifecycle anchor)."""
    resp = (client.table('deployments')
            .select('id')
            .eq('observation', observation)
            .order('id', desc=True)
            .limit(1)
            .execute())
    if resp.data:
        return resp.data[0]['id']
    return None


def get_latest_field_deployment_id(client: Client, field: str) -> int | None:
    """The most recent deployment id for a NIRCam field (epic #261 lifecycle anchor)."""
    resp = (client.table('deployments')
            .select('id')
            .eq('field', field)
            .order('id', desc=True)
            .limit(1)
            .execute())
    if resp.data:
        return resp.data[0]['id']
    return None


def get_field_deployment_ids(client: Client, field: str) -> list[int]:
    """ALL deployment ids for a NIRCam field, newest first (epic #261).

    publish/revoke act on a whole field, but a field's objects can be spread across
    several deployments (a ``--filter`` subset re-deploy re-points only that subset,
    leaving other filters on an earlier deployment). Flipping just the latest would
    partially publish — or, worse, leave part of a revoked field public — so the
    lifecycle transition flips every deployment the field has.
    """
    resp = (client.table('deployments')
            .select('id')
            .eq('field', field)
            .order('id', desc=True)
            .execute())
    return [r['id'] for r in (resp.data or [])]


def set_deployment_status(
    client: Client,
    deployment_id: int,
    to_status: str,
    *,
    actor: str | None = None,
    host: str | None = None,
) -> dict | None:
    """Transition a deployment's lifecycle (publish/revoke/draft) via the
    SECURITY DEFINER RPC: flips the deployment + its spectra + recomputes
    has_published_spectrum + writes audit rows, all server-side. Returns the RPC
    result json, or None on failure."""
    try:
        resp = client.rpc('set_deployment_status', {
            'p_deployment_id': deployment_id,
            'p_to': to_status,
            'p_actor': actor,
            'p_host': host,
        }).execute()
        return resp.data
    except Exception as e:
        print(f"  Warning: set_deployment_status({deployment_id} -> {to_status}) failed: {e}")
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
        if slug not in programs_config:
            # Refuse to invent a program from defaults: that silently creates
            # a junk private program (is_public=False, empty metadata) and is
            # what turns a slug/name mix-up into a cryptic RLS failure on the
            # observation insert. Callers should validate first; this is the
            # defense-in-depth backstop.
            known = ', '.join(sorted(programs_config)) or '(none)'
            raise ValueError(
                f"Program slug '{slug}' is not defined in programs.toml; "
                f"refusing to create a program from defaults. This usually "
                f"means a program name was used where a slug was expected. "
                f"Known slugs: {known}."
            )
        info = programs_config[slug]
        # Full config-sync row (#303): typed columns + the lossless section in
        # `config` jsonb + hash/stamp. load_programs injects 'slug' into each
        # section; strip it so `config` stays a faithful TOML mirror. A
        # section jsonb can't represent (bare TOML datetimes) drops only the
        # config mirror — this runs mid-deploy and must never fail the deploy.
        from .config_sync import find_unjsonable, program_config_row, program_typed_row
        section = {k: v for k, v in info.items() if k != 'slug'}
        bad = find_unjsonable(section)
        if bad:
            print(f"  Warning: programs.toml [{slug}] has bare TOML "
                  f"datetime(s) at {', '.join(bad)} — config not mirrored to "
                  f"cloud (quote as ISO strings to enable config sync)")
            data = program_typed_row(slug, section)
        else:
            data = program_config_row(slug, section)
        client.table('programs').upsert(data, on_conflict='slug').execute()
        print(f"  + {slug} ({data['program_name']})")
        if 'config_hash' in data:
            from .config_sync import record_synced
            record_synced('programs', {slug: data['config_hash']})


def upsert_observation(
    client: Client,
    obs_name: str,
    program_slug: str,
    jwst_program_id: int,
    field: str,
    file_globs: list[str] | None = None,
    gratings: list[str] | None = None,
    data_subdir: str | None = None,
    config_section: dict | None = None,
) -> None:
    """Upsert an observation record.

    ``config_section`` is the raw observations.toml section; when given it is
    mirrored losslessly into `config` jsonb with hash/stamp (#303), so stage
    overrides and config_groups survive the round trip. A section that is not
    JSON-representable (bare TOML datetimes) skips only the config columns —
    the typed upsert still lands and the deploy proceeds.
    """
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
    if config_section:
        from .config_sync import config_hash, find_unjsonable
        bad = find_unjsonable(config_section)
        if bad:
            print(f"  Warning: observations.toml [{obs_name}] has bare TOML "
                  f"datetime(s) at {', '.join(bad)} — config not mirrored to "
                  f"cloud (quote as ISO strings to enable config sync)")
        else:
            import datetime as _dt
            data['config'] = config_section
            data['config_hash'] = config_hash(config_section)
            data['config_updated_at'] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    client.table('observations').upsert(data, on_conflict='name').execute()
    if 'config_hash' in data:
        from .config_sync import record_synced
        record_synced('observations', {obs_name: data['config_hash']})


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
        batch_size: Records per batch

    Returns:
        Tuple of (number of objects upserted, list of new object_ids)
    """
    if not objects:
        return 0, []

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

        data = {
            'target_id': oid,
            'program_slug': obj['program_slug'],
            'observation': obj['observation'],
            'field': field,
            'ra': obj['ra'],
            'dec': obj['dec'],
            'redshift_auto': obj['redshift_best'],
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


def update_spectra_zfit_scalars(client: Client, spectra: list[dict]) -> int:
    """Update row-backed zfit scalars without touching spectrum metadata.

    ``deploy zfit`` replaces only the fit artifact, so a full spectra upsert
    would risk copying unrelated, stale ECSV metadata. Filtered updates keep
    that command narrow while ensuring the web UI cannot keep trusting the
    previous fit's row-backed values.
    """
    updated = 0
    for spectrum in spectra:
        patch = {
            'redshift_auto': spectrum['redshift_auto'],
            'chi2_min': spectrum['chi2_min'],
            'confidence': spectrum['confidence'],
        }
        response = (
            client.table('spectra')
            .update(patch)
            .eq('target_id', spectrum['target_id'])
            .eq('grating', spectrum['grating'])
            .select('id')
            .execute()
        )
        count = len(response.data or [])
        if count != 1:
            raise RuntimeError(
                'zfit scalar update matched '
                f"{count} spectra rows for {spectrum['target_id']}/{spectrum['grating']}"
            )
        updated += count
    return updated


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
        'deployed_by, cfpipe_version, jwst_version, crds_context'
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


def refresh_observations_overview(client: Client) -> None:
    """Refresh the observations overview materialized view (perf T1-5, #501).

    Backs get_observations_overview / get_observation_stats for published
    data; a nightly pg_cron job (refresh_all_matviews) is the backstop if a
    deploy is interrupted before this runs.
    """
    print("  Refreshing observations overview cache...")
    try:
        client.rpc('refresh_observations_overview').execute()
        print("  Done")
    except Exception as e:
        print(f"  Warning: Failed to refresh observations overview: {e}")
        print("  Run manually: SELECT refresh_observations_overview();")
