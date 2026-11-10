"""
Stage 3: Spec3Pipeline, optimal extraction, and 1D combination across exposure groups.

Order of operations for one product (``combine_method = "2d"``, or one exposure group):

1. With the profile fit, per disperser (``run_profile_fit_phase``): the joint first pass
   of every source, then the same-slitlet decisions redone from it and stage 2b again
   where the background exposures change (``stage2.revise_slitlet_overrides``), so
   everything below reads the canonicals as finally subtracted.
2. Spec3 on its background-subtracted exposures (``run_stage3_single_source``): the s2d,
   x1d and the outlier-flagged crf; the EXPOSURES table.
3. The profile fit (``profile_product.run_product``): load, fit, then every 2-D image of
   the product drizzled through one set of pixel maps built from the crf
   (``drizzle_map``): the nodded ``PROF2D`` / ``CONTAM2D`` / ``CONTERR2D``, and the
   master-background 2-D and its ``PROF2D`` / ``CONTAM2D`` / ``CONTERR2D``
   (``mbkg_extraction``), made in memory. Without the profile fit, the master-background
   2-D is drizzled in step 2, through the same kind of map.
4. Extraction (``opt_ext_single_source``): every 1-D extraction of both 2-D spectra into
   the ``_spec.fits``.

Several exposure groups (``combine_method = "1d"``): steps 2-4 per group, then
``combine_per_eg_spectra`` combines the groups' 1-D spectra (nodded and master
background) and takes the stacked 2-D from one more Spec3 over all exposures, with the
stacked master-background 2-D drizzled through that run's pixel maps.
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
from campfire_pipeline.nirspec import extraction_cards as XC
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
    # the master-background spectrum (mbkg_extraction): its 2-D is drizzled through each
    # product's pixel maps in the profile fit (profile_product.run_product), or, without
    # it, right after the product's Spec3
    mbkg_spectrum = bool(stage_config.get('mbkg_spectrum', True))
    s3_kwargs['mbkg_spectrum'] = mbkg_spectrum and not profile_fit
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

    # Phase 1: Spec3Pipeline and every drizzle, then the extraction. With the profile fit,
    # Spec3 runs per disperser after the same-slitlet revision (run_profile_fit_phase), so
    # the s2d and the EXPOSURES table come from the background actually subtracted
    if pf_tasks:
        from campfire_pipeline.config import get_stage_config
        log(f"Profile fit: {len(pf_tasks)} products")
        with TM.phase(f'profile_fit {obs.name} (all)'):
            run_profile_fit_phase(obs, stage_config, pf_tasks, n_processes=n_processes, overwrite=overwrite,
                                  cleanup_crfs=cleanup_crfs, stage2_config=get_stage_config('stage2', config, obs),
                                  s3_tasks=tasks, s3_kwargs=s3_kwargs)
    else:
        with TM.phase(f'spec3 {obs.name} ({len(tasks)} products)'):
            dispatch(
                run_stage3_single_source,
                tasks,
                n_processes=n_processes,
                use_starmap=True,
                **s3_kwargs,
            )

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
            profile_fit=profile_fit,
            mbkg_spectrum=mbkg_spectrum,
            mbkg_extended_frac=float(stage_config.get('mbkg_extended_frac', 0.10)),
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
            mbkg_spectrum=mbkg_spectrum,
            mbkg_extended_frac=float(stage_config.get('mbkg_extended_frac', 0.10)),
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
        mbkg_spectrum: bool = False,
    ):
    """Spec3Pipeline on a product's background-subtracted exposures (the s2d, x1d and,
    for the profile fit, the crf) and its EXPOSURES table; with ``mbkg_spectrum`` also
    the master-background 2-D, drizzled through the product's pixel maps from the crf
    (``mbkg_extraction.write_2d``; an exposure without a master background, or no crf,
    leaves the product without one, logged)."""
    from jwst.pipeline import Spec3Pipeline


    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(workspace_dir)

    try:
        # whatever an interrupted earlier run left must not stand in for this run's
        # master-background 2-D or profile fit (mbkg_spectrum_hdus reads what exists)
        from campfire_pipeline.nirspec import mbkg_extraction as MX
        MX.cleanup(workspace_dir, product_name)

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
        # whether the nods were chosen automatically (a catalogued neighbour in the slitlet,
        # or the neighbour scan's file), and why
        exposures['bkg_auto'] = [str(hdr['BKGAUTO']) if 'BKGAUTO' in hdr else 'N/A' for hdr in hdrs0]
        exposures['bkg_why'] = [str(hdr.get('BKGWHY', '')) for hdr in hdrs0]
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

        if mbkg_spectrum:
            from campfire_pipeline.nirspec import drizzle_map as DM
            from campfire_pipeline.nirspec import mbkg_extraction as MX
            crf = DM.crf_path(product_name, source_id)
            if os.path.exists(crf):
                dmap = DM.DrizzleMap.from_crf(crf, DM.step_settings(s2d_file))
                MX.write_2d(product_name, os.getcwd(), source_id, dmap, log=log)
            else:
                log(f'{product_name}: no master-background spectrum (no Spec3 crf {crf})')

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
    overrides (the hand file; the canonicals' BKGNODS cards win when present),
    the names stage 3 combines, and the disperser."""
    groups, overrides = [], []
    eff = obs.bkg_overrides
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
    ok = set(_science_files_of(list(files['path'])))
    return files[[os.path.basename(p) in ok for p in files['path']]]


def _refresh_science(obs, fg_tasks, s3_of):
    """After the same-slitlet revision reran stage 2b, which may have applied a hand
    override edited since (a nod set to none): the nods each product combines, read
    again. A nod no longer subtracted is dropped from the product's science list and its
    Spec3 inputs (a product left with none is not made); a nod of the product's groups
    now subtracted but not in it is logged with the command that adds it. Returns the
    tasks to keep."""
    keep = []
    for t in fg_tasks:
        paths = {os.path.basename(p): p for g in t['groups'] for p in g}
        ok = set(_science_files_of([paths.get(n, os.path.join(obs.workspace_dir, n)) for n in paths]))
        dropped = [n for n in t['science'] if n not in ok]
        added = sorted(n for n in ok if n not in t['science'])
        if dropped:
            log(f"{t['product_name']}: {', '.join(dropped)} no longer background-subtracted after the revision; "
                f"left out")
            t['science'] = [n for n in t['science'] if n in ok]
            s3 = s3_of.get(t['product_name'])
            if s3 is not None:
                s3_of[t['product_name']] = (list(t['science']),) + tuple(s3[1:])
        if added:
            log(f"{t['product_name']}: {', '.join(added)} now background-subtracted but not in this product; to add "
                f"it: cfpipe nirspec run --obs {obs.name} --stage3 --source-ids {t['source_id']} --overwrite")
        if t['science']:
            keep.append(t)
        else:
            log(f"{t['product_name']}: no background-subtracted exposure left after the revision; not made")
            s3_of.pop(t['product_name'], None)
    return keep


def _science_files_of(paths):
    """The names among ``paths`` stage 3 combines: background-subtracted and containing
    the source."""
    out = []
    for p in paths:
        h = fits.getheader(p)
        v = h.get('CFP_BKG')
        if bool(v) and not str(v).startswith(('skipped', 'excluded')) and str(h.get('SRCFLUX', 'T')) == 'T':
            out.append(os.path.basename(p))
    return out


def _prism_fg(fgs):
    return next((fg for fg in sorted(fgs) if str(fg).lower().startswith('prism')), None)


