"""
Stage 2: WCS assignment (2a) and nodded background subtraction (2b).
"""

import os
import glob
import warnings
import numpy as np
from typing import List
from astropy.io import fits
from astropy.table import Table

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.metafile import MetaFile
from campfire_pipeline.nirspec.observation import Observation


def _stuck_table_to_dict(table):
    """Convert a stuck_closed_shutters Table to nested dict.

    Returns ``{root: {source_id: [ordinals]}}`` suitable for passing
    to ``plot_stage2a_results(stuck_shutters=...)``.
    """
    d = {}
    for row in table:
        root = row['root']
        sid = int(row['source_id'])
        d.setdefault(root, {})[sid] = list(row['shutters'])
    return d


def build_empirical_wavecorr():
    """Build a wavecorr reference file augmented with the JADES DR4 empirical correction.

    Loads the digitized S(lambda, x) from Scholtz+2025 (package data),
    resamples it onto the existing CRDS wavecorr grid, and saves the
    combined file to $CAMPFIRE_ROOT/cache/. Returns the cached path
    immediately if the file already exists.

    Returns
    -------
    str
        Absolute path to the augmented wavecorr ASDF file.
    """
    import copy
    from pathlib import Path
    import asdf
    import crds
    from scipy.interpolate import RegularGridInterpolator, griddata
    from jwst.datamodels import WaveCorrModel

    # --- cache check ---
    from campfire_pipeline.config import _get_campfire_root
    campfire_root = _get_campfire_root()
    cache_dir = os.path.join(campfire_root, 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    out_path = os.path.join(cache_dir, 'jwst_nirspec_wavecorr_jades_dr4_empirical.asdf')
    if os.path.exists(out_path):
        log(f"Empirical wavecorr file already exists: {out_path}")
        return out_path

    log("Building empirical wavecorr reference file ...")

    # --- load empirical correction ---
    data_dir = Path(__file__).parent.parent / 'data'
    with asdf.open(str(data_dir / 'jades_dr4_empirical_wavecorr.asdf')) as af:
        emp_wav_um = np.array(af.tree['wavelength'])   # microns, shape (9,)
        emp_xoff   = np.array(af.tree['x_offset'])     # fraction of pitch, shape (14,)
        emp_S      = np.array(af.tree['S'])             # shape (14, 9), axes: (x_offset, wavelength)

    # --- fill NaN via linear interpolation ---
    # Build meshgrid of (x_offset, wavelength) coordinates
    xg, wg = np.meshgrid(emp_xoff, emp_wav_um, indexing='ij')
    valid = ~np.isnan(emp_S)
    emp_S_filled = griddata(
        (xg[valid], wg[valid]),
        emp_S[valid],
        (xg, wg),
        method='linear',
    )
    # Fall back to nearest for any remaining NaN at convex-hull boundary
    still_nan = np.isnan(emp_S_filled)
    if still_nan.any():
        emp_S_filled[still_nan] = griddata(
            (xg[valid], wg[valid]),
            emp_S[valid],
            (xg[still_nan], wg[still_nan]),
            method='nearest',
        )

    # Transpose to (wavelength, x_offset) to match reference file axis order,
    # and convert wavelength from microns to meters
    emp_wav_m = emp_wav_um * 1e-6
    emp_S_wl_x = emp_S_filled.T  # shape (9, 14) → axes: (wavelength, x_offset)

    # --- load CRDS wavecorr ---
    params = {
        "INSTRUME": "NIRSPEC",
        "DETECTOR": "NRS1",
        "EXP_TYPE": "NRS_MSASPEC",
        "DATE-OBS": "2023-01-01",
        "TIME-OBS": "00:00:00",
    }
    refs = crds.getreferences(params, reftypes=['wavecorr'], observatory='jwst')
    crds_path = refs.get('wavecorr', '')
    if not crds_path or not os.path.exists(crds_path):
        raise FileNotFoundError(f"CRDS wavecorr reference not found: {crds_path}")

    wavecorr = WaveCorrModel(crds_path)
    tab = wavecorr.apertures[0].zero_point_offset
    ref_wav_m = np.array(tab.points[0])     # shape (21,), meters
    ref_xoff  = np.array(tab.points[1])     # shape (21,)
    ref_table = np.array(tab.lookup_table)  # shape (21, 21)

    # --- resample empirical correction onto reference grid ---
    interp = RegularGridInterpolator(
        (emp_wav_m, emp_xoff),
        emp_S_wl_x,
        bounds_error=False,
        fill_value=None,  # linear extrapolation outside domain
    )
    ref_grid = np.meshgrid(ref_wav_m, ref_xoff, indexing='ij')
    pts = np.column_stack([ref_grid[0].ravel(), ref_grid[1].ravel()])
    emp_resampled = interp(pts).reshape(ref_table.shape)

    # --- combine ---
    new_table = ref_table + emp_resampled

    # --- save ---
    wavecorr_new = copy.deepcopy(wavecorr)
    wavecorr_new.apertures[0].zero_point_offset.lookup_table = new_table
    wavecorr_new.meta.description = (
        "Wavecorr augmented with empirical S(lambda,x) from Scholtz+2025 "
        "(JADES DR4 Paper II, arXiv:2510.01034) Figure 5"
    )
    wavecorr_new.meta.author = "Hollis Akins / CAMPFIRE pipeline"
    wavecorr_new.meta.pedigree = "INFLIGHT"
    wavecorr_new.save(out_path)
    wavecorr_new.close()
    wavecorr.close()

    log(f"Saved empirical wavecorr to {out_path}")
    return out_path


def _prefetch_crds_references(rate_files):
    """Pre-cache CRDS reference files to avoid multiprocessing race conditions.

    Multiple workers downloading the same CRDS file simultaneously can cause
    one to read a partially-written file. Running CRDS lookups on one file per
    unique (detector, grating, filter) combination beforehand ensures everything
    is cached.

    The three NIRSpec flat types have different dependencies:
    - dflat/sflat: depend on detector, mode, grating, and filter
    - fflat: detector agnostic, depends on grating and filter
    """
    import crds

    # All reference types used by Spec2Pipeline for NIRSpec MSA observations
    reftypes = [
        'fflat', 'dflat', 'sflat',
        'camera', 'collimator', 'disperser', 'distortion',
        'fore', 'fpa', 'msa', 'ote',
        'pathloss', 'photom', 'barshadow',
        'specwcs', 'wavecorr', 'wavelengthrange',
    ]

    seen_configs = set()
    for rate_file in rate_files:
        hdr = fits.getheader(rate_file)
        det = hdr.get('DETECTOR', 'NRS1')
        grating = hdr.get('GRATING', 'UNKNOWN')
        filt = hdr.get('FILTER', 'UNKNOWN')
        config_key = (det, grating, filt)

        if config_key in seen_configs:
            continue
        seen_configs.add(config_key)

        log(f"Pre-fetching CRDS references for {det}/{grating}/{filt} "
            f"using {os.path.basename(rate_file)}")

        params = {
            "INSTRUME": "NIRSPEC",
            "DETECTOR": det,
            "EXP_TYPE": hdr.get('EXP_TYPE', 'NRS_MSASPEC'),
            "FILTER": filt,
            "GRATING": grating,
            "DATE-OBS": hdr.get('DATE-OBS', '2023-01-01'),
            "TIME-OBS": hdr.get('TIME-OBS', '00:00:00'),
        }

        try:
            refs = crds.getreferences(params, reftypes=reftypes, observatory='jwst')
            cached = [k for k, v in refs.items()
                      if v and 'N/A' not in v.upper() and 'NOT FOUND' not in v.upper()]
            log(f"  Cached {len(cached)}/{len(reftypes)} references: "
                f"{', '.join(sorted(cached))}")
        except Exception as e:
            log(f"  CRDS prefetch warning for {config_key}: {e}")

    log("CRDS reference pre-fetch complete")


def run_stage2a(obs, stage_config, source_ids='all', overwrite=False,
                n_processes=1, plot=True, data_dir=None, products_dir=None,
                _skip_stuck_detection=False, cfg=None):
    """Orchestrate stage 2a: WCS assignment + unit fixing + optional resampling/plotting.

    Parameters
    ----------
    obs : Observation
    stage_config : dict
        Merged stage2 configuration.
    source_ids : list or 'all'
    overwrite, plot : bool
    n_processes : int
    data_dir, products_dir : str
        Used for workspace setup if not already done.
    cfg : dict, optional
        Top-level config dict (used for mask auto-apply staleness check). If
        None, loaded on demand only if manual masks are defined.
    """
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.plots import plot_stage2a_results

    log(f"Stage 2a config for {obs.name}: {stage_config}")

    # Build empirical wavecorr override if enabled
    wavecorr_override = None
    if stage_config.get('empirical_wavecorr'):
        wavecorr_override = build_empirical_wavecorr()

    # Build extended-wavelength reference overrides if enabled. Done serially here,
    # before the parallel dispatch, so the cache writes never race across workers.
    # (This block re-runs on the stuck-shutter self-recursion below; the build is
    # cache-skipped, so that is cheap and the kwargs are reconstructed, not forwarded.)
    extended_refs = None
    if stage_config.get('extend_g140m_g235m'):
        from campfire_pipeline.nirspec.extended_wavelength import (
            verify_extension_crds_compatibility, build_extended_reference_files,
        )
        verify_extension_crds_compatibility(obs.rate_files)
        extended_refs = build_extended_reference_files(obs.rate_files)

    kwargs = dict(
        set_stellarity=stage_config.get('set_stellarity'),
        source_ids=source_ids,
        overwrite=overwrite,
        wavecorr_override=wavecorr_override,
        extended_refs=extended_refs,
        single_call=stage_config.get('single_call', True),
        extract_bkgslits=stage_config.get('extract_bkgslits', True),
        empirical_barshadow=stage_config.get('empirical_barshadow', False),
    )

    if not obs.directories_setup:
        obs.setup_workspace_directory(data_dir, products_dir, overwrite=False)

    # Mask staleness check: if any rate file's stamped CFMASKSH disagrees with
    # the current observations.toml, restore and re-apply before stage2a runs.
    # Done serially before the parallel dispatch to avoid races.
    from campfire_pipeline.nirspec.masks import ensure_fresh, read_sentinel
    rate_files_with_state = obs.manual_masks or any(
        read_sentinel(p) for p in obs.rate_files
    )
    if rate_files_with_state:
        from campfire_pipeline.config import load_config, get_stage_config
        cfg_local = cfg if cfg is not None else load_config()
        masks_cfg = cfg_local.get('nirspec', {}).get('masks', {})
        if masks_cfg.get('auto_apply', True):
            stage1_cfg = get_stage_config('stage1', cfg_local, obs)
            for rate_file in obs.rate_files:
                ensure_fresh(rate_file, obs, stage1_cfg)

    # Ensure the background override file exists so it can be edited before stage2b
    _ = obs.bkg_overrides

    # Pre-fetch CRDS references to avoid multiprocessing race conditions
    if n_processes > 1 and obs.rate_files:
        _prefetch_crds_references(obs.rate_files)

    # Run Spec2Pipeline on each rate file
    results = dispatch(
        run_stage2a_single_rate,
        obs.rate_files,
        n_processes=n_processes,
        retry=True,
        obs=obs,
        **kwargs,
    )

    # Flatten source IDs from all workers
    source_ids_processed = list(set(sid for result in results for sid in result))

    # Discover canonical files and fix units
    files = obs.discover_files(ext='canonical', source_ids=source_ids_processed)
    files = Observation.group_files(files)

    dispatch(fix_units, list(files), n_processes=n_processes)

    # Resample to s2d if plotting or stuck shutter detection needs it
    detect_enabled = (stage_config.get('detect_stuck_shutters', False)
                      and not _skip_stuck_detection)
    if plot or detect_enabled:
        dispatch(resample_single_exposure, list(files), n_processes=n_processes,
                overwrite=overwrite)

    if plot:
        stuck_dict = _stuck_table_to_dict(obs.stuck_closed_shutters)
        plot_inputs = [files[files['source_id'] == sid] for sid in source_ids_processed]
        dispatch(plot_stage2a_results, plot_inputs, n_processes=n_processes,
                 stuck_shutters=stuck_dict)

    # --- Stuck shutter auto-detection ---
    if detect_enabled:
        from campfire_pipeline.nirspec.stuck_shutters import (
            detect_stuck_shutters, merge_stuck_shutters,
            write_stuck_shutters_toml, load_stuck_shutters_tagged,
            _get_n_shutters,
        )
        from campfire_pipeline.nirspec.plots import plot_stuck_shutter_diagnostics
        import toml as _toml

        detected = detect_stuck_shutters(obs, files, stage_config,
                                        n_processes=n_processes)

        if detected:
            # Merge with existing TOML entries, preserving each entry's
            # provenance tag (hand/web/auto) through the rewrite so a later
            # `pull-stuck-shutters` authority merge still tells web from hand.
            prior_tags = load_stuck_shutters_tagged(obs.stuck_closed_shutters_file)
            existing = _toml.load(obs.stuck_closed_shutters_file)
            merged, updated = merge_stuck_shutters(existing, detected)
            provenance = {k: tag for k, (_sh, tag) in prior_tags.items()}
            for (root, sid) in updated:
                provenance[(root, str(sid))] = 'auto'
            write_stuck_shutters_toml(
                merged, obs.stuck_closed_shutters_file, obs.name,
                provenance=provenance,
            )

            # Generate diagnostic plots
            if plot:
                for root, sources in detected.items():
                    for sid, stuck_list in sources.items():
                        root_files = files[(files['root'] == root) &
                                           (files['source_id'] == sid)]
                        n_shut = _get_n_shutters(root_files)
                        plot_stuck_shutter_diagnostics(
                            files, sid, root, obs.workspace_dir,
                            n_shut, stuck_list, stage_config,
                        )

            # Re-run stage2a for affected sources with detection disabled
            affected_sids = list(set(
                sid for src in detected.values() for sid in src.keys()
            ))
            log(f'Re-running stage2a for {len(affected_sids)} source(s) '
                f'with detected stuck shutters: {affected_sids}')

            run_stage2a(
                obs, stage_config,
                source_ids=affected_sids,
                overwrite=True,
                n_processes=n_processes,
                plot=plot,
                data_dir=data_dir,
                products_dir=products_dir,
                _skip_stuck_detection=True,
            )

    # Stamp the pipeline-computed exp_group (CFEXPGRP) on each canonical's primary
    # header (§4.2) so deploy populates the nods grid with the exact grouping. Runs
    # on every run_stage2a invocation (incl. the stuck-shutter re-run above, which
    # regenerates its canonicals), so all files end up stamped.
    dispatch(stamp_exp_group_single, list(files), n_processes=n_processes)


def run_stage2b(obs, stage_config, source_ids='all', overwrite=False,
                n_processes=1, data_dir=None, products_dir=None):
    """Orchestrate stage 2b: nodded background subtraction.

    Parameters
    ----------
    obs : Observation
    stage_config : dict
        Merged stage2 configuration.
    source_ids : list or 'all'
    overwrite : bool
    n_processes : int
    data_dir, products_dir : str
        Used for workspace setup if not already done.
    """
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.plots import plot_stage2a_results

    log(f"Stage 2b config for {obs.name}: {stage_config}")

    rectify = stage_config.get('rectify', True)
    plot_bkgsub = stage_config.get('plot_bkgsub', False)

    if not obs.directories_setup:
        obs.setup_workspace_directory(data_dir, products_dir, overwrite=False)

    # Discover and group canonical files
    files = obs.discover_files(ext='canonical', source_ids=source_ids)
    if len(files) == 0:
        log(f"No canonical files found for {obs.name}")
        return
    files = Observation.group_files(files)

    # Filter out sources where no exposure has source flux (e.g. all nod
    # positions land on stuck-closed shutters, leaving only background shutter)
    srcflux = np.array([
        fits.getheader(f['path'])['SRCFLUX'] == 'T'
        if 'SRCFLUX' in fits.getheader(f['path']) else True
        for f in files
    ])
    skip_sources = set()
    for source_id in np.unique(files['source_id']):
        mask = files['source_id'] == source_id
        if not np.any(srcflux[mask]):
            log(f"ID{source_id}: no exposures with source flux, skipping stage2b")
            skip_sources.add(source_id)

    # Build task list by iterating over bkg_groups
    tasks = []
    for bg in np.unique(files['bkg_group']):
        bg_files = files[files['bkg_group'] == bg]
        source_id = bg_files['source_id'][0]
        root = bg_files['root'][0]
        detector = bg_files['detector'][0]

        if source_id in skip_sources:
            continue

        # Skip check: has stage2b fully processed every canonical in the group?
        # Gating on CFP_BKG presence alone is wrong when rectify is requested:
        # CFP_BKG is stamped unconditionally, but the rectified S2D_BKGSUB_* view
        # (CFP_S2D) is only produced under rectify, so a rectify=False -> True
        # re-run would skip and silently never produce the s2d views (#225 review).
        skip = (not overwrite) and all(
            _bkgsub_group_done(f['path'], rectify) for f in bg_files
        )

        if skip:
            log(f'ID{source_id}: bkgsub products exist for {root}_*_{detector}_{source_id}, skipping (overwrite=False)')
            continue

        bkg_overrides = obs.bkg_overrides.get(root)
        if bkg_overrides is not None:
            bkg_overrides = bkg_overrides.get(str(source_id))

        log(f'ID{source_id}: Running stage2b for bkg_group {bg} ({root}_{detector})')
        tasks.append((list(bg_files['name']), obs.workspace_dir, bkg_overrides))

    # Execute
    dispatch(
        run_stage2b_single_slitlet,
        tasks,
        n_processes=n_processes,
        use_starmap=True,
        rectify=rectify,
    )

    # Re-stamp exp_group (CFEXPGRP) after bkgsub's canonical re-saves — this is the
    # final on-disk state deploy reads (§4.2). Idempotent with the stage2a stamp.
    dispatch(stamp_exp_group_single, list(files), n_processes=n_processes)

    # Optional: plot background-subtracted 2D cutouts
    if plot_bkgsub and rectify:
        bkgsub_files = obs.discover_files(ext='canonical', source_ids=source_ids)
        if len(bkgsub_files) > 0:
            bkgsub_files = Observation.group_files(bkgsub_files)
            source_ids_done = np.unique(bkgsub_files['source_id'])
            stuck_dict = _stuck_table_to_dict(obs.stuck_closed_shutters)
            plot_tasks = [
                (bkgsub_files[bkgsub_files['source_id'] == sid], 'bkgsub')
                for sid in source_ids_done
            ]
            dispatch(
                plot_stage2a_results,
                plot_tasks,
                n_processes=n_processes,
                use_starmap=True,
                stuck_shutters=stuck_dict,
            )


def _run_stage2a_fixedslit(
        rate_file,
        obs: Observation,
        overwrite: bool = False,
        wavecorr_override=None,
        extended_refs=None,
    ):
    """Stage 2a for standalone NIRSpec fixed-slit (NRS_FIXEDSLIT) exposures.

    Unlike MSA mode, fixed-slit exposures have no MSA metadata file: jwst's
    ``assign_wcs`` builds the fixed-slit WCS from the instrument model and
    ``extract_2d`` selects slits by name. We extract only the primary slit
    (the ``FXD_SLIT`` header keyword), which jwst assigns ``source_id = 1``, and
    emit a product keyed on ``source_id = 1`` so the mode-agnostic downstream
    stages — nodded background subtraction (2b) and combination (3) — pick it up
    unchanged. The product filename matches the MSA convention
    ``{root}_{nod}_{detector}_{source_id}`` so :meth:`Observation.discover_files`
    parses it identically.
    """
    from jwst.pipeline import Spec2Pipeline
    from jwst.associations import asn_from_list
    from jwst.assign_wcs.util import NoDataOnDetectorError

    prev_cwd = os.getcwd()
    os.chdir(obs.workspace_dir)

    source_ids_processed = []
    asn_file = None
    try:
        hdr = fits.getheader(rate_file)
        primary_slit = str(hdr.get('FXD_SLIT', '')).strip()
        if not primary_slit or primary_slit.upper() == 'NONE':
            log(f'No FXD_SLIT primary slit defined for '
                f'{os.path.basename(rate_file)}, skipping')
            return []

        # jwst hard-wires source_id=1 to the primary fixed slit; reuse that as
        # the product key so the existing {..._source_id} naming carries through.
        source_id = 1
        prod_name = os.path.basename(rate_file).replace('_rate.fits', f'_{source_id}')

        nodata_marker = f'{prod_name}_nodata'
        if (os.path.exists(f'{prod_name}.fits') or os.path.exists(nodata_marker)) and not overwrite:
            log(f'Skipping stage2a for {prod_name}, overwrite=False')
            return [source_id]
        if os.path.exists(nodata_marker):
            os.remove(nodata_marker)

        # Extended-wavelength override for this config (grating-gated; typically
        # None for fixed-slit gratings, but supported for consistency with MSA).
        extended_override = None
        if extended_refs:
            extended_override = extended_refs.get((
                str(hdr.get('DETECTOR', '')).strip(),
                str(hdr.get('GRATING', '')).strip(),
                str(hdr.get('FILTER', '')).strip(),
            ))

        cards = [
            ('CFEXTWAV', extended_override is not None, 'Extended wavelength reduction applied'),
            ('CFFXSLT', True, 'NIRSpec fixed-slit source'),
            ('CFFSSLIT', primary_slit, 'NIRSpec fixed-slit aperture'),
            ('STKSHTRS', 'N/A', 'Stuck shutters masked'),
        ]

        association = [(os.path.basename(rate_file), 'science')]
        asn = asn_from_list.asn_from_list(association, with_exptype=True, product_name=prod_name)
        suggested_name, serialization = asn.dump()
        asn_file = f'{prod_name}.json'
        with open(asn_file, 'w') as asn_out:
            asn_out.write(serialization)

        steps = {
            # Extract only the primary fixed slit; the WCS exposes all open
            # slits on a FULL-frame readout but only this one holds the target.
            'extract_2d': {'slit_names': [primary_slit]},
            'extract_1d': {'skip': True},
            'barshadow': {'skip': True},
            'bkg_subtract': {'skip': True},
            'resample_spec': {'skip': True},
        }
        if wavecorr_override:
            steps['wavecorr'] = {'override_wavecorr': wavecorr_override}
        if extended_override:
            from campfire_pipeline.config import get_extended_photom_path
            steps['flat_field'] = {
                'override_fflat': extended_override['fflat'],
                'override_sflat': extended_override['sflat'],
            }
            steps['assign_wcs'] = {
                'override_wavelengthrange': extended_override['wavelengthrange'],
            }
            steps['photom'] = {
                'override_photom': get_extended_photom_path(fixed_slit=True),
            }
            log(f"Applying extended-wavelength overrides for {prod_name}")

        log(f"Running Spec2Pipeline for {prod_name} (fixed slit {primary_slit})")
        try:
            Spec2Pipeline.call(asn_file, save_results=True, steps=steps)
            log(f'Completed Spec2Pipeline for {prod_name}')
            source_ids_processed.append(source_id)
        except NoDataOnDetectorError:
            log(f'No data on detector for {prod_name}')
            with open(nodata_marker, 'w') as f:
                f.write('NoDataOnDetectorError\n')

        # Promote the cal product to the bare canonical file + stamp cards + CFP_CAL.
        _finalize_canonical(prod_name, cards)

    except KeyboardInterrupt:
        return source_ids_processed

    except Exception as e:
        log(f"ERROR in fixed-slit stage2a for {os.path.basename(rate_file)}", e)
        raise

    finally:
        # Clean up any ASN files for this rate file (matches the MSA path).
        for stale in glob.glob(rate_file.replace('_rate.fits', '*.json')):
            os.remove(stale)
        os.chdir(prev_cwd)

    return source_ids_processed


# --- Stage 2a, MSA branch: one Spec2Pipeline call per rate file ----------------
#
# ``Spec2Pipeline`` is per slit from assign_wcs onwards (extract_2d, srctype,
# wavecorr, flat_field, pathloss and photom each loop over ``model.slits``; only
# msa_flagging is exposure-wide, and it is the same in both arrangements), so one
# call over an *edited full metafile* — every per-source edit ``filter_by_source_id``
# makes, applied to all sources at once (``MetaFile.for_sources``) — yields the same
# per-slit arrays as one call per source, for a single CRDS / reference-file / WCS
# set-up instead of one per source. The result is split into the same one-slit
# canonicals ``<root>_<nod>_<nrsN>_<source>.fits`` (built the way jwst's own
# ``_extend_bg_slits`` builds a sub-model), and the dedicated background-only
# slitlets of the MSA plan, which no per-source metafile ever contained, come out
# of the same call as ``bkgslits/<root>_<nod>_<nrsN>_bkgslits.fits``, calibrated
# as the extended sources they are (uniform pathloss, MJy/sr, bar shadow applied
# as a separate step so the science slits are untouched) — the input of the
# master-background work (docs/design-nirspec-master-background.md §1.7).
#
# What the split respects:
#   * jwst refuses a slitlet with two ``primary_source = 'Y'`` rows, so sources
#     sharing a slitlet are extracted in successive *rounds* (``_plan_rounds``);
#     positional duplicates are already dropped by ``MetaFile._duplicate_source_ids``,
#     so a second round is rare.
#   * Output slits are routed to their source by slitlet (``_source_of_slit``), not
#     by ``source_id``: at a nod where the source's shutter was removed as stuck
#     closed, jwst extracts the primary-less slitlet under the background name
#     (``source_id = slitlet_id``, uniform pathloss), and the per-source loop kept
#     that as the source's nod product (``fix_units`` marks it ``SRCFLUX = 'F'``).
#     Only slitlets of no source in the round are background slitlets.
#   * The extended-wavelength photom override differs for fixed-slit sources inside
#     MSA exposures, so with ``extend_g140m_g235m`` on they get their own round.
#   * A source absent from the output (its slitlet projects off the detector) is
#     the per-source ``NoDataOnDetectorError`` case → the ``_nodata`` marker.
#   * Any other failure of the single call falls back to the per-source loop
#     (``_run_spec2_per_source``, the pre-rework code path kept verbatim) for the
#     sources the call did not produce, so a pathological slitlet costs time, not
#     the rate file. ``[nirspec.stage2].single_call = false`` selects that loop
#     outright (the bit-identity reference).

_ROUND_METAFILE = '{base}_msa_r{index}.fits'
_ROUND_PRODUCT = '{base}_spec2r{index}'


def _rate_base(rate_file):
    """``<root>_<nod>_<nrsN>`` — the stem every stage-2a product name starts with."""
    return os.path.basename(rate_file).replace('_rate.fits', '')


def _nodata_marker(prod_name):
    return f'{prod_name}_nodata'


def _write_nodata_marker(prod_name):
    with open(_nodata_marker(prod_name), 'w') as f:
        f.write('NoDataOnDetectorError\n')


def _point_rate_at_metafile(rate_file, metafile_name):
    with fits.open(rate_file, mode='update') as hdul:
        hdul[0].header['MSAMETFL'] = metafile_name
        hdul.flush()


def _write_asn(rate_file, prod_name):
    """One-member science association for *rate_file* under *prod_name* (cwd)."""
    from jwst.associations import asn_from_list
    association = [(os.path.basename(rate_file), 'science')]
    asn = asn_from_list.asn_from_list(association, with_exptype=True, product_name=prod_name)
    _suggested_name, serialization = asn.dump()
    asn_file = f'{prod_name}.json'
    with open(asn_file, 'w') as asn_out:
        asn_out.write(serialization)
    return asn_file


def _stuck_ordinals(obs, root, source_id):
    """The vetted stuck-closed shutter ordinals (1-indexed position among the
    slitlet's sorted columns) for ``(root, source_id)``, or ``None`` when the
    TOML has no entry. The raw TOML value is returned so the ``STKSHTRS`` card
    reads exactly as it always has."""
    table = obs.stuck_closed_shutters
    stuck = table[(table['root'] == root) & (table['source_id'] == source_id)]
    if len(stuck) == 0:
        return None
    return stuck['shutters'][0]


def _remove_stuck_columns(rows, ordinals):
    """Drop the shutter columns at the 1-indexed *ordinals* from a source's
    edited SHUTTER_INFO rows (adapted from Anthony & Pablo's routines)."""
    if ordinals is None or len(ordinals) == 0:
        return rows
    cols = np.sort(np.unique(rows['shutter_column']))
    drop = []
    for k in ordinals:
        k = int(k)
        if not 1 <= k <= len(cols):
            raise IndexError(f'stuck shutter ordinal {k} is out of range for a '
                             f'{len(cols)}-shutter slitlet')
        drop.append(cols[k - 1])
    return rows[~np.isin(rows['shutter_column'], drop)]


def _source_cards(obs, main_metafile, source_id, stuck, extended_override):
    """The provenance cards stage 2a stamps on one source's canonical."""
    cards = [
        ('STKSHFIL', os.path.basename(obs.stuck_closed_shutters_file), 'Stuck shutter file name'),
        ('STKSHTIM', obs.stuck_closed_shutters_mtime, 'Stuck shutter file mtime'),
        ('CFEXTWAV', extended_override is not None, 'Extended wavelength reduction applied'),
    ]
    # Fixed-slit sources need a different extended-wavelength photom reference
    # than MSA sources. Detect it from the metafile and thread the status through
    # to the product metadata so downstream stages can branch on it.
    source_is_fixed_slit = main_metafile.is_fixed_slit(source_id)
    cards.append(('CFFXSLT', source_is_fixed_slit, 'NIRSpec fixed-slit source'))
    if source_is_fixed_slit:
        cards.append(('CFFSSLIT', main_metafile.fixed_slit_name(source_id),
                      'NIRSpec fixed-slit aperture'))
    if stuck is not None and len(stuck) > 0:
        cards.append(('STKSHTRS', str(stuck), 'Stuck shutters masked'))
    else:
        cards.append(('STKSHTRS', 'N/A', 'Stuck shutters masked'))
    return cards


def _spec2_steps(wavecorr_override, extended_override, fixed_slit_photom, log_name):
    """The ``Spec2Pipeline`` step overrides stage 2a runs with."""
    steps = {
        'extract_1d': {'skip': True},
        'barshadow': {'skip': True},
        'bkg_subtract': {'skip': True},
        'resample_spec': {'skip': True},
    }

    if wavecorr_override:
        steps['wavecorr'] = {'override_wavecorr': wavecorr_override}

    if extended_override:
        # Fixed-slit sources require a separately-calibrated extended photom
        # (v0014) than the MSA one resolved in extended_override (v0015); the
        # flats / wavelengthrange overrides are identical.
        if fixed_slit_photom:
            from campfire_pipeline.config import get_extended_photom_path
            photom_override = get_extended_photom_path(fixed_slit=True)
            log(f"Using fixed-slit extended photom for {log_name}")
        else:
            photom_override = extended_override['photom']

        steps['flat_field'] = {
            'override_fflat': extended_override['fflat'],
            'override_sflat': extended_override['sflat'],
        }
        steps['assign_wcs'] = {
            'override_wavelengthrange': extended_override['wavelengthrange'],
        }
        steps['photom'] = {
            'override_photom': photom_override,
        }
        log(f"Applying extended-wavelength overrides for {log_name}")
    return steps


def _plan_rounds(main_metafile, source_ids, split_fixed_slit=False):
    """Group *source_ids* into the fewest ``Spec2Pipeline`` calls.

    Two sources sharing a slitlet cannot be primary in the same metafile, so a
    source joins the first round none of whose slitlets it touches. With
    ``split_fixed_slit`` (the extended-wavelength override is active) fixed-slit
    sources are kept in rounds of their own, since their photom override differs.
    Returns a list of ``{'source_ids': [...], 'slitlets': set, 'fixed_slit': bool}``
    in first-come order; normally a single round.
    """
    rounds = []
    for sid in source_ids:
        slits = set(main_metafile.slitlets_of(sid))
        fixed = bool(split_fixed_slit and main_metafile.is_fixed_slit(sid))
        for rnd in rounds:
            if rnd['fixed_slit'] == fixed and not (rnd['slitlets'] & slits):
                rnd['source_ids'].append(sid)
                rnd['slitlets'] |= slits
                break
        else:
            rounds.append({'source_ids': [sid], 'slitlets': set(slits), 'fixed_slit': fixed})
    return rounds


def _source_of_slit(slit, source_of_slitlet):
    """The source (of this round) a ``Spec2Pipeline`` output slit belongs to, or
    ``None`` for a background-only slitlet.

    A MOS slit is matched by its slitlet (``slit.name``), not by ``source_id``:
    when the shutter holding the source at this nod was removed as stuck closed,
    the slitlet has no primary shutter left and jwst extracts it under the
    background name (``<program>_BKG<slitlet>``, ``source_id = slitlet_id``). The
    per-source call kept that as the source's nod product, so the split does
    too. Fixed-slit names are not integers; those, and any slit of a slitlet
    not in this round, go by ``source_id`` unless jwst named them background.
    """
    from jwst.master_background.nirspec_utils import is_background_msa_slit

    try:
        slitlet = int(slit.name)
    except (TypeError, ValueError):
        slitlet = None
    if slitlet is not None and slitlet in source_of_slitlet:
        return source_of_slitlet[slitlet]
    if is_background_msa_slit(slit):
        return None
    return int(slit.source_id)


def _split_spec2_result(cal_path, base, source_ids, cards_by_source, bkgslits_path, msametfl,
                        slitlets_by_source, barshadow_override=None):
    """Split one multi-slit ``Spec2Pipeline`` product into the per-source
    canonicals (``_finalize_canonical``) and the background-slitlet file.

    ``slitlets_by_source`` maps each source id of this round to the slitlet ids
    of its edited rows; a slit is the product of the source whose slitlet it is
    (``_source_of_slit``). Returns ``(produced, missing)``: the source ids that
    got a canonical and the ones absent from the output (their slitlet projects
    off this detector).
    """
    from jwst.datamodels import MultiSlitModel

    source_of_slitlet = {int(slitlet): sid
                         for sid, slitlets in slitlets_by_source.items()
                         for slitlet in slitlets}
    produced, missing = [], []
    model = MultiSlitModel(cal_path)
    try:
        by_source, bkg_slits = {}, []
        for slit in model.slits:
            sid = _source_of_slit(slit, source_of_slitlet)
            if sid is None:
                bkg_slits.append(slit)
            else:
                by_source.setdefault(sid, []).append(slit)

        for sid in source_ids:
            slits = by_source.get(sid)
            if not slits:
                missing.append(sid)
                continue
            prod_name = f'{base}_{sid}'
            one = MultiSlitModel()
            one.update(model, extra_fits=True)
            one.slits.extend(slits)
            # The per-source call let jwst's master_background_mos run and
            # record itself SKIPPED (no background slit); here it is skipped
            # outright, so stamp the same state (stage 3 copies S_MSBSUB).
            one.meta.cal_step.master_background = 'SKIPPED'
            one.save(f'{prod_name}_cal.fits')
            one.close()
            _finalize_canonical(prod_name, cards_by_source[sid], msametfl=msametfl)
            produced.append(sid)

        unexpected = sorted(set(by_source) - set(source_ids))
        if unexpected:
            log(f'{base}: ignoring {len(unexpected)} slit(s) of sources not in this '
                f'round: {unexpected}')

        if bkgslits_path is not None:
            _write_bkgslits(model, bkg_slits, bkgslits_path, msametfl, barshadow_override)
    finally:
        model.close()
    return produced, missing


def _write_bkgslits(parent, bkg_slits, path, msametfl, barshadow_override=None):
    """Write the background-only slitlets of one exposure as a
    ``MultiSlitModel`` (``bkgslits/<root>_<nod>_<nrsN>_bkgslits.fits``).

    jwst typed them ``EXTENDED`` (``stellarity = 0``), so they already carry the
    uniform pathloss and the MJy/sr photom; the bar-shadow correction — skipped in
    the main call so the ``POINT`` science slits stay exactly as before — is
    applied here as its own step (``slit.barshadow`` keeps the array, so it can be
    undone). An exposure with no background slitlet still gets a (slit-less)
    file, which is what makes the skip logic treat it as done.
    """
    from jwst.datamodels import MultiSlitModel
    from campfire_pipeline.nirspec import canonical as C

    os.makedirs(os.path.dirname(path), exist_ok=True)
    bkg_model = MultiSlitModel()
    bkg_model.update(parent, extra_fits=True)
    bkg_model.slits.extend(bkg_slits)

    barshadow_done = False
    if len(bkg_slits):
        try:
            from jwst.barshadow import BarShadowStep
            bs_kwargs = {'override_barshadow': barshadow_override} if barshadow_override else {}
            bkg_model = BarShadowStep.call(bkg_model, **bs_kwargs)
            barshadow_done = True
        except Exception as e:
            log(f'Bar-shadow correction of the background slitlets failed for '
                f'{os.path.basename(path)}; saving them uncorrected', e)

    tmp = C._tmp_path(path)
    bkg_model.save(tmp)
    bkg_model.close()
    with fits.open(tmp, mode='update') as hdul:
        hdr = hdul[0].header
        hdr['MSAMETFL'] = msametfl
        hdr['CFNBKGSL'] = (len(bkg_slits), 'campfire: number of background-only slitlets')
        hdr['CFBARSHA'] = (barshadow_done, 'campfire: bar shadow applied to background slitlets')
        hdr['CFBSREF'] = (os.path.basename(barshadow_override) if barshadow_override else 'CRDS',
                          'campfire: bar-shadow reference (empirical file or CRDS)')
        hdul.flush()
    os.replace(tmp, path)
    log(f'Wrote {len(bkg_slits)} background slitlet(s) to {os.path.basename(path)}')


def _run_spec2_round(rate_file, obs, main_metafile, rnd, index, *, root,
                     set_stellarity, wavecorr_override, extended_override,
                     bkgslits_path, msametfl, barshadow_override=None):
    """One ``Spec2Pipeline`` call over an edited full metafile (cwd = workspace).

    Returns ``(produced, nodata)``: source ids that got a canonical, and the ones
    that got a ``_nodata`` marker instead.
    """
    from jwst.pipeline import Spec2Pipeline
    from jwst.assign_wcs.util import NoDataOnDetectorError

    base = _rate_base(rate_file)

    rows_by_source, cards_by_source, all_stuck = {}, {}, []
    for sid in rnd['source_ids']:
        stuck = _stuck_ordinals(obs, root, sid)
        rows = _remove_stuck_columns(main_metafile.rows_for_source(sid), stuck)
        cards_by_source[sid] = _source_cards(obs, main_metafile, sid, stuck, extended_override)
        if len(rows) == 0:
            log(f'All shutters marked as stuck closed for {base}_{sid}, skipping extraction')
            all_stuck.append(sid)
            continue
        rows_by_source[sid] = rows
    wanted = [sid for sid in rnd['source_ids'] if sid not in all_stuck]

    bkg_ids = main_metafile.background_slitlet_ids() if bkgslits_path is not None else []
    if not rows_by_source and not bkg_ids:
        if bkgslits_path is not None:
            # Nothing to extract at all, but record that this exposure has no
            # background slitlet so the next run does not look again.
            from jwst.datamodels import ImageModel
            with ImageModel(rate_file) as parent:
                _write_bkgslits(parent, [], bkgslits_path, msametfl, barshadow_override)
        return [], []

    metafile = main_metafile.for_sources(
        rows_by_source, set_stellarity=set_stellarity,
        filename=_ROUND_METAFILE.format(base=base, index=index),
        background_slitlets=bkg_ids)
    metafile.write(obs.workspace_dir, overwrite=True)
    _point_rate_at_metafile(rate_file, metafile.filename)

    prod_name = _ROUND_PRODUCT.format(base=base, index=index)
    asn_file = _write_asn(rate_file, prod_name)
    steps = _spec2_steps(wavecorr_override, extended_override, rnd['fixed_slit'], prod_name)
    # Never let jwst's own master background run over the background slitlets
    # we are extracting (it would subtract them from the science slits).
    steps['master_background_mos'] = {'skip': True}

    log(f'Running Spec2Pipeline for {prod_name}: {len(rows_by_source)} source slitlet(s)'
        + (f' + {len(bkg_ids)} background slitlet(s)' if bkg_ids else ''))
    cal_path = f'{prod_name}_cal.fits'
    try:
        Spec2Pipeline.call(asn_file, save_results=True, steps=steps)
        log(f'Completed Spec2Pipeline for {prod_name}')
    except NoDataOnDetectorError:
        log(f'No data on detector for {prod_name}')
        for sid in wanted:
            _write_nodata_marker(f'{base}_{sid}')
        return [], wanted
    finally:
        for stale in (asn_file, metafile.filename):
            if os.path.exists(stale):
                os.remove(stale)

    try:
        slitlets_by_source = {sid: set(np.unique(rows['slitlet_id']).tolist())
                              for sid, rows in rows_by_source.items()}
        produced, missing = _split_spec2_result(
            cal_path, base, wanted, cards_by_source, bkgslits_path, msametfl,
            slitlets_by_source, barshadow_override)
    finally:
        if os.path.exists(cal_path):
            os.remove(cal_path)
    for sid in missing:
        log(f'No data on detector for {base}_{sid}')
        _write_nodata_marker(f'{base}_{sid}')
    return produced, missing


def _run_spec2_single_call(rate_file, obs, main_metafile, pending, bkgslits_path, *,
                           root, set_stellarity, wavecorr_override, extended_override,
                           msametfl, barshadow_override=None):
    """The single-call path: ``_plan_rounds`` then one ``_run_spec2_round`` each;
    the background slitlets ride with the first round."""
    base = _rate_base(rate_file)
    rounds = _plan_rounds(main_metafile, pending,
                          split_fixed_slit=extended_override is not None)
    if not rounds:
        rounds = [{'source_ids': [], 'slitlets': set(), 'fixed_slit': False}]
    if len(rounds) > 1:
        log(f'{base}: {len(pending)} source(s) need {len(rounds)} Spec2Pipeline calls '
            f'(slitlets shared by several sources, or fixed-slit sources under the '
            f'extended-wavelength override)')
    produced = []
    for index, rnd in enumerate(rounds, start=1):
        got, _nodata = _run_spec2_round(
            rate_file, obs, main_metafile, rnd, index, root=root,
            set_stellarity=set_stellarity, wavecorr_override=wavecorr_override,
            extended_override=extended_override,
            bkgslits_path=bkgslits_path if index == 1 else None, msametfl=msametfl,
            barshadow_override=barshadow_override)
        produced += got
    return produced


def _cleanup_round_files(base):
    """Remove whatever an interrupted single call left in the workspace."""
    for pattern in (_ROUND_METAFILE.format(base=base, index='*'),
                    _ROUND_PRODUCT.format(base=base, index='*') + '.fits',
                    _ROUND_PRODUCT.format(base=base, index='*') + '_cal.fits'):
        for stale in glob.glob(pattern):
            os.remove(stale)


def _run_spec2_per_source(rate_file, obs, main_metafile, source_ids, *, root,
                          set_stellarity, wavecorr_override, extended_override):
    """One ``Spec2Pipeline`` call per source (cwd = workspace): the original
    stage-2a arrangement, kept as the fallback of the single call and as the
    reference it is checked against. Returns the source ids produced."""
    from jwst.pipeline import Spec2Pipeline
    from jwst.assign_wcs.util import NoDataOnDetectorError

    base = _rate_base(rate_file)
    source_ids_processed = []
    for source_id in source_ids:
        prod_name = f'{base}_{source_id}'

        # copy the metafile to a source-specific metafile
        # modify that source-specific meta file to only have 1 source in it
        source_metafile = main_metafile.filter_by_source_id(
            source_id, filename=f'{base}_msa_{source_id}.fits',
            set_stellarity=set_stellarity)

        # remove shutters that are marked as stuck closed
        stuck = _stuck_ordinals(obs, root, source_id)
        cards = _source_cards(obs, main_metafile, source_id, stuck, extended_override)
        source_metafile.shutter_table = _remove_stuck_columns(
            source_metafile.shutter_table, stuck)

        # Skip extraction if all shutters are stuck closed
        if len(source_metafile.shutter_table) == 0:
            log(f'All shutters marked as stuck closed for {prod_name}, skipping extraction')
            continue

        source_metafile.write(obs.workspace_dir, overwrite=True)
        _point_rate_at_metafile(rate_file, source_metafile.filename)

        asn_file = _write_asn(rate_file, prod_name)
        steps = _spec2_steps(wavecorr_override, extended_override,
                             main_metafile.is_fixed_slit(source_id), prod_name)

        log(f"Running Spec2Pipeline for {prod_name}")
        try:
            Spec2Pipeline.call(asn_file, save_results=True, steps=steps)
            log(f'Completed Spec2Pipeline for {prod_name}')
            source_ids_processed.append(source_id)

        except NoDataOnDetectorError:
            log(f'No data on detector for {prod_name}')
            _write_nodata_marker(prod_name)

        # Promote the cal product to the bare canonical file + stamp cards + CFP_CAL.
        _finalize_canonical(prod_name, cards)

        if os.path.exists(asn_file):
            os.remove(asn_file)

        if os.path.exists(source_metafile.filename):
            os.remove(source_metafile.filename)

    return source_ids_processed


def run_stage2a_single_rate(
        rate_file,
        obs: Observation,
        set_stellarity: bool = False,
        source_ids='all',
        overwrite: bool = False,
        wavecorr_override=None,
        extended_refs=None,
        single_call: bool = True,
        extract_bkgslits: bool = True,
        empirical_barshadow: bool = False,
        **kwargs
    ):
    """Stage 2a for one rate file: every source's canonical, plus the exposure's
    background-only slitlets (``bkgslits/``) when ``extract_bkgslits``.

    ``single_call`` runs one ``Spec2Pipeline`` over an edited full metafile and
    splits the result (see the module comment above); ``False`` runs the original
    one-call-per-source loop. Returns the source ids with a canonical (existing
    or new).
    """

    log(f'Starting stage2a for {os.path.basename(rate_file)}')

    # Standalone fixed-slit exposures carry no MSA metadata file. jwst builds
    # the fixed-slit WCS natively and extract_2d selects slits by name, so the
    # whole MSAMETFL/per-source-metafile machinery below does not apply —
    # dispatch to the dedicated fixed-slit path. The MSA branch is unchanged.
    if str(fits.getheader(rate_file).get('EXP_TYPE', '')).upper() == 'NRS_FIXEDSLIT':
        return _run_stage2a_fixedslit(
            rate_file, obs, overwrite=overwrite,
            wavecorr_override=wavecorr_override, extended_refs=extended_refs,
        )

    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(obs.workspace_dir)


    # Restore MSAMETFL from OGMETFL if a previous interrupted run left it corrupted
    with fits.open(rate_file, mode='update') as hdul:
        msametfl = hdul[0].header['MSAMETFL']
        ogmetfl = hdul[0].header.get('OGMETFL')
        if ogmetfl and msametfl != ogmetfl:
            log(f"Restoring MSAMETFL from OGMETFL for {os.path.basename(rate_file)}: "
                f"'{msametfl}' -> '{ogmetfl}'")
            hdul[0].header['MSAMETFL'] = ogmetfl
            hdul.flush()

    # figure out list of source ids to extract from this rate file (using meta file)
    main_metafile = MetaFile.load_for_rate_file(rate_file)
    # Save original metafile path as backup — only on first run, never overwrite
    # (if a previous run crashed, MSAMETFL may already be corrupted; overwriting
    # OGMETFL with that value would destroy the only recovery path)
    with fits.open(rate_file, mode='update') as hdul:
        if 'OGMETFL' not in hdul[0].header:
            hdul[0].header['OGMETFL'] = main_metafile.filename
            hdul.flush()

    base = _rate_base(rate_file)
    root = '_'.join(base.split('_')[:2])

    # Resolve the extended-wavelength override for this rate file's config (if any).
    # Constant across source_ids in this exposure; used for both the provenance card
    # and the spec2 step overrides below. None for non-gated gratings even when the
    # extend_g140m_g235m flag is on, so those run normally.
    extended_override = None
    if extended_refs:
        _ehdr = fits.getheader(rate_file)
        extended_override = extended_refs.get((
            str(_ehdr.get('DETECTOR', '')).strip(),
            str(_ehdr.get('GRATING', '')).strip(),
            str(_ehdr.get('FILTER', '')).strip(),
        ))

    common = dict(root=root, set_stellarity=set_stellarity,
                  wavecorr_override=wavecorr_override, extended_override=extended_override)

    # The empirical bar-shadow table (campfire_pipeline/data, derived by
    # Generate_Barshadow_Cals.py) replaces the CRDS reference for the background
    # slitlets when configured and available for this grating; the science slits
    # never get a bar-shadow correction here (POINT sources, step skipped).
    barshadow_override = None
    if empirical_barshadow:
        from campfire_pipeline.config import get_empirical_barshadow_path
        _grating = str(fits.getheader(rate_file).get('GRATING', '')).strip()
        barshadow_override = get_empirical_barshadow_path(_grating)
        if barshadow_override is None:
            log(f'No empirical bar-shadow table for {_grating}; the background slitlets of '
                f'{base} use the CRDS reference')

    source_ids_processed = []
    try:
        source_ids_to_process = main_metafile.unique_source_ids
        if source_ids != 'all':
            source_ids_to_process = source_ids_to_process[np.isin(source_ids_to_process, source_ids)]

        pending = []
        for source_id in source_ids_to_process:
            prod_name = f'{base}_{source_id}'
            nodata_marker = _nodata_marker(prod_name)
            if (os.path.exists(f'{prod_name}.fits') or os.path.exists(nodata_marker)) and not overwrite:
                log(f'Skipping stage2a for {prod_name}, overwrite=False')
                source_ids_processed.append(source_id)
                continue
            # Remove stale marker before re-processing
            if os.path.exists(nodata_marker):
                os.remove(nodata_marker)
            pending.append(source_id)

        bkgslits_path = os.path.join(obs.bkgslits_dir, f'{base}_bkgslits.fits')
        bkg_needed = bool(extract_bkgslits and single_call
                          and (overwrite or not os.path.exists(bkgslits_path)))

        if not pending and not bkg_needed:
            log('No source IDs to process, exiting...')
            return source_ids_processed

        if single_call:
            try:
                source_ids_processed += _run_spec2_single_call(
                    rate_file, obs, main_metafile, pending,
                    bkgslits_path if bkg_needed else None,
                    msametfl=main_metafile.filename, barshadow_override=barshadow_override,
                    **common)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f'Single-call stage2a failed for {base}; falling back to one '
                    f'Spec2Pipeline call per source', e)
                _cleanup_round_files(base)
                remaining = [
                    sid for sid in pending
                    if not (os.path.exists(f'{base}_{sid}.fits')
                            or os.path.exists(_nodata_marker(f'{base}_{sid}')))
                ]
                source_ids_processed += _run_spec2_per_source(
                    rate_file, obs, main_metafile, remaining, **common)
        else:
            source_ids_processed += _run_spec2_per_source(
                rate_file, obs, main_metafile, pending, **common)

    except KeyboardInterrupt:
        return source_ids_processed

    except Exception as e:
        log(f"ERROR in stage2a for {base}", e)
        raise

    finally:

        # Restore original metafile info in header
        with fits.open(rate_file, mode='update') as hdul:
            if 'OGMETFL' in hdul[0].header:
                hdul[0].header['MSAMETFL'] = hdul[0].header['OGMETFL']
            else:
                hdul[0].header['MSAMETFL'] = main_metafile.filename
            hdul.flush()

        # Cleanup any remaining ASN files and single-call leftovers
        asn_files = glob.glob(rate_file.replace('_rate.fits','*.json'))
        for asn_file in asn_files:
            os.remove(asn_file)
        _cleanup_round_files(base)

        os.chdir(prev_cwd)

    return source_ids_processed


