"""
Stage 1: Detector1Pipeline processing, wisp subtraction, 1/f striping
removal, and persistence flagging for NIRCam data.

Ported from nircamx/stage1.py with refactored config/path/logging access.
"""

import os
import glob
import shutil
from copy import deepcopy
import numpy as np
from datetime import datetime
from time import sleep

from scipy.ndimage import median_filter, binary_dilation, gaussian_filter
from scipy.optimize import curve_fit

from photutils.segmentation import SegmentationImage
from photutils.segmentation import detect_threshold, detect_sources
from photutils.background import (Background2D, BiweightLocationBackground,
                                  BkgIDWInterpolator, BkgZoomInterpolator,
                                  MedianBackground, SExtractorBackground)

from astropy.io import fits
from astropy.stats import (gaussian_fwhm_to_sigma,
                           sigma_clipped_stats,
                           SigmaClip,
                           biweight_location,
                           median_absolute_deviation)

from astropy.convolution import (Tophat2DKernel,
                                 Gaussian2DKernel,
                                 Ring2DKernel,
                                 convolve,
                                 convolve_fft)

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.constants import NIR_AMPS, SW_FILTERS, LW_FILTERS


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_stage1(field, stage_config, filters=None, n_processes=1, overwrite=False):
    """Orchestrate all stage-1 steps for a NIRCam field.

    Parameters
    ----------
    field : Field
        Field dataclass (must have workspace already set up).
    stage_config : dict
        Stage 1 configuration dict (keys: detector1, remove_wisp,
        remove_striping, persistence).
    filters : list of str, optional
        Filters to process. If None, uses ``field.filters``.
    n_processes : int
        Number of parallel workers.
    overwrite : bool
        Overwrite existing products.
    """
    from campfire_pipeline.common.parallel import dispatch

    if filters is None:
        filters = field.filters

    log(f"Stage 1 for field '{field.name}', filters: {filters}")
    log(f"Stage 1 config: {stage_config}")

    for filtname in filters:
        log(f"--- Filter: {filtname} ---")

        # Ensure output directory exists
        filt_dir = os.path.join(field.stage1_dir, filtname)
        os.makedirs(filt_dir, exist_ok=True)

        # ----- detector1 -----
        uncal_files = field.get_uncal_files(filtname)
        if not uncal_files:
            log(f"No uncal files found for {filtname}, skipping detector1")
        else:
            log(f"detector1: {len(uncal_files)} uncal files")
            dispatch(
                detector1_step,
                uncal_files,
                n_processes=n_processes,
                stage_config=stage_config,
                field=field,
                overwrite=overwrite,
            )

        # ----- remove_wisp -----
        rate_files = field.get_rate_files(filtname)
        if not rate_files:
            log(f"No rate files found for {filtname}, skipping remove_wisp")
        else:
            log(f"remove_wisp: {len(rate_files)} rate files")
            dispatch(
                remove_wisps,
                rate_files,
                n_processes=n_processes,
                stage_config=stage_config,
                field=field,
            )

        # ----- remove_striping -----
        rate_files = field.get_rate_files(filtname)
        if not rate_files:
            log(f"No rate files found for {filtname}, skipping remove_striping")
        else:
            log(f"remove_striping: {len(rate_files)} rate files")
            dispatch(
                remove_striping,
                rate_files,
                n_processes=n_processes,
                stage_config=stage_config,
                field=field,
            )

        # ----- persistence -----
        rate_files = field.get_rate_files(filtname)
        if not rate_files:
            log(f"No rate files found for {filtname}, skipping persistence")
        else:
            log(f"persistence: {len(rate_files)} rate files")
            persistence_step(rate_files)


# ---------------------------------------------------------------------------
# detector1_step
# ---------------------------------------------------------------------------

