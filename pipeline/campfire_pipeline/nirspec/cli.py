"""
campfire-nirspec CLI — Click-based entry point for NIRSpec pipeline.

Usage:
    campfire-nirspec stage1  --obs ember_uds_p4 [--processes 4] [--overwrite]
    campfire-nirspec stage2  --obs ember_uds_p4 [--source-ids 12345 67890]
    campfire-nirspec stage3  --obs ember_uds_p4 [--source-ids ...]
    campfire-nirspec fit     --obs ember_uds_p4 [--source-ids ...] [--overwrite]
    campfire-nirspec summary --obs ember_uds_p4
    campfire-nirspec run     --obs ember_uds_p4 --all
    campfire-nirspec make-templates [--config config.toml]
"""

import click

from campfire_pipeline.config import load_config, setup_environment, resolve_paths, get_stage_config, resolve_template_grid_paths
from campfire_pipeline.nirspec.observation import Observation
from campfire_pipeline.common.io import log


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

def common_options(f):
    """Decorator that adds --config and --obs options."""
    f = click.option('--config', default='config.toml',
                     help='Path to configuration file.')(f)
    f = click.option('--obs', required=True,
                     help='Observation name from observations.toml.')(f)
    return f


def processing_options(f):
    """Decorator that adds --source-ids, --processes, --overwrite."""
    f = click.option('--source-ids', multiple=True, type=int, default=None,
                     help='Source IDs to process (default: all).')(f)
    f = click.option('--processes', '-p', default=1, type=int,
                     help='Number of parallel processes.')(f)
    f = click.option('--overwrite', is_flag=True,
                     help='Overwrite existing products.')(f)
    return f


def _setup(config_path, obs_name):
    """Load config, set up environment, resolve paths, load observation.

    Returns (config, obs, paths) where paths is a dict with
    data_dir, products_dir.
    """
    config = load_config(config_path)
    setup_environment(config)
    paths = resolve_paths(config)
    obs = Observation.load(obs_name)
    obs.setup_workspace_directory(paths['data_dir'], paths['products_dir'], overwrite=False)
    return config, obs, paths


def _resolve_source_ids(source_ids):
    """Convert Click tuple to list or 'all'."""
    if source_ids:
        return list(source_ids)
    return 'all'


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name='campfire-pipeline')
def main():
    """CAMPFIRE NIRSpec data reduction pipeline."""
    pass


# ---------------------------------------------------------------------------
# Stage commands
# ---------------------------------------------------------------------------