def _finalize_canonical(prod_name, cards, msametfl=None):
    """Promote Spec2's ``{prod_name}_cal.fits`` to the bare canonical
    ``{prod_name}.fits`` (issue #212): rename in place, stamp the provenance
    ``cards`` and the ``CFP_CAL`` state keyword. Must run with cwd in the
    workspace (relative paths). Returns the canonical path, or None if the cal
    product is absent (NoDataOnDetectorError). ``msametfl`` (the single-call
    path) rewrites the ``MSAMETFL`` card to the observation's real metafile,
    instead of the transient edited copy the call ran on."""
    from campfire_pipeline.common import cfp
    from campfire_pipeline.nirspec import canonical as C
    cal_out = f'{prod_name}_cal.fits'
    canonical = f'{prod_name}.fits'
    if not os.path.exists(cal_out):
        return None
    os.replace(cal_out, canonical)
    cfp_cards = cfp.format(keyset=cfp.NIRSPEC, CFP_CAL=None)
    # Stamp the canonical format version at birth. It rides through stage2b's
    # MultiSlitModel re-save via the jwst datamodel's extra_fits round-trip (same
    # path the CFP_* / provenance cards rely on), and is intentionally not a CFP_*
    # card so `nirspec reset` never strips it (a reset doesn't change the format).
    schema_card = C.schema_version_card()
    with fits.open(canonical, mode='update') as hdul:
        for card in cards:
            hdul['PRIMARY'].header[card[0]] = card[1:3]
        for key, (val, comment) in cfp_cards.items():
            hdul['PRIMARY'].header[key] = (val, comment)
        for key, (val, comment) in schema_card.items():
            hdul['PRIMARY'].header[key] = (val, comment)
        if msametfl is not None:
            hdul['PRIMARY'].header['MSAMETFL'] = msametfl
        hdul.flush()
    return canonical


