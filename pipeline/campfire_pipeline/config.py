"""
Configuration loading and environment setup.

Resolves paths in order:
1. Explicit paths in config.toml
2. $CAMPFIRE_ROOT/{raw,products,cache} if env var is set
3. Raise a clear error
"""

import os
import toml


def load_config(config_path="config.toml"):
    """Load and parse configuration file with path template expansion."""
    with open(config_path, 'r') as f:
        config = toml.load(f)
    return config


def setup_environment(config):
    """Set environment variables from config file."""
    if 'environment' in config:
        env = config['environment']
        for key, value in env.items():
            os.environ[key] = str(value)


def resolve_paths(config):
    """Extract and create pipeline directories from config.

    Returns dict with keys: data_dir, products_dir, pictureframe_dir.
    """
    paths = config.get('paths', {})
    result = {
        'data_dir': paths.get('data_dir'),
        'products_dir': paths.get('products_dir'),
        'pictureframe_dir': paths.get('pictureframe_dir'),
    }
    for d in result.values():
        if d:
            os.makedirs(d, exist_ok=True)
    return result


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
