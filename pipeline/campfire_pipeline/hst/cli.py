"""Command groups of the HST instruments: ``cfpipe acswfc`` and ``cfpipe wfc3ir``.

Each HST detector is an instrument of its own, with the same commands as
``cfpipe nircam`` (Hollis, 2026-11-15)::

    cfpipe wfc3ir process  --field pg004 -p 4    # calibrate -> align
    cfpipe wfc3ir combine  --field pg004         # outlier -> resample
    cfpipe wfc3ir run      --field pg004 --all
    cfpipe wfc3ir <step>   --field pg004         # one step
    cfpipe wfc3ir status   --field pg004         # per-exposure stamps
    cfpipe wfc3ir check    --field pg004         # which tiles are stale
    cfpipe wfc3ir reset    --field pg004 --from align

Both groups are built by :func:`make_group` from the same code; the instrument
name selects the config block (``[acswfc.*]`` / ``[wfc3ir.*]``), the table of
the field (``[<field>.acswfc]`` / ``[<field>.wfc3ir]``) and the directories.

Nothing HST-specific is imported until a command runs: the HST software
(``hstcal``, ``stwcs``, ``drizzlepac``) is an optional part of an install, and
``cfpipe --help`` must work without it.
"""

import os

import click

from campfire_pipeline.common.cli import VariadicOption
from campfire_pipeline.common.io import log

NAMES = {'acswfc': 'ACS/WFC', 'wfc3ir': 'WFC3/IR'}

_LABELS = {'calibrate': 'cal', 'wcs_init': 'wcs', 'sky': 'sky',
           'crreject': 'cr', 'align': 'algn', 'outlier': 'out'}

# `reset --from` clears stamps so that a step runs again. That is safe for
# the steps that only write flags or a WCS. The others change the pixels the
# later steps were built on (or, for wcs_init, the WCS the alignment started
# from), so they are redone from the RAW file.
_RESET_FROM = ('crreject', 'align', 'outlier')


def _common(f):
    f = click.option('--config', default=None, help='Path to configuration file.')(f)
    f = click.option('--field', required=True, help='Field name from fields.toml.')(f)
    return f


def _processing(f):
    f = click.option('--filters', multiple=True, default=None, cls=VariadicOption,
                     help="Filters to process (default: all of the field's "
                          "for this instrument).")(f)
    f = click.option('--processes', '-p', default=1, type=int,
                     help='Number of parallel processes.')(f)
    f = click.option('--overwrite', is_flag=True,
                     help='Redo steps that are already done.')(f)
    return f


def _tiles(f):
    return click.option('--tiles', multiple=True, default=None, cls=VariadicOption,
                        help='Tile name(s) to build (default: all tiles).')(f)


def _setup(config_path, field_name, instrument):
    """Config, environment and the instrument's part of the field."""
    from campfire_pipeline.config import load_config, setup_environment
    from campfire_pipeline.hst.field import HstField

    config = load_config(config_path)
    try:
        setup_environment(config, instrument=instrument)
        field = HstField.load(field_name, instrument)
    except FileNotFoundError as e:
        raise click.ClickException(
            f'{e}\n\nHST reductions read the field from '
            f'$CAMPFIRE_ROOT/config/fields.toml.')
    except ValueError as e:
        raise click.ClickException(str(e))
    field.setup_workspace()
    return config, field


def _guard(func, *args, **kwargs):
    """Run an orchestrator call; a configuration or data problem it reports
    becomes a plain error message instead of a traceback."""
    try:
        return func(*args, **kwargs)
    except (ValueError, RuntimeError) as e:
        raise click.ClickException(str(e))


