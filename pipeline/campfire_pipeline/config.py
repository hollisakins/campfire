"""
Configuration loading and environment setup.

Resolves paths in order:
1. Explicit paths in config.toml
2. $CAMPFIRE_ROOT/{raw,products,cache} if env var is set
3. Raise a clear error
"""

import os
from pathlib import Path

import toml


def load_config(config_path="config.toml"):
    """Load and parse configuration file with path template expansion."""
    with open(config_path, 'r') as f:
        config = toml.load(f)
    return config


def _resolve_path(config_value, campfire_root, default_subdir, label):
    """Resolve a single path from config value or $CAMPFIRE_ROOT.

    Parameters
    ----------
    config_value : str or None
        Explicit path from config.toml (takes priority).
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


def resolve_template_grid_paths(config):
    """Resolve template grid pickle file paths.

    If a path in template_grids.*.file is relative, resolve it relative
    to $CAMPFIRE_ROOT/cache/templates/. Absolute paths are used as-is.
    """
    campfire_root = os.environ.get('CAMPFIRE_ROOT')
    template_grids = config.get('template_grids', {})

    for name, grid_config in template_grids.items():
        filepath = grid_config.get('file', '')
        if filepath and not os.path.isabs(filepath):
            if campfire_root:
                grid_config['file'] = os.path.join(
                    campfire_root, 'cache', 'templates', filepath)
            # else: leave relative path as-is (will fail at load time with clear error)

    return template_grids


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


def get_stage_config(stage_name, config, obs):
    """Build effective config for a pipeline stage.

    Merges three layers (highest priority wins):
        1. Observation-specific overrides  (observations.toml  [obs.stageN])
        2. Global config                   (config.toml        [stageN])
        3. Hardcoded defaults              (DEFAULT_STAGEN_CONFIG)
    """
    from campfire_pipeline.nirspec.constants import (
        DEFAULT_STAGE1_CONFIG,
        DEFAULT_STAGE2_CONFIG,
        DEFAULT_STAGE3_CONFIG,
    )
    defaults = {
        'stage1': DEFAULT_STAGE1_CONFIG,
        'stage2': DEFAULT_STAGE2_CONFIG,
        'stage3': DEFAULT_STAGE3_CONFIG,
    }
    merged = dict(defaults.get(stage_name, {}))
    merged.update(config.get(stage_name, {}))
    merged.update(obs.stage_overrides.get(stage_name, {}))
    return merged