def detector1_step(uncal_file, stage_config, field, overwrite=False):
    """Run the JWST Detector1Pipeline on a single *_uncal.fits file.

    Parameters
    ----------
    uncal_file : str
        Full path to the uncal file.
    stage_config : dict
        Stage 1 configuration dict.
    field : Field
        Field dataclass.
    overwrite : bool
        Overwrite existing products.
    """
    from jwst.pipeline import calwebb_detector1

    step_cfg = stage_config.get('detector1', {})
    clean_flicker_noise = step_cfg.get('clean_flicker_noise', False)

    filtname = uncal_file.split('/')[-2]
    assert (filtname.lower() in SW_FILTERS) or (filtname.lower() in LW_FILTERS)
    uncal_file_name = os.path.basename(uncal_file)
    rate_file_name = uncal_file_name.replace('_uncal.fits', '_rate.fits')
    output_dir = os.path.join(field.stage1_dir, filtname)
    rate_file = os.path.join(output_dir, rate_file_name)

    if os.path.exists(rate_file) and not overwrite:
        log(f"Skipping detector1_step on {uncal_file_name}, rate file already exists")
        return

    if os.path.exists(rate_file) and overwrite:
        pattern = rate_file.replace('_rate.fits', '*')
        files = glob.glob(pattern)
        for file in files:
            os.remove(file)
            log(f"Removed file: {file}")

    log(f"Running detector1_step on {uncal_file_name}")

    # Pipeline-level save_results=False suppresses both _rate.fits and
    # _rateints.fits auto-save; we save _rate.fits explicitly below to skip
    # the rateints I/O (a 4D cube we never use). _jump.fits is still emitted
    # because the jump substep has its own save_results=True (PersistenceFlagStep
    # reads it from disk).
    kwargs = {
        'output_dir': output_dir,
        'save_results': False,
        'steps': {
            'group_scale': {'skip': False},
            'dq_init': {'skip': False},
            'emicorr': {'skip': True},
            'saturation': {
                'skip': False,
                'n_pix_grow_sat': 1,
                'use_readpatt': True,
            },
            'ipc': {'skip': False},
            'superbias': {'skip': False},
            'refpix': {
                'skip': False,
                'odd_even_columns': True,
                'odd_even_rows': True,
                'gaussmooth': 1.0,
                'halfwidth': 30,
                'side_gain': 1.0,
                'side_smoothing_length': 11,
                'sigreject': 4.0,
                'use_side_ref_pixels': True,
                'irs2_mean_subtraction': False,
                'ovr_corr_mitigation_ftr': 3.0,
                'preserve_irs2_refpix': False,
                'refpix_algorithm': 'median',
            },
            'rscd': {'skip': False},
            'firstframe': {'skip': False, 'bright_use_group1': False},
            'lastframe': {'skip': False},
            'linearity': {'skip': False},
            'dark_current': {
                'skip': False,
                'average_dark_current': None,
                'dark_output': None,
            },
            'reset': {'skip': False},
            'persistence': {
                'skip': False,
                'flag_pers_cutoff': 40.0,
                'save_persistence': False,
                'save_results': False,
                'save_trapsfilled': False,
            },
            'charge_migration': {'skip': True},
            'jump': {
                'skip': False,
                'after_jump_flag_dn1': 0.0,
                'after_jump_flag_dn2': 0.0,
                'after_jump_flag_time1': 0.0,
                'after_jump_flag_time2': 0.0,
                'edge_size': 25,
                'expand_factor': 2.2,
                'expand_large_events': True,
                'extend_ellipse_expand_ratio': 1.1,
                'extend_inner_radius': 1.0,
                'extend_min_area': 90,
                'extend_outer_radius': 2.6,
                'extend_snr_threshold': 1.2,
                'find_showers': False,
                'flag_4_neighbors': True,
                'four_group_rejection_threshold': 5.0,
                'mask_snowball_core_next_int': True,
                'max_extended_radius': 200,
                'max_jump_to_flag_neighbors': 300.0,
                'max_shower_amplitude': 4.0,
                'maximum_cores': 'none',
                'min_diffs_single_pass': 10,
                'min_jump_area': 15.0,
                'min_jump_to_flag_neighbors': 15.0,
                'min_sat_area': 1.0,
                'min_sat_radius_extend': 2.0,
                'minimum_groups': 3,
                'minimum_sigclip_groups': 100,
                'only_use_ints': True,
                'rejection_threshold': 4.0,
                'sat_expand': 2,
                'sat_required_snowball': False,
                'save_results': True,
                'search_output_file': True,
                'snowball_time_masked_next_int': 4000,
                'three_group_rejection_threshold': 6.0,
                'time_masked_after_shower': 15.0,
                'use_ellipses': True,
            },
            'clean_flicker_noise': {
                'skip': not clean_flicker_noise,
                'fit_by_channel': True,
            },
            'ramp_fit': {
                'skip': False,
                'algorithm': 'OLS_C',
                'maximum_cores': 'none',
            },
            'gain_scale': {'skip': False},
        }
    }

    result = calwebb_detector1.Detector1Pipeline.call(uncal_file, **kwargs)
    if result is not None:
        result.save(rate_file)
        result.close()


# ---------------------------------------------------------------------------
# Wisp subtraction
# ---------------------------------------------------------------------------

def calc_variance(data, template, coeff):
    """Calculate the absolute median deviation of wisp-subtracted image.

    Determines the variance of the function: image - coefficient * template.
    Using the median absolute deviation squared. This is not scaled to
    represent the standard deviation of normally distributed data, as would
    be appropriate for an error estimator. However, fit_wisp_feature() will
    find the coefficient that minimizes this variance, and so the relative
    values are what matter.

    Parameters
    ----------
    data : array-like
        Image array of masked data values.
    template : array-like
        Image array of wisp template.
    coeff : float
        Coefficient for scaling wisp template.

    Returns
    -------
    var_mad : float
        Median absolute deviation squared for given coeff.
    """
    func = data - coeff * template
    sigma_mad = median_absolute_deviation(func, ignore_nan=True)
    var_mad = sigma_mad**2
    return var_mad


