"""
NIRSpec CLI — Click-based entry point for NIRSpec pipeline.

Usage (via unified CLI):
    cfpipe nirspec stage1   --obs ember_uds_p4 [--processes 4] [--overwrite]
    cfpipe nirspec stage1   --obs ember_uds_p4 ember_uds_p5 -p 4
    cfpipe nirspec stage2a  --obs ember_uds_p4 [--source-ids 12345 67890]
    cfpipe nirspec stage2b  --obs ember_uds_p4 [--source-ids 12345 67890]
    cfpipe nirspec detect-stuck --obs ember_uds_p4 [-p 4]
    cfpipe nirspec stage3   --obs ember_uds_p4 [--source-ids ...]
    cfpipe nirspec zfit     --obs ember_uds_p4 [--source-ids ...] [--overwrite]
    cfpipe nirspec summary  --obs ember_uds_p4
    cfpipe nirspec run      --obs ember_uds_p4 --all
    cfpipe nirspec make-templates [--config config.toml]

Multiple observations are processed serially:
    cfpipe nirspec run --obs ember_uds_p4 ember_uds_p5 ember_uds_p6 --all -p 4

Also available directly as: campfire-nirspec <command>
"""

import matplotlib
matplotlib.use('Agg')

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
    f = click.option('--obs', required=True, multiple=True, type=str,
                     cls=_VariadicOption,
                     help='Observation name(s) from observations.toml.')(f)
    return f


class _VariadicOption(click.Option):
    """Click option that consumes multiple space-separated values after a single flag."""

    def add_to_parser(self, parser, ctx):
        super().add_to_parser(parser, ctx)
        name = self.opts[-1]
        opt = parser._long_opt.get(name)
        if opt is None:
            return
        original_process = opt.process

        def _eat_remaining(value, state):
            original_process(value, state)
            while state.rargs and not state.rargs[0].startswith('-'):
                original_process(state.rargs.pop(0), state)

        opt.process = _eat_remaining


def _parse_source_ids(ctx, param, value):
    """Convert space-separated, comma-separated, or repeated flag values to int tuple."""
    if not value:
        return None
    result = []
    for item in value:
        for part in item.replace(',', ' ').split():
            try:
                result.append(int(part))
            except ValueError:
                raise click.BadParameter(f"'{part}' is not a valid integer.")
    return tuple(result) if result else None


