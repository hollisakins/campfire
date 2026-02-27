"""
Chi-squared redshift fitting: NNLS solvers and per-spectrum fitting functions.
"""

import os
import time
import glob
import pickle
import logging
import numpy as np
from pathlib import Path
from scipy.optimize import nnls
from scipy.ndimage import generic_filter
from astropy.io import fits
from astropy import table

from numba import njit, prange
import numba

from campfire_pipeline.common.spectral import (
    resample_to_nonuniform_grid,
    convolve_with_lsf,
    resample_to_observed_grid,
)
from campfire_pipeline.nirspec.templates import assemble_full_template_grid


def calculate_redshift_confidence(z_array, chi2_array, zbest):
    """
    Calculate redshift confidence based on chi-squared distribution.

    Quality flag is set to 0 by default and will be updated during visual inspection.

    Parameters:
    -----------
    z_array : array
        Redshift grid
    chi2_array : array
        Chi-squared values corresponding to redshift grid
    zbest : float
        Best-fit redshift

    Returns:
    --------
    dict : Confidence metrics
        {
            'redshift': float - Best redshift rounded to 4 decimal places
            'redshift_quality': int - Quality flag (0 = unreviewed, to be set during inspection)
            'chi2_min': float - Minimum chi-squared value
            'confidence': float - Confidence percentage
        }
    """
    try:
        # Calculate confidence using numerically stable method
        # Offset chi2 by minimum to prevent underflow in exp(-large_number)
        chi2_min_val = np.min(chi2_array)
        chi2_offset = chi2_array - chi2_min_val  # Minimum becomes 0
        pz = np.exp(-chi2_offset)  # Convert to probability (max prob = 1.0)

        # Calculate confidence within ±0.03 of best redshift
        confidence_mask = np.abs(z_array - zbest) <= 0.03
        confidence = np.sum(pz[confidence_mask]) / np.sum(pz) * 100

        # Quality flag is 0 by default - will be set during visual inspection
        z_quality = 0   # Default: unreviewed, to be set during visual inspection

        return {
            'redshift': round(zbest, 4),        # Round to 4 decimal places
            'redshift_quality': z_quality,      # Default 0, updated during inspection
            'chi2_min': float(chi2_min_val),
            'confidence': float(confidence)
        }

    except Exception as e:
        # If confidence calculation fails, return safe defaults
        return {
            'redshift': round(zbest, 4),
            'redshift_quality': 0,  # Default 0 even for failed calculations
            'chi2_min': float(np.min(chi2_array)) if len(chi2_array) > 0 else 0.0,
            'confidence': 0.0
        }


