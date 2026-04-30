"""
Stage 1: Detector1Pipeline processing, background subtraction, and slit masking.
"""

import os
import shutil
import warnings
import numpy as np
from datetime import datetime
from astropy.io import fits

from campfire_pipeline.common.io import log
from campfire_pipeline.common.wcs import boundingbox_to_indices, wcs_to_dq


def run_stage1(obs, stage_config, n_processes=1, overwrite=False, data_dir=None, products_dir=None):
    """Orchestrate stage 1: Detector1Pipeline + background subtraction.

    Parameters
    ----------
    obs : Observation
        Observation to process.
    stage_config : dict
        Merged stage1 configuration (from get_stage_config).
    n_processes : int
        Number of parallel workers.
    overwrite : bool
        Overwrite existing products.
    data_dir, products_dir : str
        Used for workspace setup if not already done.
    """
    from campfire_pipeline.common.parallel import dispatch

    log(f"Stage 1 config for {obs.name}: {stage_config}")

    if not obs.directories_setup:
        obs.setup_workspace_directory(data_dir, products_dir, overwrite=overwrite)

    obs.discover_raw_files()
    obs.copy_uncal_files(overwrite=overwrite)

    uncal_files = obs.glob("_uncal.fits")
    if not overwrite:
        uncal_files = [f for f in uncal_files if not os.path.exists(f.replace('_uncal.fits', '_rate.fits'))]

    uncal_files = [os.path.basename(f) for f in uncal_files]

    kwargs = dict(
        do_clean_flicker_noise=stage_config['do_clean_flicker_noise'],
        mask_science_regions=stage_config['mask_science_regions'],
        cleanup_uncal=stage_config['cleanup_uncal'],
        cleanup_rateints=stage_config['cleanup_rateints'],
    )

    # Phase 1: Run Detector1Pipeline on all uncal files
    if n_processes > 1 and uncal_files:
        _prefetch_detector1_references(
            [os.path.join(obs.workspace_dir, f) for f in uncal_files],
            mask_science_regions=stage_config['do_clean_flicker_noise'] and stage_config['mask_science_regions'],
        )
    dispatch(
        run_stage1_single_uncal,
        uncal_files,
        n_processes=n_processes,
        workspace_dir=obs.workspace_dir,
        **kwargs,
    )

    # Phase 2: Background subtraction on resulting rate files
    bkg_kwargs = dict(
        override_wavelength_range=stage_config.get('override_wavelength_range', {}),
        subtract_2d=stage_config.get('subtract_2d', False),
        box_size=stage_config.get('box_size', 64),
        sigma_clip=stage_config.get('sigma_clip', True),
        bkg_estimator=stage_config.get('bkg_estimator', 'median'),
        do_col_1f=stage_config.get('do_col_1f', True),
        do_row_1f=stage_config.get('do_row_1f', True),
        col_1f_method=stage_config.get('col_1f_method', 'template'),
        plot=stage_config.get('plot', True),
        save_backup=False,
    )
    all_uncal_files = [os.path.basename(f) for f in obs.glob("_uncal.fits")]
    expected_rate_files = [
        os.path.join(obs.workspace_dir, f.replace('_uncal.fits', '_rate.fits'))
        for f in all_uncal_files
    ]
    rate_files = [f for f in expected_rate_files if os.path.exists(f)]
    missing = set(expected_rate_files) - set(rate_files)
    if missing:
        for f in sorted(missing):
            log(f"WARNING: Detector1Pipeline did not produce {os.path.basename(f)} — skipping background subtraction for this file")
    if not overwrite:
        rate_files = [f for f in rate_files if not os.path.exists(f.replace('_rate.fits', '_bkg.fits'))]
    if n_processes > 1 and rate_files:
        _prefetch_crds_references(rate_files)
    dispatch(
        subtract_background_from_rate_file,
        rate_files,
        n_processes=n_processes,
        **bkg_kwargs,
    )


