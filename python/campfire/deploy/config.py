"""
Configuration loading, credential resolution, and path helpers.

Credential resolution (env vars take priority over TOML):
  1. CAMPFIRE_SUPABASE_URL, CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY,
     CAMPFIRE_S3_ACCESS_KEY_ID, CAMPFIRE_S3_SECRET_ACCESS_KEY,
     CAMPFIRE_S3_BUCKET_NAME, and either CAMPFIRE_S3_ENDPOINT or
     CAMPFIRE_S3_ACCOUNT_ID (R2 host derived from the account id).
     The legacy CAMPFIRE_R2_* names are accepted as aliases.
  2. Explicit --config flag -> TOML file
  3. $CAMPFIRE_ROOT/config/deploy.toml

Storage backend tuning (optional, per purpose — data / tiles): CAMPFIRE_S3_*
and CAMPFIRE_S3_TILES_* accept ENDPOINT, REGION, FORCE_PATH_STYLE, BACKEND,
and PUBLIC_URL_BASE so OSN (or any S3-compatible store) is a config change.
See ``backend.py`` for how these resolve into an S3 client.

Programs resolution:
  $CAMPFIRE_ROOT/config/programs.toml
"""

import os
import sys
from pathlib import Path

import tomllib


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

# A storage section is "usable" with credentials + a bucket + a way to resolve
# its endpoint (an explicit endpoint, or an account_id to derive the R2 host).
_STORAGE_REQUIRED = ('access_key_id', 'secret_access_key', 'bucket_name')


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


def _config_from_env() -> dict | None:
    """
    Build a config dict from environment variables.

    Returns a config dict when Supabase service-role creds **and** a usable
    ``data`` storage section are present, or None otherwise. The optional
    ``tiles`` section is included when usable, but never blocks loading.
    """
    supabase = _read_env_section(_SUPABASE_ENV)
    if not all(supabase.get(k) for k in _SUPABASE_ENV):
        return None

    data = _read_env_section(_DATA_ENV)
    if not _storage_section_complete(data):
        return None

    config: dict = {'supabase': supabase, 'r2': data}

    tiles = _read_env_section(_TILES_ENV)
    if _storage_section_complete(tiles):
        config['r2_tiles'] = tiles

    return config


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


def load_config(config_path: str | None = None, *, local: bool = False) -> dict:
    """
    Load deployment credentials (Supabase + R2).

    Resolves Supabase auth into exactly ONE coherent mode, tagged on
    ``config['supabase']['_auth_mode']``:

      - ``local`` (``--local``): local instance (127.0.0.1:54321) with the
        standard Supabase CLI service-role key.
      - ``service_role``: env vars (``CAMPFIRE_SUPABASE_URL`` +
        ``CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY``) or a TOML ``[supabase]`` block
        with ``service_role_key`` + ``url``. Bypasses RLS — the preferred path
        for unattended / batch / CI deploys (no JWT expiry).
      - ``login``: the logged-in user's Supabase credentials from
        ``campfire login`` (url + anon_key + token + TokenManager, taken as a
        matched set). Operates through RLS.

    Precedence: ``--local`` > env service-role > TOML service-role > login.
    When env or TOML supplies a service-role key, login credentials are NOT
    injected. R2 / r2_tiles sections are always resolved from env vars / TOML.
    """
    if local:
        base = _config_from_env() or _find_toml(config_path) or {}
        base.setdefault('supabase', {})
        base['supabase']['url'] = _LOCAL_SUPABASE_URL
        base['supabase']['service_role_key'] = _LOCAL_SUPABASE_SERVICE_ROLE_KEY
        base['supabase'].pop('supabase_token', None)
        base['supabase'].pop('anon_key', None)
        base['supabase'].pop('_token_manager', None)
        base['supabase']['_auth_mode'] = 'local'
        print(f"  Using local Supabase at {_LOCAL_SUPABASE_URL}")
        return base

    # 1. Environment service-role credentials. `_config_from_env` only returns a
    #    config when both url and service_role_key are present, so this is always
    #    service-role mode (used as-is, no login injection).
    env_config = _config_from_env()
    if env_config:
        toml_config = _find_toml(config_path)
        if toml_config:
            for key, val in toml_config.items():
                if key not in env_config:
                    env_config[key] = val
                elif isinstance(val, dict) and isinstance(env_config[key], dict):
                    # env takes priority per-key; TOML fills any gaps (e.g. a
                    # public_url_base configured in TOML alongside env creds).
                    for subkey, subval in val.items():
                        env_config[key].setdefault(subkey, subval)
        env_config.setdefault('supabase', {})['_auth_mode'] = 'service_role'
        return env_config

    # 2. TOML service-role credentials short-circuit (no login injection).
    toml_config = _find_toml(config_path)
    if toml_config:
        sb = toml_config.setdefault('supabase', {})
        if sb.get('service_role_key') and sb.get('url'):
            sb['_auth_mode'] = 'service_role'
            return toml_config

    # 3. Logged-in user credentials (matched set), merged onto any TOML (R2 etc.).
    base = toml_config or {}
    base = _inject_user_credentials(base)
    if base.get('supabase', {}).get('_auth_mode') == 'login':
        return base

    # Nothing found — show helpful error
    candidates = []
    if config_path:
        candidates.append(config_path)
    else:
        root = os.environ.get('CAMPFIRE_ROOT')
        if root:
            candidates.append(str(Path(root) / 'config' / 'deploy.toml'))

    searched = ', '.join(candidates) if candidates else '(none)'
    # Show the canonical (neutral) env var names for the required sections.
    env_names = (
        [names[0] for names in _SUPABASE_ENV.values()]
        + [_DATA_ENV[k][0] for k in _STORAGE_REQUIRED]
    )

    print("Error: No deploy credentials found.")
    print()
    print("Option 1 — Log in with your CAMPFIRE account:")
    print("  campfire login")
    print()
    print("Option 2 — Set environment variables (service role):")
    for name in env_names:
        print(f"  export {name}=...")
    # The endpoint can be given directly (OSN/MinIO/any S3) or derived from an
    # R2 account id — one of the two is required, not both.
    print(f"  export {_DATA_ENV['endpoint'][0]}=...        "
          f"# e.g. https://<host>  (OSN / MinIO / S3)")
    print(f"  # or, for Cloudflare R2:  export {_DATA_ENV['account_id'][0]}=...")
    print()
    print("Option 3 — Create a TOML config file:")
    if candidates:
        print(f"  Searched: {searched}")
    else:
        print("  Set $CAMPFIRE_ROOT and create $CAMPFIRE_ROOT/config/deploy.toml")
        print("  Or use --config <path>")
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
    Return the products directory.

    Uses $CAMPFIRE_ROOT/products/ if CAMPFIRE_ROOT is set,
    otherwise falls back to ./products/.
    """
    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        return Path(root) / 'products'
    return Path('products')


def resolve_obs_dir(obs_name: str) -> Path:
    """
    Return the NIRSpec observation products directory, raising if it doesn't exist.

    Issue #212 (PR-4): NIRSpec products live under ``products/nirspec/<obs>/``
    (instrument-parity layout), kept in lockstep with the pipeline's
    ``Observation.setup_workspace_directory``.
    """
    obs_dir = resolve_products_dir() / 'nirspec' / obs_name
    if not obs_dir.exists():
        print(f"Error: Observation directory not found: {obs_dir}")
        print(f"Set $CAMPFIRE_ROOT or run from a directory containing products/nirspec/{obs_name}/")
        sys.exit(1)
    return obs_dir


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