def _own_partners_differ(task):
    """True when a source's canonicals (``task``: ``_profile_fit_task``) hold other
    background exposures than its override files give (``BKGNODS`` against
    ``stage2.nod_background_partners``), e.g. a same-slitlet override."""
    from campfire_pipeline.nirspec import profile_fit as PF
    from campfire_pipeline.nirspec.stage2 import nod_background_partners
    for files, ov in zip(task['groups'], task['overrides']):
        live = PF.partners_from_headers(files, [fits.getheader(f) for f in files])
        if live is not None and live != nod_background_partners(files, ov):
            return True
    return False


def run_profile_fit_phase(obs, stage_config, pf_tasks, n_processes=1, overwrite=False, cleanup_crfs=True,
                          stage2_config=None, s3_tasks=None, s3_kwargs=None):
    """Spec3 and the profile-fit extraction profiles of the products in ``pf_tasks``
    (extraction v2), every 2-D image of them drizzled before stage 3 extracts anything.

    Dispersers run PRISM first, so a grating can take the PRISM shape of the same
    source. Where it has none, a grating product that fell to the default shape is redone
    once every grating has run, with the shape of the source's grating that took its own fit
    (``grating_transfer_plan``, ``profile_fit_grating_transfer``; the gratings keep their
    Spec3 crf until then). With ``contam_predict``, trace maps for every exposure of the observation
    (cached), then per disperser:

    1. the joint first pass of every source of the configuration (also the ones not
       being re-run: their light reaches the re-run ones; ``joint_first_pass``);
    2. with ``stage2_config`` (and ``slitlet_neighbor_brightness = "model"``), the
       same-slitlet decisions of the re-run sources redone with each neighbour's
       brightness from its modeled light (``stage2.revise_slitlet_overrides``), and
       stage 2b again where the background exposures change. The pass of step 1 then
       solves every member on the nod differences of the override files, rebuilt from
       the frames before subtraction (``pre_bkgsub``), so the brightness, and with it the
       decision, does not depend on the choice an earlier run wrote into the canonicals;
    3. the joint first pass again, on the canonicals as now subtracted, of the sources
       whose own background exposures are not the override files' (``_own_partners_differ``),
       into files of their own beside the first (``profile_contam.pass1_current``), so a
       rerun finds both current: a product takes its neighbours' light from its own
       solve, which must be on its own nod differences (``joint_first_pass.add_joint_light``);

    then (every disperser) Spec3 of its products (``s3_tasks``,
    ``run_stage3_single_source``: the s2d, crf and EXPOSURES table from the canonicals as
    revised) and the products (``profile_product.run_product``: every drizzle). The
    neighbour-scan proposals are written to the observation's reference directory, with
    a review figure per proposed source (``write_scan_proposals``)."""
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec.observation import Observation
    from campfire_pipeline.nirspec import joint_first_pass as JF
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec.profile_product import run_product, stage_flags
    from campfire_pipeline.nirspec import profile_timing as TM

    if str(stage_config.get('bar_offset', 'measure')).lower() == 'measure':
        # where this observation's bars sit against jwst's slit frame, from its empty shutters (measured
        # once per disperser, kept in the workspace's bar_offset.json); every loader of this phase applies it
        from campfire_pipeline.nirspec import bar_offset as BO
        with TM.phase('bar offset'):
            bars = BO.ensure(obs.workspace_dir, processes=n_processes, log=log)
        stage_config = dict(stage_config, bar_offsets=bars)
    fl = stage_flags(stage_config)
    contam = fl['contam']
    shape = str(stage_config.get('profile_fit_shape', 'gated')).lower()
    revise = stage2_config is not None and \
        str(stage_config.get('slitlet_neighbor_brightness', 'model')).lower() == 'model'
    s3_kwargs = s3_kwargs or {}
    s3_of = {t[2]: t for t in (s3_tasks or [])}
    by_fg = {}
    for t in pf_tasks:
        by_fg.setdefault(t['filter_grating'], []).append(t)
    rest = [t for name, t in s3_of.items() if name not in {p['product_name'] for p in pf_tasks}]
    if rest:
        with TM.phase(f'spec3 other ({len(rest)} products)'):
            dispatch(run_stage3_single_source, rest, n_processes=n_processes, use_starmap=True, **s3_kwargs)
    all_fg = set(by_fg)
    sci_all = None
    # every trace map read here (the prediction, the neighbour scan, the master-background fit)
    # leaves out the stuck-closed shutters: they pass no light
    stuck_keys = CT.stuck_keys_by_root(obs)
    if contam:
        every = Observation.group_files(obs.discover_files(ext='canonical', source_ids='all'))
        sci_all = _science_files(every)
        all_fg |= set(np.unique(every['filter_grating']))
        no_map = CT.exposures_without_maps(obs.workspace_dir, list(every['path']))
        if no_map:
            log(f"Profile fit: {len(no_map)} exposures have no trace map in their rate file (rerun stage 1); "
                f"the light of their neighbours is not predicted: {', '.join(no_map)}")
    prism_fg = _prism_fg(all_fg)
    order = sorted(by_fg, key=lambda fg: (0 if fg == prism_fg else 1, fg))
    run_sids = {t['source_id'] for t in pf_tasks}
    # the grating shape transfer redoes some grating products once every grating has run, through
    # their Spec3 crf, which a product otherwise removes when it is done
    gxfer = (bool(stage_config.get('profile_fit_grating_transfer', True)) and shape == 'gated'
             and sum(fg != prism_fg for fg in by_fg) >= 2)
    task_of = {}
    results = []
    for fg in order:
        is_prism = fg == prism_fg
        pass1_products = {}
        if contam:
            sub = sci_all[sci_all['filter_grating'] == fg]
            every_fg = every[every['filter_grating'] == fg]
            jtasks = {}
            for sid in np.unique(sub['source_id']):
                tf = sub[sub['source_id'] == sid]
                t = _profile_fit_task(obs, every_fg, tf, f"{obs.name}_{fg}_{sid}", sid)
                prism_product = f"{obs.name}_{prism_fg}_{sid}" if (prism_fg and not is_prism) else ''
                pass1_products[int(sid)] = t['product_name']
                s3 = s3_of.get(t['product_name'])
                jtasks[int(sid)] = dict(product=t['product_name'], groups=t['groups'], overrides=t['overrides'],
                                        science=t['science'], prism_product=prism_product,
                                        # Spec3 runs after this pass: the boxcar fallback's own (JF._boxcar)
                                        spec3=None if s3 is None else
                                        (s3, dict(s3_kwargs, cleanup_crfs=True, mbkg_spectrum=False)))
            # every source solved with its neighbours on its nod differences (joint_first_pass)
            log(f"Profile fit: joint pass 1 for {len(jtasks)} {fg} sources"
                + (' (nod differences of the override files)' if revise else ''))
            with TM.phase(f'pass1 joint {fg} ({len(jtasks)})'):
                JF.run(jtasks, obs.workspace_dir, stage_config, is_prism, n_processes=n_processes,
                       overwrite={s for s in jtasks if s in run_sids} if overwrite else False,
                       cache_size=int(stage_config.get('contam_joint_cache', 60)), pre_bkgsub=revise,
                       stuck_keys=stuck_keys)
            own = []
            if revise:
                from campfire_pipeline.nirspec.stage2 import revise_slitlet_overrides
                with TM.phase(f'slitlet revision {fg}'):
                    revise_slitlet_overrides(obs, stage2_config, run_sids, fg, f'{obs.name}_{fg}', n_processes)
                by_fg[fg] = _refresh_science(obs, by_fg[fg], s3_of)
                own = sorted(s for s, t in jtasks.items() if _own_partners_differ(t))
                if own:
                    # into their own files (joint_first_pass.run ``own``), read by the products
                    # (CT.pass1_current); the revision reads the override files' pass
                    log(f"Profile fit: joint pass 1 again for {len(own)} {fg} sources on their own background "
                        f"exposures")
                    with TM.phase(f'pass1 joint own {fg} ({len(own)})'):
                        JF.run(jtasks, obs.workspace_dir, stage_config, is_prism, n_processes=n_processes,
                               overwrite={s for s in own if s in run_sids} if overwrite else False,
                               cache_size=int(stage_config.get('contam_joint_cache', 60)), targets=own,
                               stuck_keys=stuck_keys, own=True)
            JF.drop_own(obs.workspace_dir, [t['product'] for s, t in jtasks.items() if s not in own])
        # Spec3 from the canonicals as (now) subtracted: the s2d, crf and EXPOSURES table
        s3 = [s3_of[t['product_name']] for t in by_fg[fg] if t['product_name'] in s3_of]
        if s3:
            with TM.phase(f'spec3 {fg} ({len(s3)} products)'):
                dispatch(run_stage3_single_source, s3, n_processes=n_processes, use_starmap=True, **s3_kwargs)
        tasks = []
        keep_crfs = gxfer and not is_prism
        for t in by_fg[fg]:
            prism_product = f"{obs.name}_{prism_fg}_{t['source_id']}" if (prism_fg and not is_prism) else ''
            tasks.append((t['product_name'], t['workspace_dir'], t['groups'], t['overrides'], t['science'],
                          t['source_id'], stage_config, is_prism, prism_product, pass1_products,
                          cleanup_crfs and not keep_crfs, stuck_keys))
            if keep_crfs:
                task_of[t['product_name']] = tasks[-1]
        log(f"Profile fit: {len(tasks)} {fg} products")
        with TM.phase(f'products {fg} ({len(tasks)})'):
            res = [r for r in dispatch(run_product, tasks, n_processes=n_processes, use_starmap=True) or [] if r]
        results += res
    if gxfer:
        # a source's gratings take the shape of the one that took its own fit where they fell to the default
        plan = {p: ref for p, ref in grating_transfer_plan(results).items() if p in task_of}
        if plan:
            redo = []
            for prod, ref in sorted(plan.items()):
                t = list(task_of[prod])
                t[10] = cleanup_crfs
                redo.append(tuple(t) + (ref,))          # run_product's shape_product
            log(f"Profile fit: {len(redo)} grating products again with the shape of their source's grating that "
                f"took its own fit")
            with TM.phase(f'products grating transfer ({len(redo)})'):
                res = [r for r in dispatch(run_product, redo, n_processes=n_processes, use_starmap=True) or [] if r]
            again = {r['product']: r for r in res}
            results = [again.get(r['product'], r) for r in results]
        if cleanup_crfs:
            for prod, t in task_of.items():
                if prod not in plan:
                    _cleanup_in(t[1], t[4], prod)
    log_fallbacks(results)
    if fl['scan']:
        write_scan_proposals(obs, results, scanned_roots(pf_tasks), xdisp_tol=fl['scan_xdisp_tol'])
    return results