def remove_wisps(rate_file, stage_config, field):
    """Remove wisp artifacts from a rate file.

    Parameters
    ----------
    rate_file : str
        Full path to rate file.
    stage_config : dict
        Stage 1 configuration dict.
    field : Field
        Field dataclass.
    """
    step_cfg = stage_config.get('remove_wisp', {})
    plot = step_cfg.get('plot', True)
    apply_flat = step_cfg.get('apply_flat', True)
    use_custom_flat = step_cfg.get('use_custom_flat', False)

    try:
        crds_context = os.environ['CRDS_CONTEXT']
    except KeyError:
        import crds
        crds_context = crds.get_default_context()

    filtname = rate_file.split('/')[-2]
    rate_file_name = os.path.basename(rate_file)
    detector = rate_file_name.split('_')[3]
    rate_file_orig = rate_file.replace('_rate.fits', '_rate_without_wisps_sub.fits')

    if detector not in ['nrca3', 'nrca4', 'nrcb3', 'nrcb4']:
        log(f'Skipping wisp correction for {rate_file_name}')
        return

    res = []
    wisp_template_names = []

    # Check that image has not already been corrected
    from jwst.datamodels import ImageModel
    model = ImageModel(rate_file)
    for entry in model.history:
        if 'Removed wisps' in entry['description']:
            log(f'{rate_file_name} already corrected for wisps, exiting')
            return

    log(f'Removing wisps for {rate_file_name}')

    if apply_flat:
        log('Applying flat to match wisp templates')

        crds_dict = {
            'INSTRUME': 'NIRCAM',
            'DETECTOR': model.meta.instrument.detector,
            'FILTER': model.meta.instrument.filter,
            'PUPIL': model.meta.instrument.pupil,
            'DATE-OBS': model.meta.observation.date,
            'TIME-OBS': model.meta.observation.time,
        }

        if use_custom_flat:
            fn = crds_dict['FILTER'].upper()
            det = crds_dict['DETECTOR'].upper()
            flatfile = os.path.join(field.flats_dir, f'flat_nircam_{fn}_{det}_CLEAR.fits')
            if not os.path.exists(flatfile):
                log(f'Flat file {os.path.basename(flatfile)} was not found in {field.flats_dir}')
                log(f'Falling back to CRDS flats')
                use_custom_flat = False

        if not use_custom_flat:
            import crds
            # Pull flat from CRDS using the current context
            flats = crds.getreferences(crds_dict, reftypes=['flat'], context=crds_context)
            try:
                flatfile = flats['flat']
            except KeyError:
                log(f'Flat was not found in CRDS with the parameters: {crds_dict}')
                return

        log(f'Using flat: {os.path.basename(flatfile)}')
        from jwst.datamodels import FlatModel
        from jwst.flatfield.flat_field import do_correction
        try:
            with FlatModel(flatfile) as flat:
                model, applied_flat = do_correction(model, flat)
        except:
            sleep(3)
            try:
                with FlatModel(flatfile) as flat:
                    model, applied_flat = do_correction(model, flat)
            except:
                sleep(10)
                with FlatModel(flatfile) as flat:
                    model, applied_flat = do_correction(model, flat)

    # Construct mask for median calculation
    mask = np.zeros(model.data.shape, dtype=bool)
    mask[np.isnan(model.data)] = True

    # Source detection
    threshold = detect_threshold(model.data, nsigma=5.5)
    segm = detect_sources(model.data, threshold, npixels=55)
    try:
        wobj = np.where(segm.data > 0)
    except:
        log(f'!!! Source detection failed for {rate_file}')
        raise
    mask[wobj] = True

    masked_im = model.data.copy()
    masked_im[mask] = 0

    # Consider subsets of image focused around wisps for variance scaling
    if detector == 'nrca3':
        x1, x2, y1, y2 = 100, 1300, 1100, 2046
    elif detector == 'nrca4':
        x1, x2, y1, y2 = 300, 1450, 0, 900
    elif detector == 'nrcb3':
        x1, x2, y1, y2 = 350, 1450, 0, 1000
    elif detector == 'nrcb4':
        x1, x2, y1, y2 = 400, 1700, 850, 2046

    im_seg = masked_im[y1:y2, x1:x2]

    # Read in template and mask nans
    wisp_file_names = [
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked.fits',
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked_smoothed_1x1.fits',
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked_smoothed_2x2.fits',
        f'WISP_{detector.upper()}_{filtname.upper()}_CLEAR_masked_smoothed_3x3.fits',
    ]
    short_file_names = [
        'Masked',
        'Masked + smoothed 1x1',
        'Masked + smoothed 3x3',
        'Masked + smoothed 5x5',
    ]

    if not os.path.exists(os.path.join(field.wisp_dir, wisp_file_names[0])):
        log(f"Wisp file {wisp_file_names[0]} not found, skipping wisp subtraction for {rate_file_name}")
        return

    if plot:
        import matplotlib.pyplot as plt
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), tight_layout=True)

    min_x, min_y = np.zeros(len(wisp_file_names)), np.zeros(len(wisp_file_names))
    for i in range(len(wisp_file_names)):
        wisp_file_name = wisp_file_names[i]
        short_file_name = short_file_names[i]
        log(f'{wisp_file_name}')

        wisp_template = fits.getdata(os.path.join(field.wisp_dir, wisp_file_name))
        wisp_template[np.isnan(wisp_template)] = 0
        wisp_template[model.data == 0] = 0

        wisp_seg = wisp_template[y1:y2, x1:x2]

        log('fitting coefficients')
        coeffs = np.arange(0.01, 1.5, 0.01)
        variance_mad = np.zeros(coeffs.shape[0])
        for j, c in enumerate(coeffs):
            variance_mad[j] = calc_variance(im_seg, wisp_seg, c)

        # Fit with a curve to base scaling off of trend, rather than scatter
        fit_mad = np.polyfit(coeffs, variance_mad, deg=2)
        pfit_mad = np.poly1d(fit_mad)

        # Show difference between curve and measured variances
        variance_mad_pred = pfit_mad(coeffs)
        diff = variance_mad - variance_mad_pred

        m = np.argmin(variance_mad)
        minval = coeffs[m]
        min_x[i] = minval
        min_y[i] = variance_mad[m]

        log(f'fit coefficient = {minval:.2f}')

        if plot:
            ax1.plot(coeffs, variance_mad_pred * 1e4, f'C{i}', lw=1.5, label=short_file_name)
            ax1.plot(coeffs, variance_mad * 1e4, f'C{i}o', lw=1.5)
            ax2.plot(coeffs, diff * 1e6, f'C{i}', lw=1)

            for ax in [ax1, ax2]:
                ax.axvline(minval, color=f'C{i}', ls=':', lw=0.5)

        i += 1

    which_template = np.argmin(min_y)
    minval = min_x[which_template]
    wisp_file_name = wisp_file_names[which_template]
    log(f'Using wisp template {wisp_file_name}')

    if plot:
        ax2.set_xlabel('coefficient')
        ax2.set_ylabel(r'residuals (10$^{-6}$)')
        ax1.set_ylabel(r'var (from MAD, 10$^{-4}$)')

        outplot = rate_file.replace('_rate.fits', '_wisp_fit.pdf')
        log(f'Saving fit diagnostic plot to {outplot}')
        fig.savefig(outplot)

    # Close model and open a clean version to clear anything we've done
    # to it (i.e., flat fielding)
    model.close()
    del wisp_template

    # Copy original
    log(f'Copying input to {rate_file_orig}')
    shutil.copy2(rate_file, rate_file_orig)

    wisp_template = fits.getdata(os.path.join(field.wisp_dir, wisp_file_name))
    wisp_template[np.isnan(wisp_template)] = 0
    wisp_template[model.data == 0] = 0

    model = ImageModel(rate_file)
    # Subtract out wisp
    corrected = model.data - minval * wisp_template
    model.data = corrected

    # Add history entry
    from stdatamodels import util as stutil
    time = datetime.now()
    stepdescription = f"Removed wisps ({wisp_file_name}, scale = {minval:.2f}) {time.strftime('%Y-%m-%d %H:%M:%S')}"
    substr = stutil.create_history_entry(stepdescription)
    model.history.append(substr)

    model.save(rate_file)
    log(f'cleaned image saved to {rate_file_name}')
    model.close()

    if plot:
        outplot = rate_file.replace('_rate.fits', '_wisp.pdf')
        log(f'Saving wisp plot to {outplot}')
        plot_two(rate_file, rate_file_orig, title1='Wisp removed', title2='Original Rate', save_file=outplot)