def mask_slits(
        input_model,
        input_dir,
        mask,
        override_wavelength_range: dict = {}
    ):
    """
    Flag pixels within science regions.

    Find pixels located within MOS or fixed slit footprints
    and flag them in the mask, so that they do not get used.

    Adapted from jwst.clean_flicker_noise.clean_flicker_noise
    and jwst.msaflagopen.msaflag_open to extend the masks
    to cover the full traces

    Parameters
    ----------
    input_model : `~jwst.datamodels.JwstDataModel`
        Science data model.

    mask : array-like of bool
        2D input mask that will be updated. True indicates background
        pixels to be used. Slit regions will be set to False.

    Returns
    -------
    mask : array-like of bool
        2D output mask with additional flags for slit pixels
    """

    from jwst.clean_flicker_noise.clean_flicker_noise import _make_processed_rate_image
    from jwst.msaflagopen.msaflagopen_step import create_reference_filename_dictionary, MSAFlagOpenStep
    from jwst.msaflagopen.msaflag_open import create_slitlets
    from jwst.assign_wcs.nirspec import generate_compound_bbox, slitlets_wcs
    from gwcs.wcs import WCS
    from jwst.assign_wcs import AssignWcsStep

    filt = input_model.meta.instrument.filter
    grating = input_model.meta.instrument.grating
    filter_grating = filt + '_' + grating

    override_grating_wavelength_range = False
    if grating in override_wavelength_range:
        override_grating_wavelength_range = override_wavelength_range[grating]

    if override_grating_wavelength_range:
        import tempfile
        wavelengthrange_file = AssignWcsStep().get_reference_file(input_model, 'wavelengthrange')
        suffix = os.path.splitext(wavelengthrange_file)[1]
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix='wavelengthrange_')
        os.close(tmp_fd)
        shutil.copy2(wavelengthrange_file, tmp_path)
        wavelengthrange_file = tmp_path
        import asdf
        af = asdf.open(wavelengthrange_file, mode='rw')
        idx = af['waverange_selector'].index(filter_grating)
        af.tree['wavelengthrange'][idx] = [
            override_grating_wavelength_range[0]/1e6,
            override_grating_wavelength_range[1]/1e6
        ]
        log(f"Setting {filter_grating} wavelength range to: {af.tree['wavelengthrange'][idx]}")

        af.update()
        af.close()
        with asdf.open(wavelengthrange_file, 'r') as af:
            idx = af['waverange_selector'].index(filter_grating)
            log(f"Verified modified range for {filter_grating}: {af.tree['wavelengthrange'][idx]}")

        step = AssignWcsStep()
        step.input_dir = input_dir
        step.override_wavelengthrange = wavelengthrange_file
        processed_model = step.run(input_model)
    else:
        step = AssignWcsStep()
        step.input_dir = input_dir
        processed_model = step.run(input_model)

    # # processed_model = _make_processed_rate_image(model, single_mask=True, input_dir=os.path.dirname(rate_file), exp_type="NRS_MSASPEC", mask_science_regions=True, flat=None)

    msaoper_reffile = MSAFlagOpenStep().get_reference_file(processed_model, "msaoper")
    failed_slits = create_slitlets(msaoper_reffile)

    source_slits = processed_model.meta.wcs.get_transform("gwa", "slit_frame").slits
    # slits = slits + failed_slits

    wcs_reffile_names = create_reference_filename_dictionary(processed_model)

    temp_mask = np.zeros(input_model.data.shape,dtype=int)

    from jwst.datamodels import ImageModel
    for slits in [source_slits, failed_slits]:
        pipeline = slitlets_wcs(processed_model, wcs_reffile_names, slits)
        wcs = WCS(pipeline)

        meta_model = ImageModel()
        meta_model.meta.wcs = wcs
        meta_model.meta.wcsinfo = processed_model.meta.wcsinfo
        meta_model.meta.exposure.type = 'NRS_MSASPEC'
        meta_model.meta.wcs.bounding_box = generate_compound_bbox(meta_model, slits)

        for slitlet in slits:
            
            if slitlet.name in ["S200A1", "S200A2", "S400A1", "S1600A1", "S200B1"]:
                continue #override if fixed slits are present, as these are auto-masked anyways
            bbox = meta_model.meta.wcs.bounding_box[slitlet.name]
            xmin, xmax, ymin, ymax = boundingbox_to_indices(processed_model.data.shape, bbox)
            y_indices, x_indices = np.mgrid[ymin:ymax, xmin:xmax]
            ra, dec, lam, _ = meta_model.meta.wcs(x_indices, y_indices, slitlet.name)
            subarray = wcs_to_dq((ra,dec,lam), 1)
            temp_mask[..., ymin:ymax, xmin:xmax] += subarray

    mask[temp_mask>0] = False

    # Clean up per-process temp file if one was created
    if override_grating_wavelength_range:
        os.remove(wavelengthrange_file)

    return mask

        # if pictureframe_dir:
        #     log(f'Subtracting "picture frame" template files')
        #     if detector == 'nrs1':
        #         pictureframe_file = os.path.join(pictureframe_dir,'jwst_nirspec_pictureframe_0002.fits')
        #     else:
        #         pictureframe_file = os.path.join(pictureframe_dir,'jwst_nirspec_pictureframe_0001.fits')

        #     pictureframe_template = fits.getdata(pictureframe_file)

        #     # rescale the picture frame template so that its ~close to the data median
        #     pictureframe_template *= np.nanmedian(model.data[mask])

        #     coeffs = np.linspace(0.5, 1.5, 100)
        #     var = np.zeros_like(coeffs)
        #     for i,c in enumerate(coeffs):
        #         sub = model.data - c*pictureframe_template
        #         sub[~mask] = np.nan
        #         sigma_mad = median_absolute_deviation(sub, ignore_nan=True)
        #         var[i] = sigma_mad**2

        #     pictureframe_model = pictureframe_template * coeffs[np.argmin(var)]
        #     bkg_total += pictureframe_model

        # # Subtract pedestal in 4 horizontal quarters
        # if subtract_pedestal_quarters:
        #     log(f'Subtracting pedestal in 4 horizontal detector quarters')

        #     pedestal_model = np.zeros_like(model.data)

        #     # Define the 4 horizontal quarters (512 rows each)
        #     n_quarters = 4
        #     quarter_height = 2048 // n_quarters  # 512 rows

        #     for i in range(n_quarters):
        #         # Define the row range for this quarter
        #         row_start = i * quarter_height
        #         row_end = (i + 1) * quarter_height

        #         # Extract this quarter's data and mask
        #         quarter_data = (model.data - bkg_total)[row_start:row_end, :]
        #         quarter_mask = mask[row_start:row_end, :]

        #         # Calculate pedestal (median of valid pixels in this quarter)
        #         if sigma_clip:
        #             from astropy.stats import sigma_clipped_stats
        #             pedestal = sigma_clipped_stats(quarter_data[quarter_mask], sigma=3.0, maxiters=5)[1]
        #         else:
        #             pedestal = np.nanmedian(quarter_data[quarter_mask])

        #         log(f'  Quarter {i+1} (rows {row_start}:{row_end}): pedestal = {pedestal:.4f}')

        #         # Store pedestal in model
        #         pedestal_model[row_start:row_end, :] = pedestal

        #     # Add pedestal to total background
        #     bkg_total += pedestal_model