def _bkgsub_group_done(canonical_path, rectify):
    """True if stage2b has fully processed this canonical (issue #212).

    A canonical is done when ``CFP_BKG`` is set; for an actually-subtracted nod
    (timestamp value) the rectified ``S2D_BKGSUB_*`` view (``CFP_S2D``) must also
    be present when ``rectify`` is requested. The ``skipped:`` / ``excluded:``
    ``CFP_BKG`` markers are terminal — those canonicals stay calibrated and never
    receive a bkgsub s2d view — so they don't require ``CFP_S2D``.
    """
    hdr = fits.getheader(canonical_path)
    cfp_bkg = hdr.get('CFP_BKG')
    if not cfp_bkg:
        return False
    if str(cfp_bkg).startswith(('skipped', 'excluded')):
        return True
    return (not rectify) or ('CFP_S2D' in hdr)


def stamp_exp_group_single(file):
    """Stamp ``CFEXPGRP`` (the pipeline-computed exp_group) on one canonical's
    primary header (NIRSpec review loop §4.2).

    Dispatched over the grouped ``files`` table at the end of stage2a/2b so deploy
    can populate the web nods-renderer grid with the exact pipeline grouping. Uses
    ``append_extras`` (which copies the existing header) — additive and idempotent,
    surviving stage2b's re-save the same way ``CFSCHEMA`` does. Mirrors the
    per-file ``fix_units`` dispatch shape.
    """
    from campfire_pipeline.nirspec import canonical as C
    C.append_extras(file['path'], header_updates=C.exp_group_card(file['exp_group']))