def grating_transfer_plan(results):
    """The products the grating shape transfer redoes: {grating product: the product whose
    shape it takes}. A source's reference is its grating product that took its own fit (the
    highest detection S/N when several did); every other grating product of that source that
    fell to the default shape, and took no PRISM shape, is redone with the reference's shape
    as its transferred candidate (``profile_chain.PrismShape``, not chromatic). ``results``:
    the ``run_product`` summaries of every disperser."""
    ref = {}
    for r in results:
        if r.get('is_prism') or r.get('fallback') or r.get('shape') != 'own':
            continue
        s, snr = int(r['source_id']), float(r.get('snr_det', -np.inf))
        if s not in ref or snr > ref[s][0]:
            ref[s] = (snr, r['product'])
    plan = {}
    for r in results:
        if (r.get('is_prism') or r.get('fallback') or r.get('shape') != 'default'
                or r.get('transfer') == 'used'):
            continue
        s = int(r['source_id'])
        if s in ref and ref[s][1] != r['product']:
            plan[r['product']] = ref[s][1]
    return plan


def _cleanup_in(workspace, science, product_name):
    """``_cleanup_spec3_intermediates`` from the workspace (its paths are relative)."""
    prev = os.getcwd()
    os.chdir(workspace)
    try:
        _cleanup_spec3_intermediates(list(science), product_name)
    finally:
        os.chdir(prev)


def log_fallbacks(results):
    """One line: how many products of the profile-fit phase fell back to the 3-pixel
    boxcar, by reason."""
    from collections import Counter
    n = Counter(r['fallback'] for r in results if r.get('fallback'))
    what = ', '.join(f'{k} {v}' for k, v in sorted(n.items())) if n else 'none'
    log(f"Profile fit: {len(results)} products, fallbacks to the 3-pixel boxcar: {what}")
    return n


def _prism_xdisp(obs, r, results, tol):
    """A PRISM product's neighbour against the grating scans of the same source (this
    run's results, else the headers of earlier runs' products: their ``_pf.fits`` or,
    once extracted, ``_spec.fits``): 'single' when no grating product exists, 'none' when
    the PRISM scan found nothing, 'agree' when a grating scan finds the neighbour within
    ``tol`` of the same Delta, else 'disagree'."""
    from campfire_pipeline.nirspec.profile_product import read_scan
    sid = r['source_id']
    others = [(g['scan'] or {}) for g in results if g['source_id'] == sid and not g.get('is_prism')]
    found = [(bool(s.get('detected')), float(s.get('delta', np.nan))) for s in others if s]
    if not others:
        products = set()
        for sfx in ('_pf.fits', '_spec.fits'):
            for p in glob.glob(os.path.join(obs.workspace_dir, f'{obs.name}_*_{sid}{sfx}')):
                products.add(os.path.basename(p)[:-len(sfx)])
        for prod in sorted(products - {r['product']}):
            s = read_scan(obs.workspace_dir, prod)
            if s is not None:
                found.append(s)
    if not found:
        return 'single'
    if not (r.get('scan') or {}).get('detected'):
        return 'none'
    d = r['scan']['delta']
    return 'agree' if any(det and np.isfinite(x) and abs(x - d) <= tol for det, x in found) else 'disagree'


def _update_prism_scan_cards(obs, r, xdisp):
    """Write the cross-disperser result into a PRISM product's ``_pf.fits``: the
    grating ran after it. A disagreeing grating scan withdraws its proposals (``NBRPROP``)."""
    path = os.path.join(obs.workspace_dir, f"{r['product']}_pf.fits")
    if not os.path.exists(path):
        return
    with fits.open(path, mode='update') as h:
        hd = h[0].header
        if 'NBRXDISP' not in hd:
            return
        XC.put(hd, 'NBRXDISP', xdisp)
        if xdisp == 'disagree':
            XC.put(hd, 'NBRPROP', '')


def _proposal_notes(path):
    """The comment of each entry of a proposed-overrides file (``write_scan_proposals``
    writes one per entry: offset, statistic, PRISM check, date): {(root, source id): note}.
    TOML readers drop comments, so a rewrite keeps the others' this way."""
    from campfire_pipeline.nirspec.stage2 import proposal_entries
    return {(root, sid): note for root, sid, _, note in proposal_entries(path) if note}


