"""
Configuration loading, credential resolution, and path helpers.

Credential resolution (env vars take priority over TOML):
  1. CAMPFIRE_SUPABASE_URL, CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY,
     CAMPFIRE_R2_ACCOUNT_ID, CAMPFIRE_R2_ACCESS_KEY_ID,
     CAMPFIRE_R2_SECRET_ACCESS_KEY, CAMPFIRE_R2_BUCKET_NAME
  2. Explicit --config flag -> TOML file
  3. $CAMPFIRE_ROOT/config/deploy.toml

Programs resolution:
  1. Package-shipped data/programs.toml (always loaded)
  2. $CAMPFIRE_ROOT/config/programs.toml (merged on top if present)
"""

import os
import sys
from importlib import resources
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


def _load_toml(path: Path) -> dict:
    """Load a TOML file and return as dict."""
    with open(path, 'rb') as f:
        return tomllib.load(f)


def _config_from_env() -> dict | None:
    """
    Build a config dict from environment variables.

    Returns a complete config dict if all required env vars are set,
    or None if any are missing.
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

    return config if all_present else None


def load_config(config_path: str | None = None) -> dict:
    """
    Load deployment credentials (Supabase + R2).

    Resolution order:
      1. Environment variables (CAMPFIRE_SUPABASE_*, CAMPFIRE_R2_*)
      2. Explicit --config path
      3. $CAMPFIRE_ROOT/config/deploy.toml

    Environment variables always take priority. If all required env vars
    are set, no TOML file is needed.
    """
    # Try env vars first
    env_config = _config_from_env()
    if env_config:
        return env_config

    # Fall back to TOML file
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

    # Nothing found — show helpful error
    searched = ', '.join(str(p) for p in candidates) if candidates else '(none)'
    env_names = [v for keys in _ENV_VARS.values() for v in keys.values()]

    print("Error: No deploy credentials found.")
    print()
    print("Option 1 — Set environment variables:")
    for name in env_names:
        print(f"  export {name}=...")
    print()
    print("Option 2 — Create a TOML config file:")
    if candidates:
        print(f"  Searched: {searched}")
    else:
        print("  Set $CAMPFIRE_ROOT and create $CAMPFIRE_ROOT/config/deploy.toml")
        print("  Or use --config <path>")
    sys.exit(1)


def load_programs() -> dict[int, dict]:
    """
    Load JWST program metadata.

    Always loads the package-shipped data/programs.toml, then merges
    overrides from $CAMPFIRE_ROOT/config/programs.toml if present.

    Returns dict keyed by program_id.
    """
    # Load package-shipped defaults
    pkg_data = resources.files('campfire_deploy').parent / 'data' / 'programs.toml'
    programs = {}

    if pkg_data.is_file():
        data = _load_toml(pkg_data)
        programs = {p['program_id']: p for p in data.get('programs', [])}

    # Merge overrides from CAMPFIRE_ROOT if available
    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        override_path = Path(root) / 'config' / 'programs.toml'
        if override_path.exists():
            data = _load_toml(override_path)
            for p in data.get('programs', []):
                programs[p['program_id']] = p

    return programs


def load_observations() -> dict:
    """Load observations.toml from $CAMPFIRE_ROOT/config/."""
    root = os.environ.get('CAMPFIRE_ROOT')
    if root:
        path = Path(root) / 'config' / 'observations.toml'
        if path.exists():
            return _load_toml(path)
    return {}


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
