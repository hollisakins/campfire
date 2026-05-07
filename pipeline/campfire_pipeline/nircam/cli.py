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
from campfire_pipeline.common import cfp as cfp_mod
from campfire_pipeline.nircam.orchestrate import (
    STEP_NAMES, ALL_STEPS, PROCESS_STEPS, COMBINE_STEPS,
    run_process, run_combine, run_step,
)
from campfire_pipeline.nircam.refcat.cli import refcat as refcat_group


# Steps whose mutation is not reversible by re-running on the already-mutated
# data (subtraction or photom that would compound). `cfpipe nircam reset
# --from <step>` refuses these — the user must `--uncal` to redo them
# correctly.
_SCI_MUTATING_STEPS = {
    'wisp', 'striping', 'image2', 'sky', 'variance',
}

# Short labels for the status command's column headers (max 4 chars).
_STEP_LABELS = {
    'detector1':   'det1',
    'persistence': 'pers',
    'wisp':        'wisp',
    'striping':    '1f',
    'image2':      'img2',
    'edge':        'edge',
    'sky':         'sky',
    'variance':    'var',
    'jhat':        'jhat',
    'apply_mask':  'mask',
    'bad_pixel':   'bpix',
    'outlier':     'out',
}


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


# Refcat utilities: `cfpipe nircam refcat {query,extract,merge,compare}`
main.add_command(refcat_group)


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


@main.command()
@common_options
@click.option('--filters', multiple=True, default=None,
              help='Filters to check (default: all from field).')
def status(config, field, filters):
    """Show CFP_* completion status for each canonical exposure."""
    cfg, field_obj = _setup(config, field)
    filter_list = _resolve_filters(filters, field_obj)

    # Steps that stamp a CFP key (resample doesn't — it produces mosaics)
    steps_with_cfp = [(n, k) for n, k in ALL_STEPS if k is not None]
    col_width = 5

    for filt in filter_list:
        exposures = field_obj.get_exposure_files(filt)
        if not exposures:
            log(f'{filt}: no canonical exposure files in '
                f'{field_obj.exposures_dir}/{filt}')
            continue

        # Read CFP_* once per exposure
        per_exp = {
            os.path.basename(f).removesuffix('.fits'): cfp_mod.get_steps(f)
            for f in sorted(exposures)
        }

        rootname_width = max(len(r) for r in per_exp) + 2
        log('')
        log(f'Field: {field_obj.name}  |  Filter: {filt}  |  '
            f'{len(per_exp)} exposures')
        log(f'  Legend: + = done, . = not yet, s = skipped (n/a)')

        # Header
        header = ' ' * rootname_width
        for name, _ in steps_with_cfp:
            header += _STEP_LABELS[name].ljust(col_width)
        log(header)

        # Per-exposure rows
        for rootname, steps in per_exp.items():
            row = rootname.ljust(rootname_width)
            for _, key in steps_with_cfp:
                if key not in steps:
                    cell = '.'
                else:
                    val = str(steps[key])
                    cell = 's' if val.startswith('skipped') else '+'
                row += cell.ljust(col_width)
            log(row)

        # Per-step summary
        log('')
        for name, key in steps_with_cfp:
            done = sum(1 for steps in per_exp.values() if key in steps)
            skipped = sum(1 for steps in per_exp.values()
                          if key in steps
                          and str(steps[key]).startswith('skipped'))
            done_real = done - skipped
            label = _STEP_LABELS[name]
            line = f'  {label:5s} {done_real:>3d}/{len(per_exp)} done'
            if skipped:
                line += f', {skipped} skipped'
            log(line)


@main.command()
@common_options
@click.option('--filters', multiple=True, default=None,
              help='Filters to reset (default: all from field).')
@click.option('--from', 'from_step', default=None,
              type=click.Choice(STEP_NAMES),
              help='Clear CFP_<step> + every later CFP key on each '
                   'canonical exposure. Refuses SCI-mutating steps.')
@click.option('--uncal', 'uncal', is_flag=True,
              help='Delete every canonical exposure file (and any '
                   '_jump.fits sidecars) for the selected filters.')
@click.option('--yes', is_flag=True,
              help='Skip the confirmation prompt.')
def reset(config, field, filters, from_step, uncal, yes):
    """Reset CFP keys (or wipe canonical files) to re-run from a given step.

    Two modes, mutually exclusive:

      --from <step>   Clear CFP_<step> + every later CFP key on each
                      canonical exposure file. The data on disk is NOT
                      modified — only the provenance keys. The next run of
                      <step> will then process those exposures (since their
                      CFP key is gone). Refused for SCI-mutating steps to
                      avoid accidental double-subtraction; use --uncal for
                      those.

      --uncal         Delete every canonical exposure file (and any
                      *_jump.fits sidecars) for the selected filters.
                      Diagnostics PDFs and reference products
                      (bad_pixel_dir, refcat) are kept. The next run of
                      `cfpipe nircam process` builds them all fresh from
                      the raw uncal files.
    """
    if not (uncal or from_step):
        raise click.UsageError("Specify --uncal or --from <step>.")
    if uncal and from_step:
        raise click.UsageError(
            "--uncal and --from are mutually exclusive."
        )
    if from_step and from_step in _SCI_MUTATING_STEPS:
        raise click.ClickException(
            f"--from {from_step!r} would leave already-mutated SCI/VAR "
            f"data on disk; running {from_step} again would compound the "
            f"effect. Use --uncal to start over from the raw files."
        )

    cfg, field_obj = _setup(config, field)
    filter_list = _resolve_filters(filters, field_obj)

    # Confirmation
    if uncal:
        affected = []
        for filt in filter_list:
            affected.extend(field_obj.get_exposure_files(filt))
        action = (
            f"DELETE {len(affected)} canonical exposure files "
            f"(plus any _jump.fits sidecars) "
            f"across filters {filter_list}"
        )
    else:
        affected = []
        for filt in filter_list:
            affected.extend(
                f for f in field_obj.get_exposure_files(filt)
                if cfp_mod.has_step(f, _step_to_cfp_key(from_step))
            )
        action = (
            f"CLEAR CFP_{_step_to_cfp_key(from_step)[4:]}+ keys on "
            f"{len(affected)} canonical exposures across filters "
            f"{filter_list}"
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
            jump = path[:-len('.fits')] + '_jump.fits'
            if os.path.exists(jump):
                try:
                    os.remove(jump)
                except OSError:
                    pass
    else:
        cfp_key = _step_to_cfp_key(from_step)
        for path in affected:
            try:
                cfp_mod.clear_from(path, cfp_key)
                log(f'  cleared {cfp_key}+ on {os.path.basename(path)}')
            except Exception as e:
                log(f'  could not clear keys on {path}: {e}')


def _step_to_cfp_key(step_name):
    """Look up the CFP_* key for a step (raises if it has none, e.g. resample)."""
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
