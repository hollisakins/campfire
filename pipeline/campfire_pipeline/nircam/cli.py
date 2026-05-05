"""
NIRCam CLI — Click entry point for the canonical-exposure pipeline.

Top-level commands:

    cfpipe nircam process    # per-exposure phase (detector1 → jhat)
    cfpipe nircam combine    # ensemble phase    (apply_mask → resample)
    cfpipe nircam <step>     # any of the 14 individual steps
    cfpipe nircam run --all  # whole pipeline
    cfpipe nircam check      # tile-staleness probe
    cfpipe nircam status     # per-exposure CFP_* completion table  (TODO)
    cfpipe nircam reset      # clear CFP_* keys / wipe canonical files (TODO)

Status and reset land in a follow-up commit. Per-step commands are
registered programmatically from ``orchestrate.STEP_NAMES`` so the
top-level help auto-includes them.
"""

import os

import matplotlib
matplotlib.use('Agg')

import click

from campfire_pipeline.config import load_config, setup_environment
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.orchestrate import (
    STEP_NAMES, PROCESS_STEPS, COMBINE_STEPS,
    run_process, run_combine, run_step,
)


# ---------------------------------------------------------------------------
# Shared options
# ---------------------------------------------------------------------------

def common_options(f):
    """``--config`` and ``--field``."""
    f = click.option('--config', default=None,
                     help='Path to configuration file.')(f)
    f = click.option('--field', required=True,
                     help='Field name from fields.toml.')(f)
    return f


def processing_options(f):
    """``--filters``, ``-p`` / ``--processes``, ``--overwrite``."""
    f = click.option('--filters', multiple=True, default=None,
                     help='Filters to process (default: all from field).')(f)
    f = click.option('--processes', '-p', default=1, type=int,
                     help='Number of parallel processes.')(f)
    f = click.option('--overwrite', is_flag=True,
                     help='Overwrite existing products.')(f)
    return f


def _setup(config_path, field_name):
    """Load config + environment + field; set up the workspace."""
    config = load_config(config_path)
    setup_environment(config)
    try:
        field_obj = Field.load(field_name)
    except FileNotFoundError as e:
        raise click.ClickException(
            f"{e}\n\n"
            "NIRCam reductions need a fields.toml at "
            "$CAMPFIRE_ROOT/config/fields.toml. "
            "See pipeline/fields.example.toml for the schema."
        )
    except ValueError as e:
        raise click.ClickException(str(e))
    field_obj.setup_workspace()
    return config, field_obj


def _resolve_filters(filters, field_obj):
    if filters:
        return list(filters)
    return list(field_obj.filters)


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name='campfire-pipeline')
def main():
    """CAMPFIRE NIRCam imaging reduction pipeline."""
    pass


# ---------------------------------------------------------------------------
# Phase commands
# ---------------------------------------------------------------------------

@main.command()
@common_options
@processing_options
def process(config, field, filters, processes, overwrite):
    """Run the per-exposure process phase (detector1 → jhat)."""
    cfg, field_obj = _setup(config, field)
    run_process(field_obj, cfg,
                filters=_resolve_filters(filters, field_obj),
                n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def combine(config, field, filters, processes, overwrite):
    """Run the ensemble combine phase (apply_mask → resample)."""
    cfg, field_obj = _setup(config, field)
    run_combine(field_obj, cfg,
                filters=_resolve_filters(filters, field_obj),
                n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
@click.option('--process', 'do_process', is_flag=True,
              help='Run the process phase.')
@click.option('--combine', 'do_combine', is_flag=True,
              help='Run the combine phase.')
@click.option('--all', 'do_all', is_flag=True,
              help='Run both phases.')
def run(config, field, filters, processes, overwrite,
        do_process, do_combine, do_all):
    """Run process and/or combine in one invocation."""
    if do_all:
        do_process = do_combine = True
    if not (do_process or do_combine):
        raise click.UsageError(
            "Specify --process, --combine, or --all."
        )

    cfg, field_obj = _setup(config, field)
    filter_list = _resolve_filters(filters, field_obj)

    if do_process:
        run_process(field_obj, cfg, filters=filter_list,
                    n_processes=processes, overwrite=overwrite)
    if do_combine:
        run_combine(field_obj, cfg, filters=filter_list,
                    n_processes=processes, overwrite=overwrite)


# ---------------------------------------------------------------------------
# Per-step commands (auto-registered from STEP_NAMES)
# ---------------------------------------------------------------------------

def _make_step_command(step_name):
    """Build a Click command for a single step."""
    @click.command(name=step_name,
                   help=f"Run the {step_name} step.")
    @common_options
    @processing_options
    def _cmd(config, field, filters, processes, overwrite):
        cfg, field_obj = _setup(config, field)
        run_step(step_name, field_obj, cfg,
                 filters=_resolve_filters(filters, field_obj),
                 n_processes=processes, overwrite=overwrite)
    return _cmd


for _step_name in STEP_NAMES:
    main.add_command(_make_step_command(_step_name))


# ---------------------------------------------------------------------------
# Tile-staleness probe (kept from legacy CLI)
# ---------------------------------------------------------------------------

@main.command()
@common_options
@click.option('--filters', multiple=True, default=None,
              help='Filters to check (default: all from field).')
def check(config, field, filters):
    """Report which mosaic tiles are stale and need re-mosaicking."""
    from campfire_pipeline.nircam.manifest import get_stale_tiles
    from campfire_pipeline.config import get_nircam_step_config

    cfg, field_obj = _setup(config, field)
    filter_list = _resolve_filters(filters, field_obj)

    any_stale = False
    for filtname in filter_list:
        # ``get_stale_tiles`` expects a stage_config-shaped dict with a
        # 'resample' sub-block, matching the legacy [nircam.stage3] layout.
        # Wrap the new flat config in that shape so the helper still works.
        resample_cfg = get_nircam_step_config('resample', cfg, field_obj)
        files_to_skip = resample_cfg.get('files_to_skip', [])
        wrapped = {
            'resample': resample_cfg,
            'files_to_skip': files_to_skip,
        }
        results = get_stale_tiles(field_obj, filtname, wrapped)
        if not results:
            log(f'{filtname}: no tiles configured')
            continue

        stale = [r for r in results if r['stale']]
        fresh = [r for r in results if not r['stale']]

        if stale:
            any_stale = True
            for r in stale:
                reasons = '; '.join(r['reasons'])
                log(f'{filtname}/{r["tile"]}: STALE ({r["n_inputs"]} inputs)'
                    f' — {reasons}')
        for r in fresh:
            log(f'{filtname}/{r["tile"]}: up to date '
                f'({r["n_inputs"]} inputs)')

    if not any_stale:
        log('All tiles are up to date.')


if __name__ == '__main__':
    main()