def neighbour_figure_path(obs, source_id):
    """The review figure of a source's neighbour-scan proposal (``write_scan_proposals``)."""
    return os.path.join(obs.workspace_dir, f'{obs.name}_{int(source_id)}_neighbor.pdf')


def scanned_roots(pf_tasks):
    """The (exposure root, source id) pairs a run's products scan: a proposal is keyed by
    root, so a source's entries for a disperser not run this time are kept."""
    return {('_'.join(os.path.basename(f).split('_')[:2]), int(t['source_id']))
            for t in pf_tasks for g in t['groups'] for f in g}


def write_scan_proposals(obs, results, scanned, xdisp_tol=0.2):
    """Neighbour-scan proposals to ``nodded_background_overrides_proposed.toml`` in the
    observation's reference directory (never applied: an entry is used once copied
    into ``nodded_background_overrides.toml``), and one review figure per proposed
    source (``neighbour_figure_path``, ``plots.plot_neighbour_proposal``: its nod
    differences with the neighbour marked, and its spectrum with the background nods it
    subtracts now and with the proposed ones). Entries of the (root, source) pairs this
    run scanned (``scanned``, ``scanned_roots``) are replaced; others are kept. A
    source's figure is redrawn from this run's products when they propose; otherwise it
    is removed, unless the source keeps entries this run did not scan. Where a source has both PRISM
    and grating products, proposals are written only when both scans find the
    neighbour at the same Delta (``xdisp_tol``; the grating product checks this itself,
    ``ScanResult.cross_check``; PRISM proposals are checked here, when the grating scans
    are known)."""
    import toml
    from datetime import date
    from campfire_pipeline.nirspec.plots import plot_neighbour_proposal

    for r in results:
        s = r.get('scan') or {}
        if not (r.get('is_prism') and s):
            continue
        xd = _prism_xdisp(obs, r, results, xdisp_tol) if xdisp_tol >= 0 else 'single'
        if xd != 'single':
            _update_prism_scan_cards(obs, r, xd)
            s['xdisp'] = xd                              # recorded with the proposal (and the bkgsub plot)
        if s.get('detected') and s.get('proposals') and xd == 'disagree':
            log(f"{r['product']}: neighbour proposals withheld (grating scan disagrees)")
            s['proposals'] = {}
    path = obs.proposed_bkg_override_file
    old = toml.load(path) if os.path.exists(path) else {}
    notes = _proposal_notes(path)
    entries, kept = {}, set()
    for root, d in old.items():
        for sid, nods in d.items():
            if (root, int(sid)) not in scanned:
                entries.setdefault(root, {})[str(sid)] = ({str(k): list(v) for k, v in nods.items()},
                                                          notes.get((root, str(sid)), ''))
                kept.add(int(sid))
    for r in results:
        s = r.get('scan') or {}
        if not s.get('detected'):
            continue
        note = f"Delta={s['delta']:+.2f} dchi2={s['dchi2']:.0f} prism={s.get('xdisp', 'single')} {date.today().isoformat()}"
        if s.get('unresolved'):
            note += f" unresolved={','.join(s['unresolved'])}"
        for root, nods in s['proposals'].items():
            entries.setdefault(root, {})[str(r['source_id'])] = (nods, note)
    lines = ['# proposed nodded-background overrides from the same-slitlet neighbour scan (cfpipe stage 3);',
             '# never applied. Review <obs>_<source id>_neighbor.pdf in the workspace, copy an entry to',
             '# nodded_background_overrides.toml to use it, then rerun stage 2b and stage 3 for that source']
    for root in sorted(entries):
        lines.append(f'\n[{root}]')
        for sid in sorted(entries[root], key=int):
            nods, note = entries[root][sid]
            body = ', '.join(f'{k} = [{", ".join(str(int(x)) for x in v)}]' for k, v in sorted(nods.items()))
            lines.append(f'    {sid} = {{{body}}}' + (f'   # {note}' if note else ''))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')

    # review figures: one per source proposed in this run, from every product of it that proposes
    previews = {}
    for r in results:
        s = r.get('scan') or {}
        if s.get('detected') and s.get('proposals') and s.get('preview') is not None:
            previews.setdefault(int(r['source_id']), []).append((r['product'], s))
    for sid in {s for _, s in scanned}:
        stale = neighbour_figure_path(obs, sid)
        if os.path.exists(stale) and (sid in previews or sid not in kept):
            os.remove(stale)
    for sid, prods in sorted(previews.items()):
        try:
            plot_neighbour_proposal(neighbour_figure_path(obs, sid), sid, prods)
        except Exception as e:                           # noqa: BLE001 - a review figure must not fail the stage
            log(f'{obs.name} {sid}: neighbour proposal figure failed ({type(e).__name__}: {e})')
    proposed = sorted({int(r['source_id']) for r in results
                       if (r.get('scan') or {}).get('detected') and (r.get('scan') or {}).get('proposals')})
    if proposed:
        ids = ' '.join(str(s) for s in proposed)
        log(f"Neighbour scan: {len(proposed)} sources with proposals in {path} "
            f"(figures: {len(previews)}, {obs.name}_<id>_neighbor.pdf in the workspace). To use one, copy its "
            f"entry to nodded_background_overrides.toml and run:\n"
            f"  cfpipe nirspec run --obs {obs.name} --stage2b --stage3 --source-ids {ids} --overwrite")
    else:
        log('Neighbour scan: no proposals in this run')


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


