"""
Supabase database operations.

Handles upserts for programs, objects, and spectra tables, plus
slit geometry deployment and filter cache refresh.
"""

import threading

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
        if isinstance(resp.data, dict):
            return resp.data
        # nominal success for the advisory deploy path, flagged so a caller
        # that needs a confirmed claim (deploy remove) fails closed (Codex on #602)
        return {'claimed': True, 'conflict': False, 'error': f'unexpected claim response: {resp.data!r}'}
    except Exception as e:
        return {'claimed': True, 'conflict': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Deploy lease (data releases P1): a running deploy is visible while it runs.
# ---------------------------------------------------------------------------
# begin_deploy_scope takes the lease on a scope at the start of a deploy and
# refuses (DeployScopeBusy) while another deploy holds one with a heartbeat
# younger than 30 minutes, unless told to steal it; heartbeat_deploy_scope is
# fired by ScopeLease's background timer every LEASE_HEARTBEAT_INTERVAL_S for
# as long as the lease is held (and, as a second cadence, from the upload loop's
# registration_flusher); claim_deploy_scope clears the lease at finalize;
# release_deploy_scope clears it on a clean abort. A lease older than 30
# minutes with no heartbeat is stale and is taken over without stealing. The
# version begin returns is the ownership token: every begin bumps it, and the
# heartbeat / release / claim of a displaced deploy (stolen or taken over)
# carry an older one and fail, so the displaced process can never clear or
# finalize over the new holder. Only the busy refusal is a hard stop: every other failure
# is advisory, like the CAS before it, so an older database or a flaky network
# never blocks a deploy.

DEPLOY_SCOPE_BUSY_SQLSTATE = 'CF409'


class DeployScopeBusy(RuntimeError):
    """``begin_deploy_scope`` refused: another deploy holds a live lease."""

    def __init__(self, scope_type: str, scope_key: str, message: str,
                 holder: dict | None = None, hint: str | None = None):
        super().__init__(message)
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.holder = holder or {}
        self.hint = hint

    def describe(self) -> str:
        """Operator-facing multi-line description (who / where / since / heartbeat)."""
        h = self.holder
        who = h.get('in_progress_by') or 'service-role'
        lines = [
            f"Another deploy of {self.scope_type} '{self.scope_key}' is in progress.",
            f"  by:        {who}",
            f"  host:      {h.get('in_progress_host') or 'unknown'}",
            f"  since:     {h.get('in_progress_since') or '?'}",
            f"  heartbeat: {h.get('heartbeat_at') or '?'}",
            "Wait for it to finish (a lease with no heartbeat for 30 minutes is taken "
            "over automatically), or re-run with --steal-lease if you are certain it is dead.",
        ]
        return '\n'.join(lines)


class DeployScopeUnavailable(DeployScopeBusy):
    """``begin_deploy_scope`` failed for another reason (an RPC or network
    error): the lease could not be taken. A deploy never runs without it,
    because the lease is what keeps a data-release freeze from snapshotting a
    deploy half-way (Codex on #599); the CLI exits 1 like a busy scope."""

    def describe(self) -> str:
        return (f"Could not take the deploy lease on {self.scope_type} '{self.scope_key}' "
                f"({self}). A deploy never runs without it; retry shortly.")


def deploy_host() -> str | None:
    """The hostname recorded on the lease (best-effort)."""
    try:
        import socket
        return socket.gethostname() or None
    except Exception:
        return None


def _busy_error(exc: Exception) -> tuple[str, dict, str | None] | None:
    """(message, holder, hint) when *exc* is the busy refusal, else None.

    Duck-typed on the PostgREST APIError shape (code / message / details /
    hint attributes) so it needs no postgrest import and works with any
    client that surfaces the SQLSTATE and message.
    """
    code = getattr(exc, 'code', None)
    message = getattr(exc, 'message', None) or str(exc)
    if code != DEPLOY_SCOPE_BUSY_SQLSTATE and 'deploy_scope_busy' not in str(message):
        return None
    holder: dict = {}
    details = getattr(exc, 'details', None)
    if details:
        try:
            import json as _json
            parsed = _json.loads(details) if isinstance(details, str) else details
            if isinstance(parsed, dict):
                holder = parsed
        except Exception:
            holder = {}
    return str(message), holder, getattr(exc, 'hint', None)


def begin_deploy_scope(
    client: Client, scope_type: str, scope_key: str, *,
    actor: str | None = None, host: str | None = None, steal: bool = False,
) -> dict:
    """Take the deploy lease on a scope at the start of a deploy.

    Returns the RPC json ``{'version', 'took_over_stale', 'stolen', 'previous',
    'leased': True}`` — ``version`` is the CAS value to pass back to
    ``claim_deploy_scope`` at finalize. Raises :class:`DeployScopeBusy` when
    another deploy holds a live lease and *steal* is False. Any other failure
    (RPC missing on an older database, network) returns ``{'version':
    get_deploy_scope_version(...), 'leased': False, 'error': ...}``, which
    :meth:`ScopeLease.begin` turns into :class:`DeployScopeUnavailable`: no
    deploy runs without its lease.
    """
    try:
        resp = client.rpc('begin_deploy_scope', {
            'p_scope_type': scope_type, 'p_scope_key': scope_key,
            'p_actor': actor, 'p_host': host, 'p_steal': bool(steal),
        }).execute()
    except Exception as e:
        busy = _busy_error(e)
        if busy is not None:
            message, holder, hint = busy
            raise DeployScopeBusy(scope_type, scope_key, message, holder, hint) from e
        return {'version': get_deploy_scope_version(client, scope_type, scope_key),
                'leased': False, 'error': str(e)}
    data = resp.data if isinstance(resp.data, dict) else {}
    version = data.get('version')
    return {
        'version': int(version) if version is not None else 0,
        'took_over_stale': bool(data.get('took_over_stale')),
        'stolen': bool(data.get('stolen')),
        'previous': data.get('previous'),
        'leased': True,
    }


def heartbeat_deploy_scope(
    client: Client, scope_type: str, scope_key: str, *,
    actor: str | None = None, host: str | None = None, version: int | None = None,
) -> bool | None:
    """Refresh the lease heartbeat. True = written, False = the lease is no
    longer ours (stolen / taken over / released), None = unknown (RPC failed).
    *version* is the ownership token ``begin_deploy_scope`` returned: a
    displaced deploy's older token is refused even from the same actor and
    host."""
    try:
        resp = client.rpc('heartbeat_deploy_scope', {
            'p_scope_type': scope_type, 'p_scope_key': scope_key,
            'p_actor': actor, 'p_host': host, 'p_version': version,
        }).execute()
        return bool(resp.data)
    except Exception:
        return None


def release_deploy_scope(
    client: Client, scope_type: str, scope_key: str, *,
    actor: str | None = None, host: str | None = None, version: int | None = None,
) -> bool:
    """Clear the lease without a version bump (clean abort). Never raises.
    Same holder rule as the heartbeat (actor, host, version token)."""
    try:
        resp = client.rpc('release_deploy_scope', {
            'p_scope_type': scope_type, 'p_scope_key': scope_key,
            'p_actor': actor, 'p_host': host, 'p_version': version,
        }).execute()
        return bool(resp.data)
    except Exception:
        return False


# Leases this process currently holds (begin'd, neither claimed nor released).
# The deploy CLI releases them when a deploy flow exits abnormally (exception,
# Ctrl-C, sys.exit) so a cleanly aborted deploy does not leave a 30-minute
# lease behind; a hard crash cannot release and goes stale instead.
_ACTIVE_LEASES: list['ScopeLease'] = []

# Background heartbeat cadence (seconds): 5 minutes against a 30-minute ttl,
# independent of upload completions — one multi-GB mosaic on a slow link, or a
# long planning / reconciliation phase, must not let a live deploy go stale.
LEASE_HEARTBEAT_INTERVAL_S = 300.0


class ScopeLease:
    """One deploy's lease on one scope: begin → heartbeat* → claim | release.

    Thin stateful wrapper over the four RPCs so the deploy flows thread one
    object (``lease.heartbeat`` into the registration flusher, ``lease.claim()``
    at finalize) instead of five arguments. A successful ``begin()`` also
    starts a daemon timer thread that heartbeats every
    ``heartbeat_interval_s`` until ``claim()`` / ``release()``; the flusher
    hook is the second, progress-driven cadence.
    """

    def __init__(self, client: Client, scope_type: str, scope_key: str, *,
                 actor: str | None = None, host: str | None = None, steal: bool = False,
                 heartbeat_interval_s: float | None = LEASE_HEARTBEAT_INTERVAL_S):
        self.client = client
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.actor = actor
        self.host = host if host is not None else deploy_host()
        self.steal = steal
        self.heartbeat_interval_s = heartbeat_interval_s
        self.version_at_start: int = 0
        self.leased = False
        self.lost_warned = False
        self.heartbeats = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None

    def begin(self) -> int:
        """Take the lease; returns the scope version to CAS at finalize.
        Raises DeployScopeBusy (see begin_deploy_scope), or
        DeployScopeUnavailable when the lease could not be taken at all: no
        deploy runs unleased (fail closed; Codex on #599)."""
        info = begin_deploy_scope(
            self.client, self.scope_type, self.scope_key,
            actor=self.actor, host=self.host, steal=self.steal)
        self.version_at_start = int(info.get('version') or 0)
        self.leased = bool(info.get('leased'))
        if self.leased:
            _ACTIVE_LEASES.append(self)
            self._start_timer()
            prev = info.get('previous') or {}
            if info.get('stolen'):
                print(f"⚠  Stole the deploy lease for {self.scope_type} '{self.scope_key}' from "
                      f"{prev.get('in_progress_by') or 'service-role'} on "
                      f"{prev.get('in_progress_host') or 'unknown host'} (last heartbeat "
                      f"{prev.get('heartbeat_at') or '?'}). If that deploy is still running, "
                      f"its writes will interleave with this one.")
            elif info.get('took_over_stale'):
                print(f"  Took over a stale deploy lease for {self.scope_type} '{self.scope_key}' "
                      f"(held by {prev.get('in_progress_by') or 'service-role'} on "
                      f"{prev.get('in_progress_host') or 'unknown host'}, last heartbeat "
                      f"{prev.get('heartbeat_at') or '?'}) — that deploy is presumed dead.")
        else:
            raise DeployScopeUnavailable(self.scope_type, self.scope_key,
                                         str(info.get('error') or 'begin_deploy_scope returned no lease'))
        return self.version_at_start

    def heartbeat(self) -> None:
        """Best-effort heartbeat; warns once if the lease is no longer ours.
        Called from the timer thread and from the upload loop; serialized by a
        lock so the two cadences never overlap."""
        if not self.leased:
            return
        with self._lock:
            if not self.leased:
                return
            ok = heartbeat_deploy_scope(
                self.client, self.scope_type, self.scope_key,
                actor=self.actor, host=self.host, version=self.version_at_start)
            if ok:
                self.heartbeats += 1
            elif ok is False and not self.lost_warned:
                self.lost_warned = True
                print(f"⚠  Deploy lease for {self.scope_type} '{self.scope_key}' is no longer held by "
                      f"this process (stolen, taken over as stale, or another deploy finalized). "
                      f"Another deploy of the same scope may be running; re-check the result.")

    def claim(self) -> dict:
        """Finalize: the version CAS (clears the lease on success)."""
        self._forget()
        return claim_deploy_scope(
            self.client, self.scope_type, self.scope_key, self.version_at_start, actor=self.actor)

    def release(self) -> bool:
        """Clean abort: clear the lease without a version bump."""
        if not self.leased:
            return False
        self._forget()
        return release_deploy_scope(
            self.client, self.scope_type, self.scope_key,
            actor=self.actor, host=self.host, version=self.version_at_start)

    # -- background timer -------------------------------------------------
    def _start_timer(self) -> None:
        if not self.heartbeat_interval_s or self.heartbeat_interval_s <= 0:
            return
        self._stop.clear()
        self._timer = threading.Thread(
            target=self._timer_loop, name=f"lease-heartbeat:{self.scope_key}", daemon=True)
        self._timer.start()

    def _timer_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_s):
            if not self.leased:
                break
            try:
                self.heartbeat()
            except Exception:
                pass  # best-effort by contract; the next tick tries again

    def _forget(self) -> None:
        """Stop heartbeating (timer joined, so no beat lands after the claim
        or release RPC) and drop the lease from the abort registry."""
        self.leased = False
        self._stop.set()
        timer, self._timer = self._timer, None
        if timer is not None and timer is not threading.current_thread():
            timer.join(timeout=30)
        try:
            _ACTIVE_LEASES.remove(self)
        except ValueError:
            pass