def fix_units(file):
    """
    Fixes unit issues arising from the JWST pipeline encoding units improperly when the source isn't present in the slitlet
    """

    with fits.open(file['path'], mode='update') as hdul:

        if 'PTHLOSS' in hdul['SCI'].header:
            pthloss = hdul['SCI'].header['PTHLOSS']
        else:
            pthloss = 'POINT'

        if pthloss == 'UNIFORM' and hdul['SCI'].header['PHOTMJSR'] != 1.:
            log(f"Correcting units for {file['name']}")

            photmjsr = hdul['SCI'].header['PHOTMJSR']
            scale_factor = 1 / photmjsr
            hdul['SCI'].data *= scale_factor # SCI
            hdul['ERR'].data *= scale_factor # ERR
            hdul['VAR_POISSON'].data *= scale_factor**2 # VAR
            hdul['VAR_RNOISE'].data *= scale_factor**2 # VAR
            hdul['VAR_FLAT'].data *= scale_factor**2 # VAR
            hdul['SCI'].header['PHOTMJSR'] = 1.0
            hdul['PRIMARY'].header['SRCFLUX'] = ('F', 'Source flux present in exposure? T/F')
            hdul['PRIMARY'].header['BUNIT'] = 'MJy'
            hdul['SCI'].header['BUNIT'] = 'MJy'

        else:
            hdul['PRIMARY'].header['SRCFLUX'] = ('T', 'Source flux present in exposure? T/F')
            hdul['PRIMARY'].header['BUNIT'] = 'MJy'
            hdul['SCI'].header['BUNIT'] = 'MJy'

        hdul.flush()


