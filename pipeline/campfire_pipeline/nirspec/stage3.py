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

    # Phase 1: Spec3Pipeline + optimal extraction
    dispatch(
        run_stage3_single_source,
        tasks,
        n_processes=n_processes,
        use_starmap=True,
        **s3_kwargs,
    )

    if pf_tasks:
        log(f"Profile fit: {len(pf_tasks)} products")
        run_profile_fit_phase(obs, stage_config, pf_tasks, n_processes=n_processes, overwrite=overwrite,
                              cleanup_crfs=cleanup_crfs)

    optext_tasks = [(task[2], task[1]) for task in tasks]
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

    fl = stage_flags(stage_config)
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
        mt = CT.map_tasks(obs.workspace_dir, list(every['path']), log=log)
        if mt:
            log(f"Profile fit: building {len(mt)} trace maps")
            dispatch(CT.build_map, mt, n_processes=n_processes, use_starmap=True)
    prism_fg = _prism_fg(all_fg)
    order = sorted(by_fg, key=lambda fg: (0 if fg == prism_fg else 1, fg))
    run_sids = {t['source_id'] for t in pf_tasks}
    results = []
    for fg in order:
        is_prism = fg == prism_fg
        pass1_products = {}
        if contam:
            sub = sci_all[sci_all['filter_grating'] == fg]
            every_fg = every[every['filter_grating'] == fg]
            p1 = []
            for sid in np.unique(sub['source_id']):
                tf = sub[sub['source_id'] == sid]
                t = _profile_fit_task(obs, every_fg, tf, f"{obs.name}_{fg}_{sid}", sid)
                prism_product = f"{obs.name}_{prism_fg}_{sid}" if (prism_fg and not is_prism) else ''
                pass1_products[int(sid)] = t['product_name']
                p1.append((t['product_name'], obs.workspace_dir, t['groups'], t['overrides'], t['science'],
                           int(sid), is_prism, stage_config, prism_product, bool(overwrite and int(sid) in run_sids)))
            log(f"Profile fit: pass 1 for {len(p1)} {fg} sources")
            dispatch(CT.run_pass1, p1, n_processes=n_processes, use_starmap=True)
            # second round for the sources whose trace others cover (see profile_contam.run_pass1)
            dispatch(CT.run_pass1, p1, n_processes=n_processes, use_starmap=True,
                     pass1_products=pass1_products, second=True)
        tasks = []
        for t in by_fg[fg]:
            prism_product = f"{obs.name}_{prism_fg}_{t['source_id']}" if (prism_fg and not is_prism) else ''
            tasks.append((t['product_name'], t['workspace_dir'], t['groups'], t['overrides'], t['science'],
                          t['source_id'], stage_config, is_prism, prism_product, pass1_products, cleanup_crfs))
        log(f"Profile fit: {len(tasks)} {fg} products")
        results += [r for r in dispatch(run_product, tasks, n_processes=n_processes, use_starmap=True) or [] if r]
    if fl['scan']:
        write_scan_proposals(obs, results, run_sids, str(stage_config.get('scan_action', 'report_only')).lower(),
                             xdisp_tol=fl['scan_xdisp_tol'])
    return results


