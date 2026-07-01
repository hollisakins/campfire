"""
Configuration loading, credential resolution, and path helpers.

Deploy auth has ONE default — ``campfire login`` — and one explicit escape hatch.
The two decisions that used to be tangled are now independent:

  * **Supabase auth mode** — resolved from an *explicit* signal only (issue #250):
      - ``login`` (default): the logged-in user's credentials (device-flow OAuth
        via ``CredentialManager``). Operates through RLS; uploads go presigned.
      - ``service_role``: opt-in via ``CAMPFIRE_DEPLOY_MODE=service-role`` (or the
        top-level ``--service-role`` flag). Bypasses RLS; for unattended / CI runs.
        Requires ``CAMPFIRE_SUPABASE_URL`` + ``CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY``
        (or a TOML ``[supabase]`` block). A service-role key that merely *exists*
        in the environment no longer wins by precedence — it is ignored unless the
        mode is explicitly requested.
      - ``local``: ``--local`` (or ``CAMPFIRE_DEPLOY_MODE=local``) — the local
        Supabase instance with the standard CLI service-role key.

  * **Object-store credentials** — the ``data`` / ``tiles`` / ``osn`` storage
    sections are resolved from env vars / TOML *independently of the Supabase
    mode*. Presence of storage creds never changes the auth mode. The normal
    deploy uploads via presigned URLs (no local write keys needed); the storage
    sections are only consumed by the direct-boto3 maintenance paths
    (``remove`` / ``reconcile`` / ``registry backfill`` / ``registry copy``) and
    by ``service_role`` / ``local`` uploads.

Credential source precedence (env vars take priority over TOML), per section:
  1. CAMPFIRE_SUPABASE_URL, CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY (service-role mode);
     CAMPFIRE_S3_ACCESS_KEY_ID, CAMPFIRE_S3_SECRET_ACCESS_KEY,
     CAMPFIRE_S3_BUCKET_NAME, and either CAMPFIRE_S3_ENDPOINT or
     CAMPFIRE_S3_ACCOUNT_ID (R2 host derived from the account id).
     The legacy CAMPFIRE_R2_* names are accepted as aliases.
  2. Explicit --config flag -> TOML file
  3. $CAMPFIRE_ROOT/config/deploy.toml

Storage backend tuning (optional, per purpose — data / tiles / osn): CAMPFIRE_S3_*,
CAMPFIRE_S3_TILES_*, and CAMPFIRE_S3_OSN_* accept ENDPOINT, REGION,
FORCE_PATH_STYLE, BACKEND, and PUBLIC_URL_BASE so OSN (or any S3-compatible store)
is a config change. The ``osn`` section (-> config['r2_osn']) is the data-bucket
migration destination for epic #210 / #215. See ``backend.py`` for how these
resolve into an S3 client.

Programs resolution:
  $CAMPFIRE_ROOT/config/programs.toml
"""

import os
import sys
from pathlib import Path

import tomllib

from campfire_layout import Scope, dir_for, reference_dir, roots


# Environment-variable resolution for credentials + storage backend config.
#
# Each config key resolves from a list of env var names tried in order:
# neutral ``CAMPFIRE_S3_*`` names are canonical; ``CAMPFIRE_R2_*`` names are
# kept as backward-compatible aliases. Storage sections additionally accept
# optional tuning keys (endpoint, region, force_path_style, backend,
# public_url_base) so OSN — or any S3-compatible backend — is a config change,
# not a code edit. See ``backend.py`` for how these are consumed.

_SUPABASE_ENV = {
    'url': ['CAMPFIRE_SUPABASE_URL'],
    'service_role_key': ['CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY'],
}