@njit(cache=True)
def _nnls_gram(AtA, Atb, max_iter=135, tol=1e-10):
    """Lawson-Hanson NNLS on pre-computed Gram system.

    Solves: min ||Ax - b||^2  s.t. x >= 0
    where AtA = A^T A and Atb = A^T b are pre-computed.

    Returns (x, chi2_partial) where chi2_partial = x^T AtA x - 2 x^T Atb.
    Add btb = b^T b to get full chi-squared.
    """
    n = AtA.shape[0]
    x = np.zeros(n)
    passive = np.zeros(n, dtype=np.bool_)

    for iteration in range(max_iter):
        # Gradient: w = Atb - AtA @ x
        w = Atb - AtA @ x

        # Find most violating active constraint
        max_w_val = -1.0
        max_w_idx = -1
        for i in range(n):
            if not passive[i] and w[i] > max_w_val:
                max_w_val = w[i]
                max_w_idx = i

        if max_w_val <= tol or max_w_idx == -1:
            break

        passive[max_w_idx] = True

        # Inner loop: solve unconstrained on passive set, fix negatives
        for inner in range(max_iter):
            # Count passive variables
            n_passive = 0
            for i in range(n):
                if passive[i]:
                    n_passive += 1
            if n_passive == 0:
                break

            # Build index array for passive set
            p_idx = np.empty(n_passive, dtype=np.int64)
            k = 0
            for i in range(n):
                if passive[i]:
                    p_idx[k] = i
                    k += 1

            # Extract sub-system (no 2D fancy indexing in numba)
            AtA_sub = np.empty((n_passive, n_passive))
            Atb_sub = np.empty(n_passive)
            for ii in range(n_passive):
                Atb_sub[ii] = Atb[p_idx[ii]]
                for jj in range(n_passive):
                    AtA_sub[ii, jj] = AtA[p_idx[ii], p_idx[jj]]

            # Ridge regularization to prevent singular matrix errors
            # from near-collinear templates
            ridge = 0.0
            for ii in range(n_passive):
                ridge += AtA_sub[ii, ii]
            ridge = ridge / n_passive * 1e-10
            if ridge < 1e-20:
                ridge = 1e-20
            for ii in range(n_passive):
                AtA_sub[ii, ii] += ridge

            s = np.linalg.solve(AtA_sub, Atb_sub)

            # Check if all passive coefficients are positive
            all_positive = True
            for ii in range(n_passive):
                if s[ii] <= 0.0:
                    all_positive = False
                    break

            if all_positive:
                for ii in range(n_passive):
                    x[p_idx[ii]] = s[ii]
                break

            # Interpolation: find alpha to keep x >= 0
            alpha = 1.0e30
            for ii in range(n_passive):
                if s[ii] <= 0.0:
                    a = x[p_idx[ii]] / (x[p_idx[ii]] - s[ii])
                    if a < alpha:
                        alpha = a

            for ii in range(n_passive):
                x[p_idx[ii]] += alpha * (s[ii] - x[p_idx[ii]])

            # Move zero-valued variables back to active set
            for i in range(n):
                if passive[i] and abs(x[i]) < tol:
                    passive[i] = False
                    x[i] = 0.0

    chi2_partial = np.dot(x, AtA @ x) - 2.0 * np.dot(x, Atb)
    return x, chi2_partial


@njit(parallel=True, cache=True)
def _compute_gram(tw, bw):
    """Compute Gram matrices AtA and Atb for all redshifts in parallel.

    Exploits AtA symmetry to halve computation. Parallelized across redshifts.

    Parameters
    ----------
    tw : ndarray, shape (n_templ, n_z, N_masked), float64
        Error-weighted, masked templates
    bw : ndarray, shape (N_masked,), float64
        Error-weighted, masked flux

    Returns
    -------
    AtA : ndarray, shape (n_z, n_templ, n_templ)
    Atb : ndarray, shape (n_z, n_templ)
    """
    n_templ, n_z, n_masked = tw.shape
    AtA = np.empty((n_z, n_templ, n_templ))
    Atb = np.empty((n_z, n_templ))
    for iz in prange(n_z):
        for i in range(n_templ):
            # Atb[iz, i] = sum_k tw[i, iz, k] * bw[k]
            s = 0.0
            for k in range(n_masked):
                s += tw[i, iz, k] * bw[k]
            Atb[iz, i] = s
            # AtA upper triangle + mirror (symmetric: AtA[i,j] == AtA[j,i])
            for j in range(i, n_templ):
                s = 0.0
                for k in range(n_masked):
                    s += tw[i, iz, k] * tw[j, iz, k]
                AtA[iz, i, j] = s
                AtA[iz, j, i] = s
    return AtA, Atb


@njit(parallel=True, cache=True)
def _fit_all_redshifts_numba(AtA_all, Atb_all):
    """Parallel NNLS fitting across all redshifts using Numba threads."""
    n_z = AtA_all.shape[0]
    n_templ = AtA_all.shape[1]
    chi2 = np.empty(n_z)
    coeffs = np.empty((n_z, n_templ))
    for iz in prange(n_z):
        x, c = _nnls_gram(AtA_all[iz], Atb_all[iz])
        chi2[iz] = c
        coeffs[iz] = x
    return chi2, coeffs


def _iter(A, mask, flux, err):
    """Single iteration of chi-squared minimization using NNLS."""
    okt = A[:, mask].sum(axis=1) != 0
    Ax = A[okt, :] / err
    yx = flux / err
    x = nnls(Ax[:, mask].T, yx[mask])
    coeffs = np.zeros(A.shape[0])
    coeffs[okt] = x[0]
    model = A.T.dot(coeffs)
    chi2_i = np.sum(np.power((flux[mask] - model[mask]) / err[mask], 2))
    return chi2_i, model