def resample_single_exposure(file: Table, overwrite=False):
    """Rectify the canonical (cal-state) file and cache the resampled view as
    ``S2D_{SCI,DQ,VAR_RNOISE}`` HDUs on it (issue #212; was a standalone
    ``_s2d.fits``). Consumed by plots.py / stuck_shutters.py."""
    from campfire_pipeline.nirspec import canonical as C

    canonical_file = file['path']
    workspace_dir = os.path.dirname(canonical_file)

    # Skip if the (un-bkgsub) s2d view is already cached on the canonical.
    if not overwrite:
        with fits.open(canonical_file, memmap=False) as _h:
            if any((hh.name or '').upper() == 'S2D_SCI' for hh in _h):
                return

    # Handle directory changes
    prev_cwd = os.getcwd()
    os.chdir(workspace_dir)

    try:
        from jwst.datamodels import MultiSlitModel
        from jwst.pixel_replace import PixelReplaceStep
        from jwst.resample import ResampleSpecStep
        pixel_replace = PixelReplaceStep()
        resample_spec = ResampleSpecStep()

        model = MultiSlitModel(canonical_file)

        resampled = model.copy()
        if resampled.meta.cal_step.pathloss == 'COMPLETE':
            from jwst.pathloss import PathLossStep
            pathloss = PathLossStep()
            resampled = pathloss.call(resampled, inverse=True)
        resampled = pixel_replace.call(resampled)
        resampled = resample_spec.call(resampled)

        slit = resampled.slits[0]
        extras = C.make_prefixed_hdus(
            {'SCI': slit.data, 'DQ': slit.dq, 'VAR_RNOISE': slit.var_rnoise},
            C.S2D_PREFIX)
        resampled.close()
        model.close()

        C.append_extras(os.path.basename(canonical_file), extra_hdus=extras)

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:
        os.chdir(prev_cwd)


