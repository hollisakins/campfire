"""
Stage 3: Spec3Pipeline, optimal extraction, and 1D combination across exposure groups.
"""

import os
import glob
import warnings
import numpy as np
from typing import List
from datetime import datetime, timezone
from astropy.io import fits
from astropy.table import Table, Column
from astropy.io.fits import table_to_hdu
from jwst import associations

from campfire_pipeline.common.io import log
from campfire_pipeline.nirspec.observation import (
    read_nod_type,
    read_primary_dither_points,
)
from campfire_pipeline.nirspec.extraction import (
    boxcar_profile,
    optext_profile,
    optext_profile_is_corrupted,
    extract_with_profile,
    combine_1d_spectra,
)


def run_stage3(obs, stage_config, config, source_ids='all', n_processes=1,
               overwrite=False, data_dir=None, products_dir=None):
    """Orchestrate stage 3: Spec3Pipeline + optimal extraction + optional 1D combine.

    Parameters
    ----------
    obs : Observation
    stage_config : dict
        Merged stage3 configuration.
    config : dict
        Full pipeline config (for pipeline.version).
    source_ids : list or 'all'
    n_processes, overwrite : int, bool
    data_dir, products_dir : str
        Used for workspace setup if not already done.
    """
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.observation import Observation

    from campfire_pipeline.common.version import get_reduction_version
    version = get_reduction_version(config)
    log(f"Stage 3 config for {obs.name}: {stage_config}")

    bkg_subtraction_method = stage_config.get('method', 'nodded')
    s3_kwargs = dict(
        cleanup_asn=stage_config.get('cleanup_asn', True),
        cleanup_crfs=stage_config.get('cleanup_crfs', True),
    )
    plot_profiles = stage_config.get('plot_profiles', True)
    plot_optext = stage_config.get('plot_optext', True)
    combine_method = stage_config.get('combine_method', '2d')
    profile_fit = bool(stage_config.get('profile_fit', False))
    cleanup_crfs = s3_kwargs['cleanup_crfs']
    if profile_fit:
        # the profile fit drizzles its model through Spec3's crf; it cleans up after itself
        s3_kwargs['cleanup_crfs'] = False

    if not obs.directories_setup:
        obs.setup_workspace_directory(data_dir, products_dir, overwrite=False)

    # Discover and group canonical spectrum-exposure files (issue #212).
    if bkg_subtraction_method == 'nodded':
        files = obs.discover_files(ext='canonical', source_ids=source_ids)
    else:
        raise NotImplementedError

    if len(files) == 0:
        log(f"No canonical files found for {obs.name}")
        return

    files = Observation.group_files(files)
    files_all = files   # every exposure, incl. those only used as nod backgrounds

    # Keep only canonicals whose live SCI is background-subtracted. CFP_BKG holds
    # a timestamp for subtracted nods and a 'skipped:nods=N' / 'excluded:override'
    # marker for the no-valid-nod-pair and empty-bkg-override exclusions — which
    # were realized by file-absence before the 4->1 consolidation (the canonical
    # always exists now, so the exclusions become an explicit state filter).
    def _is_bkgsub(path):
        v = fits.getheader(path).get('CFP_BKG')
        return bool(v) and not str(v).startswith(('skipped', 'excluded'))
    files = files[[_is_bkgsub(f['path']) for f in files]]
    if len(files) == 0:
        log(f"No background-subtracted canonical files for {obs.name}")
        return

    # Filter out files where source flux is not present (SRCFLUX-absent exclusion)
    files['srcflux'] = [
        fits.getheader(f['path'])['SRCFLUX'] == 'T'
        if 'SRCFLUX' in fits.getheader(f['path']) else True
        for f in files
    ]

    tasks = []
    phase2_tasks = []
    pf_tasks = []

    for source_id in np.unique(files['source_id']):
        files1 = files[files['source_id'] == source_id]

        for filter_grating in np.unique(files1['filter_grating']):
            target_files = files1[files1['filter_grating'] == filter_grating]
            target_files = target_files[target_files['srcflux']]

            target_files.pprint()

            if combine_method == '2d':
                product_name = f"{obs.name}_{filter_grating}_{source_id}"
                if len(target_files) == 0:
                    continue

                if os.path.exists(os.path.join(obs.workspace_dir, product_name + "_spec.fits")) and not overwrite:
                    continue

                tasks.append((list(target_files['name']), obs.workspace_dir, product_name, source_id))
                if profile_fit:
                    pf_tasks.append(_profile_fit_task(obs, files_all, target_files, product_name, source_id))

            elif combine_method == '1d':
                if len(target_files) == 0:
                    continue

                exp_groups = np.unique(target_files['exp_group'])
                final_product_name = f"{obs.name}_{filter_grating}_{source_id}"

                if len(exp_groups) == 1:
                    # Single exposure group: run directly as the final product (no 1D combine needed)
                    if os.path.exists(os.path.join(obs.workspace_dir, final_product_name + "_spec.fits")) and not overwrite:
                        continue
                    eg_files = target_files[target_files['exp_group'] == exp_groups[0]]
                    if len(eg_files) == 0:
                        continue
                    tasks.append((list(eg_files['name']), obs.workspace_dir, final_product_name, source_id))
                    if profile_fit:
                        pf_tasks.append(_profile_fit_task(obs, files_all, eg_files, final_product_name, source_id))
                else:
                    # Multiple exposure groups: per-group extraction then 1D combine
                    for eg in exp_groups:
                        eg_files = target_files[target_files['exp_group'] == eg]
                        if len(eg_files) == 0:
                            continue
                        product_name = f"{obs.name}_{filter_grating}_{source_id}_g{eg}"
                        if os.path.exists(os.path.join(obs.workspace_dir, product_name + "_spec.fits")) and not overwrite:
                            continue
                        tasks.append((list(eg_files['name']), obs.workspace_dir, product_name, source_id))
                        if profile_fit:
                            pf_tasks.append(_profile_fit_task(obs, files_all, eg_files, product_name, source_id))

                    if os.path.exists(os.path.join(obs.workspace_dir, final_product_name + "_spec.fits")) and not overwrite:
                        continue

                    per_eg_spec_files = [
                        f"{obs.name}_{filter_grating}_{source_id}_g{eg}_spec.fits"
                        for eg in exp_groups
                    ]
                    all_cal_files = list(target_files['name'])
                    phase2_tasks.append((
                        per_eg_spec_files, all_cal_files,
                        obs.workspace_dir, final_product_name, source_id, filter_grating,
                    ))

            else:
                raise ValueError(f"Unknown combine_method '{combine_method}'. Must be '2d' or '1d'.")

    from campfire_pipeline.nirspec import profile_timing as TM

    # Phase 1: Spec3Pipeline + optimal extraction
    with TM.phase(f'spec3 {obs.name} ({len(tasks)} products)'):
        dispatch(
            run_stage3_single_source,
            tasks,
            n_processes=n_processes,
            use_starmap=True,
            **s3_kwargs,
        )

    if pf_tasks:
        log(f"Profile fit: {len(pf_tasks)} products")
        with TM.phase(f'profile_fit {obs.name} (all)'):
            run_profile_fit_phase(obs, stage_config, pf_tasks, n_processes=n_processes, overwrite=overwrite,
                                  cleanup_crfs=cleanup_crfs)

    optext_tasks = [(task[2], task[1]) for task in tasks]
    with TM.phase(f'extraction {obs.name} ({len(optext_tasks)} products)'):
        dispatch(
            opt_ext_single_source,
            optext_tasks,
            n_processes=n_processes,
            use_starmap=True,
            plot_profiles=plot_profiles,
            plot_optext=plot_optext,
            version=version,
            profile_fit_rows=str(stage_config.get('profile_fit_rows', 'aperture')).lower(),
            profile_fit_model_rows_frac=float(stage_config.get('profile_fit_model_rows_frac', 0.02)),
            profile_fit_contam=contam_1d_settings(stage_config),
            profile_fit=profile_fit,
        )

    # Phase 2: 1D combination (combine_method='1d' only)
    if phase2_tasks:
        sigma_clip = stage_config.get('sigma_clip', True)
        sigma_clip_low = stage_config.get('sigma_clip_low', 3.0)
        sigma_clip_high = stage_config.get('sigma_clip_high', 3.0)
        sigma_clip_maxiters = stage_config.get('sigma_clip_maxiters', 5)

        combine_kwargs = dict(
            sigma_clip=sigma_clip,
            sigma_clip_low=sigma_clip_low,
            sigma_clip_high=sigma_clip_high,
            sigma_clip_maxiters=sigma_clip_maxiters,
            plot_profiles=plot_profiles,
            plot_optext=plot_optext,
            version=version,
            profile_fit_contam=contam_1d_settings(stage_config),
        )

        log(f"Phase 2: 1D-combining {len(phase2_tasks)} source/grating groups")
        dispatch(
            combine_per_eg_spectra,
            phase2_tasks,
            n_processes=n_processes,
            use_starmap=True,
            **combine_kwargs,
        )

        # Cleanup per-exp_group intermediates
        for per_eg_spec_files, _, workspace_dir, final_product_name, _, _ in phase2_tasks:
            final_path = os.path.join(workspace_dir, final_product_name + "_spec.fits")
            if not os.path.exists(final_path):
                continue
            for eg_spec in per_eg_spec_files:
                eg_base = eg_spec.replace('_spec.fits', '')
                for suffix in ['_spec.fits', '_s2d.fits', '_x1d.fits', '_prof.pdf', '_spec.pdf', '_pf.fits']:
                    path = os.path.join(workspace_dir, eg_base + suffix)
                    if os.path.exists(path):
                        os.remove(path)
            log(f"Cleaned up per-exp_group intermediates for {final_product_name}")


