"""
NIRCam CLI — Click-based entry point for NIRCam pipeline.

Usage (via unified CLI):
    cfpipe nircam stage1  --field cosmos [--filters f444w f150w] [-p 4] [--overwrite]
    cfpipe nircam stage2  --field cosmos [--filters f444w] [-p 4] [--overwrite]
    cfpipe nircam stage3  --field cosmos [--filters f444w] [--overwrite]
    cfpipe nircam run     --field cosmos --all [-p 4]

Also available directly as: campfire-nircam <command>
"""

import os

import matplotlib
matplotlib.use('Agg')

import click

from campfire_pipeline.config import (
    load_config,
    setup_environment,
    get_nircam_stage_config,
)
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.common.io import log


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

def common_options(f):
    """Decorator that adds --config and --field options."""
    f = click.option('--config', default=None,
                     help='Path to configuration file.')(f)
    f = click.option('--field', required=True,
                     help='Field name from fields.toml.')(f)
    return f


def processing_options(f):
    """Decorator that adds --filters, --processes, --overwrite."""
    f = click.option('--filters', multiple=True, default=None,
                     help='Filters to process (default: all from field).')(f)
    f = click.option('--processes', '-p', default=1, type=int,
                     help='Number of parallel processes.')(f)
    f = click.option('--overwrite', is_flag=True,
                     help='Overwrite existing products.')(f)
    return f


def _setup(config_path, field_name):
    """Load config, set up environment, load field, set up workspace.

    Returns (config, field_obj).
    """
    config = load_config(config_path)
    setup_environment(config)
    field_obj = Field.load(field_name)
    field_obj.setup_workspace()
    return config, field_obj


def _resolve_filters(filters, field_obj):
    """Convert Click tuple to list, defaulting to field.filters."""
    if filters:
        return list(filters)
    return list(field_obj.filters)


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name='campfire-pipeline')
def main():
    """CAMPFIRE NIRCam imaging reduction pipeline."""
    pass


# ---------------------------------------------------------------------------
# Stage commands
# ---------------------------------------------------------------------------

@main.command()
@common_options
@processing_options
def stage1(config, field, filters, processes, overwrite):
    """Run stage 1: Detector1Pipeline + snowball/wisp/striping/persistence."""
    from campfire_pipeline.nircam.stage1 import run_stage1

    cfg, field_obj = _setup(config, field)
    stage_config = get_nircam_stage_config('stage1', cfg, field_obj)
    filter_list = _resolve_filters(filters, field_obj)
    run_stage1(field_obj, stage_config, filters=filter_list,
               n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage2(config, field, filters, processes, overwrite):
    """Run stage 2: Image2Pipeline + edge/sky/diagonal/variance/masks."""
    from campfire_pipeline.nircam.stage2 import run_stage2

    cfg, field_obj = _setup(config, field)
    stage_config = get_nircam_stage_config('stage2', cfg, field_obj)
    filter_list = _resolve_filters(filters, field_obj)
    run_stage2(field_obj, stage_config, filters=filter_list,
               n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def stage3(config, field, filters, processes, overwrite):
    """Run stage 3: JHAT + bad pixels + skymatch + outlier + resample."""
    from campfire_pipeline.nircam.stage3 import run_stage3

    cfg, field_obj = _setup(config, field)
    stage_config = get_nircam_stage_config('stage3', cfg, field_obj)
    filter_list = _resolve_filters(filters, field_obj)
    run_stage3(field_obj, stage_config, filters=filter_list,
               n_processes=processes, overwrite=overwrite)


# ---------------------------------------------------------------------------
# Compound command
# ---------------------------------------------------------------------------

@main.command()
@common_options
@processing_options
@click.option('--stage1', 'do_stage1', is_flag=True, help='Run stage 1.')
@click.option('--stage2', 'do_stage2', is_flag=True, help='Run stage 2.')
@click.option('--stage3', 'do_stage3', is_flag=True, help='Run stage 3.')
@click.option('--all', 'do_all', is_flag=True, help='Run all stages.')
def run(config, field, filters, processes, overwrite,
        do_stage1, do_stage2, do_stage3, do_all):
    """Run multiple pipeline stages in sequence."""
    from campfire_pipeline.nircam.stage1 import run_stage1 as _run_stage1
    from campfire_pipeline.nircam.stage2 import run_stage2 as _run_stage2
    from campfire_pipeline.nircam.stage3 import run_stage3 as _run_stage3

    if do_all:
        do_stage1 = do_stage2 = do_stage3 = True

    if not any([do_stage1, do_stage2, do_stage3]):
        raise click.UsageError("Specify at least one stage flag, or use --all.")

    cfg, field_obj = _setup(config, field)
    filter_list = _resolve_filters(filters, field_obj)

    if do_stage1:
        sc = get_nircam_stage_config('stage1', cfg, field_obj)
        _run_stage1(field_obj, sc, filters=filter_list,
                    n_processes=processes, overwrite=overwrite)

    if do_stage2:
        sc = get_nircam_stage_config('stage2', cfg, field_obj)
        _run_stage2(field_obj, sc, filters=filter_list,
                    n_processes=processes, overwrite=overwrite)

    if do_stage3:
        sc = get_nircam_stage_config('stage3', cfg, field_obj)
        _run_stage3(field_obj, sc, filters=filter_list,
                    n_processes=processes, overwrite=overwrite)


if __name__ == '__main__':
    main()