# ---------------------------------------------------------------------------
# Sky / background fitting helpers
# ---------------------------------------------------------------------------

def _gaussian(x, a, mu, sig):
    """Simple 1D Gaussian function."""
    return a * np.exp(-(x - mu)**2 / (2 * sig**2))


def fit_sky(data, use_bottleneck=True):
    """Measure 2D background using unmasked pixels.

    data is the original rate file masked using the mosaic tiermask.

    Useful for chips with a large, low surface brightness light left
    over from wisps. Model it, remove it, fit 1/f and then put the
    background back in.

    Parameters
    ----------
    data : array-like
        Image data with masked pixels set to zero.
    use_bottleneck : bool
        If True, byte-swap data for bottleneck compatibility.

    Returns
    -------
    background : array-like
        2D background map.
    """
    # First mask any leftover bright wisps that were not removed
    skystd = np.nanstd(data)
    # >2 sig works at least for F200W B4, check others!
    data[data > (2 * skystd)] = 0
    mask = data == 0
    if use_bottleneck:
        import bottleneck
        data.byteswap(inplace=True)
        data = data.view(data.dtype.newbyteorder('='))
    try:
        bkg = Background2D(data, box_size=128,
                           sigma_clip=SigmaClip(sigma=3),
                           filter_size=5,
                           bkg_estimator=BiweightLocationBackground(),
                           exclude_percentile=90, mask=mask,
                           interpolator=BkgZoomInterpolator())
    except:
        try:
            bkg = Background2D(data, box_size=128,
                               sigma_clip=SigmaClip(sigma=3),
                               filter_size=5,
                               bkg_estimator=BiweightLocationBackground(),
                               exclude_percentile=95, mask=mask,
                               interpolator=BkgZoomInterpolator())
        except:
            bkg = Background2D(data, box_size=128,
                               sigma_clip=SigmaClip(sigma=3),
                               filter_size=5,
                               bkg_estimator=BiweightLocationBackground(),
                               exclude_percentile=97.5, mask=mask,
                               interpolator=BkgZoomInterpolator())
    return bkg.background


def fit_sky_tot(data):
    """Fit distribution of sky fluxes with a Gaussian. Returns simple mean of Gaussian distribution."""
    mean, median, std = sigma_clipped_stats(data)
    bins = np.linspace(median - 10 * std, median + 10 * std, 1000)
    h, b = np.histogram(data, bins=bins)
    h = h / np.max(h)
    bc = 0.5 * (b[1:] + b[:-1])

    p0 = [1, bc[np.argmax(h)], std]
    popt, pcov = curve_fit(_gaussian, bc, h, p0=p0)

    return popt[1]


def fit_pedestal(data):
    """Fit distribution of sky fluxes with a Gaussian."""
    bins = np.arange(-1, 1.5, 0.001)
    h, b = np.histogram(data, bins=bins)
    bc = 0.5 * (b[1:] + b[:-1])
    binsize = b[1] - b[0]

    p0 = [10, bc[np.argmax(h)], 0.01]
    popt, pcov = curve_fit(_gaussian, bc, h, p0=p0)

    return popt[1]


# ---------------------------------------------------------------------------
# 1/f striping removal
# ---------------------------------------------------------------------------