def release_active_leases() -> int:
    """Release every lease this process still holds (abnormal exit path)."""
    n = 0
    for lease in list(_ACTIVE_LEASES):
        if lease.release():
            n += 1
    return n


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


class ProgramVisibilityError(ValueError):
    """A deploy would flip a public program private (P0a guard)."""


FORCE_PRIVATE_FLIP_ENV = 'CAMPFIRE_ALLOW_PROGRAM_PRIVATE_FLIP'


def _refuse_private_flip(client: Client, slug: str, data: dict, *, force: bool) -> bool:
    """Public -> private is a withdrawal, never a side effect of a deploy.

    If the local section says ``is_public = false`` while the cloud row is
    public, stop before anything is written (upsert_programs runs first in a
    deploy, so nothing else has landed yet). ``force`` (or the
    CAMPFIRE_ALLOW_PROGRAM_PRIVATE_FLIP=1 environment variable) lets an admin
    do it deliberately; the caller records the flip as a config_sync event
    once the write has succeeded. A section that omits the key never reaches
    here (program_typed_row leaves is_public out of the row, so the cloud
    value is kept).

    When the cloud row exists and is already private, ``is_public`` is dropped
    from the row: writing ``false`` would be a no-op now, but keeping it out
    means a publish that lands between this check and the upsert can never be
    undone by it (the check and the write are two PostgREST requests). A row
    that does not exist yet keeps the stated value.

    Returns True when a forced public -> private flip is about to be written.
    """
    if data.get('is_public') is not False:
        return False
    resp = (client.table('programs').select('slug, is_public')
            .in_('slug', [slug]).execute())
    rows = getattr(resp, 'data', None) or []
    cloud = rows[0] if rows else None
    if not cloud:
        return False
    if not cloud.get('is_public'):
        data.pop('is_public', None)
        return False
    import os
    if force or os.environ.get(FORCE_PRIVATE_FLIP_ENV) == '1':
        print(f"  Warning: making program '{slug}' PRIVATE (was public) because "
              f"programs.toml says is_public = false and the flip was forced.")
        return True
    raise ProgramVisibilityError(
        f"Refusing to make program '{slug}' private: the cloud row is public but "
        f"programs.toml says is_public = false. Public -> private is a withdrawal, "
        f"not a deploy side effect. Set is_public = true in programs.toml (or drop "
        f"the key to leave the cloud value alone); to withdraw on purpose, re-run "
        f"with {FORCE_PRIVATE_FLIP_ENV}=1."
    )