def fit_single_spectrum(spec_file, file_paths, convolved_wav, convolved_grid, zgrid, logger, save_models=False):
    """Perform redshift fitting for a single spectrum file."""
    start_time = time.time()
    spec_path = Path(spec_file)
    base_name = spec_path.stem.replace('_spec', '')
    zfit_file = file_paths['output_path'] + f"{base_name}_zfit.fits"
    logger.debug(f"Fitting redshift for {base_name}")

    try:
        # Load spectrum
        tab = table.Table.read(spec_file, hdu=1)
        wav = tab['wave'].value.astype('float32')
        flux = tab['fnu'].value.astype('float32')
        err = tab['fnu_err'].value.astype('float32')
        mask = np.isfinite(flux) & np.isfinite(err) & (err > 0)
        #mask the edge pixels to avoid edge effects
        maskidx = np.where(mask)[0]
        if len(maskidx) > 10:
            for idx in maskidx[:5]: mask[idx] = False
            for idx in maskidx[-5:]: mask[idx] = False
        #add 10% error floor
        err[err < 0.1*np.abs(flux)] = 0.1*np.abs(flux[err < 0.1*np.abs(flux)])
        #require errors to be >= to a rolling median error (removes low error spikes, but keeps high spikes)
        med_err = generic_filter(err, np.nanmedian, size=15)
        err[err < med_err] = med_err[err < med_err]

        logger.debug('Resampling template grid to observed wavelength grid')
        templates = resample_to_observed_grid(convolved_wav, convolved_grid, wav)
        logger.debug("Running redshift fitting with chi2 minimization")

        # Chi-squared minimization over redshift grid
        chi2 = np.zeros_like(zgrid, dtype='float32')
        models = np.zeros((len(zgrid), len(wav)), dtype='float32')
        for iz in range(len(zgrid)):
            A = templates[:, iz, :]
            chi2[iz], models[iz] = _iter(A, mask, flux, err)

        # Find best-fit redshift
        izbest = np.argmin(chi2)
        zbest = zgrid[izbest]

        # Calculate confidence and quality
        confidence_results = calculate_redshift_confidence(zgrid, chi2, zbest)
        model = models[izbest]

        logger.debug(f"✓ Redshift fitting completed for {base_name} "
                       f"(z={confidence_results['redshift']:.3f}, "
                       f"conf={confidence_results['confidence']:.1f}%, "
                       f"qual={confidence_results['redshift_quality']})")
        logger.debug(f"nans in chi2: {len(chi2[np.isnan(chi2)])}")

        # Save results with confidence information in header
        header = fits.Header({
            'EXTEND': True,
            'ZBEST': confidence_results['redshift'],
            'ZQUAL': confidence_results['redshift_quality'],
            'ZCONF': confidence_results['confidence'],
            'CHI2MIN': confidence_results['chi2_min']
        })
        t0 = fits.PrimaryHDU(header=header)
        t1 = fits.BinTableHDU.from_columns(fits.ColDefs([fits.Column(name='wav', array=wav, format='D'), fits.Column(name='fnu', array=model, format='D')]), header=fits.Header({'EXTNAME': 'MODEL'}))
        t2 = fits.BinTableHDU.from_columns(fits.ColDefs([fits.Column(name='z', array=zgrid, format='D'), fits.Column(name='chi2', array=chi2, format='D')]), header=fits.Header({'EXTNAME': 'CHI2'}))
        if save_models:
            t3 = fits.ImageHDU(models.astype('float32'), header=fits.Header({'EXTNAME': 'MODELS'}))
            out_hdul = fits.HDUList([t0, t1, t2, t3])
        else:
            out_hdul = fits.HDUList([t0, t1, t2])
        out_hdul.writeto(zfit_file, overwrite=True)

        processing_time = time.time() - start_time
        logger.info(f"✓ Redshift fitting completed for {base_name} "
                       f"(z={confidence_results['redshift']:.3f}, "
                       f"conf={confidence_results['confidence']:.1f}%, "
                       f"qual={confidence_results['redshift_quality']}, "
                       f"t={processing_time:.1f}s)")

        return True, f"Success: z={confidence_results['redshift']:.4f}, conf={confidence_results['confidence']:.1f}%"

    except Exception as e:
        logger.error(f"✗ Failed to fit redshift for {base_name}: {e}")
        return False, str(e)