def collapse_image(im, mask, maxiters, dimension='y', sig=2.):
    """Collapse an image along one dimension to check for striping.

    By default, collapse columns to show horizontal striping (collapsing
    along columns). Switch to vertical striping (collapsing along rows)
    with dimension='x'.

    Striping is measured as a sigma-clipped median of all unmasked pixels
    in the row or column.

    Parameters
    ----------
    im : array-like
        Image data array.
    mask : array-like
        Image mask array, True where pixels should be masked from the fit
        (where DQ>0, source flux has been masked, etc.)
    maxiters : int
        Maximum number of sigma-clipping iterations.
    dimension : str
        'y' collapses along columns (horizontal striping),
        'x' collapses along rows (vertical striping).
    sig : float
        Sigma to use in sigma clipping.
    """
    # axis=1 results in array along y
    # axis=0 results in array along x
    if dimension == 'y':
        res = sigma_clipped_stats(im, mask=mask, sigma=sig,
                                  cenfunc=np.nanmedian,
                                  stdfunc=np.nanstd, axis=1, maxiters=maxiters)
    elif dimension == 'x':
        res = sigma_clipped_stats(im, mask=mask, sigma=sig,
                                  cenfunc=np.nanmedian,
                                  stdfunc=np.nanstd, axis=0, maxiters=maxiters)

    return res[1]


class SourceMask:
    """Helper for making and dilating a source mask.

    See Photutils docs for make_source_mask.
    """
    def __init__(self, img, nsigma=3., npixels=3):
        self.img = img
        self.nsigma = nsigma
        self.npixels = npixels

    def single(self, filter_fwhm=3., tophat_size=5., mask=None):
        """Mask on a single scale."""
        if mask is None:
            image = self.img
        else:
            image = self.img * (1 - mask)
        mask = make_source_mask2(image, nsigma=self.nsigma,
                                npixels=self.npixels,
                                dilate_size=1, filter_fwhm=filter_fwhm)
        return dilate_mask(mask, tophat_size)

    def multiple(self, filter_fwhm=[3.], tophat_size=[3.], mask=None):
        """Mask repeatedly on different scales."""
        if mask is None:
            self.mask = np.zeros(self.img.shape, dtype=bool)
        for fwhm, tophat in zip(filter_fwhm, tophat_size):
            smask = self.single(filter_fwhm=fwhm, tophat_size=tophat)
            self.mask = self.mask | smask  # Or the masks at each iteration
        return self.mask


def produce_mask(data, bkg, sigma=3.0, maxiter=10, nsigma=2.5, npixels=10, mask=None, radius=10):
    """Produce a source mask using segmentation detection."""
    from photutils.utils import circular_footprint
    sigma_clip = SigmaClip(sigma, maxiter)
    threshold = detect_threshold(data - bkg, nsigma, sigma_clip=sigma_clip, mask=mask)
    segment_img = detect_sources(data - bkg, threshold, npixels)
    footprint = circular_footprint(radius)
    mask = segment_img.make_source_mask(footprint=footprint)
    return mask


def dilate_mask(mask, tophat_size):
    """Take a mask and make the masked regions bigger."""
    area = np.pi * tophat_size**2.
    kernel = Tophat2DKernel(tophat_size)
    dilated_mask = convolve(mask, kernel) >= 1. / area
    return dilated_mask


def masksources(image):
    """Create a tiered source mask for 1/f noise removal.

    Parameters
    ----------
    image : str
        Full path to a rate.fits image.

    Returns
    -------
    outmask : array-like
        Integer mask array (1 = masked source pixel).
    """
    from jwst.datamodels import ImageModel
    model = ImageModel(image)
    sci = model.data
    err = model.err
    wht = model.wht
    dq = model.dq

    # Bad pixel mask for SegmentationImage.make_source_mask()
    from jwst.datamodels import dqflags
    bpflag = dqflags.pixel['DO_NOT_USE']
    bp = np.bitwise_and(dq, bpflag)
    bpmask = np.logical_not(bp == 0)
    log('masksources: estimating background')
    # Make a robust estimate of the mean background and replace blank areas
    sci_nan = np.choose(np.isnan(sci), (sci, err))
    # Use the biweight estimator as a robust estimate of the mean background
    robust_mean_background = biweight_location(sci_nan, c=6., ignore_nan=True)
    sci_filled = np.choose(np.isnan(sci), (sci, robust_mean_background))

    log('masksources: initial source mask')
    # Make an initial source mask
    ring = Ring2DKernel(40, 3)
    filtered = median_filter(sci_filled, footprint=ring.array)

    log('masksources: mask tier 1')
    # Mask out sources iteratively
    # Try a reasonably big filter for masking the bright stuff
    convolved_difference = convolve_fft(sci_filled - filtered, Gaussian2DKernel(25))
    threshold = detect_threshold(convolved_difference, nsigma=3.0)
    segment_img1 = detect_sources(convolved_difference, threshold, npixels=15, mask=bpmask)
    mask1 = SegmentationImage.make_source_mask(segment_img1)

    # Grow the largest mask
    temp = np.zeros(sci.shape)
    temp[mask1] = 1
    sources = np.logical_not(temp == 0)
    dilation_sigma = 3
    dilation_window = 5
    dilation_kernel = Gaussian2DKernel(dilation_sigma)
    source_wings = binary_dilation(sources, dilation_kernel)
    temp[source_wings] = 1
    mask1 = np.logical_not(temp == 0)

    log('masksources: mask tier 2')
    # A smaller smoothing for the next tier
    convolved_difference = convolve_fft(sci_filled - filtered, Gaussian2DKernel(10))
    threshold = detect_threshold(convolved_difference, nsigma=3.0)
    segment_img2 = detect_sources(convolved_difference, threshold, npixels=10, mask=mask1)
    mask2 = SegmentationImage.make_source_mask(segment_img2) | mask1

    log('masksources: mask tier 3')
    # Still smaller
    convolved_difference = convolve_fft(sci_filled - filtered, Gaussian2DKernel(5))
    threshold = detect_threshold(convolved_difference, nsigma=3.0)
    segment_img3 = detect_sources(convolved_difference, threshold, npixels=5, mask=mask2)
    mask3 = SegmentationImage.make_source_mask(segment_img3) | mask2

    log('masksources: mask tier 4')
    # Smallest
    convolved_difference = convolve_fft(sci_filled - filtered, Gaussian2DKernel(2))
    threshold = detect_threshold(convolved_difference, nsigma=3.0)
    segment_img4 = detect_sources(convolved_difference, threshold, npixels=3, mask=mask3)
    mask4 = SegmentationImage.make_source_mask(segment_img4)
    dilated_mask4 = dilate_mask(mask4, 3)
    finalmask = mask4 | mask3

    # Save output mask
    maskname = image.replace('.fits', '_1fmask.fits')
    log(f'masksources: saving mask to {maskname}')
    outmask = np.zeros(finalmask.shape, dtype=int)
    outmask[finalmask] = 1
    fits.writeto(maskname, outmask, overwrite=True)
    return outmask


