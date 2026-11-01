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
    cfpipe nirspec linefit  --obs ember_uds_p4 [--allow-auto]   # needs `campfire pull` first
    cfpipe nirspec summary  --obs ember_uds_p4
    cfpipe nirspec run      --obs ember_uds_p4 --all
    cfpipe nirspec make-templates [--config config.toml]

Multiple observations are processed serially:
    cfpipe nirspec run --obs ember_uds_p4 ember_uds_p5 ember_uds_p6 --all -p 4

Also available directly as: campfire-nirspec <command>
"""

from campfire_pipeline import _thread_caps  # noqa: F401  (must precede numpy/matplotlib)

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


from campfire_pipeline.common.cli import VariadicOption as _VariadicOption


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
        _run_stage2a(obs_obj, stage_config, source_ids=sids, n_processes=processes, overwrite=overwrite, cfg=cfg)


@main.command('master-bkg')
@common_options
@processing_options
def master_bkg(config, obs, source_ids, processes, overwrite):
    """Derive the master background of every exposure: step 1 from the rate files
    (tables written into each rate file: the count-rate sky and the stuck-shutter fit), then
    step 2 from the stage-2a canonicals and background slitlets (_mbkg.fits: the
    calibrated sky and its painting). Both run within stage 2a too; this re-runs them
    alone (--overwrite to redo current products). Step 1's stuck calls are stored but
    not merged into the stuck list here (that is stage 2a's or detect-stuck's job, since
    the canonicals would need a re-run)."""
    from campfire_pipeline.nirspec.master_bkg import run_master_bkg
    from campfire_pipeline.nirspec.master_bkg_rate import run_step1

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage_config = get_stage_config('stage2', cfg, obs_obj)
        run_step1(obs_obj, stage_config, n_processes=processes, overwrite=overwrite, write_calls=False)
        run_master_bkg(obs_obj, stage_config, n_processes=processes, overwrite=overwrite)


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


@main.command()
@common_options
@processing_options
@click.option('--allow-auto', is_flag=True,
              help='Fall back to the zfit redshift for spectra without a usable '
                   'inspected redshift (QA only; the product is stamped ZSRC=auto '
                   'and `campfire deploy lines` refuses it without --allow-auto-z).')
@click.option('--grating', 'gratings', multiple=True, cls=_VariadicOption,
              help='Restrict to these gratings (default: all).')
def linefit(config, obs, source_ids, processes, overwrite, allow_auto, gratings):
    """Fit emission-line fluxes at the inspected redshift.

    Reads reference/nirspec/<obs>/redshifts.toml (materialized by
    `campfire pull --obs <obs>` from the portal's inspection state) and fits
    every *_spec.fits whose object meets [nirspec.line_fitting].min_quality,
    writing <base>_lines.fits (+ a QA PDF). Products whose inputs (redshift,
    quality, spectrum bytes) are unchanged are skipped unless --overwrite.

    Needs only the observation name (products and redshifts sit at
    layout-derived paths); observations.toml is optional and supplies
    per-observation [<obs>.line_fitting] overrides when present.
    """
    from campfire_pipeline.nirspec.linefit_stage import resolve_linefit_observation, run_linefit

    for obs_name in obs:
        # Unlike the reduction stages, linefit needs only the observation
        # name: its inputs live at layout-derived paths. observations.toml
        # (config plane) is used when present, for per-observation overrides,
        # and is not required.
        cfg = load_config(config)
        setup_environment(cfg)
        obs_obj = resolve_linefit_observation(obs_name, cfg)
        sids = _resolve_source_ids(source_ids)
        sids_list = sids if sids != 'all' else None
        counts = run_linefit(
            obs_obj, cfg,
            source_ids=sids_list,
            overwrite=overwrite,
            n_processes=processes,
            allow_auto=allow_auto,
            gratings=list(gratings) if gratings else None,
        )
        log(f"linefit {obs_name}: {counts['fit']} fit, {counts['skipped_uptodate']} up to date, "
            f"{counts['skipped_no_z']} without a usable redshift, {counts['failed']} failed")


@main.command('detect-stuck')
@common_options
@click.option('--processes', '-p', default=1, type=int,
              help='Number of parallel processes.')
@click.option('--overwrite', is_flag=True,
              help='Redo master-background step 1 even where its products are current.')
def detect_stuck(config, obs, processes, overwrite):
    """Find stuck-closed shutters: master-background step 1 on the rate files.

    Fits every commanded-open shutter's sky in each rate frame (nirspec/shutter_fit.py),
    calls a shutter stuck from all nods and both detectors of its exposure root, stores
    the fit and the calls in the rate files (SHUTAMP, CALLS) and merges the stuck calls into the
    observation's stuck list as auto entries (manual and web entries win). Stage 2a runs
    this itself; use this to inspect the calls before re-running it:

    \b
      1. cfpipe nirspec detect-stuck --obs my_obs -p 6
      2. Inspect reference/nirspec/<obs>/stuck_closed_shutters.toml (and the nods plots)
      3. cfpipe nirspec run --obs my_obs --stage2a --stage2b --stage3 \\
             --source-ids <affected IDs> --overwrite -p 6
    """
    from campfire_pipeline.nirspec.master_bkg_rate import run_before_stage2a

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage_config = get_stage_config('stage2', cfg, obs_obj)
        stage_config['detect_stuck_shutters'] = True          # the user asked for the calls
        sids, roots = run_before_stage2a(obs_obj, stage_config, n_processes=processes, overwrite=overwrite)
        if not sids:
            log(f'{obs_name}: no shutter changed stuck state')
            continue
        affected = ' '.join(str(s) for s in sorted(sids))
        log(f'\nAffected source IDs ({len(sids)}): {affected}')
        log('\nTo re-run downstream stages:')
        log(f'  cfpipe nirspec run --obs {obs_name} --stage2a --stage2b --stage3 '
            f'--source-ids {affected} --overwrite -p {processes}')


# ---------------------------------------------------------------------------
# Manual mask subgroup
# ---------------------------------------------------------------------------


@main.group('mask')
def mask_group():
    """Manage manual DQ masks for NIRSpec rate files.

    Polygon regions are stored inline in observations.toml under
    [<obs>.masks] keyed by rate file basename. They are OR'd as
    DO_NOT_USE into the rate file DQ array before stage1 background
    subtraction so masked pixels are excluded from the bkg fit.
    """
    pass


@mask_group.command('apply')
@common_options
def mask_apply(config, obs):
    """Apply manual masks for the given observation(s).

    Restores pre-bkg-sub rate state, ORs DO_NOT_USE into DQ from current
    region strings, re-runs background subtraction, and stamps the
    CFMASKSH header sentinel. Idempotent — skips files whose stamped
    sentinel already matches the current mask.
    """
    from campfire_pipeline.nirspec.masks import apply_to_observation

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        stage1_config = get_stage_config('stage1', cfg, obs_obj)
        apply_to_observation(obs_obj, stage1_config)


@mask_group.command('validate')
@common_options
def mask_validate(config, obs):
    """Parse all mask region strings and report pixel coverage."""
    from campfire_pipeline.nirspec.masks import validate_observation

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        if not obs_obj.manual_masks:
            log(f"{obs_name}: no manual masks defined.")
            continue
        results = validate_observation(obs_obj)
        ok_count = sum(1 for r in results if r['ok'])
        log(f"{obs_name}: {ok_count}/{len(results)} masks valid")
        for r in results:
            if r['ok']:
                log(f"  {r['basename']}: {r['n_pixels']} px")
            else:
                log(f"  {r['basename']}: FAILED — {r['message']}")


@mask_group.command('clear')
@common_options
@click.option('--exposure', 'exposure', default=None,
              help='Single exposure (rate file basename without _rate.fits) to clear. '
                   'If omitted, clears all masks for the observation.')
def mask_clear(config, obs, exposure):
    """Remove a manual mask (.reg) from reference/nirspec/<obs>/masks/.

    Does NOT modify any rate files — run `cfpipe nirspec mask apply` afterwards
    to clear the mask DQ bits and re-run bkg sub.
    """
    from campfire_pipeline.nirspec.masks import write_reference_masks

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        if not obs_obj.manual_masks:
            log(f"{obs_name}: no manual masks defined.")
            continue
        if exposure:
            if exposure not in obs_obj.manual_masks:
                log(f"{obs_name}: no mask for {exposure}; nothing to clear.")
                continue
            write_reference_masks(obs_obj, {exposure: None})
            log(f"{obs_name}: cleared mask for {exposure}")
        else:
            cleared = {k: None for k in obs_obj.manual_masks}
            write_reference_masks(obs_obj, cleared)
            log(f"{obs_name}: cleared {len(cleared)} mask(s)")
        log(f"  run `cfpipe nirspec mask apply --obs {obs_name}` to update rate files.")


@mask_group.command('edit')
@common_options
@click.option('--exposure', 'exposure', default=None,
              help='Edit a single rate file (basename without _rate.fits). '
                   'If omitted, all rate files are loaded sequentially.')
def mask_edit(config, obs, exposure):
    """Open rate file(s) in a matplotlib polygon editor.

    One window per rate file, opened sequentially. Click to place vertices,
    close near the start vertex (or press Enter), drag handles to refine.
    Press ``s`` to save & advance, ``b`` to go back, ``q`` to abort.

    Does NOT auto-apply the new masks — run `cfpipe nirspec mask apply`
    afterwards to update rate files.
    """
    from campfire_pipeline.nirspec.mpl_editor import edit_masks_in_matplotlib

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        edit_masks_in_matplotlib(obs_obj, exposure=exposure)


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
    from campfire_pipeline.metadata.pointings import (
        generate_pointings_table,
        write_pointings_ecsv,
    )

    from campfire_pipeline.common.version import get_reduction_version
    version = get_reduction_version(cfg)
    consensus_config = cfg.get('nirspec', {}).get('redshift_consensus', {})
    obs_dir = Path(obs_obj.workspace_dir)
    summary_table = generate_observation_summary(obs_obj.name, obs_dir,
                                                  cfpipe_version=version,
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

    # Generate pointings ECSV
    pointings_table = generate_pointings_table(obs_obj.name, obs_dir, obs_obj.field)
    if len(pointings_table) > 0:
        write_pointings_ecsv(pointings_table, obs_dir, obs_obj.name)
    else:
        log(f"No pointings generated for {obs_obj.name}")


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
@click.option('--linefit', 'do_linefit', is_flag=True,
              help='Fit emission lines at the inspected redshift (not part of --all: '
                   'needs `campfire pull` after inspection).')
@click.option('--all', 'do_all', is_flag=True, help='Run all reduction stages (not linefit).')
def run(config, obs, source_ids, processes, overwrite,
        do_stage1, do_stage2a, do_stage2b, do_stage3, do_zfit, do_summary, do_linefit, do_all):
    """Run multiple pipeline stages in sequence."""
    from campfire_pipeline.nirspec.stage1 import run_stage1 as _run_stage1
    from campfire_pipeline.nirspec.stage2 import run_stage2a, run_stage2b
    from campfire_pipeline.nirspec.stage3 import run_stage3 as _run_stage3
    from campfire_pipeline.nirspec.redshift_fitting import fit_redshifts

    if do_all:
        do_stage1 = do_stage2a = do_stage2b = do_stage3 = do_zfit = do_summary = True

    if not any([do_stage1, do_stage2a, do_stage2b, do_stage3, do_zfit, do_summary, do_linefit]):
        raise click.UsageError("Specify at least one stage flag, or use --all.")

    for obs_name in obs:
        cfg, obs_obj, paths = _setup(config, obs_name)
        sids = _resolve_source_ids(source_ids)

        if do_stage1:
            sc = get_stage_config('stage1', cfg, obs_obj)
            _run_stage1(obs_obj, sc, n_processes=processes, overwrite=overwrite)

        if do_stage2a:
            sc = get_stage_config('stage2', cfg, obs_obj)
            run_stage2a(obs_obj, sc, source_ids=sids, n_processes=processes, overwrite=overwrite, cfg=cfg)

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

        if do_linefit:
            from campfire_pipeline.nirspec.linefit_stage import run_linefit
            sids_list = sids if sids != 'all' else None
            run_linefit(obs_obj, cfg, source_ids=sids_list, overwrite=overwrite,
                        n_processes=processes)


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