def fit_single_spectrum_optimized(spec_file, file_paths, convolved_wav, convolved_grid, zgrid, logger, save_models=False):
    """Perform redshift fitting using Gram-matrix + Numba NNLS optimization.

    Drop-in replacement for fit_single_spectrum with identical inputs/outputs.
    Uses pre-computed Gram matrices (A^T A, A^T b) and a Numba-JIT Lawson-Hanson
    NNLS solver parallelized across redshifts via prange.
    """
    start_time = time.time()
    spec_path = Path(spec_file)
    base_name = spec_path.stem.replace('_spec', '')
    zfit_file = file_paths['output_path'] + f"{base_name}_zfit.fits"
    logger.debug(f"Fitting redshift for {base_name}")

    try:
        # Load spectrum (identical to fit_single_spectrum)
        tab = table.Table.read(spec_file, hdu=1)
        wav = tab['wave'].value.astype('float32')
        flux = tab['fnu'].value.astype('float32')
        err = tab['fnu_err'].value.astype('float32')
        mask = np.isfinite(flux) & np.isfinite(err) & (err > 0)
        maskidx = np.where(mask)[0]
        if len(maskidx) > 10:
            for idx in maskidx[:5]: mask[idx] = False
            for idx in maskidx[-5:]: mask[idx] = False
        err[err < 0.1*np.abs(flux)] = 0.1*np.abs(flux[err < 0.1*np.abs(flux)])
        med_err = generic_filter(err, np.nanmedian, size=15)
        err[err < med_err] = med_err[err < med_err]

        t0 = time.time()
        templates = resample_to_observed_grid(convolved_wav, convolved_grid, wav)
        t1 = time.time()

        # --- Gram matrix computation for all redshifts ---
        # Weight by inverse error, promote to float64 for numerical stability
        inv_err_masked = (1.0 / err[mask]).astype(np.float64)  # (N_masked,)
        bw = (flux[mask] * inv_err_masked).astype(np.float64)  # (N_masked,)
        btb = np.dot(bw, bw)  # constant across redshifts

        # Apply mask and error-weighting once: (n_templ, n_z, N_masked)
        tw = templates[:, :, mask].astype(np.float64) * inv_err_masked
        # Numba parallel Gram computation (exploits symmetry, parallelized over z)
        AtA, Atb = _compute_gram(tw, bw)
        t2 = time.time()

        # --- NNLS on Gram matrices ---
        chi2_partial, coeffs = _fit_all_redshifts_numba(AtA, Atb)
        chi2 = (chi2_partial + btb).astype('float32')
        t3 = time.time()
        logger.debug(f"Timing for {base_name}: spectres={t1-t0:.2f}s, gram={t2-t1:.2f}s, nnls={t3-t2:.2f}s")

        # Find best-fit redshift
        izbest = np.argmin(chi2)
        zbest = zgrid[izbest]

        # Calculate confidence and quality
        confidence_results = calculate_redshift_confidence(zgrid, chi2, zbest)

        # Reconstruct model only at best-fit redshift
        model = templates[:, izbest, :].T @ coeffs[izbest]

        logger.debug(f"Redshift fitting completed for {base_name} "
                       f"(z={confidence_results['redshift']:.3f}, "
                       f"conf={confidence_results['confidence']:.1f}%, "
                       f"qual={confidence_results['redshift_quality']})")
        logger.debug(f"nans in chi2: {len(chi2[np.isnan(chi2)])}")

        # Save results (identical format to fit_single_spectrum)
        header = fits.Header({
            'EXTEND': True,
            'ZBEST': confidence_results['redshift'],
            'ZQUAL': confidence_results['redshift_quality'],
            'ZCONF': confidence_results['confidence'],
            'CHI2MIN': confidence_results['chi2_min']
        })
        t0 = fits.PrimaryHDU(header=header)
        t1 = fits.BinTableHDU.from_columns(fits.ColDefs([fits.Column(name='wav', array=wav, format='D'), fits.Column(name='fnu', array=model, format='D')]), header=fits.Header({'EXTNAME': 'MODEL'}))
        t2 = fits.BinTableHDU.from_columns(fits.ColDefs([fits.Column(name='z', array=zgrid, format='D'), fits.Column(name='chi2', array=chi2, format='D')]), header=fits.Header({'EXTNAME': 'CHI2'}))
        if save_models:
            models = np.einsum('izk,zi->zk', templates, coeffs).astype('float32')
            t3 = fits.ImageHDU(models, header=fits.Header({'EXTNAME': 'MODELS'}))
            out_hdul = fits.HDUList([t0, t1, t2, t3])
        else:
            out_hdul = fits.HDUList([t0, t1, t2])
        out_hdul.writeto(zfit_file, overwrite=True)

        processing_time = time.time() - start_time
        logger.info(f"Redshift fitting completed for {base_name} "
                       f"(z={confidence_results['redshift']:.3f}, "
                       f"conf={confidence_results['confidence']:.1f}%, "
                       f"qual={confidence_results['redshift_quality']}, "
                       f"t={processing_time:.1f}s)")

        return True, f"Success: z={confidence_results['redshift']:.4f}, conf={confidence_results['confidence']:.1f}%"

    except Exception as e:
        logger.error(f"Failed to fit redshift for {base_name}: {e}")
        return False, str(e)