# Data storage backend -> config['r2'] section.
_DATA_ENV = {
    'account_id': ['CAMPFIRE_S3_ACCOUNT_ID', 'CAMPFIRE_R2_ACCOUNT_ID'],
    'access_key_id': ['CAMPFIRE_S3_ACCESS_KEY_ID', 'CAMPFIRE_R2_ACCESS_KEY_ID'],
    'secret_access_key': ['CAMPFIRE_S3_SECRET_ACCESS_KEY', 'CAMPFIRE_R2_SECRET_ACCESS_KEY'],
    'bucket_name': ['CAMPFIRE_S3_BUCKET_NAME', 'CAMPFIRE_R2_BUCKET_NAME'],
    'endpoint': ['CAMPFIRE_S3_ENDPOINT', 'CAMPFIRE_R2_ENDPOINT'],
    'region': ['CAMPFIRE_S3_REGION', 'CAMPFIRE_R2_REGION'],
    'force_path_style': ['CAMPFIRE_S3_FORCE_PATH_STYLE', 'CAMPFIRE_R2_FORCE_PATH_STYLE'],
    'backend': ['CAMPFIRE_S3_BACKEND', 'CAMPFIRE_R2_BACKEND'],
    'public_url_base': [
        'CAMPFIRE_S3_PUBLIC_URL_BASE', 'CAMPFIRE_R2_PUBLIC_URL_BASE',
        'CAMPFIRE_R2_PUBLIC_URL',
    ],
}

# Tiles storage backend -> config['r2_tiles'] section.
_TILES_ENV = {
    'account_id': ['CAMPFIRE_S3_TILES_ACCOUNT_ID', 'CAMPFIRE_R2_TILES_ACCOUNT_ID'],
    'access_key_id': ['CAMPFIRE_S3_TILES_ACCESS_KEY_ID', 'CAMPFIRE_R2_TILES_ACCESS_KEY_ID'],
    'secret_access_key': [
        'CAMPFIRE_S3_TILES_SECRET_ACCESS_KEY', 'CAMPFIRE_R2_TILES_SECRET_ACCESS_KEY',
    ],
    'bucket_name': ['CAMPFIRE_S3_TILES_BUCKET_NAME', 'CAMPFIRE_R2_TILES_BUCKET_NAME'],
    'endpoint': ['CAMPFIRE_S3_TILES_ENDPOINT', 'CAMPFIRE_R2_TILES_ENDPOINT'],
    'region': ['CAMPFIRE_S3_TILES_REGION', 'CAMPFIRE_R2_TILES_REGION'],
    'force_path_style': [
        'CAMPFIRE_S3_TILES_FORCE_PATH_STYLE', 'CAMPFIRE_R2_TILES_FORCE_PATH_STYLE',
    ],
    'backend': ['CAMPFIRE_S3_TILES_BACKEND', 'CAMPFIRE_R2_TILES_BACKEND'],
    'public_url_base': [
        'CAMPFIRE_S3_TILES_PUBLIC_URL_BASE', 'CAMPFIRE_R2_TILES_PUBLIC_URL_BASE',
        'CAMPFIRE_R2_TILES_PUBLIC_URL',
    ],
}

# OSN (Open Storage Network) data backend -> config['r2_osn'] section. This is
# the migration *destination* for the data bucket (epic #210 Track A / #215):
# the copy tool holds the R2 'data' client (source) and this 'osn' client (dest)
# simultaneously. OSN is S3-compatible Ceph — endpoint-driven, dummy region,
# path-style — so it reuses the same backend factory. Only CAMPFIRE_S3_OSN_*
# names (no legacy alias; this backend is new).
_OSN_ENV = {
    'access_key_id': ['CAMPFIRE_S3_OSN_ACCESS_KEY_ID'],
    'secret_access_key': ['CAMPFIRE_S3_OSN_SECRET_ACCESS_KEY'],
    'bucket_name': ['CAMPFIRE_S3_OSN_BUCKET_NAME'],
    'endpoint': ['CAMPFIRE_S3_OSN_ENDPOINT'],
    'region': ['CAMPFIRE_S3_OSN_REGION'],
    'force_path_style': ['CAMPFIRE_S3_OSN_FORCE_PATH_STYLE'],
    'backend': ['CAMPFIRE_S3_OSN_BACKEND'],
    'public_url_base': ['CAMPFIRE_S3_OSN_PUBLIC_URL_BASE'],
}

# A storage section is "usable" with credentials + a bucket + a way to resolve
# its endpoint (an explicit endpoint, or an account_id to derive the R2 host).
_STORAGE_REQUIRED = ('access_key_id', 'secret_access_key', 'bucket_name')

# Storage config-dict section -> its env-var spec. Each is resolved independently
# of the Supabase auth mode (issue #250): storage creds never decide who you are.
_STORAGE_ENV = {
    'r2': _DATA_ENV,
    'r2_tiles': _TILES_ENV,
    'r2_osn': _OSN_ENV,
}