def _prefetch_detector1_references(uncal_files, mask_science_regions=False):
    """Pre-cache CRDS reference files for Detector1Pipeline to avoid race conditions.

    Multiple workers downloading the same CRDS file simultaneously can cause
    one to read a partially-written file. Running CRDS lookups on one file per
    unique detector beforehand ensures everything is cached.
    """
    import crds

    reftypes = [
        'dark', 'gain', 'ipc', 'linearity', 'mask',
        'readnoise', 'refpix', 'saturation', 'superbias',
    ]
    if mask_science_regions:
        reftypes += [
            'camera', 'collimator', 'disperser', 'fore', 'fpa',
            'msa', 'ote', 'wavelengthrange', 'msaoper', 'flat',
        ]

    seen_detectors = set()
    for uncal_file in uncal_files:
        hdr = fits.getheader(uncal_file)
        det = hdr.get('DETECTOR', 'NRS1')

        if det in seen_detectors:
            continue
        seen_detectors.add(det)

        log(f"Pre-fetching Detector1Pipeline CRDS references for {det} "
            f"using {os.path.basename(uncal_file)}")

        params = {
            "INSTRUME": "NIRSPEC",
            "DETECTOR": det,
            "EXP_TYPE": hdr.get('EXP_TYPE', 'NRS_MSASPEC'),
            "READPATT": hdr.get('READPATT', 'NRSIRS2'),
            "SUBARRAY": hdr.get('SUBARRAY', 'FULL'),
            "SUBSTRT1": hdr.get('SUBSTRT1', 1),
            "SUBSTRT2": hdr.get('SUBSTRT2', 1),
            "SUBSIZE1": hdr.get('SUBSIZE1', 2048),
            "SUBSIZE2": hdr.get('SUBSIZE2', 2048),
            "DATE-OBS": hdr.get('DATE-OBS', '2023-01-01'),
            "TIME-OBS": hdr.get('TIME-OBS', '00:00:00'),
        }
        if mask_science_regions:
            params.update({
                "FILTER": hdr.get('FILTER', 'CLEAR'),
                "GRATING": hdr.get('GRATING', 'G140M'),
            })

        try:
            refs = crds.getreferences(params, reftypes=reftypes, observatory='jwst')
            cached = [k for k, v in refs.items()
                      if v and 'N/A' not in v.upper() and 'NOT FOUND' not in v.upper()]
            log(f"  Cached {len(cached)}/{len(reftypes)} references: "
                f"{', '.join(sorted(cached))}")
        except Exception as e:
            log(f"  CRDS prefetch warning for {det}: {e}")

    log("Detector1Pipeline CRDS reference pre-fetch complete")