def upsert_programs(
    client: Client,
    program_slugs: list[str],
    programs_config: dict[str, dict],
    *,
    force_visibility: bool = False,
) -> None:
    """Upsert program records.

    Visibility guard (P0a): ``is_public`` is written only when the TOML
    section states it, and a public -> private flip is refused unless forced
    (see :func:`_refuse_private_flip`).
    """
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
        forced_flip = _refuse_private_flip(client, slug, data, force=force_visibility)
        client.table('programs').upsert(data, on_conflict='slug').execute()
        if forced_flip:
            # Audit after the write: an event for a flip that then failed
            # would record a withdrawal that never happened.
            log_deploy_event(client, action='config_sync',
                             metadata={'kind': 'program_visibility',
                                       'slug': slug, 'from': True, 'to': False,
                                       'forced': True})
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
    spectrum CONTENT differs from the existing DB row — i.e. spectra that were
    re-reduced. (Formerly drove a 'reprocessed' review badge; since P3 the
    needs-review badge is membership-only and trigger-maintained, so this set
    is informational.) New rows (no
    existing hash) are NOT included; their parent object instead picks up
    the membership-based staleness signal.

    "Changed" (P0c, docs/design-data-releases-impl-plan.md §2) is decided on
    the science hash when BOTH the existing row and the new record carry one:
    a rerun that only rewrote the volatile header cards (CMPFRTIM, the DR
    stamp) moves `file_hash` but not `science_hash`, and must not flag the
    object. When either side lacks a science hash (rows that predate P0b, a
    summary without the column) the whole-file hash decides, as before.

    Args:
        spectra: List of dicts from summary.get_spectra_records(), optionally
                 enriched with thumbnail_svg_fnu / thumbnail_svg_flambda.
        batch_size: Records per batch

    Returns:
        Tuple of (n_upserted, changed_hash_pairs).
    """
    if not spectra:
        return 0, set()

    # (file_hash, science_hash) per incoming record.
    new_hashes: dict[tuple[str, str], tuple[str | None, str | None]] = {
        (s['target_id'], s['grating']): (s.get('file_hash'), s.get('science_hash'))
        for s in spectra
    }

    # Pre-fetch the existing hashes for these (target_id, grating) pairs.
    # PostgREST can't filter on tuples, so we fetch by target_id IN (...) and
    # filter Python-side. Gratings per target are few (<6), so the over-fetch
    # is small.
    target_ids = sorted({tid for (tid, _) in new_hashes.keys()})
    existing_hashes: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    fetch_batch = 200
    for i in range(0, len(target_ids), fetch_batch):
        batch = target_ids[i:i + fetch_batch]
        resp = (
            client.table('spectra')
            .select('target_id, grating, file_hash, science_hash')
            .in_('target_id', batch)
            .execute()
        )
        for row in resp.data or []:
            key = (row['target_id'], row['grating'])
            if key in new_hashes:
                existing_hashes[key] = (row.get('file_hash'), row.get('science_hash'))

    for i in range(0, len(spectra), batch_size):
        batch = spectra[i:i + batch_size]
        client.table('spectra').upsert(
            batch, on_conflict='target_id,grating'
        ).execute()

    changed: set[tuple[str, str]] = set()
    for key, new_pair in new_hashes.items():
        if key not in existing_hashes:
            # Brand-new row. Membership signal, not a reprocessing signal.
            continue
        if spectrum_content_changed(existing_hashes[key], new_pair):
            changed.add(key)

    return len(spectra), changed


def spectrum_content_changed(
    old: tuple[str | None, str | None],
    new: tuple[str | None, str | None],
) -> bool:
    """The one rule for "this spectrum's content changed" between two rows.

    ``old`` / ``new`` are ``(file_hash, science_hash)`` pairs. When both sides
    carry a science hash, it alone decides (P0c): equal science hashes mean
    the same science even if the file bytes differ. Otherwise the whole-file
    hash decides, and any delta counts — including NULL→hash, which occurs on
    the first upload after a hash column was added; without that branch,
    pre-rollout rows are silently treated as clean on their first re-upload,
    losing a real "data changed" signal.
    """
    old_file, old_sci = old
    new_file, new_sci = new
    if old_sci and new_sci:
        return old_sci != new_sci
    return old_file != new_file


def fetch_spectrum_ids(client: Client, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
    """``{(target_id, grating): spectra.id}`` for the given pairs (missing pairs absent).

    PostgREST cannot filter on tuples: fetch by target_id IN (...) and filter
    Python-side (gratings per target are few).
    """
    wanted = set(pairs)
    target_ids = sorted({tid for (tid, _) in wanted})
    out: dict[tuple[str, str], int] = {}
    fetch_batch = 200
    for i in range(0, len(target_ids), fetch_batch):
        resp = (
            client.table('spectra')
            .select('id, target_id, grating')
            .in_('target_id', target_ids[i:i + fetch_batch])
            .execute()
        )
        for row in resp.data or []:
            key = (row['target_id'], row['grating'])
            if key in wanted:
                out[key] = int(row['id'])
    return out


def batch_upsert_line_fits(client: Client, rows: list[dict], batch_size: int = 100) -> int:
    """Upsert ``line_fits`` rows keyed on ``spectrum_id`` (one per spectrum).

    A re-fit replaces the whole row — the ``lines`` jsonb included — so a line
    dropped from the catalog never lingers from an older fit. ``updated_at``
    is bumped by trigger so the sync client's incremental cursor sees it.
    """
    if not rows:
        return 0
    for i in range(0, len(rows), batch_size):
        client.table('line_fits').upsert(
            rows[i:i + batch_size], on_conflict='spectrum_id'
        ).execute()
    return len(rows)


def update_spectra_zfit_scalars(
    client: Client,
    spectra: list[dict],
) -> int:
    """Update row-backed zfit scalars without touching spectrum metadata.

    ``deploy zfit`` replaces only the fit artifact, so a full spectra upsert
    would risk copying unrelated, stale ECSV metadata. Each (target_id,
    grating) row gets a plain UPDATE of just the three scalar columns, which
    also fires ``bump_spectra_updated_at`` so incremental sync sees the row.

    Not an upsert: PostgREST would send ``INSERT ... ON CONFLICT DO UPDATE``
    with only these columns, and Postgres checks NOT NULL (fits_path,
    program_slug, observation) on the proposed row *before* the conflict is
    resolved, so a partial upsert fails even when the row exists.

    Every (target_id, grating) pair must already exist: a standalone re-fit
    never creates spectra rows, so a missing one means the ECSV and the
    catalog disagree, and nothing is written.
    """
    if not spectra:
        return 0

    wanted = {(s['target_id'], s['grating']) for s in spectra}
    existing: set[tuple[str, str]] = set()
    target_ids = sorted({tid for (tid, _) in wanted})
    fetch_batch = 200
    for i in range(0, len(target_ids), fetch_batch):
        resp = (
            client.table('spectra')
            .select('target_id, grating')
            .in_('target_id', target_ids[i:i + fetch_batch])
            .execute()
        )
        existing.update((row['target_id'], row['grating']) for row in resp.data or [])
    missing = sorted(wanted - existing)
    if missing:
        shown = ', '.join(f'{tid}/{grating}' for tid, grating in missing[:5])
        more = f' (+{len(missing) - 5} more)' if len(missing) > 5 else ''
        raise RuntimeError(
            f'zfit scalar update matched 0 spectra rows for {shown}{more}'
        )

    for s in spectra:
        (
            client.table('spectra')
            .update({
                'redshift_auto': s['redshift_auto'],
                'chi2_min': s['chi2_min'],
                'confidence': s['confidence'],
            })
            .eq('target_id', s['target_id'])
            .eq('grating', s['grating'])
            .execute()
        )
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