def pad_to_common_detector_region(models):
    """
    Returns padded models and padding info needed to un-pad later
    """
    # Find the common bounding box in detector coordinates
    min_y = min(model.slits[0].ystart for model in models)
    min_x = min(model.slits[0].xstart for model in models)

    max_y = max(model.slits[0].ystart + model.slits[0].ysize for model in models)
    max_x = max(model.slits[0].xstart + model.slits[0].xsize for model in models)

    common_shape = (max_y - min_y, max_x - min_x)

    # Pad each cutout to the common region
    new_models = []
    padding_info = []

    for model in models:
        data = model.slits[0].data
        err = model.slits[0].err
        dq = model.slits[0].dq
        start_y, start_x = model.slits[0].ystart, model.slits[0].xstart

        # Calculate padding needed on each side
        pad_top = start_y - min_y
        pad_bottom = max_y - (start_y + data.shape[0])
        pad_left = start_x - min_x
        pad_right = max_x - (start_x + data.shape[1])

        pad_width = ((pad_top, pad_bottom), (pad_left, pad_right))

        padded_data = np.pad(data, pad_width,
                            mode='constant', constant_values=0)
        padded_err = np.pad(err, pad_width,
                           mode='constant', constant_values=0)
        padded_dq = np.pad(dq, pad_width,
                          mode='constant', constant_values=0)

        new_model = model.copy()
        new_model.slits[0].data = padded_data
        new_model.slits[0].err = padded_err
        new_model.slits[0].dq = padded_dq
        new_models.append(new_model)

        # Store padding info for later un-padding
        padding_info.append({
            'pad_top': pad_top,
            'pad_bottom': pad_bottom,
            'pad_left': pad_left,
            'pad_right': pad_right
        })

    return new_models, padding_info