def _prefetch_crds_references(rate_files):
    """Pre-cache CRDS reference files to avoid multiprocessing race conditions.

    Multiple workers downloading the same CRDS file simultaneously can cause
    one to read a partially-written file (e.g. empty JSON). Running CRDS
    lookups on one file per detector beforehand ensures everything is cached.
    """
    from jwst.datamodels import ImageModel
    from jwst.assign_wcs import AssignWcsStep
    from jwst.msaflagopen.msaflagopen_step import MSAFlagOpenStep

    seen_detectors = set()
    for rate_file in rate_files:
        det = 'nrs1' if 'nrs1' in os.path.basename(rate_file) else 'nrs2'
        if det in seen_detectors:
            continue
        seen_detectors.add(det)

        log(f"Pre-fetching CRDS references for {det} using {os.path.basename(rate_file)}")

        with ImageModel(rate_file) as model:
            # Run AssignWcsStep to trigger download of all WCS reference files
            step = AssignWcsStep()
            step.input_dir = os.path.dirname(rate_file)
            processed = step.run(model)

            # Trigger msaoper reference download
            MSAFlagOpenStep().get_reference_file(processed, "msaoper")

        # Trigger pictureframe reference download
        _get_pictureframe_file(rate_file)

    log("CRDS reference pre-fetch complete")