def _prism_scan_agrees(obs, r, results, tol):
    """A PRISM product's neighbour against the grating scans of the same source (this
    run's results, else their ``_pf.fits`` headers): True when no grating product
    exists, or one finds the neighbour within ``tol`` of the same Delta."""
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
        return True
    d = r['scan']['delta']
    return any(det and np.isfinite(x) and abs(x - d) <= tol for det, x in found)


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
        if r.get('is_prism') and s.get('detected') and s.get('proposals') and \
                not _prism_scan_agrees(obs, r, results, xdisp_tol):
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
    ):
    """
    Runs an optimal extraction routine and saves an msaexp-style "_spec.fits" file (combining 2D and 1D) for a single object.
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
    profile_3px = boxcar_profile(cen-1.5, cen+1.5, len(collapsed))
    fnu_3px, fnu_3px_err = extract_with_profile(profile_3px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

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

    def fnu_to_flam(fnu, wave): # Assumed fnu in uJy and wave in um
        return fnu / wave**2 * 2.99792458e-19

    spec1d = Table()
    spec1d.add_column(Column(name='wave', data=wave, description='Wavelength', unit='um'))
    spec1d.add_column(Column(name='fnu', data=fnu_opt, description='Optimally extracted flux, fnu units', unit='uJy'))
    spec1d.add_column(Column(name='fnu_err', data=fnu_opt_err, description='Optimally extracted flux error, fnu units', unit='uJy'))
    spec1d.add_column(Column(name='flam', data=fnu_to_flam(fnu_opt, wave=wave), description='Optimally extracted flux, flam units', unit='erg / (s cm2 Angstrom)'))
    spec1d.add_column(Column(name='flam_err', data=fnu_to_flam(fnu_opt_err, wave=wave), description='Optimally extracted flux error, flam units', unit='erg / (s cm2 Angstrom)'))

    for size, fnu, fnu_err in zip(['3px','4px','5px'],[fnu_3px,fnu_4px,fnu_5px],[fnu_3px_err,fnu_4px_err,fnu_5px_err]):
        spec1d.add_column(Column(name=f'fnu_{size}', data=fnu, description=f'{size} boxcar extracted flux, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'fnu_{size}_err', data=fnu_err, description=f'{size} boxcar extracted flux error, fnu units', unit='uJy'))
        spec1d.add_column(Column(name=f'flam_{size}', data=fnu_to_flam(fnu, wave=wave), description=f'{size} boxcar extracted flux, flam units', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name=f'flam_{size}_err', data=fnu_to_flam(fnu_err, wave=wave), description=f'{size} boxcar extracted flux error, flam units', unit='erg / (s cm2 Angstrom)'))

    # indices = np.where(fnu_3px_err!=0)[0]
    # spec1d = spec1d[indices[0]:indices[-1] + 1]

    # Profile-fit extraction ([nirspec.stage3].profile_fit): present only when the
    # profile fit ran for this product.
    pf_hdus = []
    pf_file = os.path.join(workspace_dir, f'{product_name}_pf.fits')
    if os.path.exists(pf_file):
        pf = _profile_fit_columns(pf_file, s2d_sci, s2d_err, s2d_mask, x1d_start, x1d_stop, wave,
                                  rows=profile_fit_rows, model_rows_frac=profile_fit_model_rows_frac)
        if pf is not None:
            for name, (data, desc) in pf['columns'].items():
                spec1d.add_column(Column(name=name, data=data, description=desc, unit='uJy'))
            spec1d.add_column(Column(name='flam_pf', data=fnu_to_flam(pf['columns']['fnu_pf'][0], wave=wave),
                                     description='Profile-fit flux, flam units', unit='erg / (s cm2 Angstrom)'))
            spec1d.add_column(Column(name='flam_pf_err', data=fnu_to_flam(pf['columns']['fnu_pf_err'][0], wave=wave),
                                     description='Profile-fit flux error, flam units', unit='erg / (s cm2 Angstrom)'))
            for key in pf['header']:
                primary.header[key] = (pf['header'][key], pf['header'].comments[key])
            pf_hdus = pf['hdus']

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


    spec = fits.HDUList(hdus=[primary, spec1d, sci, err, wav, wht, prof1d, exp] + pf_hdus)
    spec.writeto(out_filename, overwrite=True)
    if pf_hdus and os.path.exists(pf_file):
        os.remove(pf_file)   # its contents now live in the _spec.fits (PROF2D, PF* keys, fnu_pf)


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


def _profile_fit_columns(pf_file, s2d_sci, s2d_err, s2d_mask, ap_start, ap_stop, wave, rows='aperture',
                         model_rows_frac=0.02):
    """Extraction of the s2d with the profile-fit 2D profile.

    PROF2D is the drizzled source model per unit reported flux (uJy, against the s2d
    SCI in MJy): it is not normalised per column, so its in-aperture sum carries
    the aperture and path-loss correction. The weighted fit of the data on the
    aperture rows, sum(P D / V) / sum(P^2 / V), is the reported flux directly:
    main-shutter light on the point-source path-loss calibration, as for ``fnu``.

    ``rows``: which extraction is ``fnu_pf``: ``aperture`` (the x1d aperture rows)
    or ``model`` (every row where PROF2D is significant, including the negative nod
    copies; ``model_rows_extraction``). Both are written (``fnu_pfa``, ``fnu_pfm``).
    With ``CONTAM2D`` the predicted other-source light is extracted with the same
    weights (``fnu_pf_contam``; reported, not subtracted).
    """
    with fits.open(pf_file) as h:
        P = np.asarray(h['PROF2D'].data, float)
        header = h['PRIMARY'].header.copy()
        C2D = np.asarray(h['CONTAM2D'].data, float) if 'CONTAM2D' in h else None
    if rows not in ('aperture', 'model'):
        raise ValueError(f"profile_fit_rows must be 'aperture' or 'model', got {rows!r}")
    if P.shape != s2d_sci.shape:
        log(f"{os.path.basename(pf_file)}: profile {P.shape} does not match the s2d {s2d_sci.shape}; skipping")
        return None
    x = np.arange(P.shape[0] + 1)
    in_ap = (x[:-1] > ap_start) & (x[1:] <= ap_stop)
    Pa = np.where(in_ap[:, None] & np.isfinite(P), P * 1e12, 0.0)   # per uJy, against s2d_sci in uJy units
    norm = Pa.sum(axis=0)
    good = norm > 1e-3 * np.nanmax(norm)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        amp, amp_err = extract_with_profile(np.where(good, Pa / np.where(good, norm, 1.0), 0.0),
                                            s2d_sci.copy(), s2d_err.copy(), mask=s2d_mask, ivw=True)
    flux_a = np.where(good, amp / np.where(good, norm, 1.0), np.nan)
    flux_a_err = np.where(good, amp_err / np.where(good, norm, 1.0), np.nan)
    Pm = np.where(np.isfinite(P), P * 1e12, np.nan)
    flux_m, flux_m_err, sel_m = model_rows_extraction(Pm, s2d_sci, s2d_err, s2d_mask, frac=model_rows_frac)
    if rows == 'model':
        flux, flux_err = flux_m, flux_m_err
        weight_rows = sel_m
    else:
        flux, flux_err = flux_a, flux_a_err
        weight_rows = in_ap[:, None] & np.isfinite(P) & ~s2d_mask
    columns = {
        'fnu_pf': (flux, f'Profile-fit extracted flux (PROF2D weights, {rows} rows), fnu units'),
        'fnu_pf_err': (flux_err, 'Profile-fit extracted flux error, fnu units'),
        'fnu_pfa': (flux_a, 'Profile-fit flux, aperture rows'),
        'fnu_pfa_err': (flux_a_err, 'Profile-fit flux error, aperture rows'),
        'fnu_pfm': (flux_m, 'Profile-fit flux, all significant PROF2D rows (incl. negative nod copies)'),
        'fnu_pfm_err': (flux_m_err, 'Profile-fit flux error, all significant PROF2D rows'),
    }
    keep = fits.Header()
    for key in header:
        if key.startswith('PF'):
            keep[key] = (header[key], header.comments[key])
    keep['PFROWS'] = (rows, 'Rows of fnu_pf: aperture / model')
    hdus = []
    prof = fits.ImageHDU(P, name='PROF2D')
    prof.header['BUNIT'] = ('MJy / uJy', 'SCI per unit reported flux; not normalised per column')
    hdus.append(prof)
    if C2D is not None and C2D.shape == P.shape:
        Cu = np.where(np.isfinite(C2D), C2D * 1e12, 0.0)          # uJy, as s2d_sci
        V = s2d_err ** 2
        with np.errstate(invalid='ignore', divide='ignore'):
            w = np.where(weight_rows & np.isfinite(V) & (V > 0), Pm / np.where(V > 0, V, 1.0), 0.0)
            den = np.sum(w * np.where(np.isfinite(Pm), Pm, 0.0), axis=0)
            cflux = np.where(den > 0, np.sum(w * Cu, axis=0) / np.where(den > 0, den, 1.0), np.nan)
            ok = np.isfinite(cflux) & np.isfinite(flux)
            tot = np.nansum(flux[ok])
            keep['PFCONTAM'] = (float(np.nansum(cflux[ok]) / tot) if tot > 0 else -999.0,
                                'Predicted other-source light / fnu_pf, integrated')
            sn = ok & (np.abs(flux) > 3 * flux_err)
            frac = np.abs(cflux[sn] / flux[sn])
            keep['PFCONMAX'] = (float(np.percentile(frac, 95)) if frac.size else -999.0,
                                '95th pct of |contam/fnu_pf| where S/N > 3')
        columns['fnu_pf_contam'] = (cflux, 'Predicted other-source flux in the fnu_pf extraction (not subtracted)')
        c = fits.ImageHDU(C2D, name='CONTAM2D')
        c.header['BUNIT'] = ('MJy', 'Predicted other-source light, as SCI')
        hdus.append(c)
    return dict(columns=columns, header=keep, hdus=hdus)


def combine_per_eg_spectra(
    per_eg_spec_files,
    all_cal_files,
    workspace_dir, product_name, source_id, filter_grating,
    sigma_clip=True, sigma_clip_low=3.0, sigma_clip_high=3.0,
    sigma_clip_maxiters=5, plot_profiles=True, plot_optext=True, version='v0.1',
):
    """
    1D combination: combine per-exp_group 1D spectra (exposure-time weighted) + produce stacked 2D.

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
        'opt': ('fnu', 'fnu_err'),
        '3px': ('fnu_3px', 'fnu_3px_err'),
        '4px': ('fnu_4px', 'fnu_4px_err'),
        '5px': ('fnu_5px', 'fnu_5px_err'),
    }

    def _has_pf(spec_file):
        with fits.open(os.path.join(workspace_dir, spec_file)) as h:
            return 'fnu_pf' in h['SPEC1D'].columns.names
    if per_eg_spec_files and all(_has_pf(f) for f in per_eg_spec_files):
        extraction_cols['pf'] = ('fnu_pf', 'fnu_pf_err')

    per_eg_data = {key: {'wavelengths': [], 'fluxes': [], 'errors': []} for key in extraction_cols}
    exposure_times = []

    for spec_file in per_eg_spec_files:
        spec_path = os.path.join(workspace_dir, spec_file)
        with fits.open(spec_path) as hdul:
            exposure_times.append(float(hdul['PRIMARY'].header.get('EFFEXPTM', 1.0)))
            spec1d = hdul['SPEC1D'].data
            wave = spec1d['wave']
            for key, (flux_col, err_col) in extraction_cols.items():
                per_eg_data[key]['wavelengths'].append(wave.copy())
                per_eg_data[key]['fluxes'].append(spec1d[flux_col].copy())
                per_eg_data[key]['errors'].append(spec1d[err_col].copy())

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
    primary = fits.PrimaryHDU(header=ph)

    # SPEC1D: 1D-combined columns
    def fnu_to_flam(fnu, wave):
        return fnu / wave**2 * 2.99792458e-19

    spec1d = Table()
    spec1d.add_column(Column(name='wave', data=common_wave, description='Wavelength', unit='um'))

    fnu_opt, fnu_opt_err, n_opt = combined['opt']
    spec1d.add_column(Column(name='fnu', data=fnu_opt, description='1D-combined optimally extracted flux', unit='uJy'))
    spec1d.add_column(Column(name='fnu_err', data=fnu_opt_err, description='1D-combined optimally extracted flux error', unit='uJy'))
    spec1d.add_column(Column(name='flam', data=fnu_to_flam(fnu_opt, common_wave), description='1D-combined optimal flux, flam', unit='erg / (s cm2 Angstrom)'))
    spec1d.add_column(Column(name='flam_err', data=fnu_to_flam(fnu_opt_err, common_wave), description='1D-combined optimal flux error, flam', unit='erg / (s cm2 Angstrom)'))

    for size in ['3px', '4px', '5px']:
        fnu_box, fnu_box_err, _ = combined[size]
        spec1d.add_column(Column(name=f'fnu_{size}', data=fnu_box, description=f'1D-combined {size} boxcar flux', unit='uJy'))
        spec1d.add_column(Column(name=f'fnu_{size}_err', data=fnu_box_err, description=f'1D-combined {size} boxcar flux error', unit='uJy'))
        spec1d.add_column(Column(name=f'flam_{size}', data=fnu_to_flam(fnu_box, common_wave), description=f'1D-combined {size} boxcar flux, flam', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name=f'flam_{size}_err', data=fnu_to_flam(fnu_box_err, common_wave), description=f'1D-combined {size} boxcar flux error, flam', unit='erg / (s cm2 Angstrom)'))

    if 'pf' in combined:
        fnu_pf, fnu_pf_err, _ = combined['pf']
        spec1d.add_column(Column(name='fnu_pf', data=fnu_pf, description='1D-combined profile-fit flux', unit='uJy'))
        spec1d.add_column(Column(name='fnu_pf_err', data=fnu_pf_err, description='1D-combined profile-fit flux error', unit='uJy'))
        spec1d.add_column(Column(name='flam_pf', data=fnu_to_flam(fnu_pf, common_wave), description='1D-combined profile-fit flux, flam', unit='erg / (s cm2 Angstrom)'))
        spec1d.add_column(Column(name='flam_pf_err', data=fnu_to_flam(fnu_pf_err, common_wave), description='1D-combined profile-fit flux error, flam', unit='erg / (s cm2 Angstrom)'))

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
    spec = fits.HDUList(hdus=[primary, spec1d_hdu, sci_hdu, err_hdu, wav_hdu, wht_hdu, prof1d_hdu, exp_hdu])
    spec.writeto(out_filename, overwrite=True)
    log(f"Stage3 (1D combine): wrote {out_filename}")

    if plot_optext:
        from campfire_pipeline.nirspec.plots import plot_spectrum_qa
        plot_spectrum_qa(
            out_filename.replace('_spec.fits', '_spec.pdf'),
            wave=common_wave,
            fnu=fnu_opt, fnu_err=fnu_opt_err,
            flam=fnu_to_flam(fnu_opt, common_wave),
            flam_err=fnu_to_flam(fnu_opt_err, common_wave),
            sci_2d=s2d['SCI'].data, err_2d=s2d['ERR'].data,
            profile_ypos=prof1d['ypos'], profile_opt=profile_opt,
            cen=cen, product_name=product_name,
            subtitle='(1D combine)',
        )

    x1d.close()
    s2d.close()