def run_stage3_single_source(
        cal_files: List,
        workspace_dir: str,
        product_name: str,
        source_id: int,
        cleanup_asn: bool = True,
        cleanup_crfs: bool = True,
    ):
    from jwst.pipeline import Spec3Pipeline


    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(workspace_dir)

    try:
        association= [(file, 'science') for file in cal_files]

        asn = associations.asn_from_list.asn_from_list(
            association,
            with_exptype=True,
            product_name=product_name
        )
        suggested_name, serialization = asn.dump()

        asn_file = f'{product_name}_spec3.json'
        with open(asn_file, 'w') as asn_file_out:
            asn_file_out.write(serialization)


        Spec3Pipeline.call(asn_file,
            save_results=True,
            # steps={'extract_1d':{'override_extract1d':('jwst_nirspec_extract1d_4px.json')}}
        )

        # Normalize Spec3's per-source output name to {product_name}_{ext}.fits.
        # The naming differs by mode: MSA emits {product_name}_s{source_id:09d}_{ext}.fits,
        # whereas fixed slit inserts the slit name
        # ({product_name}_{slit}_s{source_id:09d}_{ext}.fits). Prefer the exact MSA
        # name, then fall back to the slit-prefixed fixed-slit name.
        for ext in ('s2d', 'x1d'):
            target = f'{product_name}_{ext}.fits'
            msa_name = f'{product_name}_s{source_id:09d}_{ext}.fits'
            if os.path.exists(msa_name):
                os.rename(msa_name, target)
                continue
            fs_matches = sorted(glob.glob(f'{product_name}_*_s{source_id:09d}_{ext}.fits'))
            if fs_matches:
                os.rename(fs_matches[0], target)


        # Create "exposures" table
        hdrs0 = [fits.getheader(cal_file,ext=0) for cal_file in cal_files]
        exposures = Table()
        exposures['filename'] = cal_files
        exposures['dither_type'] = [hdr['PATTTYPE'] for hdr in hdrs0]
        exposures['nod_type'] = [read_nod_type(hdr, fn) for hdr, fn in zip(hdrs0, cal_files)]
        # NB: this is PRIDTPTS, the *count* of primary dither points, not a
        # per-exposure nod index — the column name predates that observation
        # and is left alone here (issue #415 is about the missing keyword).
        exposures['nod_number'] = [read_primary_dither_points(hdr, fn)
                                   for hdr, fn in zip(hdrs0, cal_files)]
        exposures['dither_number'] = [hdr['PATT_NUM'] for hdr in hdrs0]
        exposures['exptime'] = [hdr['EFFEXPTM'] for hdr in hdrs0]
        exposures['stuck_shutter_list'] = [hdr['STKSHTRS'] if 'STKSHTRS' in hdr else 'N/A' for hdr in hdrs0]
        # Background nods stage 2b subtracted, and whether an override chose them
        # ('N/A' for canonicals written before stage 2b recorded them).
        exposures['bkg_nods'] = [str(hdr.get('BKGNODS', 'N/A')) for hdr in hdrs0]
        exposures['bkg_override'] = [str(hdr['BKGOVR']) if 'BKGOVR' in hdr else 'N/A' for hdr in hdrs0]
        # whether the nods came from the automatic (neighbour-scan) override file
        exposures['bkg_auto'] = [str(hdr['BKGAUTO']) if 'BKGAUTO' in hdr else 'N/A' for hdr in hdrs0]
        hdrs1 = [fits.getheader(cal_file,ext=1) for cal_file in cal_files]
        # Fixed-slit cal products omit the MSA-only SHUTSTA shutter state and the
        # slit-level SRCRA/SRCDEC (the latter come from the MSA source catalog);
        # default the shutter state and fall back to the target position, which is
        # the source position for a fixed-slit exposure.
        exposures['shutter_state'] = [hdr.get('SHUTSTA', 'N/A') for hdr in hdrs1]
        exposures['source_ra'] = [h1.get('SRCRA', h0.get('TARG_RA', np.nan))
                                  for h0, h1 in zip(hdrs0, hdrs1)]
        exposures['source_dec'] = [h1.get('SRCDEC', h0.get('TARG_DEC', np.nan))
                                   for h0, h1 in zip(hdrs0, hdrs1)]
        exposures['source_xpos'] = [hdr.get('SRCXPOS', np.nan) for hdr in hdrs1]
        exposures['source_ypos'] = [hdr.get('SRCYPOS', np.nan) for hdr in hdrs1]
        exposures['v3pa'] = [hdr.get('PA_V3', np.nan) for hdr in hdrs1]
        # Fixed-slit provenance (set by stage2a): whether the source was observed
        # through a NIRSpec fixed slit and which one. Carried here so the shutters
        # ECSV (which reads this HDU) can emit the correct aperture geometry, and
        # so it lands in the final spec.fits. Applies to standalone NRS_FIXEDSLIT
        # and to fixed-slit sources inside MSA exposures.
        exposures['fixed_slit'] = [bool(hdr.get('CFFXSLT', False)) for hdr in hdrs0]
        exposures['slit_name'] = [str(hdr.get('CFFSSLIT', '')) for hdr in hdrs0]
           


        s2d_file = f'{product_name}_s2d.fits'
        with fits.open(s2d_file, mode='update') as hdul:
            exposures = table_to_hdu(exposures)
            exposures.name = 'EXPOSURES'
            hdul.append(exposures)

    except Exception as e:
        log("ERROR", e)
        raise e

    finally:

        if cleanup_asn:
            # Cleanup any remaining ASN files
            asn_file = f'{product_name}_spec3.json'
            if os.path.exists(asn_file):
                os.remove(asn_file)

        if cleanup_crfs:
            _cleanup_spec3_intermediates(cal_files, product_name)

        os.chdir(prev_cwd)


def _cleanup_spec3_intermediates(cal_files, product_name):
    """Remove Spec3's per-exposure and per-source intermediates (run in the workspace)."""
    for cal_file in cal_files:
        crf_file = cal_file.replace('.fits', '_a3001_crf.fits')  # e.g. jw07076019001_03101_00003_nrs1_10059_a3001_crf
        if os.path.exists(crf_file):
            os.remove(crf_file)

    # Spec3Pipeline also drops per-source intermediates that carry the
    # product_name prefix: {product_name}_*_crf.fits and the calibrated
    # {product_name}_s{source_id:09d}_cal.fits. Clean both. NOTE: NOT
    # dead code — these are product_name-prefixed, distinct from the bare
    # canonical jw..._nrs[12]_<src>.fits inputs, so it never deletes a
    # canonical. (Removing the _cal glob left 4 stray Spec3 _cal.fits per
    # obs — caught by the #225 end-to-end cfpipe golden run.)
    stray = (glob.glob(f'{product_name}_*_crf.fits')
             + glob.glob(f'{product_name}_*_cal.fits'))
    for file in stray:
        os.remove(file)


def _profile_fit_task(obs, files_all, target_files, product_name, source_id):
    """Arguments for one product's profile fit: every exposure of the background
    groups its science exposures came from (partners included), each group's nod
    overrides (hand and automatic; the canonicals' BKGNODS cards win when present),
    the names stage 3 combines, and the disperser."""
    groups, overrides = [], []
    eff = obs.effective_bkg_overrides()[0]
    for bg in np.unique(target_files['bkg_group']):
        g = files_all[files_all['bkg_group'] == bg]
        groups.append(list(g['path']))
        ov = eff.get(g['root'][0])
        overrides.append(ov.get(str(source_id)) if ov is not None else None)
    return dict(product_name=product_name, workspace_dir=obs.workspace_dir, groups=groups, overrides=overrides,
                science=list(target_files['name']), source_id=int(source_id),
                filter_grating=str(target_files['filter_grating'][0]))


def _science_files(files):
    """Canonicals stage 3 combines: background-subtracted and containing the source."""
    def _ok(path):
        h = fits.getheader(path)
        v = h.get('CFP_BKG')
        return (bool(v) and not str(v).startswith(('skipped', 'excluded'))
                and str(h.get('SRCFLUX', 'T')) == 'T')
    return files[[_ok(f['path']) for f in files]]


def _prism_fg(fgs):
    return next((fg for fg in sorted(fgs) if str(fg).lower().startswith('prism')), None)


def run_profile_fit_phase(obs, stage_config, pf_tasks, n_processes=1, overwrite=False, cleanup_crfs=True):
    """Profile-fit extraction profiles of the products in ``pf_tasks`` (extraction v2).

    Dispersers run PRISM first, so a grating can take the PRISM shape of the same
    source. With ``contam_predict``: trace maps for every exposure of the
    observation (cached), then per disperser the pass-1 spectra of every source of
    the configuration (also the ones not being re-run: their light reaches the
    re-run ones), then the products. The neighbour-scan proposals are written to
    the observation's reference directory (``scan_action``)."""
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.observation import Observation
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec.profile_product import run_product, stage_flags

    from campfire_pipeline.nirspec import profile_joint as PJ
    from campfire_pipeline.nirspec import profile_timing as TM

    fl = stage_flags(stage_config)
    js = PJ.joint_settings(stage_config)
    shape = str(stage_config.get('profile_fit_shape', 'gated')).lower()
    contam = fl['contam'] and shape != 'free'
    by_fg = {}
    for t in pf_tasks:
        by_fg.setdefault(t['filter_grating'], []).append(t)
    all_fg = set(by_fg)
    sci_all = None
    if contam:
        every = Observation.group_files(obs.discover_files(ext='canonical', source_ids='all'))
        sci_all = _science_files(every)
        all_fg |= set(np.unique(every['filter_grating']))
        # stuck-closed shutters pass no light: left out of the maps
        mt = CT.map_tasks(obs.workspace_dir, list(every['path']), log=log,
                          stuck=CT.stuck_by_root(obs.stuck_closed_shutters))
        if mt:
            log(f"Profile fit: building {len(mt)} trace maps")
            with TM.phase(f'trace_maps ({len(mt)})'):
                dispatch(CT.build_map, mt, n_processes=n_processes, use_starmap=True)
    prism_fg = _prism_fg(all_fg)
    order = sorted(by_fg, key=lambda fg: (0 if fg == prism_fg else 1, fg))
    run_sids = {t['source_id'] for t in pf_tasks}
    results = []
    for fg in order:
        is_prism = fg == prism_fg
        pass1_products = {}
        all_tasks = {}       # every source of the disperser, as run_product arguments (joint-solve groups)
        if contam:
            sub = sci_all[sci_all['filter_grating'] == fg]
            every_fg = every[every['filter_grating'] == fg]
            p1, jtasks = [], {}
            for sid in np.unique(sub['source_id']):
                tf = sub[sub['source_id'] == sid]
                t = _profile_fit_task(obs, every_fg, tf, f"{obs.name}_{fg}_{sid}", sid)
                prism_product = f"{obs.name}_{prism_fg}_{sid}" if (prism_fg and not is_prism) else ''
                pass1_products[int(sid)] = t['product_name']
                p1.append((t['product_name'], obs.workspace_dir, t['groups'], t['overrides'], t['science'],
                           int(sid), is_prism, stage_config, prism_product, bool(overwrite and int(sid) in run_sids)))
                jtasks[int(sid)] = dict(product=t['product_name'], groups=t['groups'], overrides=t['overrides'],
                                        science=t['science'], prism_product=prism_product)
                all_tasks[int(sid)] = (t['product_name'], obs.workspace_dir, t['groups'], t['overrides'],
                                       t['science'], int(sid), stage_config, is_prism, prism_product,
                                       pass1_products, False)
            if CT.pass1_settings(stage_config)['mode'] == 'joint':
                # every source solved with its neighbours on its nod differences
                # (joint_first_pass); replaces the masked pass and its second round
                from campfire_pipeline.nirspec import joint_first_pass as JF
                log(f"Profile fit: joint pass 1 for {len(jtasks)} {fg} sources")
                with TM.phase(f'pass1 joint {fg} ({len(jtasks)})'):
                    JF.run(jtasks, obs.workspace_dir, stage_config, is_prism, n_processes=n_processes,
                           overwrite={s for s in jtasks if s in run_sids} if overwrite else False,
                           cache_size=int(stage_config.get('contam_joint_cache', 60)))
            else:
                log(f"Profile fit: pass 1 for {len(p1)} {fg} sources")
                with TM.phase(f'pass1 {fg} ({len(p1)})'):
                    dispatch(CT.run_pass1, p1, n_processes=n_processes, use_starmap=True)
                # second round for the sources whose trace others cover (see profile_contam.run_pass1)
                with TM.phase(f'pass1b {fg} ({len(p1)})'):
                    dispatch(CT.run_pass1, p1, n_processes=n_processes, use_starmap=True,
                             pass1_products=pass1_products, second=True)
        joint = contam and js['enabled']
        tasks = []
        for t in by_fg[fg]:
            prism_product = f"{obs.name}_{prism_fg}_{t['source_id']}" if (prism_fg and not is_prism) else ''
            tasks.append((t['product_name'], t['workspace_dir'], t['groups'], t['overrides'], t['science'],
                          t['source_id'], stage_config, is_prism, prism_product, pass1_products,
                          cleanup_crfs and not joint))      # the joint solve redoes products: keep their crfs
        log(f"Profile fit: {len(tasks)} {fg} products")
        with TM.phase(f'products {fg} ({len(tasks)})'):
            res = [r for r in dispatch(run_product, tasks, n_processes=n_processes, use_starmap=True) or [] if r]
        if joint:
            with TM.phase(f'joint {fg}'):
                res = _joint_phase(obs, fg, tasks, res, stage_config, pass1_products, js, n_processes,
                                   cleanup_crfs, all_tasks=all_tasks)
        results += res
    log_fallbacks(results)
    if fl['scan']:
        write_scan_proposals(obs, results, run_sids, str(stage_config.get('scan_action', 'report_only')).lower(),
                             xdisp_tol=fl['scan_xdisp_tol'])
    return results