def _get_pictureframe_file(rate_file):
    """Look up the NIRSpec pictureframe reference file via CRDS.

    The file is automatically downloaded to $CRDS_PATH on first use.
    Returns None if CRDS cannot resolve the reference.
    """
    import crds
    from jwst.datamodels import ImageModel

    with ImageModel(rate_file) as model:
        params = {
            "INSTRUME": "NIRSPEC",
            "DETECTOR": model.meta.instrument.detector,
            "EXP_TYPE": model.meta.exposure.type,
            "DATE-OBS": model.meta.observation.date,
            "TIME-OBS": model.meta.observation.time,
        }
    try:
        result = crds.getreferences(params, reftypes=['pictureframe'], observatory='jwst')
        pf = result.get('pictureframe', '')
        if not pf or pf.upper().startswith('N/A') or 'NOT FOUND' in pf.upper():
            log(f"CRDS has no pictureframe reference for this observation (returned: {pf!r})")
            return None
        if not os.path.isfile(pf):
            log(f"CRDS pictureframe file not found on disk: {pf} — check CRDS_PATH and network access")
            return None
        log(f"Using pictureframe reference: {pf}")
        return pf
    except Exception as e:
        log(f"CRDS pictureframe lookup failed: {e}")
    return None


def _fit_col_template(residual, mask, template, sigma_clip=True,
                      min_valid=20, n_clip_iter=5):
    """Per-column fit of residual[:,j] = alpha_j * template[:,j] + beta_j.

    Alternative to the flat per-column median used in the default col_1f step.
    Captures column-wise mismatches in the picture-frame template that the
    coarser per-quarter PF fit cannot correct. Returns an ndarray same shape
    as `residual`, with 0 in columns that have fewer than `min_valid` unmasked
    pixels.
    """
    from astropy.stats import median_absolute_deviation

    col_model = np.zeros_like(residual)
    _, n_cols = residual.shape

    for j in range(n_cols):
        col_mask = mask[:, j]
        if col_mask.sum() < min_valid:
            continue
        d = residual[col_mask, j]
        t = template[col_mask, j]

        # Degenerate: if the template column is essentially flat, only a
        # constant shift is identifiable — fall back to the column median.
        if np.ptp(t) == 0:
            col_model[:, j] = np.median(d)
            continue

        A = np.column_stack([t, np.ones_like(t)])
        (alpha, beta), _, _, _ = np.linalg.lstsq(A, d, rcond=None)

        if sigma_clip:
            for _ in range(n_clip_iter):
                resid = d - (alpha * t + beta)
                sigma_mad = median_absolute_deviation(resid)
                if sigma_mad == 0:
                    break
                keep = np.abs(resid - np.median(resid)) < 3.0 * sigma_mad
                if keep.sum() == len(d) or keep.sum() < min_valid:
                    break
                d, t = d[keep], t[keep]
                A = np.column_stack([t, np.ones_like(t)])
                (alpha, beta), _, _, _ = np.linalg.lstsq(A, d, rcond=None)

        col_model[:, j] = alpha * template[:, j] + beta

    return col_model