def unpad_model(model, padding_info):
    """
    Remove padding from a model using stored padding info
    """
    data = model.slits[0].data
    err = model.slits[0].err
    dq = model.slits[0].dq
    pad = padding_info

    # Slice out the original region
    unpadded_data = data[
        pad['pad_top'] : data.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : data.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]
    unpadded_err = err[
        pad['pad_top'] : err.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : err.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]
    unpadded_dq = dq[
        pad['pad_top'] : dq.shape[0] - pad['pad_bottom'] if pad['pad_bottom'] > 0 else None,
        pad['pad_left'] : dq.shape[1] - pad['pad_right'] if pad['pad_right'] > 0 else None
    ]

    new_model = model.copy()
    new_model.slits[0].data = unpadded_data
    new_model.slits[0].err = unpadded_err
    new_model.slits[0].dq = unpadded_dq
    return new_model

def run_stage2b_single_slitlet(
        cal_files: List,
        workspace_dir: str,
        bkg_overrides,
        rectify: bool = False, # whether to produce rectified (i.e., s2d) products as well
    ):
    # this function shouldn't be doing any file discovery!

    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(workspace_dir)

    try:
        from campfire_pipeline.common import cfp
        from campfire_pipeline.nirspec import canonical as C
        from jwst.pathloss import PathLossStep
        from jwst.background import BackgroundStep
        from jwst.datamodels import MultiSlitModel, ImageModel
        bkg_subtract = BackgroundStep()
        pathloss = PathLossStep()

        if rectify:
            from jwst.pixel_replace import PixelReplaceStep
            from jwst.resample import ResampleSpecStep
            pixel_replace = PixelReplaceStep()
            resample_spec = ResampleSpecStep()

        if len(cal_files)==1:
            log(f"Single exposure only ({os.path.basename(cal_files[0])}), no background subtraction to be done!")
            # Canonical stays calibrated; mark it excluded from the stage3 science
            # combination (was: no _cal_bkgsub file written for it).
            C.append_extras(cal_files[0], header_updates=cfp.format(
                keyset=cfp.NIRSPEC, CFP_BKG='skipped:nods=1'))
            return

        elif len(cal_files) in [2, 3, 5]:
            # Re-run safety: restore any canonical already subtracted in place back
            # to the calibrated frame (the leapfrog backgrounds must be clean cal
            # slits), then capture the cal arrays for the revert (PRE_BKGSUB_*) HDUs.
            for cf in cal_files:
                if C.has_pre_bkgsub(cf):
                    C.restore_pre_bkgsub(cf)
            pre_arrays = [C.read_slit_arrays(cf) for cf in cal_files]

            # Load cal files as MultiSlitModels, but undo any pathloss corrections
            models = []
            do_pathloss = []
            for cal_file in cal_files:
                model = MultiSlitModel(cal_file)
                if 'COMPLETE' in model.meta.cal_step.pathloss.upper():
                    log(f'Inverting the pathloss for {os.path.basename(cal_file)}')
                    inverted = pathloss.call(model, inverse=True)
                    do_pathloss.append(True)
                else:
                    inverted = model
                    do_pathloss.append(False)

                models.append(inverted)

            shapes = [model[0].data.shape for model in models]
            # print(shapes)
            padded = False
            if not len(list(set(shapes))) == 1:
                models, padding_info = pad_to_common_detector_region(models)
                padded = True
                # raise ValueError('Inconsistent shapes!')

            # shapes = [model[0].data.shape for model in models]
            # print(shapes)
            # assert 1==2

            for i in range(len(cal_files)):
                science = models[i][0]


                bkg = [mod[0] for j,mod in enumerate(models) if j != i]
                nods = [int(os.path.basename(cal_file).split('_')[2]) for cal_file in cal_files]
                bkg_nods = [n for j, n in enumerate(nods) if j != i]
                overridden = False

                if bkg_overrides is not None:
                    if str(nods[i]) in bkg_overrides:
                        nods_to_use = bkg_overrides[str(nods[i])]
                        if len(nods_to_use) == 0:
                            log(f'{os.path.basename(cal_files[i])}: Override empty for nod {nods[i]}, excluding nod from science combination')
                            # Canonical stays calibrated; mark it excluded for stage3.
                            header_updates = cfp.format(keyset=cfp.NIRSPEC, CFP_BKG='excluded:override')
                            header_updates.update(C.bkg_nods_cards([], overridden=True))
                            C.append_extras(cal_files[i], header_updates=header_updates)
                            continue
                        log(f'{os.path.basename(cal_files[i])}: Only using nods {nods_to_use} for bkg subtraction for nod {nods[i]}')
                        bkg = [b[0] for n,b in zip(nods,models) if n in nods_to_use]
                        bkg_nods = [n for n in nods if n in nods_to_use]
                        overridden = True

                bkgsub = bkg_subtract.call(science, bkg)


                result = models[i].copy()
                # # janky, but this is the best way I could find to ensure that all metadata gets retained through file I/O
                result.slits[0].data = bkgsub.data
                result.slits[0].err = bkgsub.err
                result.slits[0].dq = bkgsub.dq
                result.slits[0].var_rnoise = bkgsub.var_rnoise
                result.slits[0].var_poisson = bkgsub.var_poisson
                result.meta.cal_step.bkg_subtract = 'COMPLETE'
                result[0].meta.cal_step.bkg_subtract = 'COMPLETE'

                im = ImageModel(cal_files[i])
                result[0].meta.pointing = im.meta.pointing

                if padded:
                    result = unpad_model(result, padding_info[i])

                # apply the pathloss correction again
                if do_pathloss[i]:
                    result = pathloss.call(result)



                # Write the bkgsub state into the canonical in place: live SCI =
                # bkgsub'd, stash the cal arrays as PRE_BKGSUB_* (reversible),
                # cache the rectified view as S2D_BKGSUB_*, stamp CFP_BKG/CFP_S2D.
                extras = C.make_prefixed_hdus(pre_arrays[i], C.PRE_BKGSUB_PREFIX)
                header_updates = cfp.format(keyset=cfp.NIRSPEC, CFP_BKG=None)
                header_updates.update(C.bkg_nods_cards(bkg_nods, overridden=overridden))
                if rectify:
                    resampled = result.copy()
                    resampled = pixel_replace.call(resampled)
                    resampled = resample_spec.call(resampled)
                    rslit = resampled.slits[0]
                    extras += C.make_prefixed_hdus(
                        {'SCI': rslit.data, 'DQ': rslit.dq, 'VAR_RNOISE': rslit.var_rnoise},
                        C.S2D_BKGSUB_PREFIX)
                    header_updates.update(cfp.format(keyset=cfp.NIRSPEC, CFP_S2D=None))
                    resampled.close()

                C.save_canonical(result, cal_files[i], extra_hdus=extras,
                                 header_updates=header_updates)
                result.close()

        else:
            raise Exception("not sure how to handle this # of exposures!")

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:
        os.chdir(prev_cwd)