@main.command()
@common_options
@click.option('--processes', '-p', default=1, type=int)
@click.option('--overwrite', is_flag=True)
def stage1(config, obs, processes, overwrite):
    """Run stage 1: Detector1Pipeline + background subtraction."""
    from campfire_pipeline.nirspec.stage1 import run_stage1

    cfg, obs_obj, paths = _setup(config, obs)
    stage_config = get_stage_config('stage1', cfg, obs_obj)
    run_stage1(obs_obj, stage_config, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage2(config, obs, source_ids, processes, overwrite):
    """Run stage 2: WCS assignment (2a) + nodded background subtraction (2b)."""
    from campfire_pipeline.nirspec.stage2 import run_stage2a, run_stage2b

    cfg, obs_obj, paths = _setup(config, obs)
    stage_config = get_stage_config('stage2', cfg, obs_obj)
    sids = _resolve_source_ids(source_ids)
    run_stage2a(obs_obj, stage_config, source_ids=sids, n_processes=processes, overwrite=overwrite)
    run_stage2b(obs_obj, stage_config, source_ids=sids, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage3(config, obs, source_ids, processes, overwrite):
    """Run stage 3: Spec3Pipeline + optimal extraction."""
    from campfire_pipeline.nirspec.stage3 import run_stage3

    cfg, obs_obj, paths = _setup(config, obs)
    stage_config = get_stage_config('stage3', cfg, obs_obj)
    sids = _resolve_source_ids(source_ids)
    run_stage3(obs_obj, stage_config, cfg, source_ids=sids, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def fit(config, obs, source_ids, processes, overwrite):
    """Run redshift fitting."""
    from campfire_pipeline.nirspec.fitting import fit_observation

    cfg, obs_obj, paths = _setup(config, obs)
    sids = _resolve_source_ids(source_ids)
    sids_list = sids if sids != 'all' else None
    fit_observation(
        obs_name=obs_obj.name,
        config=cfg,
        source_ids=sids_list,
        overwrite=overwrite,
        workspace_dir=obs_obj.workspace_dir,
        gratings=obs_obj.gratings,
    )


@main.command()
@common_options
def summary(config, obs):
    """Generate observation summary ECSV."""
    from pathlib import Path
    from campfire_pipeline.metadata.summary import (
        generate_observation_summary,
        write_summary_ecsv,
    )

    cfg, obs_obj, paths = _setup(config, obs)
    version = cfg.get('pipeline', {}).get('version', 'unknown')
    obs_dir = Path(obs_obj.workspace_dir)

    summary_table = generate_observation_summary(obs_obj.name, obs_dir,
                                                  reduction_version=version)
    if len(summary_table) > 0:
        write_summary_ecsv(summary_table, obs_dir, obs_obj.name)
    else:
        log(f"No spectra found for {obs_obj.name}, skipping summary")


# ---------------------------------------------------------------------------
# Compound commands
# ---------------------------------------------------------------------------

@main.command()
@common_options
@processing_options
@click.option('--stage1', 'do_stage1', is_flag=True, help='Run stage 1.')
@click.option('--stage2', 'do_stage2', is_flag=True, help='Run stage 2.')
@click.option('--stage3', 'do_stage3', is_flag=True, help='Run stage 3.')
@click.option('--fit', 'do_fit', is_flag=True, help='Run fitting.')
@click.option('--summary', 'do_summary', is_flag=True, help='Generate summary.')
@click.option('--all', 'do_all', is_flag=True, help='Run all stages.')
def run(config, obs, source_ids, processes, overwrite,
        do_stage1, do_stage2, do_stage3, do_fit, do_summary, do_all):
    """Run multiple pipeline stages in sequence."""
    from campfire_pipeline.nirspec.stage1 import run_stage1 as _run_stage1
    from campfire_pipeline.nirspec.stage2 import run_stage2a, run_stage2b
    from campfire_pipeline.nirspec.stage3 import run_stage3 as _run_stage3
    from campfire_pipeline.nirspec.fitting import fit_observation

    if do_all:
        do_stage1 = do_stage2 = do_stage3 = do_fit = do_summary = True

    if not any([do_stage1, do_stage2, do_stage3, do_fit, do_summary]):
        raise click.UsageError("Specify at least one stage flag, or use --all.")

    cfg, obs_obj, paths = _setup(config, obs)
    sids = _resolve_source_ids(source_ids)

    if do_stage1:
        sc = get_stage_config('stage1', cfg, obs_obj)
        _run_stage1(obs_obj, sc, n_processes=processes, overwrite=overwrite)

    if do_stage2:
        sc = get_stage_config('stage2', cfg, obs_obj)
        run_stage2a(obs_obj, sc, source_ids=sids, n_processes=processes, overwrite=overwrite)
        run_stage2b(obs_obj, sc, source_ids=sids, n_processes=processes, overwrite=overwrite)

    if do_stage3:
        sc = get_stage_config('stage3', cfg, obs_obj)
        _run_stage3(obs_obj, sc, cfg, source_ids=sids, n_processes=processes, overwrite=overwrite)

    if do_fit:
        sids_list = sids if sids != 'all' else None
        fit_observation(
            obs_name=obs_obj.name,
            config=cfg,
            source_ids=sids_list,
            overwrite=overwrite,
            workspace_dir=obs_obj.workspace_dir,
            gratings=obs_obj.gratings,
        )

    if do_summary:
        from pathlib import Path
        from campfire_pipeline.metadata.summary import (
            generate_observation_summary,
            write_summary_ecsv,
        )

        version = cfg.get('pipeline', {}).get('version', 'unknown')
        obs_dir = Path(obs_obj.workspace_dir)
        summary_table = generate_observation_summary(obs_obj.name, obs_dir,
                                                      reduction_version=version)
        if len(summary_table) > 0:
            write_summary_ecsv(summary_table, obs_dir, obs_obj.name)
        else:
            log(f"No spectra found for {obs_obj.name}, skipping summary")


# ---------------------------------------------------------------------------
# Utility commands
# ---------------------------------------------------------------------------

@main.command('make-templates')
@click.option('--config', default='config.toml', help='Path to configuration file.')
def make_templates(config):
    """Generate continuum template grids from config."""
    import os
    from campfire_pipeline.nirspec.templates import make_template_grid

    cfg = load_config(config)
    setup_environment(cfg)

    template_grids_config = resolve_template_grid_paths(cfg)
    if not template_grids_config:
        click.echo("No [template_grids] section found in config")
        raise SystemExit(1)

    for name, grid_config in template_grids_config.items():
        output_file = os.path.abspath(grid_config['file'])
        click.echo(f"\n=== Generating '{name}' template grid ===")
        make_template_grid(
            z_min=grid_config.get('z_min', 0),
            z_max=grid_config.get('z_max', 20),
            dv=grid_config['dv'],
            output_file=output_file,
        )


if __name__ == '__main__':
    main()