def log_fallbacks(results):
    """One line: how many products of the profile-fit phase fell back to the 3-pixel
    boxcar, by reason."""
    from collections import Counter
    n = Counter(r['fallback'] for r in results if r.get('fallback'))
    what = ', '.join(f'{k} {v}' for k, v in sorted(n.items())) if n else 'none'
    log(f"Profile fit: {len(results)} products, fallbacks to the 3-pixel boxcar: {what}")
    return n


def _joint_phase(obs, fg, tasks, results, stage_config, pass1_products, js, n_processes, cleanup_crfs,
                 all_tasks=None, metrics=None):
    """Joint solve of the strongly coupled overlap groups of one disperser
    (``contam_joint``, ``profile_joint``), after its one-pass products. Returns the
    results with the redone members' one-pass summaries replaced.

    Groups are formed from the coupling of every source of the disperser
    (``all_tasks``: sid -> ``run_product`` arguments), not only the products of this
    run, so a ``--source-ids`` run finds the same groups as a full run. The pair
    metrics of the products of this run come from their results; those of every other
    source are computed in memory (``profile_product.one_pass_metrics``; nothing of
    theirs is written), together with the one-pass shape they enter a joint solve
    with. Only the groups with a source of this run are solved, and only this run's
    receivers are redone: another source's product is never rewritten.

    metrics : replaces ``one_pass_metrics`` (tests)."""
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec import profile_joint as PJ
    from campfire_pipeline.nirspec.profile_product import one_pass_metrics

    pairs = {int(r['source_id']): (r.get('pairs') or {}) for r in results}
    by_sid = {int(t[5]): t for t in tasks}
    requested = set(by_sid)
    members = {**(all_tasks or {}), **by_sid}
    others = [t for sid, t in sorted(members.items()) if sid not in requested]
    shapes = {}
    if others:
        log(f"Profile fit: pair metrics of {len(others)} {fg} sources not in this run (in memory)")
        for m in dispatch(metrics or one_pass_metrics, others, n_processes=n_processes, use_starmap=True) or []:
            pairs[int(m['source_id'])] = m['pairs'] or {}
            shapes[int(m['source_id'])] = m['shape']
    groups = PJ.select_groups(pairs, set(members), min_sigma=js['min_sigma'], min_frac=js['min_frac'],
                              max_members=js['max_members'])
    # a group whose receivers are all outside this run would change no product of it
    redo = [PJ.receivers(pairs, g, min_sigma=js['min_sigma'], min_frac=js['min_frac']) & requested
            for g in groups]
    groups, redo = [g for g, r in zip(groups, redo) if r], [r for r in redo if r]
    log(f"Profile fit: {len(groups)} {fg} joint-solve groups "
        f"({sum(len(g) for g in groups)} sources, sizes {sorted(len(g) for g in groups)}; "
        f"{sum(len(r) for r in redo)} products redone)")
    if groups:
        gtasks = [([members[s] for s in g], obs.workspace_dir, stage_config, pass1_products, r, None,
                   {s: shapes[s] for s in g if s in shapes})
                  for g, r in zip(groups, redo)]
        redone = {}
        for out in dispatch(PJ.run_group, gtasks, n_processes=n_processes, use_starmap=True) or []:
            for r in out or []:
                if r:
                    redone[int(r['source_id'])] = r
        results = [redone.get(int(r['source_id']), r) for r in results]
    if cleanup_crfs:
        prev = os.getcwd()
        os.chdir(obs.workspace_dir)
        try:
            for t in tasks:
                _cleanup_spec3_intermediates(list(t[4]), t[0])
        finally:
            os.chdir(prev)
    return results


def _prism_xdisp(obs, r, results, tol):
    """A PRISM product's neighbour against the grating scans of the same source (this
    run's results, else their ``_pf.fits`` headers): 'single' when no grating product
    exists, 'none' when the PRISM scan found nothing, 'agree' when a grating scan finds
    the neighbour within ``tol`` of the same Delta, else 'disagree'."""
    from campfire_pipeline.nirspec.profile_product import read_scan
    sid = r['source_id']
    others = [(g['scan'] or {}) for g in results if g['source_id'] == sid and not g.get('is_prism')]
    found = [(bool(s.get('detected')), float(s.get('delta', np.nan))) for s in others if s]
    if not others:
        for p in glob.glob(os.path.join(obs.workspace_dir, f'{obs.name}_*_{sid}_pf.fits')):
            if os.path.basename(p) == f"{r['product']}_pf.fits":
                continue
            s = read_scan(obs.workspace_dir, os.path.basename(p)[:-len('_pf.fits')])
            if s is not None:
                found.append(s)
    if not found:
        return 'single'
    if not (r.get('scan') or {}).get('detected'):
        return 'none'
    d = r['scan']['delta']
    return 'agree' if any(det and np.isfinite(x) and abs(x - d) <= tol for det, x in found) else 'disagree'


def _prism_scan_agrees(obs, r, results, tol):
    """True when no grating product exists, or one finds the neighbour within ``tol``
    of the same Delta (``_prism_xdisp``)."""
    return _prism_xdisp(obs, r, results, tol) in ('single', 'agree')


def _update_prism_scan_cards(obs, r, xdisp):
    """Write the cross-disperser result into a PRISM product's ``_pf.fits``: the
    grating ran after it. An agreeing grating scan makes the detection a candidate
    (``PFNBR``)."""
    path = os.path.join(obs.workspace_dir, f"{r['product']}_pf.fits")
    if not os.path.exists(path):
        return
    with fits.open(path, mode='update') as h:
        hd = h[0].header
        if 'PFNBRX' not in hd:
            return
        hd['PFNBRX'] = xdisp
        if xdisp == 'agree':
            hd['PFNBR'] = 1
            r['scan']['candidate'] = True
        elif xdisp == 'disagree' and hd.get('PFNBRDRP'):
            hd['PFNBRDRP'] = ''
            hd['PFNBRSUP'] = 'grating scan disagrees'


def write_scan_proposals(obs, results, run_sids, action='report_only', xdisp_tol=0.2):
    """Neighbour-scan proposals to the observation's reference directory.

    ``auto_override``: ``nodded_background_overrides_auto.toml``, which stage 2b
    applies on its next run (a hand override of the same nod wins);
    ``report_only``: ``nodded_background_overrides_proposed.toml`` (never applied).
    Entries of the sources processed in this run are replaced; others are kept.
    Where a source has both PRISM and grating products, proposals are written only
    when both scans find the neighbour at the same Delta (``xdisp_tol``; the grating
    product checks this itself, ``ScanResult.cross_check``; PRISM proposals are
    checked here, when the grating scans are known)."""
    import toml
    from datetime import date

    if action not in ('report_only', 'auto_override'):
        raise ValueError(f"scan_action must be 'report_only' or 'auto_override', got {action!r}")
    for r in results:
        s = r.get('scan') or {}
        if not (r.get('is_prism') and s):
            continue
        xd = _prism_xdisp(obs, r, results, xdisp_tol) if xdisp_tol >= 0 else 'single'
        if xd != 'single':
            _update_prism_scan_cards(obs, r, xd)
        if s.get('detected') and s.get('proposals') and xd == 'disagree':
            log(f"{r['product']}: neighbour proposals withheld (grating scan disagrees)")
            s['proposals'] = {}
    path = obs.auto_bkg_override_file if action == 'auto_override' else obs.proposed_bkg_override_file
    old = toml.load(path) if os.path.exists(path) else {}
    entries = {}
    for root, d in old.items():
        for sid, nods in d.items():
            if int(sid) not in run_sids:
                entries.setdefault(root, {})[str(sid)] = ({str(k): list(v) for k, v in nods.items()}, '')
    for r in results:
        s = r.get('scan') or {}
        if not s.get('detected'):
            continue
        note = f"Delta={s['delta']:+.2f} dchi2={s['dchi2']:.0f} {date.today().isoformat()}"
        if s.get('unresolved'):
            note += f" unresolved={','.join(s['unresolved'])}"
        for root, nods in s['proposals'].items():
            entries.setdefault(root, {})[str(r['source_id'])] = (nods, note)
    head = ('# automatic nodded-background overrides from the same-slitlet neighbour scan (cfpipe stage 3);\n'
            '# stage 2b applies them on its next run; a hand override of the same nod wins\n'
            if action == 'auto_override' else
            '# proposed nodded-background overrides from the same-slitlet neighbour scan (cfpipe stage 3);\n'
            '# report only: never applied. Copy an entry to nodded_background_overrides.toml to use it\n')
    lines = [head.rstrip('\n')]
    for root in sorted(entries):
        lines.append(f'\n[{root}]')
        for sid in sorted(entries[root], key=int):
            nods, note = entries[root][sid]
            body = ', '.join(f'{k} = [{", ".join(str(int(x)) for x in v)}]' for k, v in sorted(nods.items()))
            lines.append(f'    {sid} = {{{body}}}' + (f'   # {note}' if note else ''))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    log(f"Neighbour scan: {sum(len(v) for v in entries.values())} entries in {path}")


def run_profile_fit_single_source(product_name, workspace_dir, groups, overrides, science,
                                  source_id, chromatic=True, cleanup_crfs=True):
    """Original (v1) profile fit for one source after Spec3 (needs Spec3's crf):
    free shape, original priors. Kept for reference; stage 3 uses
    ``run_profile_fit_phase``. A failure is logged and leaves the standard products
    untouched."""
    from campfire_pipeline.nirspec import profile_fit as PF

    prev_cwd = os.getcwd()
    os.chdir(workspace_dir)
    try:
        crf = f'{product_name}_s{source_id:09d}_crf.fits'
        if not os.path.exists(crf):
            log(f"{product_name}: no Spec3 crf ({crf}); skipping profile fit")
            return
        PF.run_single_source(product_name, workspace_dir, groups, overrides, set(science), crf, chromatic=chromatic)
    except Exception as e:
        log(f"{product_name}: profile fit failed ({type(e).__name__}: {e}); standard extraction only")
    finally:
        if cleanup_crfs:
            _cleanup_spec3_intermediates(list(science), product_name)
        os.chdir(prev_cwd)


def boxcar3(s2d_sci, s2d_err, s2d_mask, cen):
    """The 3-pixel boxcar of an s2d (``fnu_3px``): rows cen +- 1.5, summed, no weights.
    Returns (profile, fnu, fnu_err) in the s2d's units."""
    profile = boxcar_profile(cen - 1.5, cen + 1.5, s2d_sci.shape[0])
    fnu, err = extract_with_profile(profile, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)
    return profile, fnu, err