def _discover_gratings(workspace_dir):
    """Discover gratings from existing *_spec.fits files in the workspace.

    Filenames follow the pattern ``{obs}_{grating}_{filter}_{source_id}_spec.fits``.
    The grating is identified by matching path components against the known set.
    """
    from campfire_pipeline.nirspec.constants import GRATING_LIMITS

    known = set(GRATING_LIMITS.keys())
    spec_files = sorted(glob.glob(os.path.join(workspace_dir, '*_spec.fits')))
    found = set()
    for f in spec_files:
        parts = os.path.basename(f).replace('_spec.fits', '').split('_')
        for part in parts:
            if part.lower() in known:
                found.add(part.lower())
    return sorted(found)


def fit_redshifts(obs_name, config, source_ids=None, overwrite=False,
                    workspace_dir=None, gratings=None, n_processes=1):
    """
    Run redshift fitting for all spectra in an observation.

    Refactored from the old fitting.py main() into a callable function.

    Parameters
    ----------
    obs_name : str
        Observation name (key in observations.toml)
    config : dict
        Loaded config.toml dict
    source_ids : list of int, optional
        If provided, restrict fitting to these source IDs
    overwrite : bool
        Overwrite existing zfit files
    workspace_dir : str, optional
        Explicit workspace directory.  If None, resolved from config + observations.toml.
    gratings : list of str, optional
        Gratings to fit.  If None, auto-discovered from *_spec.fits files.
    n_processes : int
        Number of parallel workers (default: 1).
    """
    from multiprocessing import Pool
    from campfire_pipeline.common.spectral import air_to_vac
    from campfire_pipeline.config import resolve_paths, get_r_curve_path, resolve_template_grid_paths

    paths = resolve_paths(config)

    if workspace_dir is None:
        workspace_dir = paths['products_dir'] + f'/{obs_name}/'

    if gratings is None:
        gratings = _discover_gratings(workspace_dir)
        if not gratings:
            logging.getLogger('nirspec_fitting').warning(
                f"No *_spec.fits files found in {workspace_dir} — nothing to fit")
            return 0

    # File paths
    file_paths = {}
    file_paths['input_path'] = workspace_dir if workspace_dir.endswith('/') else workspace_dir + '/'
    file_paths['output_path'] = file_paths['input_path']

    # Build grating -> template grid file mapping from config
    template_grids_config = resolve_template_grid_paths(config)
    grating_to_template = {}
    for name, grid_config in template_grids_config.items():
        for g in grid_config['gratings']:
            grating_to_template[g] = os.path.abspath(grid_config['file'])

    options = config.get('nirspec', {}).get('redshift_fitting', {})
    ncores = n_processes
    save_models = options.get('save_models', False)
    f_LSF = options.get('f_LSF', 1.3)
    f_LSF_prism = options.get('f_LSF_prism', f_LSF)
    f_LSF_g395m = options.get('f_LSF_g395m', f_LSF)
    use_optimized = options.get('optimized', True)

    # Set up logging
    log_config = config.get('logging', {})
    log_level = log_config.get('level', 'INFO').upper()
    log_format = log_config.get('format', '%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    logging.basicConfig(level=getattr(logging, log_level), format=log_format)
    logger = logging.getLogger('nirspec_fitting')

    if use_optimized:
        fit_func = fit_single_spectrum_optimized
        logger.info("Using optimized Gram-matrix + Numba NNLS fitting")
    else:
        fit_func = fit_single_spectrum
        logger.info("Using standard scipy NNLS fitting")

    for grating in gratings:
        logger.info(f"Fitting {grating} spectra...")
        file_paths['r_curve_file'] = get_r_curve_path(grating)

        # Resolve per-grating template file
        template_file = grating_to_template.get(grating.lower())
        if not template_file:
            logger.error(f"No template grid configured for grating '{grating}' in [template_grids]")
            continue

        logger.debug(f"  R-curve: {file_paths['r_curve_file']}")
        logger.debug(f"  Continuum templates: {template_file}")

        for name, path in [("R-curve", file_paths['r_curve_file']),
                        ("continuum templates", template_file)]:
            if not os.path.exists(path):
                logger.warning(f"{name} file not found: {path}")

        try:
            with open(template_file, 'rb') as f:
                continuum_templates = pickle.load(f)

            logger.info(f"Loaded {grating} continuum templates: {len(continuum_templates['redshifts'])} redshifts")
        except (FileNotFoundError, IOError) as e:
            logger.error(f"Failed to load template files: {e}")
            return False, f"Template loading failed: {e}"

        template_wav = continuum_templates['wavelengths']
        zgrid = continuum_templates['redshifts']

        # Build all template types and assemble
        logger.info('Building emission line, broadline, blackbody, and modified blackbody templates')
        templates = assemble_full_template_grid(continuum_templates, zgrid, template_wav)

        oversample = 1
        logger.info('Resampling template grid to non-uniform wavelength grid')
        convolved_wav, temp_grid = resample_to_nonuniform_grid(template_wav, templates, file_paths['r_curve_file'], oversample=oversample)
        convolved_wav = convolved_wav.astype('float32')

        logger.info('Convolving template grid with LSF')
        convolved_grid = convolve_with_lsf(convolved_wav, temp_grid, oversample=oversample, f_LSF=f_LSF)
        convolved_grid = convolved_grid.astype('float32')

        all_spec_files = np.array(sorted(glob.glob(file_paths['input_path'] + f'/*{grating.lower()}*_spec.fits')))
        spec_files = all_spec_files

        if source_ids is not None:
            actual_spec_files = []
            for spec_file in all_spec_files:
                source_id = int(spec_file.split('_')[-2])
                if source_id in source_ids:
                    actual_spec_files.append(spec_file)
            spec_files = actual_spec_files

        if not overwrite:
            actual_spec_files = []
            for spec_file in spec_files:
                spec_path = Path(spec_file)
                base_name = spec_path.stem.replace('_spec', '')
                zfit_file = file_paths['output_path'] + f"{base_name}_zfit.fits"
                if os.path.exists(zfit_file):
                    logger.info(f"Zfit file already exists for {base_name}, skipping")
                else:
                    actual_spec_files.append(spec_file)
            spec_files = actual_spec_files

        logger.info(f"Starting redshift fitting for {len(spec_files)} objects using {ncores} cores")
        if use_optimized:
            if ncores > 1:
                numba.set_num_threads(1)
            else:
                numba.set_num_threads(os.cpu_count() or 1)
        if use_optimized:
            logger.debug("Warming up Numba JIT compilation...")
            n_templ = templates.shape[0]
            _compute_gram(np.ones((n_templ, 1, 1)), np.ones(1))
            _fit_all_redshifts_numba(np.eye(n_templ, dtype=np.float64)[np.newaxis], np.ones((1, n_templ)))
        start_time = time.time()
        if ncores > 1:
            with Pool(ncores) as pool:
                _ = pool.starmap(fit_func, [[spec_file, file_paths, convolved_wav, convolved_grid, zgrid, logger, save_models] for spec_file in spec_files])
        else:
            for spec_file in spec_files:
                fit_func(spec_file, file_paths, convolved_wav, convolved_grid, zgrid, logger, save_models)
        end_time = time.time()
        logger.info(f"Fit {len(spec_files)} redshifts in {end_time-start_time:.1f}s")

    return 0