def processing_options(f):
    """Decorator that adds --source-ids, --processes, --overwrite."""
    f = click.option('--source-ids', multiple=True, type=str, default=None,
                     cls=_VariadicOption, callback=_parse_source_ids,
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

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage_config = get_stage_config('stage1', cfg, obs_obj)
        run_stage1(obs_obj, stage_config, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage2a(config, obs, source_ids, processes, overwrite):
    """Run stage 2a: WCS assignment + rectification."""
    from campfire_pipeline.nirspec.stage2 import run_stage2a as _run_stage2a

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage_config = get_stage_config('stage2', cfg, obs_obj)
        sids = _resolve_source_ids(source_ids)
        _run_stage2a(obs_obj, stage_config, source_ids=sids, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage2b(config, obs, source_ids, processes, overwrite):
    """Run stage 2b: nodded background subtraction."""
    from campfire_pipeline.nirspec.stage2 import run_stage2b as _run_stage2b

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage_config = get_stage_config('stage2', cfg, obs_obj)
        sids = _resolve_source_ids(source_ids)
        _run_stage2b(obs_obj, stage_config, source_ids=sids, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage3(config, obs, source_ids, processes, overwrite):
    """Run stage 3: Spec3Pipeline + optimal extraction."""
    from campfire_pipeline.nirspec.stage3 import run_stage3

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage_config = get_stage_config('stage3', cfg, obs_obj)
        sids = _resolve_source_ids(source_ids)
        run_stage3(obs_obj, stage_config, cfg, source_ids=sids, n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def zfit(config, obs, source_ids, processes, overwrite):
    """Run redshift fitting."""
    from campfire_pipeline.nirspec.redshift_fitting import fit_redshifts

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
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


@main.command('detect-stuck')
@common_options
@click.option('--processes', '-p', default=1, type=int,
              help='Number of parallel processes.')
@click.option('--source-ids', multiple=True, type=str, default=None,
              cls=_VariadicOption, callback=_parse_source_ids,
              help='Source IDs to analyze (default: all).')
@click.option('--overwrite', is_flag=True,
              help='Replace existing TOML with fresh detection results.')
def detect_stuck(config, obs, processes, source_ids, overwrite):
    """Run stuck shutter detection on existing s2d files.

    Detects stuck shutters, writes results to the observation's TOML file,
    and generates diagnostic plots.  Does NOT re-run stage2a — use this to
    inspect results before re-running downstream stages manually.

    Typical workflow for an already-completed observation:

    \b
      1. cfpipe nirspec detect-stuck --obs my_obs -p 4
      2. Inspect plots in workspace/stuck_shutters/ and edit TOML if needed
      3. cfpipe nirspec run --obs my_obs --stage2a --stage2b --stage3 \\
             --source-ids <affected IDs> --overwrite -p 4
    """
    import numpy as np
    import toml as _toml
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.stage2 import resample_single_exposure
    from campfire_pipeline.nirspec.stuck_shutters import (
        detect_stuck_shutters, merge_stuck_shutters,
        write_stuck_shutters_toml, _get_n_shutters,
    )
    from campfire_pipeline.nirspec.plots import plot_stuck_shutter_diagnostics

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage_config = get_stage_config('stage2', cfg, obs_obj)

        # Force detection enabled (the user explicitly asked for it)
        stage_config['detect_stuck_shutters'] = True

        sids = _resolve_source_ids(source_ids)
        files = obs_obj.discover_files(ext='cal', source_ids=sids)
        files = Observation.group_files(files)
        log(f'Found {len(files)} cal files for {obs_name}')

        # Ensure s2d files exist (skips if already present)
        dispatch(resample_single_exposure, list(files), n_processes=processes)

        detected = detect_stuck_shutters(obs_obj, files, stage_config,
                                         n_processes=processes)

        if detected:
            if overwrite:
                # Write fresh detection results, replacing existing TOML
                all_detected = set(
                    (root, sid)
                    for root, sources in detected.items()
                    for sid in sources.keys()
                )
                write_stuck_shutters_toml(
                    {r: {str(s): sh for s, sh in srcs.items()}
                     for r, srcs in detected.items()},
                    obs_obj.stuck_closed_shutters_file, obs_obj.name,
                    auto_detected=all_detected,
                )
            else:
                # Merge with existing TOML entries (manual entries preserved)
                existing = _toml.load(obs_obj.stuck_closed_shutters_file)
                merged, updated = merge_stuck_shutters(existing, detected)
                write_stuck_shutters_toml(
                    merged, obs_obj.stuck_closed_shutters_file, obs_obj.name,
                    auto_detected=updated,
                )

            # Generate diagnostic plots
            for root, sources in detected.items():
                for sid, stuck_list in sources.items():
                    root_files = files[(files['root'] == root) &
                                       (files['source_id'] == sid)]
                    n_shut = _get_n_shutters(root_files)
                    plot_stuck_shutter_diagnostics(
                        files, sid, root, obs_obj.workspace_dir,
                        n_shut, stuck_list, stage_config,
                    )

            # Regenerate nods plots with stuck shutter annotations
            from campfire_pipeline.nirspec.plots import plot_stage2a_results
            stuck_dict = {}
            for row in obs_obj.stuck_closed_shutters:
                r = row['root']
                sid = int(row['source_id'])
                stuck_dict.setdefault(r, {})[sid] = list(row['shutters'])
            affected_sids = sorted(set(
                sid for src in detected.values() for sid in src.keys()
            ))
            plot_inputs = [files[files['source_id'] == sid]
                           for sid in affected_sids]
            dispatch(plot_stage2a_results, plot_inputs,
                     n_processes=processes, stuck_shutters=stuck_dict)

            # Print affected source IDs for easy copy-paste
            log(f'\nAffected source IDs ({len(affected_sids)}): '
                f'{" ".join(str(s) for s in affected_sids)}')
            log(f'\nTo re-run downstream stages:')
            log(f'  cfpipe nirspec run --obs {obs_name} '
                f'--stage2a --stage2b --stage3 '
                f'--source-ids {" ".join(str(s) for s in affected_sids)} '
                f'--overwrite -p {processes}')


def _run_summary(cfg, obs_obj):
    """Generate (or regenerate) the observation summary ECSV and shutters ECSV."""
    from pathlib import Path
    from campfire_pipeline.metadata.summary import (
        generate_observation_summary,
        write_effective_config,
        write_summary_ecsv,
    )
    from campfire_pipeline.metadata.shutters import (
        generate_shutters_table,
        write_shutters_ecsv,
    )

    version = cfg.get('pipeline', {}).get('version', 'unknown')
    consensus_config = cfg.get('nirspec', {}).get('redshift_consensus', {})
    obs_dir = Path(obs_obj.workspace_dir)
    summary_table = generate_observation_summary(obs_obj.name, obs_dir,
                                                  reduction_version=version,
                                                  field=obs_obj.field,
                                                  program_slug=obs_obj.program,
                                                  consensus_config=consensus_config)
    if len(summary_table) > 0:
        write_summary_ecsv(summary_table, obs_dir, obs_obj.name)
    else:
        log(f"No spectra found for {obs_obj.name}, skipping summary")

    # Write effective config for provenance tracking
    write_effective_config(cfg, obs_dir, obs_obj.name,
                           obs_stage_overrides=obs_obj.stage_overrides)

    # Generate shutters ECSV
    shutters_table = generate_shutters_table(obs_obj.name, obs_dir, obs_obj.field)
    if len(shutters_table) > 0:
        write_shutters_ecsv(shutters_table, obs_dir, obs_obj.name)
    else:
        log(f"No shutters generated for {obs_obj.name}")


@main.command()
@common_options
def summary(config, obs):
    """Generate observation summary ECSV."""
    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
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

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
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