def spec3_boxcar3(workspace_dir, product_name):
    """(wave [um], fnu_3px, fnu_3px_err [uJy], source) of a product, as stage 3 extracts it:
    from Spec3's ``_s2d.fits`` and ``_x1d.fits`` (the aperture centre from the x1d's
    extraction rows, as ``opt_ext_single_source``), else the ``fnu_3px`` column of an
    existing ``_spec.fits``. None when neither exists."""
    s2d_file = os.path.join(workspace_dir, f'{product_name}_s2d.fits')
    x1d_file = os.path.join(workspace_dir, f'{product_name}_x1d.fits')
    spec_file = os.path.join(workspace_dir, f'{product_name}_spec.fits')
    if os.path.exists(s2d_file) and os.path.exists(x1d_file):
        with fits.open(s2d_file) as s2d, fits.open(x1d_file) as x1d:
            sci = np.asarray(s2d['SCI'].data, float) * 1e12       # MJy -> uJy
            err = np.asarray(s2d['ERR'].data, float) * 1e12
            mask = s2d['WHT'].data == 0
            h = x1d['EXTRACT1D'].header
            cen = ((h['EXTRYSTR'] - 1) + h['EXTRYSTP']) / 2
            wave = np.asarray(x1d['EXTRACT1D'].data['WAVELENGTH'], float)
        _, fnu, fnu_err = boxcar3(sci, err, mask, cen)
        return wave, fnu, fnu_err, 's2d'
    if os.path.exists(spec_file):
        with fits.open(spec_file) as h:
            d = h['SPEC1D'].data
            if 'fnu_3px' in d.columns.names:
                return (np.asarray(d['wave'], float), np.asarray(d['fnu_3px'], float),
                        np.asarray(d['fnu_3px_err'], float), 'spec')
    return None


