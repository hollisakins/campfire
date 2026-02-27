"""
Configuration loading and environment setup.

Resolves paths in order:
1. Explicit paths in config
2. $CAMPFIRE_ROOT/{raw,products,cache} if env var is set
3. Raise a clear error
"""

import os
from pathlib import Path

import toml


# ---------------------------------------------------------------------------
# Deep merge utility
# ---------------------------------------------------------------------------

def deep_merge(base, override):
    """Recursively merge *override* into *base*.

    Dict values are merged recursively; all other types are replaced.
    Neither input is mutated — returns a new dict.
    """
    merged = dict(base)
    for key, val in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


# ---------------------------------------------------------------------------
# Package defaults
# ---------------------------------------------------------------------------

def _load_package_defaults():
    """Load the default config shipped as package data."""
    default_path = Path(__file__).parent / 'data' / 'config_default.toml'
    with open(default_path, 'r') as f:
        return toml.load(f)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path=None):
    """Load and return the merged pipeline configuration.

    1. Load package defaults (always).
    2. If *config_path* is given explicitly, it must exist (error if not).
       If *config_path* is None, search:
         a. $CAMPFIRE_ROOT/config/config.toml
         b. ./config.toml
    3. If a user config is found, deep-merge it over defaults.
    4. Return the merged dict.

    The config file is optional — defaults alone are sufficient to run.
    """
    defaults = _load_package_defaults()

    # Determine user config path
    user_path = None
    if config_path is not None:
        if not os.path.isfile(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        user_path = config_path
    else:
        campfire_root = os.environ.get('CAMPFIRE_ROOT')
        if campfire_root:
            candidate = os.path.join(campfire_root, 'config', 'config.toml')
            if os.path.isfile(candidate):
                user_path = candidate
        if user_path is None and os.path.isfile('config.toml'):
            user_path = 'config.toml'

    if user_path is not None:
        with open(user_path, 'r') as f:
            user_config = toml.load(f)
        return deep_merge(defaults, user_config)

    return defaults


# ---------------------------------------------------------------------------
# Environment and path resolution
# ---------------------------------------------------------------------------

def _resolve_path(config_value, campfire_root, default_subdir, label):
    """Resolve a single path from config value or $CAMPFIRE_ROOT.

    Parameters
    ----------
    config_value : str or None
        Explicit path from config (takes priority).
    campfire_root : str or None
        Value of $CAMPFIRE_ROOT environment variable.
    default_subdir : str
        Subdirectory under $CAMPFIRE_ROOT (e.g. 'raw', 'products').
    label : str
        Human-readable label for error messages.

    Returns
    -------
    str
        Resolved absolute path.
    """
    if config_value:
        return config_value
    if campfire_root:
        return os.path.join(campfire_root, default_subdir)
    raise RuntimeError(
        f"{label}: set [paths].{label} in config.toml or export CAMPFIRE_ROOT"
    )


def setup_environment(config):
    """Set environment variables from config file.

    For CRDS_PATH, falls back to $CAMPFIRE_ROOT/cache/crds if not specified
    in config and $CAMPFIRE_ROOT is set.
    """
    campfire_root = os.environ.get('CAMPFIRE_ROOT')

    if 'environment' in config:
        env = config['environment']

        # Handle CRDS_PATH fallback before setting env vars
        if 'CRDS_PATH' not in env and campfire_root:
            env['CRDS_PATH'] = os.path.join(campfire_root, 'cache', 'crds')

        for key, value in env.items():
            os.environ[key] = str(value)


def resolve_paths(config):
    """Extract and create pipeline directories from config.

    Returns dict with keys: data_dir, products_dir.
    """
    paths = config.get('paths', {})
    campfire_root = os.environ.get('CAMPFIRE_ROOT')

    result = {
        'data_dir': _resolve_path(
            paths.get('data_dir'), campfire_root, 'raw', 'data_dir'),
        'products_dir': _resolve_path(
            paths.get('products_dir'), campfire_root, 'products', 'products_dir'),
    }
    for d in result.values():
        if d:
            os.makedirs(d, exist_ok=True)
    return result


# ---------------------------------------------------------------------------
# Observation / field file resolution
# ---------------------------------------------------------------------------

def resolve_observations_file(explicit_path=None):
    """Find the observations.toml file.

    Search order:
    1. explicit_path (if provided and exists)
    2. $CAMPFIRE_ROOT/config/observations.toml
    3. ./observations.toml (backwards compat)

    Returns
    -------
    str
        Path to the observations file.

    Raises
    ------
    FileNotFoundError
        If no observations file is found.
    """
    tried = []

    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path
    if explicit_path:
        tried.append(explicit_path)

    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    if campfire_root:
        candidate = os.path.join(campfire_root, 'config', 'observations.toml')
        if os.path.isfile(candidate):
            return candidate
        tried.append(candidate)

    if os.path.isfile('observations.toml'):
        return 'observations.toml'
    tried.append('observations.toml')

    raise FileNotFoundError(
        f"observations.toml not found. Searched: {tried}"
    )


def resolve_fields_file(explicit_path=None):
    """Find the fields.toml file for NIRCam field definitions.

    Search order:
    1. explicit_path (if provided and exists)
    2. $CAMPFIRE_ROOT/config/fields.toml
    3. ./fields.toml (backwards compat)

    Returns
    -------
    str
        Path to the fields file.

    Raises
    ------
    FileNotFoundError
        If no fields file is found.
    """
    tried = []

    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path
    if explicit_path:
        tried.append(explicit_path)

    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    if campfire_root:
        candidate = os.path.join(campfire_root, 'config', 'fields.toml')
        if os.path.isfile(candidate):
            return candidate
        tried.append(candidate)

    if os.path.isfile('fields.toml'):
        return 'fields.toml'
    tried.append('fields.toml')

    raise FileNotFoundError(
        f"fields.toml not found. Searched: {tried}"
    )


# ---------------------------------------------------------------------------
# Stage config getters
# ---------------------------------------------------------------------------

def get_stage_config(stage_name, config, obs):
    """Build effective config for a NIRSpec pipeline stage.

    Merges two layers (highest priority wins):
        1. Observation-specific overrides  (observations.toml  [obs.stageN])
        2. Config defaults + user overrides (already merged in load_config)
    """
    merged = dict(config.get('nirspec', {}).get(stage_name, {}))
    merged.update(obs.stage_overrides.get(stage_name, {}))
    return merged


def get_nircam_stage_config(stage_name, config, field):
    """Build effective config for a NIRCam pipeline stage.

    Merges two layers (highest priority wins):
        1. Field-specific overrides   (fields.toml  [field.stageN])
        2. Config defaults + user overrides (already merged in load_config)
    """
    base = config.get('nircam', {}).get(stage_name, {})
    return deep_merge(base, field.stage_overrides.get(stage_name, {}))


# ---------------------------------------------------------------------------
# Template grid paths
# ---------------------------------------------------------------------------

def resolve_template_grid_paths(config):
    """Resolve template grid pickle file paths.

    If a path in template_grids.*.file is relative, resolve it relative
    to $CAMPFIRE_ROOT/cache/templates/. Absolute paths are used as-is.
    """
    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    template_grids = config.get('nirspec', {}).get('template_grids', {})

    for name, grid_config in template_grids.items():
        filepath = grid_config.get('file', '')
        if filepath and not os.path.isabs(filepath):
            if campfire_root:
                grid_config['file'] = os.path.join(
                    campfire_root, 'cache', 'templates', filepath)

    return template_grids


# ---------------------------------------------------------------------------
# Package data helpers
# ---------------------------------------------------------------------------

def get_r_curve_path(grating):
    """Get the path to an r-curve FITS file shipped as package data.

    Parameters
    ----------
    grating : str
        Grating name (e.g. 'prism', 'g395m'). Case-insensitive.

    Returns
    -------
    str
        Absolute path to the r-curve FITS file.
    """
    data_dir = Path(__file__).parent / 'data'
    filename = f'jwst_nirspec_{grating.lower()}_disp.fits'
    path = data_dir / filename
    if not path.exists():
        available = sorted(p.name for p in data_dir.glob('jwst_nirspec_*_disp.fits'))
        raise FileNotFoundError(
            f"R-curve file not found: {path}\nAvailable: {available}"
        )
    return str(path)