def extract_columns(s2d_sci, s2d_err, s2d_mask, ap_start, ap_stop, wave, pf_file=None, is_fixed_slit=False,
                    label=''):
    """Every 1-D extraction of one 2-D spectrum (the nodded one or the master-background
    one, ``mbkg_extraction``): the optimal extraction, the 3/4/5-pixel boxcars and, with
    ``pf_file``, the profile extraction and the best spectrum ``fnu``
    (``_profile_fit_columns``); without it, ``fnu`` is the optimal extraction.

    ``s2d_sci`` / ``s2d_err`` in uJy; ``ap_start`` / ``ap_stop`` the x1d aperture rows.
    Returns dict(spec1d, prof1d (astropy Tables), cards (EXTRACT and the profile-fit
    cards), pf_hdus (PROF2D, CONTAM2D), choice, optext_status, collapsed, cen,
    profiles (for the plot))."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        collapsed = np.nanmedian(s2d_sci, axis=1)
    cen = (ap_start + ap_stop) / 2
    profile_opt = optext_profile(collapsed, ap_start, ap_stop, bounded=not is_fixed_slit)
    corrupted, n_pos, pos_frac = optext_profile_is_corrupted(collapsed, ap_start, ap_stop)
    if corrupted or all(np.isnan(profile_opt)):
        log(f"Optimal-extraction profile is corrupted for {label} "
            f"(n_positive={n_pos}, positive_fraction={pos_frac:.2f}); "
            f"falling back to 3px boxcar")
        profile_opt = boxcar_profile(cen-1.5, cen+1.5, len(collapsed))
        optext_status = 'boxcar-3px'
        opt_label = 'Optimal (3px boxcar fallback)'
    else:
        optext_status = 'optimal'
        opt_label = 'Optimal'

    fnu_opt, fnu_opt_err = extract_with_profile(profile_opt, s2d_sci, s2d_err, mask=s2d_mask, ivw=True)

    # 3 pixel boxcar, centered on the expected position of the source
    profile_3px, fnu_3px, fnu_3px_err = boxcar3(s2d_sci, s2d_err, s2d_mask, cen)

    # 4 pixel boxcar, centered on the expected position of the source
    profile_4px = boxcar_profile(cen-2.0, cen+2.0, len(collapsed))
    fnu_4px, fnu_4px_err = extract_with_profile(profile_4px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    # 5 pixel boxcar, centered on the expected position of the source
    profile_5px = boxcar_profile(cen-2.5, cen+2.5, len(collapsed))
    fnu_5px, fnu_5px_err = extract_with_profile(profile_5px, s2d_sci, s2d_err, mask=s2d_mask, ivw=False)

    # Profile-fit extraction ([nirspec.stage3].profile_fit): fnu_prof, fnu_contam and the
    # choice of the best spectrum fnu. Without it, fnu is the optimal extraction.
    pf, pf_hdus = None, []
    if pf_file is not None:
        if not os.path.exists(pf_file):
            raise FileNotFoundError(f'{label}: profile_fit is on but {pf_file} was not written')
        pf = _profile_fit_columns(pf_file, s2d_sci, s2d_err, s2d_mask, ap_start, ap_stop, wave)
        cols = pf['columns']
        fnu_best, fnu_best_err = best_spectrum(pf['choice'], cols['fnu_prof'][0], cols['fnu_prof_err'][0],
                                               fnu_3px, fnu_3px_err)
        cards, pf_hdus, choice = pf['header'], pf['hdus'], pf['choice']
    else:
        fnu_best, fnu_best_err = fnu_opt, fnu_opt_err
        choice = 'optimal'
        cards = extraction_cards('optimal', 'profile_fit_off', 'off')

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

    prof1d = Table()
    prof1d['ypos'] = np.arange(len(collapsed))+0.5
    prof1d['opt'] = profile_opt
    prof1d['3px'] = profile_3px
    prof1d['4px'] = profile_4px
    prof1d['5px'] = profile_5px
    profiles = {opt_label: profile_opt, '3px boxcar': profile_3px, '4px boxcar': profile_4px,
                '5px boxcar': profile_5px}
    return dict(spec1d=spec1d, prof1d=prof1d, cards=cards, pf_hdus=pf_hdus, choice=choice,
                optext_status=optext_status, collapsed=collapsed, cen=cen, profiles=profiles)


MBKG_EXTENSIONS = ('SPEC1D_MBKG', 'SCI_MBKG', 'ERR_MBKG', 'PROF1D_MBKG')


def mbkg_spectrum_hdus(product_name, workspace_dir, wave, sci_shape, ap_start, ap_stop, s2dh, exposures,
                       primary_hdr, is_fixed_slit, profile_fit, extended_frac):
    """The master-background spectrum of a product (``mbkg_extraction``): its 2-D
    spectrum and every 1-D extraction of it, made exactly as the nodded ones
    (``extract_columns`` on the master-background 2-D, drizzled through the nodded
    product's pixel maps onto its grid, with the nodded aperture rows and the nodded
    ``wave``, so the deploy contract's identical wave holds), as ``SPEC1D_MBKG``
    (SPEC1D's columns on the same ``wave``; its header carries the extraction cards,
    ``MBKGVER`` and the per-exposure ``MBKGSRC``), ``SCI_MBKG`` / ``ERR_MBKG`` (SCI's grid)
    and ``PROF1D_MBKG``; and the primary cards ``MBKGPREF`` / ``MBKGWHY``
    (``mbkg_extraction.preferred``, from the exposures and the nodded fit's ``SELFSUB``)
    and ``MBKGNEXP``.

    Returns (hdus, cards). Named conditions return ([], {}) and log why: no
    master-background 2-D (an exposure without a master background, or no crf), or a
    2-D of another shape than the nodded SCI (``sci_shape``; the grid is the nodded one
    by construction, so only a stale file could)."""
    from campfire_pipeline.nirspec import mbkg_extraction as MX

    paths = MX.product_paths(workspace_dir, product_name, 0)
    mb = _read_mbkg_2d(paths['s2d'], sci_shape, product_name)
    if mb is None:
        return [], {}
    mb_sci, mb_err, mb_mask = mb
    ex = extract_columns(mb_sci * 1e12, mb_err * 1e12, mb_mask, ap_start, ap_stop, np.asarray(wave),
                         pf_file=paths['pf'] if profile_fit else None, is_fixed_slit=is_fixed_slit,
                         label=f'{product_name} (master background)')
    spec = table_to_hdu(ex['spec1d'])
    for key in ex['cards']:
        spec.header[key] = (ex['cards'][key], ex['cards'].comments[key])
    return _mbkg_hdus(spec, table_to_hdu(ex['prof1d']), mb_sci, mb_err, s2dh, exposures, workspace_dir,
                      primary_hdr.get('SELFSUB'), extended_frac)


def _read_mbkg_2d(path, sci_shape, product_name, what='master-background 2-D'):
    """A product's master-background 2-D (``mbkg_extraction.write_2d``): (sci, err, mask
    where no exposure has weight), or None, logged, when there is none or it is of
    another shape than the nodded SCI (``sci_shape``; the grid is the nodded one by
    construction, so only a stale file could)."""
    if not os.path.exists(path):
        log(f'{product_name}: no master-background spectrum (no {what})')
        return None
    with fits.open(path) as s2d:
        mb_sci = np.asarray(s2d['SCI'].data, float)
        mb_err = np.asarray(s2d['ERR'].data, float)
        mb_mask = s2d['WHT'].data == 0
    if mb_sci.shape != tuple(sci_shape):
        log(f'{product_name}: no master-background spectrum: its {what} {mb_sci.shape} differs from the nodded one '
            f'{tuple(sci_shape)}')
        return None
    return mb_sci, mb_err, mb_mask


def _mbkg_hdus(spec, prof, mb_sci, mb_err, s2dh, exposures, workspace_dir, selfsub, extended_frac):
    """The master-background extensions and primary cards of a product, from its 1-D
    columns (``spec``, the extraction cards already on it) and profiles (``prof``):
    ``SPEC1D_MBKG`` with ``MBKGVER`` and the per-exposure ``MBKGSRC``, ``SCI_MBKG`` /
    ``ERR_MBKG`` on the nodded SCI's header, ``PROF1D_MBKG``; ``MBKGPREF`` / ``MBKGWHY``
    (``mbkg_extraction.preferred``, with the nodded fit's ``selfsub``) and ``MBKGNEXP``.
    Returns (hdus, cards)."""
    from campfire_pipeline.nirspec import mbkg_extraction as MX
    sources, version = MX.background_sources(exposures.get('filename', []), workspace_dir)
    spec.name = 'SPEC1D_MBKG'
    spec.header['MBKGVER'] = (version, 'master-background algorithm version (CFMBKGV)')
    spec.header['MBKGSRC'] = (','.join(sources), 'per EXPOSURES row: combined / exposure')
    prof.name = 'PROF1D_MBKG'
    hdus = [spec, fits.ImageHDU(data=mb_sci.astype(np.float32), header=s2dh.copy(), name='SCI_MBKG'),
            fits.ImageHDU(data=mb_err.astype(np.float32), header=s2dh.copy(), name='ERR_MBKG'), prof]
    pref, why = MX.preferred(exposures, selfsub, extended_frac)
    cards = {'MBKGPREF': (bool(pref), 'master-background spectrum likely better'),
             'MBKGWHY': (why, 'Reason for MBKGPREF'),
             'MBKGNEXP': (int(sum(s == 'exposure' for s in sources)), 'exposures with their own master background')}
    return hdus, cards


def opt_ext_single_source(
      product_name,
      workspace_dir,
      optimal_extraction_profile = 'single', # or 'wavelength-dependent'
      plot_profiles = False,
      plot_optext = False,
      overwrite = False,
      version = 'v0.1',
      profile_fit = False,
      mbkg_spectrum = False,
      mbkg_extended_frac = 0.10,
    ):
    """
    Runs an optimal extraction routine and saves an msaexp-style "_spec.fits" file (combining 2D and 1D) for a single object.

    ``fnu`` is the best spectrum (header ``EXTRACT``): with ``profile_fit`` the profile
    extraction (other sources' predicted light reported in ``fnu_contam``), or the
    3-pixel boxcar on a named fallback (``_profile_fit_columns``);
    without it, the optimal extraction. ``fnu_opt`` is always the optimal extraction.

    ``NEXP`` / ``NEXPBKG`` count the exposures combined and the fewest background
    exposures subtracted from one of them. With ``mbkg_spectrum`` the master-background
    spectrum (``mbkg_extraction``) is added: ``mbkg_spectrum_hdus``.
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

    ph['CMPFRTIM'] = (datetime.now(timezone.utc).isoformat(), 'Reduction time, UTC (ISO 8601)')
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

    x1d_start = x1d['EXTRACT1D'].header['EXTRYSTR']-1
    x1d_stop = x1d['EXTRACT1D'].header['EXTRYSTP']
    wave = x1d['EXTRACT1D'].data['WAVELENGTH']
    pf_file = os.path.join(workspace_dir, f'{product_name}_pf.fits')
    ex = extract_columns(s2d_sci, s2d_err, s2d_mask, x1d_start, x1d_stop, wave,
                         pf_file=pf_file if profile_fit else None, is_fixed_slit=is_fixed_slit,
                         label=product_name)
    collapsed, cen = ex['collapsed'], ex['cen']
    primary.header['CMPFROPT'] = (ex['optext_status'], 'Optimal extraction: optimal or a fallback')
    if plot_profiles:
        from campfire_pipeline.nirspec.plots import plot_extraction_profiles
        plot_extraction_profiles(
            out_filename.replace('_spec.fits', '_prof.pdf'),
            collapsed, ex['profiles'], x1d_start, x1d_stop, cen,
        )
    cards, pf_hdus = ex['cards'], ex['pf_hdus']
    for key in cards:
        primary.header[key] = (cards[key], cards.comments[key])

    spec1d = table_to_hdu(ex['spec1d'])
    spec1d.name = 'SPEC1D'

    prof1d = table_to_hdu(ex['prof1d'])
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

    from campfire_pipeline.nirspec import mbkg_extraction as MX
    exposures = {c: list(exp.data[c]) for c in exp.data.columns.names}
    nexp, nexp_bkg = MX.exposure_counts(exposures)
    primary.header['NEXP'] = (nexp, 'Exposures combined')
    from campfire_pipeline.nirspec.variance import stamps_of
    primary.header['CFVARPS'] = (stamps_of([os.path.join(workspace_dir, str(f).strip()) for f in exposures.get('filename', [])]),
                                 'rate Poisson variance')
    if nexp_bkg is not None:
        primary.header['NEXPBKG'] = (nexp_bkg, 'Fewest background exposures subtracted from one')
    mbkg = []
    if mbkg_spectrum:
        mbkg, mcards = mbkg_spectrum_hdus(product_name, workspace_dir, wave, s2d['SCI'].data.shape, x1d_start,
                                          x1d_stop, s2dh, exposures, primary.header, is_fixed_slit, profile_fit,
                                          mbkg_extended_frac)
        for key, value in mcards.items():
            primary.header[key] = value

    spec = fits.HDUList(hdus=[primary, spec1d, sci, err, wav, wht] + pf_hdus + [prof1d, exp] + mbkg)
    spec.writeto(out_filename, overwrite=True)
    # every product's profile fit (cross-disperser scan included) finished before
    # extraction began; later runs read the shape cards from the _spec.fits
    if profile_fit and os.path.exists(pf_file):
        os.remove(pf_file)
    if mbkg_spectrum:
        MX.cleanup(workspace_dir, product_name)


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
    """fnu_prof / fnu_contam (with errors and flam twins)."""
    for sfx, desc in PF_FLUX_COLUMNS:
        _add_flux_columns(table, sfx, columns[f'fnu{sfx}'][0], columns[f'fnu{sfx}_err'][0], wave, desc)


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
        contam = float(np.nansum(cflux[ok]) / tot) if tot > 0 else np.nan
        sn = ok & (np.abs(flux) > 3 * flux_err)
        frac = np.abs(cflux[sn] / flux[sn])
        conmax = float(np.percentile(frac, 95)) if frac.size else np.nan
        # the prediction in units of the noise of the integrated (inverse-variance) flux
        okn = ok & np.isfinite(flux_err) & (flux_err > 0)
        ivar = 1.0 / flux_err[okn] ** 2
        csig = float(abs(np.sum(cflux[okn] * ivar)) / np.sqrt(np.sum(ivar))) if okn.any() else 0.0
    return dict(cflux=cflux, cerr=cerr, predicted=predicted, contam=contam, conmax=conmax, csig=csig)


# Which extraction is the best spectrum ``fnu`` of a product (header EXTRACT), and why
# (EXTRWHY). 'optimal' only when the profile fit is not configured (profile_fit = false).
EXTRACTIONS = ('profile', 'boxcar3', 'optimal')
EXTRACT_REASONS = {
    'contam_predicted': 'profile: other light predicted, reported in fnu_contam (never subtracted)',
    'contam_none': 'profile: no other light predicted on the extraction',
    'contam_off': 'profile: no prediction (contam_predict off, or no trace maps)',
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


def choose_extraction(have_contam, any_predicted):
    """(EXTRACT, EXTRWHY) of a product whose profile fit ran: always the profile
    extraction as it is; the reason says whether other light is predicted on it
    (``fnu_contam``, for the user to subtract or not)."""
    if not have_contam:
        return 'profile', 'contam_off'
    if not any_predicted:
        return 'profile', 'contam_none'
    return 'profile', 'contam_predicted'


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


def best_spectrum(choice, prof, prof_err, box, box_err):
    """The best spectrum ``fnu`` and its error for an extraction choice (statistical)."""
    if choice == 'boxcar3':
        return box, box_err
    if choice == 'profile':
        return prof, prof_err
    raise ValueError(f'no best spectrum for extraction {choice!r}')


def _profile_fit_columns(pf_file, s2d_sci, s2d_err, s2d_mask, ap_start, ap_stop, wave):
    """Extraction of the s2d with the profile-fit 2D profile, and the choice of the best
    spectrum.

    PROF2D is the drizzled source model per unit reported flux (uJy, against the s2d
    SCI in MJy): it is not normalised per column, so its in-aperture sum carries
    the aperture and path-loss correction. The weighted fit of the data on the
    aperture rows, sum(P D / V) / sum(P^2 / V), is the reported flux directly: the
    source's light inside ``profile_fit.REPORT_APERTURE`` (``reported_factor``), with the
    model's loss across the slit and its along-dispersion correction. ``fnu_opt`` stays on
    jwst's point-source calibration: for a point source at the shutter centre the two
    agree up to the along-dispersion correction; off centre they differ by the ratio of
    jwst's loss across the slit to the model's (about 20 % for a source planned 0.4
    shutter pitches from the centre); an extended source's ``fnu_opt`` is the light in its shutter.
    ``fnu_prof`` uses the x1d aperture rows.

    Other sources' light (``CONTAM2D``, its uncertainty ``CONTERR2D``) is extracted with
    the same weights: ``fnu_contam`` (NaN where nothing is predicted) and
    ``fnu_contam_err`` (systematic), integrated as ``CONTFRAC`` and
    ``CONTSIG``. It is reported, never subtracted from ``fnu`` (``choose_extraction``).

    A ``_pf.fits`` without PROF2D is a named fallback (``profile_product.write_fallback``):
    the best spectrum is the 3-pixel boxcar.

    Returns dict(columns={name: (data, description)} of fnu_prof / fnu_contam and their
    errors, header, hdus (PROF2D, CONTAM2D), choice, reason).
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
        NS = h['NOTSEP'].data if 'NOTSEP' in h else None
        if NS is not None:
            NS = (np.asarray(NS['lam'], float), np.asarray(NS['notsep'], bool))
    if P.shape != s2d_sci.shape:
        raise ValueError(f"{os.path.basename(pf_file)}: profile {P.shape} does not match the s2d {s2d_sci.shape}")
    x = np.arange(P.shape[0] + 1)
    in_ap = (x[:-1] > ap_start) & (x[1:] <= ap_stop)
    Pm = np.where(np.isfinite(P), P * 1e12, np.nan)       # per uJy, against s2d_sci in uJy units
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
        'fnu_prof': (flux, 'Profile-fit flux (PROF2D weights, aperture rows), nothing subtracted'),
        'fnu_prof_err': (flux_err, 'Profile-fit flux error (statistical)'),
    }
    keep = fits.Header()
    XC.copy(header, keep)
    hdus = []
    prof = fits.ImageHDU(P, name='PROF2D')
    prof.header['BUNIT'] = ('MJy / uJy', 'SCI per unit reported flux (not per column)')
    hdus.append(prof)
    if C2D is None or C2D.shape != P.shape:
        columns['fnu_contam'] = (nan, 'Predicted other-source flux (no prediction)')
        columns['fnu_contam_err'] = (nan, 'Its uncertainty (systematic)')
        choice, reason = choose_extraction(False, False)
        keep.update(extraction_cards(choice, reason, 'off'))
        return dict(columns=columns, header=keep, hdus=hdus, choice=choice, reason=reason)
    V = s2d_err ** 2
    main = _contam_1d(C2D, E2D, Pm, weight_rows, V, flux, flux_err)
    cflux, cerr, csig, predicted = (main[k] for k in ('cflux', 'cerr', 'csig', 'predicted'))
    XC.put(keep, 'CONTFRAC', main['contam'])
    XC.put(keep, 'CONTSIG', csig)
    notsep = _notsep_on(NS, wave)
    if NS is not None:
        XC.put(keep, 'CONTNSEP', float(notsep[np.isfinite(flux)].mean()) if np.isfinite(flux).any() else 0.0)
    predicted = predicted & ~notsep
    columns['fnu_contam'] = (np.where(predicted, cflux, np.nan),
                             'Predicted other-source flux in the fnu_prof extraction '
                             '(NaN: none predicted, or not separable)')
    columns['fnu_contam_err'] = (np.where(predicted, cerr, np.nan), 'Its uncertainty (systematic)')
    choice, reason = choose_extraction(True, bool(np.any(predicted)))
    keep.update(extraction_cards(choice, reason, 'predicted' if np.any(predicted) else 'none'))
    c = fits.ImageHDU(C2D, name='CONTAM2D')
    c.header['BUNIT'] = ('MJy', 'Predicted other-source light, as SCI')
    hdus.append(c)
    return dict(columns=columns, header=keep, hdus=hdus, choice=choice, reason=reason)


def _combined_profile_fit(prof, groups_pf, exposure_times, common_wave, fnu_prof, fnu_prof_err):
    """The profile-fit columns and cards of a 1-D combined product.

    prof : the groups' ``fnu_prof`` (``per_eg_data['prof']``); groups_pf : per group its
    ``fnu_contam`` / ``fnu_contam_err`` and EXTRACT / EXTRWHY / CONTSTAT; fnu_prof /
    fnu_prof_err : the combined profile extraction.
    ``fnu_contam`` is combined with the exposure-time weights of ``fnu_prof`` (no
    clipping; a group with nothing predicted adds zero light), its error linearly (the
    predictions of the groups come from the same pass-1 spectra). The best spectrum is
    chosen by ``choose_extraction`` on the combined spectra, with ``CONTSIG``
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
        choice, reason = choose_extraction(states != {'off'}, bool(predicted.any()))
    state = 'off' if states <= {'off'} else 'predicted' if 'predicted' in states else 'none'
    XC.put(keep, 'CONTSIG', csig)
    keep.update(extraction_cards(choice, reason, state))
    return dict(columns=columns, header=keep, hdus=[], choice=choice, reason=reason)


def _combined_spec1d(common_wave, combined, fnu_best, fnu_best_err, choice, pf_columns):
    """The 1D-combined ``SPEC1D`` table (as an HDU, unnamed): ``combined`` maps 'opt',
    '3px', '4px', '5px' (and 'prof') to (flux, error, n) on ``common_wave``."""
    fnu_opt, fnu_opt_err, n_opt = combined['opt']
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
    return table_to_hdu(spec1d)


def _combined_mbkg(per_eg_spec_files, workspace_dir, product_name, source_id, common_wave,
                   exposure_times, sci_shape, ap_start, ap_stop, s2dh, exposures, is_fixed_slit,
                   combine_kw, extended_frac):
    """The master-background spectrum of a 1D-combined product, made as its nodded
    spectrum is: every group's ``SPEC1D_MBKG`` column combined onto ``common_wave``
    (exposure-time weights, the same clipping), the best spectrum chosen again on the
    combined spectra (``_combined_profile_fit``), and the 2-D spectrum drizzled through
    the stacked product's pixel maps with its Spec3 (``run_stage3_single_source``,
    ``mbkg_extraction.write_2d``), on the nodded stacked grid by construction
    (``sci_shape`` checks it). Returns (hdus, cards) like ``mbkg_spectrum_hdus``; ([], {})
    with a log line when a group has no master-background spectrum or there is no
    stacked master-background 2-D."""
    from campfire_pipeline.nirspec import mbkg_extraction as MX

    keys = {'opt': ('fnu_opt', 'fnu_opt_err'), '3px': ('fnu_3px', 'fnu_3px_err'), '4px': ('fnu_4px', 'fnu_4px_err'),
            '5px': ('fnu_5px', 'fnu_5px_err')}
    names = []
    for spec_file in per_eg_spec_files:
        with fits.open(os.path.join(workspace_dir, spec_file)) as h:
            if 'SPEC1D_MBKG' not in h:
                log(f'{product_name}: no master-background spectrum ({spec_file} has none)')
                return [], {}
            names.append(set(h['SPEC1D_MBKG'].columns.names))
    if names and all('fnu_prof' in n for n in names):          # as the nodded combine (combine_per_eg_spectra)
        keys['prof'] = ('fnu_prof', 'fnu_prof_err')
    data = {k: {'wavelengths': [], 'fluxes': [], 'errors': []} for k in keys}
    groups_pf, selfsub = [], []
    for spec_file in per_eg_spec_files:
        with fits.open(os.path.join(workspace_dir, spec_file)) as h:
            t = h['SPEC1D_MBKG'].data
            for k, (fc, ec) in keys.items():
                data[k]['wavelengths'].append(np.array(t['wave']))
                data[k]['fluxes'].append(np.array(t[fc]))
                data[k]['errors'].append(np.array(t[ec]))
            if 'prof' in keys:
                hh = h['SPEC1D_MBKG'].header
                groups_pf.append(dict(contam=np.array(t['fnu_contam'], float), contam_err=np.array(t['fnu_contam_err'], float),
                                      extract=str(hh.get('EXTRACT', '')), reason=str(hh.get('EXTRWHY', '')),
                                      state=str(hh.get('CONTSTAT', 'off'))))
            selfsub.append(h[0].header.get('SELFSUB'))
    paths = MX.product_paths(workspace_dir, product_name, source_id)
    mb = _read_mbkg_2d(paths['s2d'], sci_shape, product_name, what='stacked master-background 2-D')
    if mb is None:
        return [], {}
    mb_sci, mb_err, _ = mb
    combined = {k: combine_1d_spectra(v['wavelengths'], v['fluxes'], v['errors'], exposure_times, common_wave,
                                      **combine_kw) for k, v in data.items()}
    fnu_3px, fnu_3px_err, _ = combined['3px']
    pf_columns, choice = None, 'optimal'
    hdr_cards = extraction_cards('optimal', 'profile_fit_off', 'off')
    if 'prof' in combined:
        fnu_prof, fnu_prof_err, _ = combined['prof']
        pf = _combined_profile_fit(data['prof'], groups_pf, exposure_times, common_wave, fnu_prof, fnu_prof_err)
        pf_columns, hdr_cards, choice = pf['columns'], pf['header'], pf['choice']
        fnu_best, fnu_best_err = best_spectrum(choice, fnu_prof, fnu_prof_err, fnu_3px, fnu_3px_err)
    else:
        fnu_best, fnu_best_err = combined['opt'][0], combined['opt'][1]
    spec = _combined_spec1d(common_wave, combined, fnu_best, fnu_best_err, choice, pf_columns)
    for key in hdr_cards:
        spec.header[key] = (hdr_cards[key], hdr_cards.comments[key])
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        collapsed = np.nanmedian(mb_sci * 1e12, axis=1)
    cen = (ap_start + ap_stop) / 2
    prof = Table()
    prof['ypos'] = np.arange(len(collapsed)) + 0.5
    prof['opt'] = optext_profile(collapsed, ap_start, ap_stop, bounded=not is_fixed_slit)
    prof['3px'] = boxcar_profile(cen - 1.5, cen + 1.5, len(collapsed))
    prof['4px'] = boxcar_profile(cen - 2.0, cen + 2.0, len(collapsed))
    prof['5px'] = boxcar_profile(cen - 2.5, cen + 2.5, len(collapsed))
    ss = [float(v) for v in selfsub if v is not None and np.isfinite(float(v))]
    return _mbkg_hdus(spec, table_to_hdu(prof), mb_sci, mb_err, s2dh, exposures, workspace_dir,
                      max(ss) if ss else None, extended_frac)


def combine_per_eg_spectra(
    per_eg_spec_files,
    all_cal_files,
    workspace_dir, product_name, source_id, filter_grating,
    sigma_clip=True, sigma_clip_low=3.0, sigma_clip_high=3.0,
    sigma_clip_maxiters=5, plot_profiles=True, plot_optext=True, version='v0.1',
    mbkg_spectrum=False, mbkg_extended_frac=0.10,
):
    """
    1D combination: combine per-exp_group 1D spectra (exposure-time weighted) + produce stacked 2D.

    Every extraction column is combined (``fnu_opt``, the boxcars, ``fnu_prof``); the
    best spectrum ``fnu`` is chosen again on the combined spectra by the per-product rule
    (``_combined_profile_fit``), never combined from the groups' ``fnu``. With
    ``mbkg_spectrum`` the groups' master-background spectra are combined the same way
    (``_combined_mbkg``); ``NEXP`` / ``NEXPBKG`` are written either way.

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

    # --- Step 2: Stacked 2D via Spec3Pipeline on ALL canonical files (and the stacked
    # master-background 2-D through its pixel maps) ---
    run_stage3_single_source(all_cal_files, workspace_dir, product_name, source_id, mbkg_spectrum=mbkg_spectrum)

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
    ph['CMPFRTIM'] = (datetime.now(timezone.utc).isoformat(), 'Reduction time, UTC (ISO 8601)')
    ph['CMPFRVER'] = (version, 'campfire-pipeline version (PEP 440)')
    ph['CMPFRSTG'] = ('stage3-1d', 'CAMPFIRE stage that produced this file')
    ph['CMPFROPT'] = (optext_status, 'Optimal extraction: optimal or a fallback')
    ph['NCOMBINE'] = (len(per_eg_spec_files), 'Number of per-exp_group spectra combined')

    fnu_opt, fnu_opt_err, n_opt = combined['opt']
    fnu_3px, fnu_3px_err, _ = combined['3px']
    pf_columns, pf_hdus = None, []
    if has_pf:
        fnu_prof, fnu_prof_err, _ = combined['prof']
        pf = _combined_profile_fit(per_eg_data['prof'], groups_pf, exposure_times, common_wave, fnu_prof,
                                   fnu_prof_err)
        pf_columns, pf_hdus = pf['columns'], pf['hdus']
        cards, choice = pf['header'], pf['choice']
        fnu_best, fnu_best_err = best_spectrum(choice, fnu_prof, fnu_prof_err, fnu_3px, fnu_3px_err)
    else:
        choice = 'optimal'
        cards = extraction_cards('optimal', 'profile_fit_off', 'off')
        fnu_best, fnu_best_err = fnu_opt, fnu_opt_err
    for key in cards:
        ph[key] = (cards[key], cards.comments[key])
    primary = fits.PrimaryHDU(header=ph)

    spec1d_hdu = _combined_spec1d(common_wave, combined, fnu_best, fnu_best_err, choice, pf_columns)
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

    from campfire_pipeline.nirspec import mbkg_extraction as MX
    exposures = {c: list(exp_hdu.data[c]) for c in exp_hdu.data.columns.names}
    nexp, nexp_bkg = MX.exposure_counts(exposures)
    primary.header['NEXP'] = (nexp, 'Exposures combined')
    from campfire_pipeline.nirspec.variance import stamps_of
    primary.header['CFVARPS'] = (stamps_of([os.path.join(workspace_dir, str(f).strip()) for f in exposures.get('filename', [])]),
                                 'rate Poisson variance')
    if nexp_bkg is not None:
        primary.header['NEXPBKG'] = (nexp_bkg, 'Fewest background exposures subtracted from one')
    mbkg = []
    if mbkg_spectrum:
        mbkg, mcards = _combined_mbkg(
            per_eg_spec_files, workspace_dir, product_name, source_id, common_wave, exposure_times,
            s2d['SCI'].data.shape, x1d_start, x1d_stop, s2dh, exposures, is_fixed_slit,
            dict(sigma_clip_enabled=sigma_clip, sigma_clip_low=sigma_clip_low, sigma_clip_high=sigma_clip_high,
                 sigma_clip_maxiters=sigma_clip_maxiters),
            mbkg_extended_frac)
        for key, value in mcards.items():
            primary.header[key] = value

    out_filename = os.path.join(workspace_dir, f'{product_name}_spec.fits')
    # the groups' PROF2D / CONTAM2D are on their own s2d grids, not the stacked one: not carried
    spec = fits.HDUList(hdus=[primary, spec1d_hdu, sci_hdu, err_hdu, wav_hdu, wht_hdu] + pf_hdus + [prof1d_hdu, exp_hdu]
                        + mbkg)
    spec.writeto(out_filename, overwrite=True)
    log(f"Stage3 (1D combine): wrote {out_filename}")
    if mbkg_spectrum:
        MX.cleanup(workspace_dir, product_name)

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