def make_group(instrument):
    """The ``cfpipe <instrument>`` command group of one HST instrument."""
    if instrument not in NAMES:
        raise ValueError(f'{instrument!r} is not an HST instrument')
    label = NAMES[instrument]

    @click.group(name=instrument,
                 help=f'CAMPFIRE HST {label} imaging reduction pipeline.')
    def group():
        pass

    @group.command(help='Run the per-exposure process phase '
                        '(calibrate, wcs_init, sky, crreject, align).')
    @_common
    @_processing
    def process(config, field, filters, processes, overwrite):
        from campfire_pipeline.hst.orchestrate import run_process
        cfg, fld = _setup(config, field, instrument)
        _guard(run_process, fld, cfg, filters=list(filters) or None,
               n_processes=processes, overwrite=overwrite)

    @group.command(help='Run the combine phase (outlier, resample) on working '
                        'copies of the aligned exposures.')
    @_common
    @_processing
    @_tiles
    @click.option('--include-unaligned', is_flag=True,
                  help='Also drizzle exposures that failed alignment '
                       '(default: they are held back).')
    def combine(config, field, filters, processes, overwrite, tiles,
                include_unaligned):
        from campfire_pipeline.hst.orchestrate import run_combine
        cfg, fld = _setup(config, field, instrument)
        _guard(run_combine, fld, cfg, filters=list(filters) or None,
               n_processes=processes, overwrite=overwrite,
               tiles=list(tiles) or None, include_unaligned=include_unaligned)

    @group.command(help='Run process and/or combine in one invocation.')
    @_common
    @_processing
    @_tiles
    @click.option('--process', 'do_process', is_flag=True,
                  help='Run the process phase.')
    @click.option('--combine', 'do_combine', is_flag=True,
                  help='Run the combine phase.')
    @click.option('--all', 'do_all', is_flag=True, help='Run both phases.')
    @click.option('--include-unaligned', is_flag=True,
                  help='Also drizzle exposures that failed alignment.')
    def run(config, field, filters, processes, overwrite, tiles, do_process,
            do_combine, do_all, include_unaligned):
        from campfire_pipeline.hst.orchestrate import run_combine, run_process
        if do_all:
            do_process = do_combine = True
        if not (do_process or do_combine):
            raise click.UsageError('Specify --process, --combine, or --all.')
        cfg, fld = _setup(config, field, instrument)
        filter_list = list(filters) or None
        if do_process:
            _guard(run_process, fld, cfg, filters=filter_list,
                   n_processes=processes, overwrite=overwrite)
        if do_combine:
            _guard(run_combine, fld, cfg, filters=filter_list,
                   n_processes=processes, overwrite=overwrite,
                   tiles=list(tiles) or None,
                   include_unaligned=include_unaligned)

    def _step_command(step_name, with_tiles):
        @click.command(name=step_name, help=f'Run the {step_name} step.')
        @_common
        @_processing
        def _cmd(config, field, filters, processes, overwrite, tiles=()):
            from campfire_pipeline.hst.orchestrate import run_step
            cfg, fld = _setup(config, field, instrument)
            _guard(run_step, step_name, fld, cfg, filters=list(filters) or None,
                   n_processes=processes, overwrite=overwrite,
                   tiles=list(tiles) or None)
        return _tiles(_cmd) if with_tiles else _cmd

    from campfire_pipeline.hst.orchestrate import ALL_STEPS, STEP_NAMES
    for name in STEP_NAMES:
        group.add_command(_step_command(name, with_tiles=(name == 'resample')))

    @group.command(help='Show which steps are done on each canonical exposure.')
    @_common
    @click.option('--filters', multiple=True, default=None, cls=VariadicOption,
                  help='Filters to show (default: all).')
    def status(config, field, filters):
        from campfire_pipeline.common import cfp
        from campfire_pipeline.hst.orchestrate import exposure_status
        _cfg, fld = _setup(config, field, instrument)
        columns = [(n, k) for n, k in ALL_STEPS if k is not None]
        for filt in (list(filters) or fld.filters):
            n_raw = len(fld.get_raw_files(filt))
            rows = exposure_status(fld, filt)
            log('')
            log(f'Field: {fld.name}  |  {instrument} {filt}  |  {n_raw} RAW '
                f'selected, {len(rows)} calibrated')
            if not rows:
                continue
            log('  Legend: + = done, . = not yet, x = alignment failed')
            width = max(len(r) for r in rows) + 2
            log(' ' * width + ''.join(_LABELS[n].ljust(6) for n, _ in columns))
            for root, steps in rows.items():
                cells = []
                for _, key in columns:
                    if key not in steps:
                        cells.append('.')
                    elif key == 'CFP_ALGN' and steps[key] == cfp.NOT_ALIGNED:
                        cells.append('x')
                    else:
                        cells.append('+')
                log(root.ljust(width) + ''.join(c.ljust(6) for c in cells))
            log('')
            for name, key in columns:
                done = sum(1 for s in rows.values() if key in s)
                line = f'  {_LABELS[name]:5s} {done:>3d}/{len(rows)} done'
                if key == 'CFP_ALGN':
                    bad = sum(1 for s in rows.values()
                              if s.get(key) == cfp.NOT_ALIGNED)
                    if bad:
                        line += f', {bad} of them failed'
                log(line)

    @group.command(help='Report which mosaic tiles are stale.')
    @_common
    @click.option('--filters', multiple=True, default=None, cls=VariadicOption,
                  help='Filters to check (default: all).')
    @_tiles
    def check(config, field, filters, tiles):
        from campfire_pipeline.hst.orchestrate import stale_tiles
        cfg, fld = _setup(config, field, instrument)
        any_stale = False
        for filt in (list(filters) or fld.filters):
            results = _guard(stale_tiles, fld, cfg, filt,
                             tiles=list(tiles) or None)
            for r in results:
                if not r['n_inputs']:
                    continue
                if r['stale']:
                    any_stale = True
                    log(f"{filt}/{r['tile']}: STALE ({r['n_inputs']} inputs): "
                        f"{'; '.join(r['reasons'])}")
                else:
                    log(f"{filt}/{r['tile']}: up to date ({r['n_inputs']} inputs)")
            if not any(r['n_inputs'] for r in results):
                log(f'{filt}: no working copies overlap a tile (run combine)')
        if not any_stale:
            log('All tiles that have inputs are up to date.')

    @group.command(help='Clear step stamps, or delete the calibrated '
                        'exposures, so that steps run again.')
    @_common
    @click.option('--filters', multiple=True, default=None, cls=VariadicOption,
                  help='Filters to reset (default: all).')
    @click.option('--from', 'from_step', default=None,
                  type=click.Choice(STEP_NAMES),
                  help='Clear the stamp of this step and of every later one. '
                       f'Allowed: {", ".join(_RESET_FROM)}.')
    @click.option('--raw', 'raw', is_flag=True,
                  help='Delete every calibrated exposure and working copy of '
                       'the selected filters; the next process run starts '
                       'from the RAW files.')
    @click.option('--yes', is_flag=True, help='Skip the confirmation prompt.')
    def reset(config, field, filters, from_step, raw, yes):
        from campfire_pipeline.common import cfp
        from campfire_pipeline.hst.orchestrate import OUTLIER_MANIFEST
        if bool(raw) == bool(from_step):
            raise click.UsageError('Specify exactly one of --raw or --from <step>.')
        if from_step and from_step not in _RESET_FROM:
            raise click.ClickException(
                f'--from {from_step}: the later steps were built on what this '
                f'step wrote (the pixels, or the WCS the alignment started '
                f'from), so it cannot be redone in place. Use --raw to start '
                f'again from the RAW files. --from accepts: '
                f'{", ".join(_RESET_FROM)}.')
        _cfg, fld = _setup(config, field, instrument)
        filter_list = list(filters) or fld.filters

        canonical = [f for filt in filter_list for f in fld.get_exposure_files(filt)]
        work = [f for filt in filter_list
                for f in fld.get_exposure_files(filt, work=True)]
        key = dict(ALL_STEPS).get(from_step)
        if raw:
            action = (f'DELETE {len(canonical)} calibrated exposures and '
                      f'{len(work)} working copies')
        elif from_step == 'outlier':
            action = (f'DELETE {len(work)} working copies (they hold the '
                      f'outlier flags; combine makes them again)')
        else:
            canonical = [f for f in canonical
                         if cfp.has_step(f, key, keyset=cfp.HST)]
            action = (f'CLEAR {key} and later stamps on {len(canonical)} '
                      f'calibrated exposures')
        log(f'Reset ({instrument}, filters {filter_list}): {action}')
        if not (canonical or work):
            log('Nothing to reset.')
            return
        if not yes and not click.confirm('Proceed?'):
            log('Aborted.')
            return

        if raw or from_step == 'outlier':
            for path in (work + (canonical if raw else [])):
                os.remove(path)
            for filt in filter_list:
                manifest = os.path.join(fld.filter_dir(filt, work=True),
                                        OUTLIER_MANIFEST)
                if os.path.exists(manifest):
                    os.remove(manifest)
        else:
            for path in canonical:
                cfp.clear_from(path, key, keyset=cfp.HST)
        log('Done.')

    return group
