"""
Configuration loading, credential resolution, and path helpers.

Credential resolution (env vars take priority over TOML):
  1. CAMPFIRE_SUPABASE_URL, CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY,
     CAMPFIRE_R2_ACCOUNT_ID, CAMPFIRE_R2_ACCESS_KEY_ID,
     CAMPFIRE_R2_SECRET_ACCESS_KEY, CAMPFIRE_R2_BUCKET_NAME
  2. Explicit --config flag -> TOML file
  3. $CAMPFIRE_ROOT/config/deploy.toml

Programs resolution:
  $CAMPFIRE_ROOT/config/programs.toml
"""

import os
import sys
from pathlib import Path

import tomllib


# Environment variable names for credentials
_ENV_VARS = {
    'supabase': {
        'url': 'CAMPFIRE_SUPABASE_URL',
        'service_role_key': 'CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY',
    },
    'r2': {
        'account_id': 'CAMPFIRE_R2_ACCOUNT_ID',
        'access_key_id': 'CAMPFIRE_R2_ACCESS_KEY_ID',
        'secret_access_key': 'CAMPFIRE_R2_SECRET_ACCESS_KEY',
        'bucket_name': 'CAMPFIRE_R2_BUCKET_NAME',
    },
}

# Optional environment variable sections (not required for core commands)
_OPTIONAL_ENV_VARS = {
    'r2_tiles': {
        'account_id': 'CAMPFIRE_R2_TILES_ACCOUNT_ID',
        'access_key_id': 'CAMPFIRE_R2_TILES_ACCESS_KEY_ID',
        'secret_access_key': 'CAMPFIRE_R2_TILES_SECRET_ACCESS_KEY',
        'bucket_name': 'CAMPFIRE_R2_TILES_BUCKET_NAME',
        'public_url_base': 'CAMPFIRE_R2_TILES_PUBLIC_URL_BASE',
    },
}


def _load_toml(path: Path) -> dict:
    """Load a TOML file and return as dict."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def _config_from_env() -> dict | None:
    """
    Build a config dict from environment variables.

    Returns a complete config dict if all required env vars are set,
    or None if any are missing. Optional sections (e.g. r2_tiles) are
    included when all their env vars are present, but never block loading.
    """
    config: dict = {}
    all_present = True

    for section, keys in _ENV_VARS.items():
        config[section] = {}
        for key, env_var in keys.items():
            val = os.environ.get(env_var)
            if val:
                config[section][key] = val
            else:
                all_present = False

    if not all_present:
        return None

    # Populate optional sections if all their env vars are present
    for section, keys in _OPTIONAL_ENV_VARS.items():
        section_vals = {}
        section_complete = True
        for key, env_var in keys.items():
            val = os.environ.get(env_var)
            if val:
                section_vals[key] = val
            else:
                section_complete = False
        if section_complete:
            config[section] = section_vals

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


def load_config(config_path: str | None = None) -> dict:
    """
    Load deployment credentials (Supabase + R2).

    Resolution order:
      1. Environment variables (CAMPFIRE_SUPABASE_*, CAMPFIRE_R2_*)
      2. Explicit --config path
      3. $CAMPFIRE_ROOT/config/deploy.toml

    Environment variables take priority for core sections. Extra TOML
    sections (e.g. r2_tiles) are merged in when not covered by env vars.
    """
    # Try env vars first
    env_config = _config_from_env()
    if env_config:
        # Merge any extra sections from TOML that env vars don't cover
        toml_config = _find_toml(config_path)
        if toml_config:
            for key, val in toml_config.items():
                if key not in env_config:
                    env_config[key] = val
        return _inject_user_credentials(env_config)

    # Fall back to TOML file
    toml_config = _find_toml(config_path)
    if toml_config:
        return _inject_user_credentials(toml_config)

    # Try user credentials alone (no env vars or TOML needed if logged in)
    # The user still needs CAMPFIRE_SUPABASE_URL and CAMPFIRE_SUPABASE_ANON_KEY
    url = os.environ.get('CAMPFIRE_SUPABASE_URL')
    anon_key = os.environ.get('CAMPFIRE_SUPABASE_ANON_KEY')
    if url and anon_key:
        config: dict = {'supabase': {'url': url, 'anon_key': anon_key}}
        return _inject_user_credentials(config)

    # Nothing found — show helpful error
    candidates = []
    if config_path:
        candidates.append(config_path)
    else:
        root = os.environ.get('CAMPFIRE_ROOT')
        if root:
            candidates.append(str(Path(root) / 'config' / 'deploy.toml'))

    searched = ', '.join(candidates) if candidates else '(none)'
    env_names = [v for keys in _ENV_VARS.values() for v in keys.values()]

    print("Error: No deploy credentials found.")
    print()
    print("Option 1 — Log in with your CAMPFIRE account:")
    print("  campfire login")
    print("  export CAMPFIRE_SUPABASE_URL=...")
    print("  export CAMPFIRE_SUPABASE_ANON_KEY=...")
    print()
    print("Option 2 — Set environment variables (service role):")
    for name in env_names:
        print(f"  export {name}=...")
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
    Enrich deploy config with the user's Supabase token from stored OAuth credentials.

    If the user has logged in via ``campfire login``, their supabase_token is
    injected into ``config['supabase']``. This allows ``get_supabase_client()``
    to use the user JWT path instead of requiring a service_role_key.
    """
    try:
        from campfire.api.session import resolve_base_url
        from campfire.auth.tokens import TokenManager

        base_url = resolve_base_url()
        tm = TokenManager(base_url=base_url)
        if tm.is_oauth():
            sb_token = tm.get_supabase_token(auto_refresh=True)
            if sb_token:
                config.setdefault('supabase', {})
                config['supabase']['supabase_token'] = sb_token

                # Also inject anon_key from env if available
                anon_key = os.environ.get('CAMPFIRE_SUPABASE_ANON_KEY')
                if anon_key:
                    config['supabase']['anon_key'] = anon_key
    except Exception:
        # Auth not available or not configured — that's fine,
        # fall back to service_role_key in get_supabase_client()
        pass

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
    Return the observation products directory, raising if it doesn't exist.
    """
    obs_dir = resolve_products_dir() / obs_name
    if not obs_dir.exists():
        print(f"Error: Observation directory not found: {obs_dir}")
        print(f"Set $CAMPFIRE_ROOT or run from a directory containing products/{obs_name}/")
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


def resolve_imaging_config(imaging_config: str | None = None) -> Path:
    """
    Resolve the imaging.toml config path.

    Resolution order:
      1. Explicit --imaging-config argument
      2. $CAMPFIRE_ROOT/config/imaging.toml
      3. ./pipeline/imaging.toml (repo fallback)
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

    print("Error: No imaging.toml found.")
    print("  Use --imaging-config <path> or set $CAMPFIRE_ROOT")
    sys.exit(1)
