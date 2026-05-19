"""
MIRI CLI — Click entry point for the MIRI imaging reduction pipeline.

v1 stub. The phase commands work end-to-end (workspace setup, filter
resolution, status scan, prefetch hook), they just iterate over empty
step lists. As reduction steps land, ``orchestrate.PROCESS_STEPS`` /
``COMBINE_STEPS`` / ``_RUNNERS`` get populated and the per-step
commands automatically appear in ``cfpipe miri --help`` (they're
registered programmatically from ``orchestrate.STEP_NAMES``).
"""

from campfire_pipeline import _thread_caps  # noqa: F401  (must precede numpy/matplotlib)

import os

import matplotlib
matplotlib.use('Agg')

import click

from campfire_pipeline.common.cli import VariadicOption
from campfire_pipeline.common.io import log
from campfire_pipeline.config import load_config, setup_environment
from campfire_pipeline.miri import cfp as cfp_mod
from campfire_pipeline.miri.field import MiriField
from campfire_pipeline.miri.orchestrate import (
    ALL_STEPS, COMBINE_STEPS, PROCESS_STEPS, STEP_NAMES,
    run_combine, run_process, run_step,
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
                     cls=VariadicOption,
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
        field_obj = MiriField.load(field_name)
    except FileNotFoundError as e:
        raise click.ClickException(
            f"{e}\n\n"
            "MIRI reductions need a fields.toml at "
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
    """CAMPFIRE MIRI imaging reduction pipeline (v1 stub)."""
    pass


# ---------------------------------------------------------------------------
# Phase commands
# ---------------------------------------------------------------------------

@main.command()
@common_options
@processing_options
def process(config, field, filters, processes, overwrite):
    """Run the per-exposure process phase."""
    cfg, field_obj = _setup(config, field)
    run_process(field_obj, cfg,
                filters=_resolve_filters(filters, field_obj),
                n_processes=processes, overwrite=overwrite)


@main.command()
@common_options
@processing_options
def combine(config, field, filters, processes, overwrite):
    """Run the ensemble combine phase."""
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
# Per-step commands (auto-registered from STEP_NAMES — empty in v1)
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
# Status / reset (work with whatever CFP keys exist; empty list → empty table)
# ---------------------------------------------------------------------------

@main.command()
@common_options
@click.option('--filters', multiple=True, default=None, cls=VariadicOption,
              help='Filters to check (default: all from field).')
def status(config, field, filters):
    """Show CFP_* completion status for each canonical exposure."""
    cfg, field_obj = _setup(config, field)
    filter_list = _resolve_filters(filters, field_obj)

    steps_with_cfp = [(n, k) for n, k in ALL_STEPS if k is not None]
    if not steps_with_cfp:
        log("MIRI pipeline stub: no CFP-stamping steps defined yet "
            "(see docs/design-miri-reduction.md)")
        return

    for filt in filter_list:
        exposures = field_obj.get_exposure_files(filt)
        if not exposures:
            log(f'{filt}: no canonical exposure files in '
                f'{field_obj.filter_dir(filt)}')
            continue

        per_exp = {
            os.path.basename(f).removesuffix('.fits'): cfp_mod.get_steps(f)
            for f in sorted(exposures)
        }

        log('')
        log(f'Field: {field_obj.name}  |  Filter: {filt}  |  '
            f'{len(per_exp)} exposures')

        for name, key in steps_with_cfp:
            done = sum(1 for steps in per_exp.values() if key in steps)
            log(f'  {name:20s} {done:>3d}/{len(per_exp)} done')


@main.command()
@common_options
@click.option('--filters', multiple=True, default=None, cls=VariadicOption,
              help='Filters to reset (default: all from field).')
@click.option('--from', 'from_step', default=None,
              type=click.Choice(STEP_NAMES) if STEP_NAMES else None,
              help='Clear CFP_<step> + every later CFP key on each '
                   'canonical exposure.')
@click.option('--uncal', 'uncal', is_flag=True,
              help='Delete every canonical exposure file for the selected filters.')
@click.option('--yes', is_flag=True,
              help='Skip the confirmation prompt.')
def reset(config, field, filters, from_step, uncal, yes):
    """Reset CFP keys (or wipe canonical files) to re-run from a given step."""
    if not (uncal or from_step):
        raise click.UsageError("Specify --uncal or --from <step>.")
    if uncal and from_step:
        raise click.UsageError(
            "--uncal and --from are mutually exclusive."
        )

    cfg, field_obj = _setup(config, field)
    filter_list = _resolve_filters(filters, field_obj)

    affected = []
    for filt in filter_list:
        affected.extend(field_obj.get_exposure_files(filt))

    if uncal:
        action = (
            f"DELETE {len(affected)} canonical exposure files "
            f"across filters {filter_list}"
        )
    else:
        cfp_key = _step_to_cfp_key(from_step)
        affected = [f for f in affected if cfp_mod.has_step(f, cfp_key)]
        action = (
            f"CLEAR {cfp_key}+ keys on {len(affected)} canonical exposures "
            f"across filters {filter_list}"
        )

    log(f'Reset action: {action}')
    if not affected:
        log('Nothing to reset.')
        return

    if not yes and not click.confirm('Proceed?'):
        log('Aborted.')
        return

    if uncal:
        for path in affected:
            try:
                os.remove(path)
                log(f'  removed {os.path.basename(path)}')
            except OSError as e:
                log(f'  could not remove {path}: {e}')
    else:
        cfp_key = _step_to_cfp_key(from_step)
        for path in affected:
            try:
                cfp_mod.clear_from(path, cfp_key)
                log(f'  cleared {cfp_key}+ on {os.path.basename(path)}')
            except Exception as e:
                log(f'  could not clear keys on {path}: {e}')


def _step_to_cfp_key(step_name):
    """Look up the CFP_* key for a step (raises if it has none)."""
    for name, key in ALL_STEPS:
        if name == step_name:
            if key is None:
                raise click.ClickException(
                    f"Step {step_name!r} has no CFP key — nothing to clear."
                )
            return key
    raise click.ClickException(f"Unknown step: {step_name!r}")


if __name__ == '__main__':
    main()