def opt_ext_single_source(
      product_name,
      workspace_dir,
      optimal_extraction_profile = 'single', # or 'wavelength-dependent'
      plot_profiles = False,
      plot_optext = False,
      overwrite = False,
      version = 'v0.1',
      profile_fit_rows = 'aperture',
      profile_fit_model_rows_frac = 0.02,
      profile_fit_contam = None,
      profile_fit = False,
    ):
    """
    Runs an optimal extraction routine and saves an msaexp-style "_spec.fits" file (combining 2D and 1D) for a single object.

    ``fnu`` is the best spectrum (header ``EXTRACT``): with ``profile_fit`` the profile
    extraction, with the other sources' predicted light subtracted when it is
    significant, or the 3-pixel boxcar on a named fallback (``_profile_fit_columns``);
    without it, the optimal extraction. ``fnu_opt`` is always the optimal extraction.
    """
    log(f"Running optimal extraction and compiling *_spec.fits file for {product_name}")

    s2d_file = os.path.join(workspace_dir, f'{product_name}_s2d.fits')
    x1d_file = os.path.join(workspace_dir, f'{product_name}_x1d.fits')
    s2d = fits.open(s2d_file)
    x1d = fits.open(x1d_file)
    s2d_sci = s2d['SCI'].data * 1e12 # convert from MJy to uJy
    s2d_err = s2d['ERR'].data * 1e12 # convert from MJy to uJy
    s2d_mask = s2d['WHT'].data==0

    out_filename = s2d_file.replace('_s2d.fits','_spec.fits')

    ph = fits.Header()
    ph['EXTEND'] = 'T'
    ph['FILENAME'] = (os.path.basename(out_filename), 'Name of the file')
    for keyword in ['ORIGIN','TIMESYS','TIMEUNIT','TELESCOP','PROGRAM','PI_NAME','CATEGORY','DATE-OBS',
                    'TIME-OBS','VISIT_ID','OBSERVTN','TARGPROP','TARGNAME','FILTER','GRATING','MSAMETFL',
                    'MSAMETID','MSACONID','EXP_TYPE','READPATT','EFFEXPTM','CAL_VER','CAL_VCS','CRDS_VER',
                    'CRDS_CTX','R_AREA','R_CAMERA','R_COLLIM','R_DARK','R_DISPER','R_DISTOR','R_EXTR1D',
                    'R_FILOFF','R_FLAT','R_DFLAT','R_FFLAT','R_SFLAT','R_FORE','R_FPA','R_GAIN','R_IFUFOR',
                    'R_IFUPOS','R_IFUSLI','R_LINEAR','R_MASK','R_MSA','R_OTE','R_PTHLOS','R_PHOTOM','R_READNO',
                    'R_REFPIX','R_REGION','R_SATURA','R_SPCWCS','R_SUPERB','R_WAVCOR','R_WAVRAN','S_WCS',
                    'S_BKDSUB','S_BPXSLF','S_BARSHA','S_CHGMIG','S_CLNFNS','S_DARK','S_DQINIT','S_EXTR1D',
                    'S_EXTR2D','S_FLAT','S_GANSCL','S_GRPSCL','S_IMPRNT','S_IPC','S_JUMP','S_LINEAR',
                    'S_MSBSUB','S_MSAFLG','S_NSCLEN','S_OUTLIR','S_PTHLOS','S_PHOTOM','S_PXREPL','S_RAMP',
                    'S_REFPIX','S_RESAMP','S_SATURA','S_SRCTYP','S_SUPERB','S_WAVCOR','NDRIZ','RESWHT',
                    'PIXFRAC','PXSCLRT']:
        try:
            ph[keyword] = (x1d['PRIMARY'].header[keyword], x1d['PRIMARY'].header.comments[keyword])
        except KeyError:
            log(f'Keyword {keyword} not found')
            continue

    ph['CMPFRTIM'] = (datetime.now(timezone.utc).isoformat(), 'UTC date/time of CAMPFIRE reduction (ISO 8601)')
    ph['CMPFRVER'] = (version, 'campfire-pipeline version (PEP 440)')

    # Carry fixed-slit provenance from the EXPOSURES table into the primary header
    # (the curated keyword copy above reads from x1d, which no longer carries the
    # campfire CFxxx cards after Spec3).
    try:
        _exp = s2d['EXPOSURES'].data
        if 'fixed_slit' in _exp.columns.names:
            ph['CFFXSLT'] = (bool(np.any(_exp['fixed_slit'])), 'NIRSpec fixed-slit source')
            if 'slit_name' in _exp.columns.names and str(_exp['slit_name'][0]):
                ph['CFFSSLIT'] = (str(_exp['slit_name'][0]), 'NIRSpec fixed-slit aperture')
    except (KeyError, IndexError):
        pass

    # Fixed-slit exposures have no MSA bars, so the optimal-extraction profile
    # need not be bounded to the nominal aperture (the bounding exists to avoid
    # picking up neighbouring shutters across bars in MSA data).
    is_fixed_slit = bool(ph.get('CFFXSLT', False))

    primary = fits.PrimaryHDU(header=ph)

    # Optimal extraction
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        collapsed = np.nanmedian(s2d_sci, axis=1)
    x1d_start = x1d['EXTRACT1D'].header['EXTRYSTR']-1
    x1d_stop = x1d['EXTRACT1D'].header['EXTRYSTP']
    cen = (x1d_start+x1d_stop)/2
    profile_opt = optext_profile(collapsed, x1d_start, x1d_stop, bounded=not is_fixed_slit)
    corrupted, n_pos, pos_frac = optext_profile_is_corrupted(collapsed, x1d_start, x1d_stop)
    if corrupted or all(np.isnan(profile_opt)):
        log(f"Optimal-extraction profile is corrupted for {product_name} "
            f"(n_positive={n_pos}, positive_fraction={pos_frac:.2f}); "
            f"falling back to 3px boxcar")
        profile_opt = boxcar_profile(cen-1.5, cen+1.5, len(collapsed))
        optext_status = 'boxcar-3px'
        opt_label = 'Optimal (3px boxcar fallback)'
    else:
        optext_status = 'optimal'
        opt_label = 'Optimal'
    primary.header['CMPFROPT'] = (optext_status, "Optimal extraction status ('optimal' or fallback)")

    fnu_opt, fnu_opt_err = extract_with_profile(profile_opt, s2d_sci, s2d_err, mask=s2d_mask, ivw=True)

    # 3 pixel boxcar, centered on the expected position of the source
    profile_3px, fnu_3px, fnu_3px_err = boxcar3(s2d_sci, s2d_err, s2d_mask, cen)

    # 4 pixel boxcar, centered on the expected position of the source
    profile_4px = boxcar_profile(cen-2.0, cen+2.0, len(collapsed))
    fnu_4px, fnu_4px_err = extract_with_profile(profile_4px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    # 5 pixel boxcar, centered on the expected position of the source
    profile_5px = boxcar_profile(cen-2.5, cen+2.5, len(collapsed))
    fnu_5px, fnu_5px_err = extract_with_profile(profile_5px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    if plot_profiles:
        from campfire_pipeline.nirspec.plots import plot_extraction_profiles
        profiles = {opt_label: profile_opt, '3px boxcar': profile_3px,
                    '4px boxcar': profile_4px, '5px boxcar': profile_5px}
        plot_extraction_profiles(
            out_filename.replace('_spec.fits', '_prof.pdf'),
            collapsed, profiles, x1d_start, x1d_stop, cen,
        )


    wave = x1d['EXTRACT1D'].data['WAVELENGTH']

    # Profile-fit extraction ([nirspec.stage3].profile_fit): fnu_prof, fnu_contam and the
    # choice of the best spectrum fnu. Without it, fnu is the optimal extraction.
    pf, pf_hdus = None, []
    pf_file = os.path.join(workspace_dir, f'{product_name}_pf.fits')
    if profile_fit:
        if not os.path.exists(pf_file):
            raise FileNotFoundError(f'{product_name}: profile_fit is on but {pf_file} was not written')
        pf = _profile_fit_columns(pf_file, s2d_sci, s2d_err, s2d_mask, x1d_start, x1d_stop, wave,
                                  rows=profile_fit_rows, model_rows_frac=profile_fit_model_rows_frac,
                                  **(profile_fit_contam or {}))
        cols = pf['columns']
        fnu_best, fnu_best_err = best_spectrum(pf['choice'], cols['fnu_prof'][0], cols['fnu_prof_err'][0],
                                               cols['fnu_contam'][0], fnu_3px, fnu_3px_err)
        cards, pf_hdus, choice = pf['header'], pf['hdus'], pf['choice']
    else:
        fnu_best, fnu_best_err = fnu_opt, fnu_opt_err
        choice = 'optimal'
        cards = extraction_cards('optimal', 'profile_fit_off', 'off')
    for key in cards:
        primary.header[key] = (cards[key], cards.comments[key])

    spec1d = Table()
    spec1d.add_column(Column(name='wave', data=wave, description='Wavelength', unit='um'))
    _add_flux_columns(spec1d, '', fnu_best, fnu_best_err, wave, f'Best spectrum ({choice}, EXTRACT)')
    _add_flux_columns(spec1d, '_opt', fnu_opt, fnu_opt_err, wave, 'Optimally extracted flux')
    for size, fnu, fnu_err in zip(['3px','4px','5px'],[fnu_3px,fnu_4px,fnu_5px],[fnu_3px_err,fnu_4px_err,fnu_5px_err]):
        spec1d.add_column(Column(name=f'fnu_{size}', data=fnu, description=f'{size} boxcar extracted flux, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'fnu_{size}_err', data=fnu_err, description=f'{size} boxcar extracted flux error, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'flam_{size}', data=_fnu_to_flam(fnu, wave), description=f'{size} boxcar extracted flux, flam units', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name=f'flam_{size}_err', data=_fnu_to_flam(fnu_err, wave), description=f'{size} boxcar extracted flux error, flam units', unit='erg / (s cm2 Angstrom)'))
    if pf is not None:
        _add_pf_columns(spec1d, pf['columns'], wave)

    # indices = np.where(fnu_3px_err!=0)[0]
    # spec1d = spec1d[indices[0]:indices[-1] + 1]

    spec1d = table_to_hdu(spec1d)
    spec1d.name = 'SPEC1D'

    prof1d = Table()
    prof1d['ypos'] = np.arange(len(collapsed))+0.5
    prof1d['opt'] = profile_opt
    prof1d['3px'] = profile_3px
    prof1d['4px'] = profile_4px
    prof1d['5px'] = profile_5px
    prof1d = table_to_hdu(prof1d)
    prof1d.name = 'PROF1D'

    s2dh = fits.Header()
    for keyword in ['EXTNAME', 'SRCTYPE','XPOSURE','PHOTMJSR','PHOTUJA2','PIXAR_SR','PIXAR_A2','DISPAXIS',
                    'SPORDER','SOURCEID','STLARITY','SRCRA','SRCDEC','WAVECOR','PTHLOSS']:
        if keyword in s2d['SCI'].header:
            s2dh[keyword] = (s2d['SCI'].header[keyword], s2d['SCI'].header.comments[keyword])

    # Fixed-slit products lack catalog SRCRA/SRCDEC; populate from the EXPOSURES
    # table (target position) so the spec.fits is self-describing and the summary
    # reader doesn't fall back to (0, 0).
    if 'SRCRA' not in s2dh or 'SRCDEC' not in s2dh:
        try:
            ed = s2d['EXPOSURES'].data
            if 'source_ra' in ed.columns.names:
                sra = ed['source_ra'][ed['source_ra'] != 0]
                sdec = ed['source_dec'][ed['source_dec'] != 0]
                if len(sra) and 'SRCRA' not in s2dh:
                    s2dh['SRCRA'] = (float(np.nanmedian(sra)), 'Source RA (target position)')
                if len(sdec) and 'SRCDEC' not in s2dh:
                    s2dh['SRCDEC'] = (float(np.nanmedian(sdec)), 'Source Dec (target position)')
        except (KeyError, IndexError):
            pass

    sci = fits.ImageHDU(data=s2d['SCI'].data, header=s2dh, name='SCI')
    err = fits.ImageHDU(data=s2d['ERR'].data, header=s2dh, name='ERR')
    wav = fits.ImageHDU(data=s2d['WAVELENGTH'].data, header=s2dh, name='WAVELENGTH')
    wht = fits.ImageHDU(data=s2d['WHT'].data, header=s2dh, name='WHT')
    # prof = Table({''})
    exp = s2d['EXPOSURES']


    spec = fits.HDUList(hdus=[primary, spec1d, sci, err, wav, wht] + pf_hdus + [prof1d, exp])
    spec.writeto(out_filename, overwrite=True)
    # every product's profile fit (joint solve, cross-disperser scan) finished before
    # extraction began; later runs read the shape cards from the _spec.fits
    if profile_fit and os.path.exists(pf_file):
        os.remove(pf_file)


    if plot_optext:
        from campfire_pipeline.nirspec.plots import plot_spectrum_qa
        wave = spec1d.data['wave']
        plot_spectrum_qa(
            out_filename.replace('_spec.fits', '_spec.pdf'),
            wave=wave,
            fnu=spec1d.data['fnu'], fnu_err=spec1d.data['fnu_err'],
            flam=spec1d.data['flam'], flam_err=spec1d.data['flam_err'],
            sci_2d=s2d['SCI'].data, err_2d=s2d['ERR'].data,
            profile_ypos=prof1d.data['ypos'], profile_opt=prof1d.data['opt'],
            cen=cen, product_name=product_name,
        )

    s2d.close()
    x1d.close()

def _fnu_to_flam(fnu, wave):
    """fnu [uJy] at wave [um] to flam [erg / (s cm2 Angstrom)]."""
    return fnu / wave**2 * 2.99792458e-19


def _add_flux_columns(table, suffix, fnu, err, wave, desc):
    """fnu{suffix}, fnu{suffix}_err, flam{suffix}, flam{suffix}_err."""
    flam_unit = 'erg / (s cm2 Angstrom)'
    table.add_column(Column(name=f'fnu{suffix}', data=fnu, description=f'{desc}, fnu units', unit='uJy'))
    table.add_column(Column(name=f'fnu{suffix}_err', data=err, description=f'{desc} error, fnu units', unit='uJy'))
    table.add_column(Column(name=f'flam{suffix}', data=_fnu_to_flam(fnu, wave), description=f'{desc}, flam units',
                            unit=flam_unit))
    table.add_column(Column(name=f'flam{suffix}_err', data=_fnu_to_flam(err, wave),
                            description=f'{desc} error, flam units', unit=flam_unit))


# SPEC1D columns of the profile fit, after the boxcars: (suffix, description)
PF_FLUX_COLUMNS = (('_prof', 'Profile-fit flux, nothing subtracted'),
                   ('_contam', 'Predicted other-source flux with the profile-fit weights'))


def _add_pf_columns(table, columns, wave):
    """fnu_prof / fnu_contam (with errors and flam twins), then the joint-solve columns."""
    for sfx, desc in PF_FLUX_COLUMNS:
        _add_flux_columns(table, sfx, columns[f'fnu{sfx}'][0], columns[f'fnu{sfx}_err'][0], wave, desc)
    for name, (data, desc) in columns.items():
        if name.startswith('joint_'):
            table.add_column(Column(name=name, data=data, description=desc))


def model_rows_extraction(P, data, err, mask, frac=0.02):
    """Weighted fit of the s2d with an unnormalised profile on every row where the
    profile is significant, negative nod copies included:
    flux = sum(P D / V) / sum(P^2 / V), error = 1 / sqrt(sum(P^2 / V)).

    P is per unit reported flux (same units as data per flux unit), so no
    normalisation enters; rows are those with |P| >= frac x the column's max |P|."""
    V = err ** 2
    with np.errstate(invalid='ignore', divide='ignore'):
        amax = np.nanmax(np.abs(np.where(np.isfinite(P), P, np.nan)), axis=0)
        sel = (np.isfinite(P) & np.isfinite(data) & np.isfinite(V) & (V > 0)
               & (np.abs(P) >= frac * amax[None, :]) & (amax[None, :] > 0))
        if mask is not None:
            sel &= ~mask
        Pw = np.where(sel, P, 0.0)
        Vs = np.where(sel, V, 1.0)
        num = np.sum(np.where(sel, Pw * data / Vs, 0.0), axis=0)
        den = np.sum(np.where(sel, Pw ** 2 / Vs, 0.0), axis=0)
        good = den > 0
        flux = np.where(good, num / np.where(good, den, 1.0), np.nan)
        flux_err = np.where(good, 1.0 / np.sqrt(np.where(good, den, 1.0)), np.nan)
    return flux, flux_err, sel


CONTAM_1D = ('report', 'subtract')


def contam_1d_settings(sc):
    """The 1-D other-light knobs of ``[nirspec.stage3]`` (``_profile_fit_columns``)."""
    return dict(contam_1d=str(sc.get('contam_1d', 'report')).lower(),
                contam_1d_min_sigma=float(sc.get('contam_1d_min_sigma', 3.0)))


def _contam_1d(C2D, E2D, Pm, weight_rows, V, flux, flux_err):
    """Other sources' light (CONTAM2D, its uncertainty CONTERR2D) extracted with the
    fnu_prof weights: per column the predicted flux ``cflux``, its systematic error
    ``cerr`` and whether any light is predicted there ``predicted``; integrated:
    ``contam`` (over fnu_prof), ``conmax`` (95th pct of |contam / fnu_prof| at S/N > 3)
    and ``csig`` (in units of the integrated noise)."""
    Cu = np.where(np.isfinite(C2D), C2D * 1e12, 0.0)          # uJy, as s2d_sci
    Eu = np.where(np.isfinite(E2D), E2D * 1e12, 0.0) if E2D is not None and E2D.shape == Cu.shape else 0.0 * Cu
    with np.errstate(invalid='ignore', divide='ignore'):
        w = np.where(weight_rows & np.isfinite(V) & (V > 0), Pm / np.where(V > 0, V, 1.0), 0.0)
        wp = w * np.where(np.isfinite(Pm), Pm, 0.0)
        den = np.sum(wp, axis=0)
        okd = den > 0
        cflux = np.where(okd, np.sum(w * Cu, axis=0) / np.where(okd, den, 1.0), np.nan)
        predicted = okd & (np.sum(np.abs(w * Cu), axis=0) > 0)
        # systematic uncertainty of the prediction, extracted linearly (fully correlated)
        cerr = np.where(okd, np.sum(np.abs(w) * Eu, axis=0) / np.where(okd, den, 1.0), np.nan)
        ok = np.isfinite(cflux) & np.isfinite(flux)
        tot = np.nansum(flux[ok])
        contam = float(np.nansum(cflux[ok]) / tot) if tot > 0 else -999.0
        sn = ok & (np.abs(flux) > 3 * flux_err)
        frac = np.abs(cflux[sn] / flux[sn])
        conmax = float(np.percentile(frac, 95)) if frac.size else -999.0
        # the prediction in units of the noise of the integrated (inverse-variance) flux
        okn = ok & np.isfinite(flux_err) & (flux_err > 0)
        ivar = 1.0 / flux_err[okn] ** 2
        csig = float(abs(np.sum(cflux[okn] * ivar)) / np.sqrt(np.sum(ivar))) if okn.any() else 0.0
    return dict(cflux=cflux, cerr=cerr, predicted=predicted, contam=contam, conmax=conmax, csig=csig)


def _joint_inflation(JT, wave):
    """The joint solve's error inflation (joint over isolated error) and most negative
    correlation, from the JOINT table, at the 1-D wavelengths (NaN outside)."""
    jl = JT['lam']
    jok = np.isfinite(jl) & np.isfinite(JT['err']) & np.isfinite(JT['err_iso']) & (JT['err_iso'] > 0)
    if jok.sum() <= 1:
        return np.full(wave.shape, np.nan), np.full(wave.shape, np.nan)
    o = np.argsort(jl[jok])
    infl = np.interp(wave, jl[jok][o], (JT['err'][jok] / JT['err_iso'][jok])[o], left=np.nan, right=np.nan)
    jc = np.isfinite(JT['corr']) & jok
    oc = np.argsort(jl[jc])
    jcorr = np.interp(wave, jl[jc][oc], JT['corr'][jc][oc], left=np.nan, right=np.nan) \
        if jc.sum() > 1 else np.full(wave.shape, np.nan)
    return infl, jcorr


def _joint_stat(infl, flux_err):
    """The statistical cost of the joint split, added to the joint result's other-light error."""
    if infl is None:
        return 0.0 * flux_err
    with np.errstate(invalid='ignore'):
        return np.where(np.isfinite(infl), flux_err * np.sqrt(np.clip(infl ** 2 - 1.0, 0.0, None)), 0.0)


def _joint_noise_ratio(joint_set, onepass_set, infl, Pm, weight_rows, V, flux, flux_err, contam_1d, min_sigma):
    """Integrated noise of the joint result over the one-pass one (same columns): each as
    the profile extraction would be with it (other light subtracted above ``min_sigma``, its systematic
    error, and for the joint one the split's statistical cost, added)."""
    def err_of(s, joint):
        c = _contam_1d(s[0], s[1], Pm, weight_rows, V, flux, flux_err)
        ce = np.where(np.isfinite(c['cerr']), c['cerr'], 0.0)
        if joint:
            ce = np.sqrt(ce ** 2 + _joint_stat(infl, flux_err) ** 2)
        sub = contam_1d == 'subtract' and c['csig'] > min_sigma
        return np.sqrt(flux_err ** 2 + ce ** 2) if sub else flux_err
    eJ, eO = err_of(joint_set, True), err_of(onepass_set, False)
    ok = np.isfinite(eJ) & np.isfinite(eO) & (eJ > 0) & (eO > 0)
    if ok.sum() < 10:
        return np.nan
    return float(np.sqrt(np.sum(1 / eO[ok] ** 2) / np.sum(1 / eJ[ok] ** 2)))


# Which extraction is the best spectrum ``fnu`` of a product (header EXTRACT), and why
# (EXTRWHY). 'optimal' only when the profile fit is not configured (profile_fit = false).
EXTRACTIONS = ('profile', 'profile_decontam', 'boxcar3', 'optimal')
EXTRACT_REASONS = {
    'contam_significant': "profile_decontam: the predicted other light exceeds contam_1d_min_sigma x the "
                          "integrated noise (PFCONSIG); subtracted",
    'contam_insignificant': 'profile: other light predicted, below that threshold; not subtracted',
    'contam_none': 'profile: no other light predicted on the extraction',
    'contam_report': 'profile: contam_1d = "report", never subtracted',
    'contam_off': 'profile: no prediction (contam_predict off, free shape, or no trace maps)',
    'no_crf': 'boxcar3: Spec3 wrote no crf for the source',
    'not_bkg_subtracted': 'boxcar3: old-layout observation, canonical files not background-subtracted',
    'no_usable_pixels': "boxcar3: no usable pixel in the target's open shutters",
    'too_few_wavelengths': 'boxcar3: too few wavelength samples for a spectrum grid',
    'group_fallback': 'boxcar3: a per-group product of the 1-D combine fell back',
    'profile_fit_off': 'optimal: profile_fit is off',
}
# CONTSTAT: what fnu_contam holds. off: no prediction; none: nothing predicted on the
# extraction; predicted: other light predicted
CONTAM_STATES = ('off', 'none', 'predicted')


def extraction_cards(choice, reason, contam_state):
    """EXTRACT / EXTRWHY / CONTSTAT as a header."""
    h = fits.Header()
    h['EXTRACT'] = (choice, 'Extraction in fnu (stage3.EXTRACTIONS)')
    h['EXTRWHY'] = (reason, 'Why that extraction (stage3.EXTRACT_REASONS)')
    h['CONTSTAT'] = (contam_state, 'fnu_contam: off/none/predicted')
    return h


def choose_extraction(have_contam, any_predicted, csig, contam_1d='report', min_sigma=3.0):
    """(EXTRACT, EXTRWHY) of a product whose profile fit ran: the profile extraction with
    the predicted other light subtracted when the prediction exceeds ``min_sigma`` times
    the noise of the integrated flux (``PFCONSIG``) and ``contam_1d`` is 'subtract',
    else the profile extraction as it is."""
    if not have_contam:
        return 'profile', 'contam_off'
    if not any_predicted:
        return 'profile', 'contam_none'
    if contam_1d != 'subtract':
        return 'profile', 'contam_report'
    if csig > min_sigma:
        return 'profile_decontam', 'contam_significant'
    return 'profile', 'contam_insignificant'


def _notsep_on(NS, wave):
    """The source's not-separable wavelengths (``NOTSEP``: (lam, notsep) of its joint pass
    1) on the extraction's ``wave``, nearest pass-1 point; False outside its range or
    without one."""
    wave = np.asarray(wave, float)
    out = np.zeros(wave.shape, bool)
    if NS is None:
        return out
    lam, ns = NS
    ok = np.isfinite(lam)
    if ok.sum() < 2:
        return out
    o = np.argsort(lam[ok])
    lw, nw = lam[ok][o], ns[ok][o]
    i = np.clip(np.searchsorted(lw, wave), 1, lw.size - 1)
    near = np.where(np.abs(wave - lw[i - 1]) < np.abs(wave - lw[i]), i - 1, i)
    inside = np.isfinite(wave) & (wave >= lw[0]) & (wave <= lw[-1])
    out[inside] = nw[near[inside]]
    return out


def best_spectrum(choice, prof, prof_err, contam, box, box_err):
    """The best spectrum ``fnu`` and its error for an extraction choice. The error is
    the statistical one only: the prediction's uncertainty is ``fnu_contam_err``."""
    if choice == 'boxcar3':
        return box, box_err
    if choice == 'profile_decontam':
        with np.errstate(invalid='ignore'):
            return prof - np.where(np.isfinite(contam), contam, 0.0), prof_err
    if choice == 'profile':
        return prof, prof_err
    raise ValueError(f'no best spectrum for extraction {choice!r}')


def _profile_fit_columns(pf_file, s2d_sci, s2d_err, s2d_mask, ap_start, ap_stop, wave, rows='aperture',
                         model_rows_frac=0.02, contam_1d='report', contam_1d_min_sigma=3.0):
    """Extraction of the s2d with the profile-fit 2D profile, and the choice of the best
    spectrum.

    PROF2D is the drizzled source model per unit reported flux (uJy, against the s2d
    SCI in MJy): it is not normalised per column, so its in-aperture sum carries
    the aperture and path-loss correction. The weighted fit of the data on the
    aperture rows, sum(P D / V) / sum(P^2 / V), is the reported flux directly:
    main-shutter light on the point-source path-loss calibration, as for ``fnu_opt``.

    ``rows``: which rows ``fnu_prof`` uses: ``aperture`` (the x1d aperture rows) or
    ``model`` (every row where PROF2D is significant, including the negative nod
    copies; ``model_rows_extraction``).

    Other sources' light (``CONTAM2D``, its uncertainty ``CONTERR2D``) is extracted with
    the same weights: ``fnu_contam`` (NaN where nothing is predicted) and
    ``fnu_contam_err`` (systematic), integrated as ``PFCONTAM`` / ``PFCONMAX`` /
    ``PFCONSIG``. The best spectrum is chosen by ``choose_extraction`` (``contam_1d``,
    ``contam_1d_min_sigma``).

    A ``_pf.fits`` without PROF2D is a named fallback (``profile_product.write_fallback``):
    the best spectrum is the 3-pixel boxcar.

    Returns dict(columns={name: (data, description)} of fnu_prof / fnu_contam and their
    errors (plus the joint-solve columns), header, hdus (PROF2D, CONTAM2D), choice,
    reason).
    """
    nan = np.full(np.shape(wave), np.nan)
    with fits.open(pf_file) as h:
        header = h['PRIMARY'].header.copy()
        if 'PROF2D' not in h:
            reason = str(header.get('EXTRWHY', 'fallback'))
            columns = {
                'fnu_prof': (nan, 'Profile-fit flux (not available: fallback)'),
                'fnu_prof_err': (nan, 'Its error'),
                'fnu_contam': (nan, 'Predicted other-source flux (not available: fallback)'),
                'fnu_contam_err': (nan, 'Its uncertainty (systematic)'),
            }
            return dict(columns=columns, header=extraction_cards('boxcar3', reason, 'off'), hdus=[],
                        choice='boxcar3', reason=reason)
        P = np.asarray(h['PROF2D'].data, float)
        C2D = np.asarray(h['CONTAM2D'].data, float) if 'CONTAM2D' in h else None
        E2D = np.asarray(h['CONTERR2D'].data, float) if 'CONTERR2D' in h else None
        JT = h['JOINT'].data if 'JOINT' in h else None
        NS = h['NOTSEP'].data if 'NOTSEP' in h else None
        if NS is not None:
            NS = (np.asarray(NS['lam'], float), np.asarray(NS['notsep'], bool))
        if JT is not None:
            JT = {n: np.asarray(JT[n], float) for n in ('lam', 'err', 'err_iso', 'corr', 'flag') if n in JT.names}
        # joint-solve receivers carry the result that was not delivered (``profile_joint.deliver``):
        # the one-pass other light (*_1P) when fnu_prof is the joint result, the joint one (*_J) else
        joint_use = bool(header['PFJUSE']) if 'PFJUSE' in header else None
        alt_sfx = None if joint_use is None else ('_1P' if joint_use else '_J')
        ALT = {}
        if alt_sfx is not None:
            for n in ('CONTAM2D', 'CONTERR2D'):
                if n + alt_sfx in h:
                    ALT[n] = np.asarray(h[n + alt_sfx].data, float)
    if rows not in ('aperture', 'model'):
        raise ValueError(f"profile_fit_rows must be 'aperture' or 'model', got {rows!r}")
    if P.shape != s2d_sci.shape:
        raise ValueError(f"{os.path.basename(pf_file)}: profile {P.shape} does not match the s2d {s2d_sci.shape}")
    if contam_1d not in CONTAM_1D:
        raise ValueError(f'contam_1d must be one of {CONTAM_1D}, got {contam_1d!r}')
    x = np.arange(P.shape[0] + 1)
    in_ap = (x[:-1] > ap_start) & (x[1:] <= ap_stop)
    Pm = np.where(np.isfinite(P), P * 1e12, np.nan)       # per uJy, against s2d_sci in uJy units
    if rows == 'model':
        flux, flux_err, weight_rows = model_rows_extraction(Pm, s2d_sci, s2d_err, s2d_mask, frac=model_rows_frac)
    else:
        Pa = np.where(in_ap[:, None] & np.isfinite(P), P * 1e12, 0.0)
        norm = Pa.sum(axis=0)
        good = norm > 1e-3 * np.nanmax(norm)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', category=RuntimeWarning)
            amp, amp_err = extract_with_profile(np.where(good, Pa / np.where(good, norm, 1.0), 0.0),
                                                s2d_sci.copy(), s2d_err.copy(), mask=s2d_mask, ivw=True)
        flux = np.where(good, amp / np.where(good, norm, 1.0), np.nan)
        flux_err = np.where(good, amp_err / np.where(good, norm, 1.0), np.nan)
        weight_rows = in_ap[:, None] & np.isfinite(P) & ~s2d_mask
    columns = {
        'fnu_prof': (flux, f'Profile-fit flux (PROF2D weights, {rows} rows), nothing subtracted'),
        'fnu_prof_err': (flux_err, 'Profile-fit flux error (statistical)'),
    }
    keep = fits.Header()
    for key in header:
        if key.startswith('PF'):
            keep[key] = (header[key], header.comments[key])
    keep['PFROWS'] = (rows, 'Rows of fnu_prof: aperture / model')
    if (joint_use is not None and str(header.get('PFJGATE', '')).lower() == 'noise' and C2D is not None
            and ALT.get('CONTAM2D') is not None and ALT['CONTAM2D'].shape == P.shape == C2D.shape):
        # the noise gate (``profile_joint``): decided here, on the two extracted results
        sets = {joint_use: (C2D, E2D), (not joint_use): (ALT['CONTAM2D'], ALT.get('CONTERR2D'))}
        infl = _joint_inflation(JT, wave)[0] if JT is not None else None
        ratio = _joint_noise_ratio(sets[True], sets[False], infl, Pm, weight_rows, s2d_err ** 2, flux, flux_err,
                                   contam_1d, contam_1d_min_sigma)
        gmax = float(header.get('PFJGMAX', np.inf))
        use = bool(np.isfinite(ratio) and ratio <= gmax)
        why = (f'noise<={gmax:g}' if use else (f'noise>{gmax:g}' if np.isfinite(ratio) else 'noise_undefined'))
        keep['PFJUSE'] = (use, 'fnu_prof is the joint result (F: one-pass)')
        keep['PFJGVAL'] = (float(ratio) if np.isfinite(ratio) else -999.0,
                           'Noise of the joint result / the one-pass one')
        keep['PFJWHY'] = (why, 'Why the joint result is (not) delivered')
        if use != joint_use:
            (C2D, E2D) = sets[use]
            joint_use = use
    hdus = []
    prof = fits.ImageHDU(P, name='PROF2D')
    prof.header['BUNIT'] = ('MJy / uJy', 'SCI per unit reported flux (not per column)')
    hdus.append(prof)
    if C2D is None or C2D.shape != P.shape:
        columns['fnu_contam'] = (nan, 'Predicted other-source flux (no prediction)')
        columns['fnu_contam_err'] = (nan, 'Its uncertainty (systematic)')
        choice, reason = choose_extraction(False, False, 0.0, contam_1d, contam_1d_min_sigma)
        keep.update(extraction_cards(choice, reason, 'off'))
        return dict(columns=columns, header=keep, hdus=hdus, choice=choice, reason=reason)
    V = s2d_err ** 2
    main = _contam_1d(C2D, E2D, Pm, weight_rows, V, flux, flux_err)
    cflux, cerr, csig, predicted = (main[k] for k in ('cflux', 'cerr', 'csig', 'predicted'))
    keep['PFCONTAM'] = (main['contam'], 'Predicted other light / fnu_prof, integrated')
    keep['PFCONMAX'] = (main['conmax'], '95th pct of |contam/fnu_prof| where S/N > 3')
    keep['PFCONSIG'] = (csig, 'Predicted other light / noise, integrated')
    with np.errstate(invalid='ignore', divide='ignore'):
        if JT is not None:
            # joint solve: the other members' light was solved with this source's
            # spectrum; its statistical uncertainty is the error inflation of the joint
            # covariance over the isolated one, added to the systematic part (CONTERR2D).
            # It belongs to the joint result only: to fnu_contam when fnu_prof is the joint
            # one (PFJUSE true, or a product written before the gate)
            jl = JT['lam']
            jok = np.isfinite(jl) & np.isfinite(JT['err']) & np.isfinite(JT['err_iso']) & (JT['err_iso'] > 0)
            o = np.argsort(jl[jok])
            infl, jcorr = _joint_inflation(JT, wave)
            if joint_use is None or joint_use:
                cerr = np.sqrt(np.where(np.isfinite(cerr), cerr, 0.0) ** 2 + _joint_stat(infl, flux_err) ** 2)
            thr = float(header.get('PFJDEGT', 0.9))
            jflag = (np.isfinite(jcorr) & (jcorr < -thr)).astype(np.int16)
            if 'flag' in JT and jok.sum() > 1:
                # nearest spectrum point of the joint solve: 1 split not determined, 2 shape-limited
                jw = jl[jok][o]
                i = np.clip(np.searchsorted(jw, wave), 1, jw.size - 1)
                near = np.where(np.abs(wave - jw[i - 1]) < np.abs(wave - jw[i]), i - 1, i)
                jflag = np.where(np.isfinite(wave) & (wave >= jw[0]) & (wave <= jw[-1]),
                                 JT['flag'][jok][o][near], 0).astype(np.int16)
    notsep = _notsep_on(NS, wave)
    if NS is not None:
        keep['PFNOTSEP'] = (float(notsep[np.isfinite(flux)].mean()) if np.isfinite(flux).any() else 0.0,
                            'Fraction of fnu_prof where other light is not separable')
    predicted = predicted & ~notsep
    columns['fnu_contam'] = (np.where(predicted, cflux, np.nan),
                             'Predicted other-source flux in the fnu_prof extraction '
                             '(NaN: none predicted, or not separable)')
    columns['fnu_contam_err'] = (np.where(predicted, cerr, np.nan), 'Its uncertainty (systematic)')
    if JT is not None:
        columns['joint_corr'] = (jcorr, 'Most negative correlation with a joint-solve member')
        columns['joint_infl'] = (infl, 'Joint-solve error inflation over the isolated error')
        columns['joint_flag'] = (jflag, 'Shared light: 1 split not determined, 2 shape-limited')
    choice, reason = choose_extraction(True, bool(np.any(predicted)), csig, contam_1d, contam_1d_min_sigma)
    keep.update(extraction_cards(choice, reason, 'predicted' if np.any(predicted) else 'none'))
    c = fits.ImageHDU(C2D, name='CONTAM2D')
    c.header['BUNIT'] = ('MJy', 'Predicted other-source light, as SCI')
    hdus.append(c)
    return dict(columns=columns, header=keep, hdus=hdus, choice=choice, reason=reason)


def _combined_profile_fit(prof, groups_pf, exposure_times, common_wave, fnu_prof, fnu_prof_err,
                          contam_1d='report', contam_1d_min_sigma=3.0):
    """The profile-fit columns and cards of a 1-D combined product.

    prof : the groups' ``fnu_prof`` (``per_eg_data['prof']``); groups_pf : per group its
    ``fnu_contam`` / ``fnu_contam_err`` and EXTRACT / EXTRWHY / CONTSTAT; fnu_prof /
    fnu_prof_err : the combined profile extraction.
    ``fnu_contam`` is combined with the exposure-time weights of ``fnu_prof`` (no
    clipping; a group with nothing predicted adds zero light), its error linearly (the
    predictions of the groups come from the same pass-1 spectra). The best spectrum is
    chosen by ``choose_extraction`` on the combined spectra, with ``PFCONSIG``
    recomputed; any group on a fallback makes the combined product the 3-pixel boxcar
    (``group_fallback``)."""
    waves = prof['wavelengths']
    pfin = [np.isfinite(f) for f in prof['fluxes']]

    def _comb(values):
        v = [np.where(ok, x, np.nan) for ok, x in zip(pfin, values)]
        c, _, _ = combine_1d_spectra(waves, v, prof['errors'], exposure_times, common_wave,
                                     sigma_clip_enabled=False)
        return c
    contam = _comb([np.nan_to_num(g['contam']) for g in groups_pf])
    contam_err = _comb([np.nan_to_num(g['contam_err']) for g in groups_pf])
    reach = _comb([np.isfinite(g['contam']).astype(float) for g in groups_pf])
    predicted = np.isfinite(reach) & (reach > 0)
    columns = {
        'fnu_prof': (fnu_prof, '1D-combined profile-fit flux, nothing subtracted'),
        'fnu_prof_err': (fnu_prof_err, 'Its error (statistical)'),
        'fnu_contam': (np.where(predicted, contam, np.nan), '1D-combined predicted other-source flux'),
        'fnu_contam_err': (np.where(predicted, contam_err, np.nan), 'Its uncertainty (systematic)'),
    }
    keep = fits.Header()
    states = {g['state'] for g in groups_pf}
    fell = [g['reason'] for g in groups_pf if g['extract'] == 'boxcar3']
    with np.errstate(invalid='ignore', divide='ignore'):
        c0 = np.where(np.isfinite(fnu_prof), np.where(predicted, contam, 0.0), np.nan)
        okn = np.isfinite(c0) & np.isfinite(fnu_prof_err) & (fnu_prof_err > 0)
        iv = 1.0 / fnu_prof_err[okn] ** 2
        csig = float(abs(np.sum(c0[okn] * iv)) / np.sqrt(np.sum(iv))) if okn.any() else 0.0
    if fell:
        choice, reason = 'boxcar3', 'group_fallback'
        log(f'1D combine: groups fell back ({", ".join(sorted(set(fell)))}); best spectrum is the 3-pixel boxcar')
    else:
        choice, reason = choose_extraction(states != {'off'}, bool(predicted.any()), csig, contam_1d,
                                           contam_1d_min_sigma)
    state = 'off' if states <= {'off'} else 'predicted' if 'predicted' in states else 'none'
    keep['PFCONSIG'] = (csig, 'Predicted other light / noise (1-D combined)')
    keep.update(extraction_cards(choice, reason, state))
    return dict(columns=columns, header=keep, hdus=[], choice=choice, reason=reason)


def combine_per_eg_spectra(
    per_eg_spec_files,
    all_cal_files,
    workspace_dir, product_name, source_id, filter_grating,
    sigma_clip=True, sigma_clip_low=3.0, sigma_clip_high=3.0,
    sigma_clip_maxiters=5, plot_profiles=True, plot_optext=True, version='v0.1', profile_fit_contam=None,
):
    """
    1D combination: combine per-exp_group 1D spectra (exposure-time weighted) + produce stacked 2D.

    Every extraction column is combined (``fnu_opt``, the boxcars, ``fnu_prof``); the
    best spectrum ``fnu`` is chosen again on the combined spectra by the per-product rule
    (``_combined_profile_fit``), never combined from the groups' ``fnu``.

    Parameters
    ----------
    per_eg_spec_files : list of str
        Basenames of per-exp_group _spec.fits files
    all_cal_files : list of str
        All canonical (background-subtracted) filenames for producing the stacked 2D
    workspace_dir : str
        Working directory
    product_name : str
        Output product name (without extension)
    source_id : int
        Source ID
    filter_grating : str
        Filter/grating combination string
    """
    log(f"Stage3 (1D combine): combining {len(per_eg_spec_files)} per-exp_group spectra for {product_name}")

    # --- Step 1: Read individual 1D spectra from each per-exp_group _spec.fits ---
    extraction_cols = {
        'opt': ('fnu_opt', 'fnu_opt_err'),
        '3px': ('fnu_3px', 'fnu_3px_err'),
        '4px': ('fnu_4px', 'fnu_4px_err'),
        '5px': ('fnu_5px', 'fnu_5px_err'),
    }

    def _names(spec_file):
        with fits.open(os.path.join(workspace_dir, spec_file)) as h:
            return set(h['SPEC1D'].columns.names)
    names = [_names(f) for f in per_eg_spec_files]
    has_pf = bool(per_eg_spec_files) and all('fnu_prof' in n for n in names)
    if has_pf:
        extraction_cols['prof'] = ('fnu_prof', 'fnu_prof_err')

    per_eg_data = {key: {'wavelengths': [], 'fluxes': [], 'errors': []} for key in extraction_cols}
    exposure_times = []
    groups_pf = []        # per group: fnu_contam, its error and the extraction cards

    for spec_file, cols in zip(per_eg_spec_files, names):
        spec_path = os.path.join(workspace_dir, spec_file)
        with fits.open(spec_path) as hdul:
            exposure_times.append(float(hdul['PRIMARY'].header.get('EFFEXPTM', 1.0)))
            spec1d = hdul['SPEC1D'].data
            wave = spec1d['wave']
            for key, (flux_col, err_col) in extraction_cols.items():
                if key == 'opt' and flux_col not in cols:
                    flux_col, err_col = 'fnu', 'fnu_err'       # written before fnu_opt existed
                per_eg_data[key]['wavelengths'].append(wave.copy())
                per_eg_data[key]['fluxes'].append(spec1d[flux_col].copy())
                per_eg_data[key]['errors'].append(spec1d[err_col].copy())
            if has_pf:
                ph = hdul['PRIMARY'].header
                groups_pf.append(dict(
                    contam=np.array(spec1d['fnu_contam'], float), contam_err=np.array(spec1d['fnu_contam_err'], float),
                    extract=str(ph.get('EXTRACT', '')), reason=str(ph.get('EXTRWHY', '')),
                    state=str(ph.get('CONTSTAT', 'off'))))

    # --- Step 2: Stacked 2D via Spec3Pipeline on ALL canonical files ---
    run_stage3_single_source(all_cal_files, workspace_dir, product_name, source_id)

    # --- Step 3: Get common wavelength grid from stacked x1d ---
    x1d_file = os.path.join(workspace_dir, f'{product_name}_x1d.fits')
    s2d_file = os.path.join(workspace_dir, f'{product_name}_s2d.fits')
    x1d = fits.open(x1d_file)
    s2d = fits.open(s2d_file)
    common_wave = x1d['EXTRACT1D'].data['WAVELENGTH'].copy()

    # --- Step 4: Combine ALL extraction columns via exposure-time weighting ---
    combined = {}
    for key in extraction_cols:
        c_flux, c_err, c_n = combine_1d_spectra(
            per_eg_data[key]['wavelengths'],
            per_eg_data[key]['fluxes'],
            per_eg_data[key]['errors'],
            exposure_times,
            common_wave,
            sigma_clip_enabled=sigma_clip,
            sigma_clip_low=sigma_clip_low,
            sigma_clip_high=sigma_clip_high,
            sigma_clip_maxiters=sigma_clip_maxiters,
        )
        combined[key] = (c_flux, c_err, c_n)

    # --- Step 5: Extraction profiles from stacked 2D (for visualization) ---
    s2d_sci = s2d['SCI'].data * 1e12  # MJy to uJy
    s2d_err = s2d['ERR'].data * 1e12
    s2d_mask = s2d['WHT'].data == 0

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        collapsed = np.nanmedian(s2d_sci, axis=1)
    x1d_start = x1d['EXTRACT1D'].header['EXTRYSTR'] - 1
    x1d_stop = x1d['EXTRACT1D'].header['EXTRYSTP']
    cen = (x1d_start + x1d_stop) / 2
    # Fixed-slit exposures have no MSA bars, so skip the aperture bounding on the
    # optimal-extraction profile (see opt_ext_single_source).
    try:
        _exp = s2d['EXPOSURES'].data
        is_fixed_slit = ('fixed_slit' in _exp.columns.names
                         and bool(np.any(_exp['fixed_slit'])))
    except (KeyError, IndexError):
        is_fixed_slit = False
    profile_opt = optext_profile(collapsed, x1d_start, x1d_stop, bounded=not is_fixed_slit)
    corrupted, n_pos, pos_frac = optext_profile_is_corrupted(collapsed, x1d_start, x1d_stop)
    if corrupted or all(np.isnan(profile_opt)):
        log(f"Stacked optimal-extraction profile is corrupted for {product_name} "
            f"(n_positive={n_pos}, positive_fraction={pos_frac:.2f}); "
            f"falling back to 3px boxcar")
        profile_opt = boxcar_profile(cen - 1.5, cen + 1.5, len(collapsed))
        optext_status = 'boxcar-3px'
        opt_label = 'Optimal (3px boxcar fallback)'
    else:
        optext_status = 'optimal'
        opt_label = 'Optimal'
    profile_3px = boxcar_profile(cen - 1.5, cen + 1.5, len(collapsed))
    profile_4px = boxcar_profile(cen - 2.0, cen + 2.0, len(collapsed))
    profile_5px = boxcar_profile(cen - 2.5, cen + 2.5, len(collapsed))

    if plot_profiles:
        from campfire_pipeline.nirspec.plots import plot_extraction_profiles
        profiles = {opt_label: profile_opt, '3px boxcar': profile_3px,
                    '4px boxcar': profile_4px, '5px boxcar': profile_5px}
        prof_path = os.path.join(workspace_dir, f'{product_name}_prof.pdf')
        plot_extraction_profiles(prof_path, collapsed, profiles,
                                x1d_start, x1d_stop, cen)

    # --- Step 6: Package final _spec.fits ---
    # PRIMARY header
    ph = fits.Header()
    ph['EXTEND'] = 'T'
    ph['FILENAME'] = (f'{product_name}_spec.fits', 'Name of the file')
    for keyword in ['ORIGIN', 'TIMESYS', 'TIMEUNIT', 'TELESCOP', 'PROGRAM', 'PI_NAME', 'CATEGORY', 'DATE-OBS',
                    'TIME-OBS', 'VISIT_ID', 'OBSERVTN', 'TARGPROP', 'TARGNAME', 'FILTER', 'GRATING', 'MSAMETFL',
                    'MSAMETID', 'MSACONID', 'EXP_TYPE', 'READPATT', 'EFFEXPTM', 'CAL_VER', 'CAL_VCS', 'CRDS_VER',
                    'CRDS_CTX', 'R_AREA', 'R_CAMERA', 'R_COLLIM', 'R_DARK', 'R_DISPER', 'R_DISTOR', 'R_EXTR1D',
                    'R_FILOFF', 'R_FLAT', 'R_DFLAT', 'R_FFLAT', 'R_SFLAT', 'R_FORE', 'R_FPA', 'R_GAIN', 'R_IFUFOR',
                    'R_IFUPOS', 'R_IFUSLI', 'R_LINEAR', 'R_MASK', 'R_MSA', 'R_OTE', 'R_PTHLOS', 'R_PHOTOM', 'R_READNO',
                    'R_REFPIX', 'R_REGION', 'R_SATURA', 'R_SPCWCS', 'R_SUPERB', 'R_WAVCOR', 'R_WAVRAN', 'S_WCS',
                    'S_BKDSUB', 'S_BPXSLF', 'S_BARSHA', 'S_CHGMIG', 'S_CLNFNS', 'S_DARK', 'S_DQINIT', 'S_EXTR1D',
                    'S_EXTR2D', 'S_FLAT', 'S_GANSCL', 'S_GRPSCL', 'S_IMPRNT', 'S_IPC', 'S_JUMP', 'S_LINEAR',
                    'S_MSBSUB', 'S_MSAFLG', 'S_NSCLEN', 'S_OUTLIR', 'S_PTHLOS', 'S_PHOTOM', 'S_PXREPL', 'S_RAMP',
                    'S_REFPIX', 'S_RESAMP', 'S_SATURA', 'S_SRCTYP', 'S_SUPERB', 'S_WAVCOR', 'NDRIZ', 'RESWHT',
                    'PIXFRAC', 'PXSCLRT']:
        try:
            ph[keyword] = (x1d['PRIMARY'].header[keyword], x1d['PRIMARY'].header.comments[keyword])
        except KeyError:
            continue
    ph['CMPFRTIM'] = (datetime.now(timezone.utc).isoformat(), 'UTC date/time of CAMPFIRE reduction (ISO 8601)')
    ph['CMPFRVER'] = (version, 'campfire-pipeline version (PEP 440)')
    ph['CMPFRSTG'] = ('stage3-1d', 'CAMPFIRE stage that produced this file')
    ph['CMPFROPT'] = (optext_status, "Optimal extraction status ('optimal' or fallback)")
    ph['NCOMBINE'] = (len(per_eg_spec_files), 'Number of per-exp_group spectra combined')

    fnu_opt, fnu_opt_err, n_opt = combined['opt']
    fnu_3px, fnu_3px_err, _ = combined['3px']
    pf_columns, pf_hdus = None, []
    if has_pf:
        fnu_prof, fnu_prof_err, _ = combined['prof']
        pf = _combined_profile_fit(per_eg_data['prof'], groups_pf, exposure_times, common_wave, fnu_prof,
                                   fnu_prof_err, **(profile_fit_contam or {}))
        pf_columns, pf_hdus = pf['columns'], pf['hdus']
        cards, choice = pf['header'], pf['choice']
        fnu_best, fnu_best_err = best_spectrum(choice, fnu_prof, fnu_prof_err, pf_columns['fnu_contam'][0],
                                               fnu_3px, fnu_3px_err)
    else:
        choice = 'optimal'
        cards = extraction_cards('optimal', 'profile_fit_off', 'off')
        fnu_best, fnu_best_err = fnu_opt, fnu_opt_err
    for key in cards:
        ph[key] = (cards[key], cards.comments[key])
    primary = fits.PrimaryHDU(header=ph)

    # SPEC1D: 1D-combined columns
    spec1d = Table()
    spec1d.add_column(Column(name='wave', data=common_wave, description='Wavelength', unit='um'))
    _add_flux_columns(spec1d, '', fnu_best, fnu_best_err, common_wave, f'1D-combined best spectrum ({choice})')
    _add_flux_columns(spec1d, '_opt', fnu_opt, fnu_opt_err, common_wave, '1D-combined optimally extracted flux')

    for size in ['3px', '4px', '5px']:
        fnu_box, fnu_box_err, _ = combined[size]
        spec1d.add_column(Column(name=f'fnu_{size}', data=fnu_box, description=f'1D-combined {size} boxcar flux', unit='uJy'))
        spec1d.add_column(Column(name=f'fnu_{size}_err', data=fnu_box_err, description=f'1D-combined {size} boxcar flux error', unit='uJy'))
        spec1d.add_column(Column(name=f'flam_{size}', data=_fnu_to_flam(fnu_box, common_wave), description=f'1D-combined {size} boxcar flux, flam', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name=f'flam_{size}_err', data=_fnu_to_flam(fnu_box_err, common_wave), description=f'1D-combined {size} boxcar flux error, flam', unit='erg / (s cm2 Angstrom)'))

    if pf_columns is not None:
        _add_pf_columns(spec1d, pf_columns, common_wave)

    spec1d.add_column(Column(name='n_combined', data=n_opt, description='Number of spectra combined per pixel'))

    spec1d_hdu = table_to_hdu(spec1d)
    spec1d_hdu.name = 'SPEC1D'

    # PROF1D
    prof1d = Table()
    prof1d['ypos'] = np.arange(len(collapsed)) + 0.5
    prof1d['opt'] = profile_opt
    prof1d['3px'] = profile_3px
    prof1d['4px'] = profile_4px
    prof1d['5px'] = profile_5px
    prof1d_hdu = table_to_hdu(prof1d)
    prof1d_hdu.name = 'PROF1D'

    # 2D extensions from stacked s2d
    s2dh = fits.Header()
    for keyword in ['EXTNAME', 'SRCTYPE', 'XPOSURE', 'PHOTMJSR', 'PHOTUJA2', 'PIXAR_SR', 'PIXAR_A2', 'DISPAXIS',
                    'SPORDER', 'SOURCEID', 'STLARITY', 'SRCRA', 'SRCDEC', 'WAVECOR', 'PTHLOSS']:
        if keyword in s2d['SCI'].header:
            s2dh[keyword] = (s2d['SCI'].header[keyword], s2d['SCI'].header.comments[keyword])

    sci_hdu = fits.ImageHDU(data=s2d['SCI'].data, header=s2dh, name='SCI')
    err_hdu = fits.ImageHDU(data=s2d['ERR'].data, header=s2dh, name='ERR')
    wav_hdu = fits.ImageHDU(data=s2d['WAVELENGTH'].data, header=s2dh, name='WAVELENGTH')
    wht_hdu = fits.ImageHDU(data=s2d['WHT'].data, header=s2dh, name='WHT')
    exp_hdu = s2d['EXPOSURES']

    out_filename = os.path.join(workspace_dir, f'{product_name}_spec.fits')
    # the groups' PROF2D / CONTAM2D are on their own s2d grids, not the stacked one: not carried
    spec = fits.HDUList(hdus=[primary, spec1d_hdu, sci_hdu, err_hdu, wav_hdu, wht_hdu] + pf_hdus + [prof1d_hdu, exp_hdu])
    spec.writeto(out_filename, overwrite=True)
    log(f"Stage3 (1D combine): wrote {out_filename}")

    if plot_optext:
        from campfire_pipeline.nirspec.plots import plot_spectrum_qa
        plot_spectrum_qa(
            out_filename.replace('_spec.fits', '_spec.pdf'),
            wave=common_wave,
            fnu=fnu_best, fnu_err=fnu_best_err,
            flam=_fnu_to_flam(fnu_best, common_wave),
            flam_err=_fnu_to_flam(fnu_best_err, common_wave),
            sci_2d=s2d['SCI'].data, err_2d=s2d['ERR'].data,
            profile_ypos=prof1d['ypos'], profile_opt=profile_opt,
            cen=cen, product_name=product_name,
            subtitle='(1D combine)',
        )

    x1d.close()
    s2d.close()