def subtract_background_from_rate_file(
        rate_file: str,
        override_wavelength_range: dict = {},
        n_iter: int = 5,
        subtract_2d: bool = False,
        box_size: int = 64,
        sigma_clip: bool = True,
        bkg_estimator: str = 'median',
        do_col_1f: str = True,
        do_row_1f: str = True,
        col_1f_method: str = 'template',
        plot: bool = True,
        save_backup: bool = False,
    ):

    from stdatamodels import util as stutil
    from jwst.datamodels import ImageModel
    from jwst.clean_flicker_noise.clean_flicker_noise import _make_processed_rate_image
    from astropy.stats import median_absolute_deviation

    input_dir = os.path.dirname(rate_file)
    with ImageModel(rate_file) as model:

        if not 'PRISM' in model.meta.instrument.grating:
            do_row_1f = False

        for entry in model.history:
            if 'Subtracted' in entry['description'] and 'rescaled variance' in entry['description']:
                log(f'Background subtraction already done for {os.path.basename(rate_file)}, skipping...')
                return

        log(f'Subtracting background and rescaling variance for {os.path.basename(rate_file)}')

        # True indicates valid pixels
        slitmask = np.full(model.data.shape, True)

        # always mask fixed slit area
        slitmask[2048//2-100:2048//2+100,:] = False

        slitmask = mask_slits(model, input_dir, slitmask, override_wavelength_range=override_wavelength_range)

        slitmask[model.dq > 0] = False

        detector = 'nrs2'
        if 'nrs1' in rate_file:
            detector = 'nrs1'

        # Initialize background model
        bkg_total = np.zeros_like(model.data)

        # Track components for plotting
        pictureframe_model = None
        pedestal_model = None
        bkg2d_model = None
        col_model = None
        row_model = None

        # Look up pictureframe reference via CRDS
        pictureframe_file = _get_pictureframe_file(rate_file)
        use_pictureframe = pictureframe_file is not None

        if use_pictureframe:
            pictureframe_model_total = np.zeros_like(model.data)
            pictureframe_template = fits.getdata(pictureframe_file)
        if subtract_2d:
            bkg2d_model_total = np.zeros_like(model.data)
        if do_col_1f:
            col_model_total = np.zeros_like(model.data)
        if do_row_1f:
            row_model_total = np.zeros_like(model.data)


        from jwst.clean_flicker_noise.clean_flicker_noise import clip_to_background
        n_sigma, fit_histogram = 2.0, False

        for j in range(n_iter):
            log(f'Iteration {j+1}/{n_iter}')

            mask = slitmask.copy()
            clip_to_background(model.data-bkg_total, mask, sigma_upper=n_sigma, fit_histogram=fit_histogram, verbose=True)

            if use_pictureframe:
                log(f'Subtracting "picture frame" template (per-quarter fitting)')
                n_quarters = 4
                quarter_height = 2048 // n_quarters

                pictureframe_model = np.zeros_like(model.data)

                for i in range(n_quarters):
                    row_start = i * quarter_height
                    row_end = (i + 1) * quarter_height

                    q_data = model.data[row_start:row_end, :] - bkg_total[row_start:row_end, :]
                    q_mask = mask[row_start:row_end, :]
                    q_template = pictureframe_template[row_start:row_end, :]

                    # 2-parameter fit: data = coeff * template + pedestal
                    q_valid = q_data[q_mask]
                    q_templ_valid = q_template[q_mask]
                    A = np.column_stack([q_templ_valid, np.ones_like(q_templ_valid)])
                    (best_coeff, pedestal), _, _, _ = np.linalg.lstsq(A, q_valid, rcond=None)

                    # Iterative sigma clipping on the residuals
                    if sigma_clip:
                        for iteration in range(5):
                            residual = q_valid - (best_coeff * q_templ_valid + pedestal)
                            sigma_mad = median_absolute_deviation(residual)
                            clip_mask = np.abs(residual - np.median(residual)) < 3.0 * sigma_mad
                            if clip_mask.sum() == len(q_valid):
                                break  # converged, nothing more to clip
                            q_valid = q_valid[clip_mask]
                            q_templ_valid = q_templ_valid[clip_mask]
                            A = np.column_stack([q_templ_valid, np.ones_like(q_templ_valid)])
                            (best_coeff, pedestal), _, _, _ = np.linalg.lstsq(A, q_valid, rcond=None)

                    log(f'  Quarter {i+1} (rows {row_start}:{row_end}): '
                        f'PF coeff = {best_coeff:.4f}, pedestal = {pedestal:.4f}')

                    pictureframe_model[row_start:row_end, :] = best_coeff * q_template + pedestal

                pictureframe_model_total += pictureframe_model
                bkg_total += pictureframe_model

                # Re-clip mask against updated residual so subsequent steps
                # (2D background, 1/f) use a mask consistent with the post-PF data
                mask = slitmask.copy()
                clip_to_background(model.data-bkg_total, mask, sigma_upper=n_sigma, fit_histogram=fit_histogram, verbose=False)

            # if pictureframe_dir:
            #     log(f'Subtracting "picture frame" template (global coeff + per-quarter pedestal)')
            #     n_quarters = 4
            #     quarter_height = 2048 // n_quarters

            #     pictureframe_model = np.zeros_like(model.data)

            #     residual = (model.data - bkg_total)

            #     # Step 1: Fit a single global coefficient for the template
            #     global_valid = residual[mask]
            #     global_templ_valid = pictureframe_template[mask]

            #     A = global_templ_valid[:, np.newaxis]  # single-parameter fit (no constant)
            #     (best_coeff,), _, _, _ = np.linalg.lstsq(A, global_valid, rcond=None)

            #     if sigma_clip:
            #         for iteration in range(5):
            #             resid = global_valid - best_coeff * global_templ_valid
            #             sigma_mad = median_absolute_deviation(resid)
            #             clip_mask = np.abs(resid - np.median(resid)) < 3.0 * sigma_mad
            #             if clip_mask.sum() == len(global_valid):
            #                 break
            #             global_valid = global_valid[clip_mask]
            #             global_templ_valid = global_templ_valid[clip_mask]
            #             A = global_templ_valid[:, np.newaxis]
            #             (best_coeff,), _, _, _ = np.linalg.lstsq(A, global_valid, rcond=None)

            #     log(f'  Global PF coeff = {best_coeff:.4f}')
            #     pictureframe_model = best_coeff * pictureframe_template

            #     # Step 2: Fit per-quarter pedestals on the PF-subtracted residual
            #     pedestal_model = np.zeros_like(model.data)
            #     pf_residual = residual - pictureframe_model

            #     for i in range(n_quarters):
            #         row_start = i * quarter_height
            #         row_end = (i + 1) * quarter_height

            #         q_resid = pf_residual[row_start:row_end, :]
            #         q_mask = mask[row_start:row_end, :]

            #         if sigma_clip:
            #             from astropy.stats import sigma_clipped_stats
            #             pedestal = sigma_clipped_stats(q_resid[q_mask], sigma=3.0, maxiters=5)[1]
            #         else:
            #             pedestal = np.nanmedian(q_resid[q_mask])

            #         log(f'  Quarter {i+1} (rows {row_start}:{row_end}): pedestal = {pedestal:.4f}')
            #         pedestal_model[row_start:row_end, :] = pedestal

            #     pictureframe_model_total += pictureframe_model
            #     pedestal_model_total += pedestal_model
            #     bkg_total += pictureframe_model + pedestal_model

            if subtract_2d:
                from photutils.background import Background2D, MedianBackground
                from astropy.stats import SigmaClip
                match bkg_estimator:
                    case 'median':
                        bkg_est = MedianBackground()
                    case _:
                        bkg_est = None
                if sigma_clip:
                    sclip = SigmaClip(sigma=3.0, maxiters=5)
                else:
                    sclip = None

                bkg = Background2D(
                    model.data - bkg_total,
                    box_size=box_size,
                    filter_size=(3,3),
                    mask=~mask,
                    sigma_clip=sclip,
                    bkg_estimator=bkg_est,
                )

                bkg2d_model = bkg.background
                bkg_total += bkg2d_model
                bkg2d_model_total += bkg2d_model


            if do_col_1f:
                if col_1f_method == 'template' and use_pictureframe:
                    log(f'Subtracting column 1/f via per-column PF template fit')
                    col_model = _fit_col_template(
                        model.data - bkg_total, mask, pictureframe_template,
                        sigma_clip=sigma_clip,
                    )
                else:
                    if col_1f_method == 'template' and not use_pictureframe:
                        log(f'WARNING: col_1f_method="template" requested but no PF template '
                            f'available; falling back to per-column median')
                    rate_masked = (model.data - bkg_total).copy()
                    rate_masked[~mask] = np.nan

                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        col_model = np.nanmedian(rate_masked, axis=0)[np.newaxis,:]
                    col_model[~np.isfinite(col_model)] = 0.0

                bkg_total += col_model
                col_model_total += col_model

            if do_row_1f:
                rate_masked = (model.data - bkg_total).copy()
                rate_masked[~mask] = np.nan

                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    full_row_masked = np.sum(np.isfinite(rate_masked),axis=1)==0
                    rate_masked[full_row_masked,:] = np.nanmedian(rate_masked)
                    row_model = np.nanmedian(rate_masked, axis=1)[:,np.newaxis]
                row_model[~np.isfinite(row_model)] = 0.0

                bkg_total += row_model
                row_model_total += row_model


        # Point the names the plotting code expects at the totals
        if use_pictureframe:
            pictureframe_model = pictureframe_model_total
            # pedestal_model = pedestal_model_total
        if subtract_2d:
            bkg2d_model = bkg2d_model_total
        if do_col_1f:
            col_model = col_model_total
        if do_row_1f:
            row_model = row_model_total

        if plot:
            from campfire_pipeline.nirspec.plots import plot_bkg_subtraction
            plot_bkg_subtraction(
                rate_file, model.data, mask,
                pictureframe_model=pictureframe_model,
                pedestal_model=pedestal_model,
                bkg2d_model=bkg2d_model,
                col_model=col_model,
                row_model=row_model,
            )

        fits.writeto(rate_file.replace('_rate.fits','_mask.fits'), mask.astype(int), overwrite=True)
        fits.writeto(rate_file.replace('_rate.fits','_bkg.fits'), bkg_total, overwrite=True)
        rate_new = model.data - bkg_total

        model.data = rate_new

        nsci = model.data / np.sqrt(model.var_rnoise)
        from astropy.stats import sigma_clipped_stats
        rms = sigma_clipped_stats(nsci[mask])[2]
        log(f'Scaling up VAR_RNOISE by {rms**2:.2f}')
        model.var_rnoise = model.var_rnoise * rms**2

        log(f"Saving to {os.path.basename(rate_file)}")
        time = datetime.now()
        stepdescription = f"Subtracted pedestal, rescaled variance {time.strftime('%Y-%m-%d %H:%M:%S')}"
        substr = stutil.create_history_entry(stepdescription)
        model.history.append(substr)

        if save_backup:
            shutil.copy2(rate_file, rate_file.replace('_rate.fits', '_rate_before_bkgsub.fits'))

        model.save(rate_file)


def run_stage1_single_uncal(
        uncal_file,
        workspace_dir,
        do_clean_flicker_noise=True,
        mask_science_regions=True,
        cleanup_uncal=True,
        cleanup_rateints=True,
    ):
    """
    Runs the JWST Detector1Pipeline on a single *_uncal.fits file.
    Optionally includes the clean_flicker_noise step.
    """

    # Handle directory changes
    prev_cwd = os.getcwd()

    os.chdir(workspace_dir)

    try:
        from jwst.pipeline import Detector1Pipeline
        steps = {
                'clean_flicker_noise' :{
                    'skip': not do_clean_flicker_noise,
                    'mask_science_regions':mask_science_regions,
                    'save_mask': False,
                },
                'jump': {
                    'skip': False, # testing, should be False normally
                    'expand_large_events': True, # testing, should be True normally
                }
            }
        Detector1Pipeline.call(uncal_file,
            save_results=True,
            steps=steps,
        )
        if cleanup_uncal:
            log(f'Finished Detector1Pipeline for {uncal_file}, removing...')
            os.remove(uncal_file)
        if cleanup_rateints:
            os.remove(uncal_file.replace('_uncal.fits', '_rateints.fits'))

        return 1

    except Exception as e:
        log(f"ERROR: Detector1Pipeline FAILED for {uncal_file}: {e}")
        import traceback
        log(traceback.format_exc())
        return 0

    finally:
        # Always restore working directory
        os.chdir(prev_cwd)