def measure_fullimage_striping(fitdata, mask, maxiters):
    """Measure striping in countrate images using the full rows.

    Measures the horizontal and vertical striping present across the
    full image. The full image median will be used for amp-rows that
    are entirely or mostly masked out.

    Parameters
    ----------
    fitdata : array-like
        Image data array for fitting.
    mask : array-like
        Image mask array, True where pixels should be masked.

    Returns
    -------
    horizontal_striping, vertical_striping : tuple of array-like
    """
    # Fit horizontal striping, collapsing along columns
    horizontal_striping = collapse_image(fitdata, mask, maxiters, dimension='y')
    # Remove horizontal striping, requires taking transpose of image
    temp_image = fitdata.T - horizontal_striping
    # Transpose back
    temp_image2 = temp_image.T

    # Fit vertical striping, collapsing along rows
    vertical_striping = collapse_image(temp_image2, mask, maxiters, dimension='x')

    return horizontal_striping, vertical_striping


def find_optimal_threshold(model, mask, full_horizontal, maxiters):
    """Find optimal masking threshold by minimizing variance.

    Parameters
    ----------
    model : ImageModel
        JWST data model.
    mask : array-like
        Source mask.
    full_horizontal : array-like
        Full-image horizontal striping pattern.
    maxiters : int
        Maximum sigma-clipping iterations.

    Returns
    -------
    optimal_maskparam : float
    """
    maskparams = np.array([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.65, 0.70, 0.75, 0.80])

    var_mad = np.zeros(len(maskparams))
    for m, maskparam in enumerate(maskparams):
        log(f'trying maskparam = {maskparam}')
        hstriping = np.zeros(model.data.shape)
        for amp in ['A', 'B', 'C', 'D']:
            rowstart, rowstop, colstart, colstop = NIR_AMPS[amp]['data']
            ampdata = model.data[:, colstart:colstop]
            ampmask = mask[:, colstart:colstop]
            # Fit horizontal striping in amp, collapsing along columns
            hstriping_amp = collapse_image(ampdata, ampmask, maxiters, dimension='y')
            # Check that at least 1/4 of pixels in each row are unmasked
            nmask = np.sum(ampmask, axis=1)
            max_nmask = ampmask.shape[1] * maskparam
            hstriping[nmask > max_nmask, colstart:colstop] = full_horizontal[nmask > max_nmask][:, None]
            hstriping[nmask <= max_nmask, colstart:colstop] = hstriping_amp[nmask <= max_nmask][:, None]

        # Remove horizontal striping
        temp_sub = model.data - hstriping

        # Fit vertical striping, collapsing along rows
        vstriping = collapse_image(temp_sub, mask, maxiters, dimension='x')

        temp_sci = model.data - hstriping

        # Transpose back
        temp_sci = temp_sci - vstriping
        sigma_mad = sigma_clipped_stats(temp_sci[~mask], cenfunc=np.nanmedian, stdfunc=np.nanstd, sigma=5)[2]
        var_mad[m] = sigma_mad**2

    minm = np.argmin(var_mad)
    return maskparams[minm]