# The explicit deploy-mode switch. Values (case/dash-insensitive): 'login'
# (default), 'service-role', 'local'. The top-level --service-role / --local
# flags override it.
_DEPLOY_MODE_ENV = 'CAMPFIRE_DEPLOY_MODE'


def _load_toml(path: Path) -> dict:
    """Load a TOML file and return as dict."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def _read_env_section(spec: dict[str, list[str]]) -> dict:
    """Read each key whose env var (canonical-first, then aliases) is set."""
    out: dict = {}
    for key, names in spec.items():
        for name in names:
            val = os.environ.get(name)
            if val:
                out[key] = val
                break
    return out


def _storage_section_complete(section: dict) -> bool:
    """A storage section needs creds, a bucket, and a resolvable endpoint."""
    if not all(section.get(k) for k in _STORAGE_REQUIRED):
        return False
    return bool(section.get('endpoint') or section.get('account_id'))


def _resolve_storage_sections(toml_config: dict | None) -> dict:
    """Resolve every storage section (data / tiles / osn) from TOML + env vars.

    Storage is resolved the SAME way in every auth mode — presence of these creds
    no longer influences who you authenticate as (issue #250). TOML seeds each
    section; env vars override per-key (env wins, TOML fills gaps).

    A section is included when it is actually usable (complete creds + resolvable
    endpoint) or when a TOML block explicitly declared it — an intentional config
    is honored even if partial, and ``backend.resolve_backend`` surfaces any gap
    precisely at the point of use. A stray partial *env* section (e.g. a lone
    ``CAMPFIRE_R2_TILES_ACCOUNT_ID``) is dropped rather than half-created.
    """
    config: dict = {}
    for section_key, spec in _STORAGE_ENV.items():
        toml_section = (toml_config or {}).get(section_key)
        has_toml = isinstance(toml_section, dict) and bool(toml_section)
        merged: dict = dict(toml_section) if has_toml else {}
        merged.update(_read_env_section(spec))  # env overrides TOML per key
        if merged and (has_toml or _storage_section_complete(merged)):
            config[section_key] = merged
    return config


def _resolve_deploy_mode(*, local: bool, service_role: bool) -> str:
    """Resolve the explicit Supabase auth mode: 'login' | 'service_role' | 'local'.

    Flags win over the env var; the env var wins over the 'login' default. A
    service-role key that merely exists in the environment does NOT flip the mode
    (that silent precedence was the issue #250 footgun) — it is used only when the
    mode is explicitly ``service_role``.
    """
    if local:
        return 'local'
    if service_role:
        return 'service_role'

    raw = (os.environ.get(_DEPLOY_MODE_ENV) or '').strip().lower().replace('-', '_')
    if raw in ('', 'login'):
        return 'login'
    if raw == 'local':
        return 'local'
    if raw in ('service_role', 'servicerole'):
        return 'service_role'

    print(
        f"Error: {_DEPLOY_MODE_ENV}={os.environ.get(_DEPLOY_MODE_ENV)!r} is not a "
        f"valid deploy mode. Use one of: login (default), service-role, local."
    )
    sys.exit(1)


def _find_toml(config_path: str | None = None) -> dict | None:
    """Locate and load a TOML config file, or return None."""
    candidates: list[Path] = []

    if config_path:
        candidates.append(Path(config_path))
    else:
        root = os.environ.get('CAMPFIRE_ROOT')
        if root:
            candidates.append(Path(root) / 'config' / 'deploy.toml')

    for path in candidates:
        if path.exists():
            return _load_toml(path)

    return None


_LOCAL_SUPABASE_URL = 'http://127.0.0.1:54321'
# Supabase CLI's deterministic local service-role JWT (not a secret).
_LOCAL_SUPABASE_SERVICE_ROLE_KEY = (
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.'
    'eyJpc3MiOiJzdXBhYmFzZS1kZW1vIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImV4cCI6MTk4MzgxMjk5Nn0.'
    'EGIM96RAZx35lJzdJsyH-qQwv8Hdp7fsn3W0YpN81IU'
)


def load_config(
    config_path: str | None = None,
    *,
    local: bool = False,
    service_role: bool = False,
) -> dict:
    """
    Load deployment credentials (Supabase + object storage).

    Object-storage sections (``r2`` / ``r2_tiles`` / ``r2_osn``) are resolved from
    env vars + TOML in **every** mode, independently of the Supabase auth decision.
    The Supabase auth mode — tagged on ``config['supabase']['_auth_mode']`` — is
    chosen *explicitly*, never by which credentials happen to be lying around
    (issue #250):

      - ``login`` (default): the logged-in user's Supabase credentials from
        ``campfire login`` (url + anon_key + token + TokenManager, a matched set).
        Operates through RLS; uploads go via presigned URLs (no local write keys).
      - ``service_role`` (``--service-role`` / ``CAMPFIRE_DEPLOY_MODE=service-role``):
        ``CAMPFIRE_SUPABASE_URL`` + ``CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY`` (or a TOML
        ``[supabase]`` block). Bypasses RLS — for unattended / CI deploys. A
        service-role key present in the env is used ONLY when this mode is
        explicitly requested; it never silently wins.
      - ``local`` (``--local`` / ``CAMPFIRE_DEPLOY_MODE=local``): the local instance
        (127.0.0.1:54321) with the standard Supabase CLI service-role key.

    ``local`` and ``service_role`` args are the flag overrides; absent them the
    mode comes from ``CAMPFIRE_DEPLOY_MODE`` (default ``login``).
    """
    mode = _resolve_deploy_mode(local=local, service_role=service_role)
    toml_config = _find_toml(config_path)

    # Storage first — same in every mode, decoupled from auth.
    base = _resolve_storage_sections(toml_config)

    if mode == 'local':
        base['supabase'] = {
            'url': _LOCAL_SUPABASE_URL,
            'service_role_key': _LOCAL_SUPABASE_SERVICE_ROLE_KEY,
            '_auth_mode': 'local',
        }
        print(f"  Using local Supabase at {_LOCAL_SUPABASE_URL}")
        return base

    if mode == 'service_role':
        # url + service_role_key from env (canonical) then TOML [supabase] gaps.
        sb = _read_env_section(_SUPABASE_ENV)
        toml_sb = (toml_config or {}).get('supabase', {})
        if isinstance(toml_sb, dict):
            for key in ('url', 'service_role_key'):
                if not sb.get(key) and toml_sb.get(key):
                    sb[key] = toml_sb[key]
        if not (sb.get('url') and sb.get('service_role_key')):
            _service_role_missing_error(config_path)
        sb['_auth_mode'] = 'service_role'
        base['supabase'] = sb
        return base

    # mode == 'login': inject the logged-in user's matched credential set.
    base = _inject_user_credentials(base)
    if base.get('supabase', {}).get('_auth_mode') == 'login':
        return base

    # Login was requested (default) but no usable session exists.
    _no_login_error()


def _service_role_missing_error(config_path: str | None) -> None:
    """Explicit service-role mode was requested but creds are absent."""
    candidates: list[str] = []
    if config_path:
        candidates.append(config_path)
    else:
        root = os.environ.get('CAMPFIRE_ROOT')
        if root:
            candidates.append(str(Path(root) / 'config' / 'deploy.toml'))

    print("Error: service-role deploy mode requested, but no service-role "
          "credentials were found.")
    print()
    print("Provide both, via environment:")
    print(f"  export {_SUPABASE_ENV['url'][0]}=...")
    print(f"  export {_SUPABASE_ENV['service_role_key'][0]}=...")
    print("Or a TOML [supabase] block with url + service_role_key.")
    if candidates:
        print(f"  Searched: {', '.join(candidates)}")
    print()
    print("To deploy as yourself instead, drop the service-role mode and run:")
    print("  campfire login")
    sys.exit(1)


def _no_login_error() -> None:
    """Default (login) mode but the user isn't logged in."""
    print("Error: Not logged in — no deploy credentials available.")
    print()
    print("Deploys authenticate as you by default (uploads go via presigned URLs,")
    print("so your machine needs no object-store write keys):")
    print("  campfire login")
    print()
    print("For an unattended / CI deploy, opt into service-role mode explicitly:")
    print(f"  export {_DEPLOY_MODE_ENV}=service-role")
    print(f"  export {_SUPABASE_ENV['url'][0]}=...")
    print(f"  export {_SUPABASE_ENV['service_role_key'][0]}=...")
    print("  # plus CAMPFIRE_S3_* object-store credentials for direct uploads")
    print()
    print("Or target a local Supabase instance with --local.")
    sys.exit(1)


def _inject_user_credentials(config: dict) -> dict:
    """
    Inject the logged-in user's Supabase credentials as a coherent matched set.

    If the user has logged in via ``campfire login``, their ``supabase_url`` +
    ``supabase_anon_key`` + ``supabase_token`` + ``TokenManager`` are injected
    **together** (they are minted for the same project) and tagged
    ``_auth_mode='login'``. The login URL/anon_key OVERWRITE any carried in from
    TOML/env so a login token is never paired with a foreign URL — the exact
    mismatch that lets an admin pass a gate yet be rejected on writes.

    On no usable login session (not logged in, offline, or refresh failed) the
    config is returned untouched and untagged; downstream resolution then
    reports "no credentials". It can never degrade to an anon client because
    ``get_supabase_client`` requires a complete login set.
    """
    try:
        from campfire.api.session import resolve_base_url
        from campfire.auth.tokens import TokenManager

        base_url = resolve_base_url()
        tm = TokenManager(base_url=base_url)
        if not tm.is_oauth():
            return config
        sb_token = tm.get_supabase_token(auto_refresh=True)
        creds = tm._cached_creds
    except Exception:
        # Auth layer unavailable / not logged in / refresh failed.
        return config

    if not (sb_token and creds and creds.supabase_url and creds.supabase_anon_key):
        return config

    sb = config.setdefault('supabase', {})
    existing_url = sb.get('url')
    if existing_url and existing_url != creds.supabase_url:
        print(
            f"  Note: ignoring configured Supabase url ({existing_url}); using "
            f"the URL your login token was issued for ({creds.supabase_url})."
        )
    # Matched set from login — overwrite, never mix a login token with a
    # foreign url/anon_key.
    sb['url'] = creds.supabase_url
    sb['anon_key'] = creds.supabase_anon_key
    sb['supabase_token'] = sb_token
    sb['_token_manager'] = tm
    sb['_auth_mode'] = 'login'
    return config


def load_programs() -> dict[str, dict]:
    """
    Load CAMPFIRE program metadata from $CAMPFIRE_ROOT/config/programs.toml.

    File format: each top-level key is the program slug.
      [capers]
      program_name = "CAPERS"
      pi_name = "M. Dickinson"
      ...

    Returns dict keyed by program slug.
    """
    root = os.environ.get('CAMPFIRE_ROOT')
    if not root:
        print("Error: $CAMPFIRE_ROOT is not set.")
        sys.exit(1)

    path = Path(root) / 'config' / 'programs.toml'
    if not path.exists():
        print(f"Error: Programs config not found: {path}")
        sys.exit(1)

    data = _load_toml(path)
    return {slug: {**info, 'slug': slug} for slug, info in data.items()}


def load_observations() -> dict:
    """Load observations.toml from $CAMPFIRE_ROOT/config/."""
    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        path = Path(root) / 'config' / 'observations.toml'
        if path.exists():
            return _load_toml(path)
    return {}


def resolve_program_slug(obs_name: str) -> str:
    """Get CAMPFIRE program slug for an observation from observations.toml."""
    obs = load_observations()
    if obs_name in obs:
        return obs[obs_name].get('program', '')
    return ''


def validate_program_slug(program_slug: str, programs_config: dict, obs_name: str) -> None:
    """Ensure *program_slug* is a real slug defined in programs.toml.

    Guards against the common mistake of putting a program *name* (or any
    other non-slug string) in the observations.toml ``program`` field. That
    value is baked into the ECSV metadata by the pipeline and, left
    unguarded, silently creates a junk private program at deploy time; the
    observation insert then fails its representation read-back with a cryptic
    Supabase error (``new row violates row-level security policy``). Fail
    early here with an actionable message instead.
    """
    if program_slug in programs_config:
        return

    # Did they use the program *name* instead of the slug?
    matches = [
        slug for slug, info in programs_config.items()
        if str(info.get('program_name', '')).lower() == str(program_slug).lower()
    ]
    known = ', '.join(sorted(programs_config)) or '(none)'
    print(
        f"Error: observation '{obs_name}' resolves to program_slug "
        f"'{program_slug}', which is not a program defined in programs.toml."
    )
    if matches:
        print(
            f"  '{program_slug}' is the program_name of slug '{matches[0]}' "
            f"— use the slug, not the name."
        )
    print("  Program slugs are the [section] keys in programs.toml.")
    print(f"  Known slugs: {known}")
    print(
        f"  Fix the 'program' field in observations.toml, then regenerate the "
        f"ECSV metadata (e.g. 'cfpipe nirspec summary --obs {obs_name}')."
    )
    sys.exit(1)


def resolve_field(obs_name: str) -> str:
    """Get field name for an observation from observations.toml."""
    obs = load_observations()
    if obs_name in obs:
        return obs[obs_name].get('field', '')
    print(f"Warning: observation '{obs_name}' not found in observations.toml")
    return ''


def resolve_products_dir() -> Path:
    """
    Return the products directory (``$CAMPFIRE_ROOT/products``).

    Delegates to the shared layout contract (``campfire_layout.roots``), which
    resolves ``$CAMPFIRE_ROOT`` (default ``~/campfire``) identically to the
    pipeline — collapsing the prior deploy-only ``./products`` fallback.
    """
    return roots().products


def resolve_obs_dir(obs_name: str) -> Path:
    """
    Return the NIRSpec observation products directory, raising if it doesn't exist.

    Issue #212 (PR-4): NIRSpec products live under ``products/nirspec/<obs>/``
    (instrument-parity layout). Resolved via the shared layout contract
    (``campfire_layout.dir_for``), the single authority shared with the pipeline.
    """
    obs_dir = dir_for('nirspec_spec', Scope(obs=obs_name))
    if not obs_dir.exists():
        print(f"Error: Observation directory not found: {obs_dir}")
        print(f"Set $CAMPFIRE_ROOT or run from a directory containing products/nirspec/{obs_name}/")
        sys.exit(1)
    return obs_dir


def resolve_reference_dir() -> Path:
    """
    Return the reference directory (reducer-decision state).

    Delegates to the shared layout contract (``campfire_layout.roots``):
    ``$CAMPFIRE_ROOT/reference`` (default ``~/campfire``), matching the pipeline.
    """
    return roots().reference


def resolve_reference_obs_dir(obs_name: str) -> Path:
    """
    Return the NIRSpec observation reference directory (issue #212 PR-4).

    Reducer-decision state (stuck-shutter / nodded-bkg-override TOMLs) lives
    under ``reference/nirspec/<obs>/``. Resolved via the shared layout contract
    (``campfire_layout.reference_dir``), the single authority shared with the
    pipeline. Unlike :func:`resolve_obs_dir` this does not require the directory
    to exist — the TOMLs are optional.
    """
    return reference_dir('nirspec', Scope(obs=obs_name))


def resolve_tiles_dir(tile_dir: str | None = None) -> Path:
    """
    Resolve the tiles output directory.

    Resolution order:
      1. Explicit --tile-dir argument
      2. $CAMPFIRE_ROOT/tiles/
      3. Error
    """
    if tile_dir:
        return Path(tile_dir)

    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        return Path(root) / 'tiles'

    print("Error: No tile directory found.")
    print("  Use --tile-dir <path> or set $CAMPFIRE_ROOT")
    sys.exit(1)


def resolve_imaging_config(imaging_config: str | None = None) -> Path | None:
    """
    Resolve the imaging.toml config path.

    Resolution order:
      1. Explicit --imaging-config argument
      2. $CAMPFIRE_ROOT/config/imaging.toml
      3. ./pipeline/imaging.toml (repo fallback)

    Returns None if no imaging config is found during auto-discovery.
    Still exits if an explicit path is provided but does not exist.
    """
    if imaging_config:
        p = Path(imaging_config)
        if not p.exists():
            print(f"Error: Imaging config not found: {p}")
            sys.exit(1)
        return p

    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        p = Path(root) / 'config' / 'imaging.toml'
        if p.exists():
            return p

    # Repo fallback
    p = Path('pipeline') / 'imaging.toml'
    if p.exists():
        return p

    return None


def resolve_photometry_config(photometry_config: str | None = None) -> Path | None:
    """
    Resolve the photometry.toml config path.

    Returns None if no config is found (photometry is optional).

    Resolution order:
      1. Explicit --photometry-config argument
      2. $CAMPFIRE_ROOT/config/photometry.toml
    """
    if photometry_config:
        p = Path(photometry_config)
        if not p.exists():
            print(f"Error: Photometry config not found: {p}")
            sys.exit(1)
        return p

    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        p = Path(root) / 'config' / 'photometry.toml'
        if p.exists():
            return p

    return None
