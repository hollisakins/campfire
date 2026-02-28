"""
NIRSpec CLI — Click-based entry point for NIRSpec pipeline.

Usage (via unified CLI):
    cfpipe nirspec stage1   --obs ember_uds_p4 [--processes 4] [--overwrite]
    cfpipe nirspec stage2a  --obs ember_uds_p4 [--source-ids 12345 67890]
    cfpipe nirspec stage2b  --obs ember_uds_p4 [--source-ids 12345 67890]
    cfpipe nirspec stage3   --obs ember_uds_p4 [--source-ids ...]
    cfpipe nirspec zfit     --obs ember_uds_p4 [--source-ids ...] [--overwrite]
    cfpipe nirspec summary  --obs ember_uds_p4
    cfpipe nirspec run      --obs ember_uds_p4 --all
    cfpipe nirspec make-templates [--config config.toml]

Also available directly as: campfire-nirspec <command>
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
    f = click.option('--config', default=None,
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
def stage2a(config, obs, source_ids, processes, overwrite):
    """Run stage 2a: WCS assignment + rectification."""
    from campfire_pipeline.nirspec.stage2 import run_stage2a as _run_stage2a

    cfg, obs_obj, paths = _setup(config, obs)
    stage_config = get_stage_config('stage2', cfg, obs_obj)
    sids = _resolve_source_ids(source_ids)
    _run_stage2a(obs_obj, stage_config, source_ids=sids, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage2b(config, obs, source_ids, processes, overwrite):
    """Run stage 2b: nodded background subtraction."""
    from campfire_pipeline.nirspec.stage2 import run_stage2b as _run_stage2b

    cfg, obs_obj, paths = _setup(config, obs)
    stage_config = get_stage_config('stage2', cfg, obs_obj)
    sids = _resolve_source_ids(source_ids)
    _run_stage2b(obs_obj, stage_config, source_ids=sids, n_processes=processes, overwrite=overwrite)


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
def zfit(config, obs, source_ids, processes, overwrite):
    """Run redshift fitting."""
    from campfire_pipeline.nirspec.redshift_fitting import fit_redshifts

    cfg, obs_obj, paths = _setup(config, obs)
    sids = _resolve_source_ids(source_ids)
    sids_list = sids if sids != 'all' else None
    fit_redshifts(
        obs_name=obs_obj.name,
        config=cfg,
        source_ids=sids_list,
        overwrite=overwrite,
        workspace_dir=obs_obj.workspace_dir,
        n_processes=processes,
    )


def _run_summary(cfg, obs_obj):
    """Generate (or regenerate) the observation summary ECSV."""
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


@main.command()
@common_options
def summary(config, obs):
    """Generate observation summary ECSV."""
    cfg, obs_obj, paths = _setup(config, obs)
    _run_summary(cfg, obs_obj)


# ---------------------------------------------------------------------------
# Compound commands
# ---------------------------------------------------------------------------

@main.command()
@common_options
@processing_options
@click.option('--stage1', 'do_stage1', is_flag=True, help='Run stage 1.')
@click.option('--stage2a', 'do_stage2a', is_flag=True, help='Run stage 2a.')
@click.option('--stage2b', 'do_stage2b', is_flag=True, help='Run stage 2b.')
@click.option('--stage3', 'do_stage3', is_flag=True, help='Run stage 3.')
@click.option('--zfit', 'do_zfit', is_flag=True, help='Run redshift fitting.')
@click.option('--summary', 'do_summary', is_flag=True, help='Generate summary.')
@click.option('--all', 'do_all', is_flag=True, help='Run all stages.')
def run(config, obs, source_ids, processes, overwrite,
        do_stage1, do_stage2a, do_stage2b, do_stage3, do_zfit, do_summary, do_all):
    """Run multiple pipeline stages in sequence."""
    from campfire_pipeline.nirspec.stage1 import run_stage1 as _run_stage1
    from campfire_pipeline.nirspec.stage2 import run_stage2a, run_stage2b
    from campfire_pipeline.nirspec.stage3 import run_stage3 as _run_stage3
    from campfire_pipeline.nirspec.redshift_fitting import fit_redshifts

    if do_all:
        do_stage1 = do_stage2a = do_stage2b = do_stage3 = do_zfit = do_summary = True

    if not any([do_stage1, do_stage2a, do_stage2b, do_stage3, do_zfit, do_summary]):
        raise click.UsageError("Specify at least one stage flag, or use --all.")

    cfg, obs_obj, paths = _setup(config, obs)
    sids = _resolve_source_ids(source_ids)

    if do_stage1:
        sc = get_stage_config('stage1', cfg, obs_obj)
        _run_stage1(obs_obj, sc, n_processes=processes, overwrite=overwrite)

    if do_stage2a:
        sc = get_stage_config('stage2', cfg, obs_obj)
        run_stage2a(obs_obj, sc, source_ids=sids, n_processes=processes, overwrite=overwrite)

    if do_stage2b:
        sc = get_stage_config('stage2', cfg, obs_obj)
        run_stage2b(obs_obj, sc, source_ids=sids, n_processes=processes, overwrite=overwrite)

    if do_stage3:
        sc = get_stage_config('stage3', cfg, obs_obj)
        _run_stage3(obs_obj, sc, cfg, source_ids=sids, n_processes=processes, overwrite=overwrite)

    if do_summary:
        _run_summary(cfg, obs_obj)

    if do_zfit:
        sids_list = sids if sids != 'all' else None
        fit_redshifts(
            obs_name=obs_obj.name,
            config=cfg,
            source_ids=sids_list,
            overwrite=overwrite,
            workspace_dir=obs_obj.workspace_dir,
            n_processes=processes,
        )
        # Re-generate summary with redshift results
        if do_summary:
            _run_summary(cfg, obs_obj)


# ---------------------------------------------------------------------------
# Utility commands
# ---------------------------------------------------------------------------

@main.command('make-templates')
@click.option('--config', default=None, help='Path to configuration file.')
def make_templates(config):
    """Generate continuum template grids from config.

    Generates sfhz-format (rest-frame, z-binned) templates when
    format="sfhz" is set in config, or legacy format otherwise.
    """
    import os
    import numpy as np
    from campfire_pipeline.nirspec.templates import make_template_grid, make_sfhz_template_grid

    cfg = load_config(config)
    setup_environment(cfg)

    template_grids_config = resolve_template_grid_paths(cfg)
    if not template_grids_config:
        click.echo("No [template_grids] section found in config")
        raise SystemExit(1)

    for name, grid_config in template_grids_config.items():
        output_file = os.path.abspath(grid_config['file'])
        fmt = grid_config.get('format', 'legacy')

        click.echo(f"\n=== Generating '{name}' template grid (format={fmt}) ===")

        if fmt == 'sfhz':
            z_bin_edges = grid_config.get('z_bin_edges', None)
            if z_bin_edges is not None:
                z_bin_edges = np.array(z_bin_edges, dtype=float)
            make_sfhz_template_grid(
                z_min=grid_config.get('z_min', 0),
                z_max=grid_config.get('z_max', 20),
                dv=grid_config['dv'],
                z_bin_edges=z_bin_edges,
                output_file=output_file,
            )
        else:
            make_template_grid(
                z_min=grid_config.get('z_min', 0),
                z_max=grid_config.get('z_max', 20),
                dv=grid_config['dv'],
                output_file=output_file,
            )


if __name__ == '__main__':
    main()