def remove_striping(image, stage_config, field):
    """Remove 1/f striping in rate.fits files before flat fielding.

    Measures and subtracts the horizontal and vertical striping present in
    countrate images. The striping is most likely due to 1/f noise, and
    the RefPixStep does not fully remove the pattern.

    Parameters
    ----------
    image : str
        Full path to rate file.
    stage_config : dict
        Stage 1 configuration dict.
    field : Field
        Field dataclass.
    """
    step_cfg = stage_config.get('remove_striping', {})
    apply_flat = step_cfg.get('apply_flat', True)
    use_custom_flat = step_cfg.get('use_custom_flat', False)
    mask_sources = step_cfg.get('mask_sources', True)
    maskparam = step_cfg.get('maskparam', 'none')
    subtract_background = step_cfg.get('subtract_background', True)
    maxiters = step_cfg.get('maxiters', 3)
    use_bottleneck = stage_config.get('use_bottleneck', True)
    do_plot = step_cfg.get('plot', True)

    if maskparam == 'none':
        maskparam = None

    try:
        crds_context = os.environ['CRDS_CONTEXT']
    except KeyError:
        import crds
        crds_context = crds.get_default_context()

    from jwst.datamodels import ImageModel
    model = ImageModel(image)
    # Check that image has not already been corrected
    for entry in model.history:
        if 'Removed horizontal,vertical striping' in entry['description']:
            log(f'{image} already corrected for 1/f noise, exiting')
            return

    log('Measuring image striping')
    log(f'Working on {image}')

    # Apply the flat to get a cleaner measurement of the striping
    if apply_flat:
        log('Applying flat for cleaner measurement of striping patterns')

        crds_dict = {
            'INSTRUME': 'NIRCAM',
            'DETECTOR': model.meta.instrument.detector,
            'FILTER': model.meta.instrument.filter,
            'PUPIL': model.meta.instrument.pupil,
            'DATE-OBS': model.meta.observation.date,
            'TIME-OBS': model.meta.observation.time,
        }

        if use_custom_flat:
            fn = crds_dict['FILTER'].upper()
            det = crds_dict['DETECTOR'].upper()
            flatfile = os.path.join(field.flats_dir, f'flat_nircam_{fn}_{det}_CLEAR.fits')
            if not os.path.exists(flatfile):
                log(f'Flat file {os.path.basename(flatfile)} was not found in {field.flats_dir}')
                log(f'Falling back to CRDS flats')
                use_custom_flat = False

        if not use_custom_flat:
            import crds
            # Pull flat from CRDS using the current context
            flats = crds.getreferences(crds_dict, reftypes=['flat'], context=crds_context)
            try:
                flatfile = flats['flat']
            except KeyError:
                log(f'Flat was not found in CRDS with the parameters: {crds_dict}')
                return

        log(f'Using flat: {os.path.basename(flatfile)}')
        from jwst.flatfield.flat_field import do_correction
        from jwst.datamodels import FlatModel
        try:
            with FlatModel(flatfile) as flat:
                model, applied_flat = do_correction(model, flat)
        except:
            sleep(5)
            try:
                with FlatModel(flatfile) as flat:
                    model, applied_flat = do_correction(model, flat)
            except:
                sleep(5)
                with FlatModel(flatfile) as flat:
                    model, applied_flat = do_correction(model, flat)

    mask = np.zeros(model.data.shape, dtype=bool)
    mask[model.dq > 0] = True

    if mask_sources:
        # First look for a source mask that already exists
        srcmask = maskname = image.replace('.fits', '_1fmask.fits')
        if os.path.exists(srcmask):
            log(f'Using existing source mask {srcmask}')
            seg = fits.getdata(srcmask)
        else:
            log('Detecting sources to mask out source flux')
            seg = masksources(image)

        wobj = np.where(seg > 0)
        mask[wobj] = True

    # Measure the pedestal in the unmasked parts of the image
    log('Measuring the pedestal in the image')
    pedestal_data = model.data[~mask]
    pedestal_data = pedestal_data.flatten()
    median_image = np.median(pedestal_data)
    log(f'Image median (unmasked and DQ==0): {median_image:.5e}')
    try:
        pedestal = fit_pedestal(pedestal_data)
    except RuntimeError as e:
        log("Can't fit sky, using median value instead")
        pedestal = median_image
    else:
        log(f'Fit pedestal: {pedestal:.5e}')
    # Subtract off pedestal so it's not included in fit
    model.data -= pedestal

    if subtract_background:
        try:
            log('Further measuring and subtracting the 2D background')
            backgrounddata = deepcopy(model.data)
            backgrounddata[mask > 0] = 0
            bkgd = fit_sky(backgrounddata, use_bottleneck=use_bottleneck)
            # Subtract off background so it's not included in fit
            model.data -= bkgd
        except:
            log(f'2D background subtraction failed for {image}, only using pedestal subtraction')

    # Measure full pattern across image
    full_horizontal, vertical_striping = measure_fullimage_striping(model.data, mask, maxiters)
    # If thresh is not defined by user, search array of possible values
    if maskparam is None:
        try:
            log('maskparam=None, automatically determining optimal value (can be slow)')
            maskparam = find_optimal_threshold(model, mask, full_horizontal, maxiters)
        except:
            log(f'find_optimal_threshold failed on {image}')
            raise

        log(f'Using threshold: {maskparam:.2f}')

    horizontal_striping = np.zeros(model.data.shape)
    vertical_striping = np.zeros(model.data.shape)

    # Keep track of number of times the number of masked pixels
    # in an amp-row exceeds thresh and a full-row median is used instead
    ampcounts = []
    for amp in ['A', 'B', 'C', 'D']:
        ampcount = 0
        rowstart, rowstop, colstart, colstop = NIR_AMPS[amp]['data']
        ampdata = model.data[:, colstart:colstop]
        ampmask = mask[:, colstart:colstop]
        # Fit horizontal striping in amp, collapsing along columns
        hstriping_amp = collapse_image(ampdata, ampmask, dimension='y', maxiters=maxiters)
        # Check that at least maskparam of pixels in each row are unmasked
        nmask = np.sum(ampmask, axis=1)
        for i, row in enumerate(ampmask):
            if nmask[i] > (ampmask.shape[1] * maskparam):
                # Use median from full row
                horizontal_striping[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            # Upper limit on total number of masked pixels
            elif nmask[i] > (0.95 * ampmask.shape[1]):
                horizontal_striping[i, colstart:colstop] = full_horizontal[i]
                ampcount += 1
            else:
                # Use the amp fit
                horizontal_striping[i, colstart:colstop] = hstriping_amp[i]
        ampcounts.append('%s-%i' % (amp, ampcount))

    ampinfo = ', '.join(ampcounts)
    log(f'{os.path.basename(image)}, full row medians used: {ampinfo}/{rowstop-rowstart}')

    # Remove horizontal striping
    temp_sub = model.data - horizontal_striping

    # Fit vertical striping, collapsing along rows
    vstriping = collapse_image(temp_sub, mask, maxiters, dimension='x')
    vertical_striping[:, :] = vstriping

    model.close()

    # Copy image
    image_orig = image.replace('.fits', '_orig.fits')
    if do_plot:
        log(f"Copying input to {image_orig}")
        shutil.copy2(image, image_orig)

    # Remove striping from science image
    with ImageModel(image) as immodel:
        sci = immodel.data
        # To replace zeros
        wzero = np.where(sci == 0)
        temp_sci = sci - horizontal_striping
        # Transpose back
        outsci = temp_sci - vertical_striping
        outsci[wzero] = 0
        # Replace NaNs with zeros and update DQ array
        wnan = np.isnan(outsci)
        from jwst.datamodels import dqflags
        bpflag = dqflags.pixel['DO_NOT_USE']
        outsci[wnan] = 0
        immodel.dq[wnan] = np.bitwise_or(immodel.dq[wnan], bpflag)

        # Write output
        immodel.data = outsci
        # Add history entry
        time = datetime.now()
        stepdescription = f"Removed horizontal,vertical striping; {time.strftime('%Y-%m-%d %H:%M:%S')}"
        software_dict = {
            'name': 'remstriping.py',
            'author': 'Micaela Bagley',
            'version': '1.0',
            'homepage': 'ceers.github.io',
        }
        from stdatamodels import util as stutil
        substr = stutil.create_history_entry(stepdescription, software=software_dict)
        immodel.history.append(substr)
        log(f'Saving cleaned image to {image}')
        immodel.save(image)

    if do_plot:
        image_name = os.path.basename(image)
        log(f'Making striping removal plot for {image_name}')
        image_orig = image.replace('_rate.fits', '_rate_orig.fits')
        output_file = image.replace('_rate.fits', '_striping.pdf')
        plot_two(image, image_orig, title1='Striping removed', title2='Original Rate', save_file=output_file)


# ---------------------------------------------------------------------------
# Persistence flagging
# ---------------------------------------------------------------------------

def persistence_step(rate_files):
    """Flag persistence artifacts using snowblind.

    Parameters
    ----------
    rate_files : list of str
        Full paths to rate files for a single filter.
    """
    if len(rate_files) == 0:
        return

    from jwst.datamodels import ImageModel, ModelContainer
    path = os.path.dirname(rate_files[0])
    images = ModelContainer()
    for rate_file in rate_files:
        if os.path.exists(rate_file.replace('_rate.fits', '_jump.fits')):
            images.append(ImageModel(rate_file))

    import snowblind
    output = snowblind.PersistenceFlagStep.call(images,
        save_results=True,
        suffix="rate",
        input_dir=path,
        output_dir=path)

    # Detector1 only emits _jump.fits as a side product (used above by
    # PersistenceFlagStep). Other intermediates (_rateints, _output_pers,
    # _trapsfilled, _persistence) are no longer written.
    for rate_file in rate_files:
        try:
            os.remove(rate_file.replace('_rate.fits', '_jump.fits'))
            log(f"Removed _jump.fits for {os.path.basename(rate_file).replace('_rate.fits', '')}")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Plotting utility (ported from nircamx/utils.py)
# ---------------------------------------------------------------------------

def plot_two(image1, image2, group=0, title1=None, title2=None, save_file=None, scaling=None):
    """Display two images side-by-side for comparison.

    Parameters
    ----------
    image1, image2 : str or array-like
        Filenames or data arrays.
    group : int
        Group index to plot for 4D uncal images.
    title1, title2 : str, optional
        Titles for each panel.
    save_file : str, optional
        Output file path.
    scaling : int, optional
        None for independent scaling, 1 to match to image1, 2 to match to image2.
    """
    import matplotlib.pyplot as plt
    from astropy.visualization import ImageNormalize, ZScaleInterval

    if isinstance(image1, str) or isinstance(image2, str):
        im1 = fits.getdata(image1, 'SCI')
        im2 = fits.getdata(image2, 'SCI')
    else:
        im1 = image1
        im2 = image2
    # If images are 4D, pick slices to plot
    if len(im1.shape) == 4:
        im1 = im1[0, group, :, :]
    if len(im2.shape) == 4:
        im2 = im2[0, group, :, :]

    if scaling is None:
        norm1 = ImageNormalize(im1, interval=ZScaleInterval())
        norm2 = ImageNormalize(im2, interval=ZScaleInterval())
    elif scaling == 1:
        norm1 = ImageNormalize(im1, interval=ZScaleInterval())
        norm2 = norm1
    elif scaling == 2:
        norm2 = ImageNormalize(im2, interval=ZScaleInterval())
        norm1 = norm2
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(8, 4), tight_layout=True)
    ax1.imshow(im1, origin='lower', interpolation='none', cmap='Greys', norm=norm1)
    ax2.imshow(im2, origin='lower', interpolation='none', cmap='Greys', norm=norm2)
    ax1.axis('off')
    ax2.axis('off')
    if title1:
        ax1.set_title(title1)
    if title2:
        ax2.set_title(title2)
    if save_file is not None:
        fig.savefig(save_file)
    plt.close(fig)
