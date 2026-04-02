"""
Stage 2: Image2Pipeline calibration, edge removal, sky subtraction,
variance rescaling, and mask application for NIRCam imaging.

Ported from nircamx.stage2 with config/path/logging refactoring.
"""

import os
import shutil
import warnings
from datetime import datetime

import numpy as np
from numpy import ma
import matplotlib.pyplot as plt
from scipy.ndimage import gaussian_filter
from scipy.optimize import curve_fit
from astropy.io import fits
from astropy.nddata import block_reduce
from astropy.stats import (
    sigma_clip,
    biweight_location,
    biweight_midvariance,
    sigma_clipped_stats,
)
from astropy.visualization import ImageNormalize, ZScaleInterval
from astropy.convolution import (
    Tophat2DKernel,
    Gaussian2DKernel,
    Ring2DKernel,
    convolve,
    convolve_fft,
)
from regions import Regions

from campfire_pipeline.common.io import log
from campfire_pipeline.nircam.constants import (
    NIR_AMPS,
    NIR_REFERENCE_SECTIONS,
    SW_FILTERS,
    LW_FILTERS,
)


# ---------------------------------------------------------------------------
# Helper: Gaussian for sky fitting
# ---------------------------------------------------------------------------

def _gaussian(x, a, mu, sig):
    return a * np.exp(-(x - mu) ** 2 / (2 * sig ** 2))


def _calc_variance(data, template, coeff):
    """Calculates the absolute median deviation of wisp subtracted image.

    Determines the variance of the function: image - coefficient * template.
    Using the median absolute deviation squared. This is not scaled to
    represent the standard deviation of normally distributed data, as would
    be appropriate for an error estimator. However, the caller will
    find the coefficient that minimizes this variance, and so the relative
    values are what matter.

    Parameters
    ----------
    data : array
        Image array of masked data values.
    template : array
        Image array of wisp template.
    coeff : float
        Coefficient for scaling wisp template.

    Returns
    -------
    float
        Variance estimate (MAD squared).
    """
    from astropy.stats import median_absolute_deviation
    sub = data - coeff * template
    mad = median_absolute_deviation(sub, ignore_nan=True)
    return mad ** 2


def _fit_sky_tot(data):
    """Fit distribution of sky fluxes with a Gaussian.

    Returns the simple mean of the Gaussian distribution.
    """
    mean, median, std = sigma_clipped_stats(data)
    bins = np.linspace(median - 10 * std, median + 10 * std, 1000)
    h, b = np.histogram(data, bins=bins)
    h = h / np.max(h)
    bc = 0.5 * (b[1:] + b[:-1])

    p0 = [1, bc[np.argmax(h)], std]
    popt, pcov = curve_fit(_gaussian, bc, h, p0=p0)

    return popt[1]


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def image2_step(rate_file, field, stage_config, overwrite=False):
    """Run the JWST Image2Pipeline on a single rate file.

    Parameters
    ----------
    rate_file : str
        Full path to the *_rate.fits file.
    field : Field
        Field dataclass providing directory paths.
    stage_config : dict
        Stage 2 configuration dictionary.
    overwrite : bool
        Overwrite existing products.
    """
    from jwst.pipeline import calwebb_image2

    step_config = stage_config.get('image2', {})

    filtname = rate_file.split('/')[-2]
    assert (filtname in SW_FILTERS) or (filtname in LW_FILTERS)

    rate_file_name = os.path.basename(rate_file)
    cal_file_name = rate_file_name.replace('_rate.fits', '_cal.fits')
    output_dir = os.path.join(field.stage2_dir, filtname)
    cal_file = os.path.join(output_dir, cal_file_name)

    if os.path.exists(cal_file) and not overwrite:
        log(f"Skipping image2_step on {rate_file_name}, cal file already exists")
        return

    log(f"Running image2_step on {rate_file_name}")

    kwargs = {
        'output_dir': output_dir,
        'save_results': True,
        'steps': {
            'bkg_subtract': {'skip': True},
            'assign_wcs': {
                'skip': False,
                'save_results': False,
                'sip_approx': True,
                'sip_degree': None,
                'sip_inv_degree': None,
                'sip_max_inv_pix_error': 0.25,
                'sip_max_pix_error': 0.25,
                'sip_npoints': 32,
                'slit_y_high': 0.55,
                'slit_y_low': -0.55,
            },
            'flat_field': {'skip': False},
            'photom': {'skip': False},
            'resample': {'skip': True},
        },
    }

    if step_config.get('use_custom_flat', False):
        # jw01727028001_04101_00003_nrcalong_rate.fits
        detector = rate_file_name.split('_')[-2]
        flat_file = os.path.join(
            field.flats_dir,
            f'flat_nircam_{filtname.upper()}_{detector.upper()}_CLEAR.fits',
        )
        if os.path.exists(flat_file):
            kwargs['steps']['flat_field']['user_supplied_flat'] = flat_file
        else:
            log(f'Flat file {os.path.basename(flat_file)} was not found in {field.flats_dir}')
            log(f'Falling back to CRDS flats')

    try:
        calwebb_image2.Image2Pipeline.call(rate_file, **kwargs)
    except ValueError as e:
        print(rate_file)
        raise e


def remove_edge(cal_file, field, stage_config):
    """Remove noisy edge columns/rows by flagging them in the DQ array.

    Parameters
    ----------
    cal_file : str
        Full path to the *_cal.fits file.
    field : Field
        Field dataclass (unused here, kept for interface consistency).
    stage_config : dict
        Stage 2 configuration dictionary.
    """
    from jwst.datamodels import ImageModel
    from stdatamodels import util as stutil

    cal_file_name = os.path.basename(cal_file)

    with ImageModel(cal_file) as model:
        # check that image has not already had edges removed
        for entry in model.history:
            if 'Removed edges' in entry['description']:
                log(f'Edges already removed for {cal_file_name}, skipping...')
                return
        if os.path.exists(cal_file.replace('_cal.fits', '_before_removing_edge.fits')):
            log(f'Edges already removed for {cal_file_name}, skipping...')
            return

        log(f'Running edge removal for {cal_file_name}')

        size = model.data.shape[0]

        mean_ = []
        mean_h = []
        for ii in range(size):
            mean_.append(np.mean(model.data[:, ii]))
            mean_h.append(np.mean(model.data[ii, :]))

        index_beg = 0
        for ii in range(size):
            if index_beg == 0:
                if np.abs(np.mean(model.data[:, ii])) > np.std(mean_):
                    model.dq[:, ii] = 1
                else:
                    index_beg = 1

        index_end = 0
        for ii in range(size):
            if index_end == 0:
                if np.abs(np.mean(model.data[:, size - 1 - ii])) > np.std(mean_):
                    model.dq[:, size - 1 - ii] = 1
                else:
                    index_end = 1

        index_beg = 0
        for ii in range(size):
            if index_beg == 0:
                if np.abs(np.mean(model.data[ii, :])) > np.std(mean_h):
                    model.dq[ii, :] = 1
                else:
                    index_beg = 1

        index_end = 0
        for ii in range(size):
            if index_end == 0:
                if np.abs(np.mean(model.data[size - 1 - ii, :])) > np.std(mean_h):
                    model.dq[size - 1 - ii, :] = 1
                else:
                    index_end = 1

        time = datetime.now()
        stepdescription = f"Removed edges; {time.strftime('%Y-%m-%d %H:%M:%S')}"
        substr = stutil.create_history_entry(stepdescription)
        model.history.append(substr)

        model.save(cal_file)


def apply_masks(cal_file, field, stage_config):
    """Apply region-file masks to a cal file by flagging DQ bits.

    Parameters
    ----------
    cal_file : str
        Full path to the *_cal.fits file.
    field : Field
        Field dataclass providing mask_dir path.
    stage_config : dict
        Stage 2 configuration dictionary.
    """
    cal_file_name = os.path.basename(cal_file)
    filtname = cal_file.split('/')[-2]
    reg_file = os.path.join(
        field.mask_dir, filtname,
        cal_file_name.replace('_cal.fits', '.reg'),
    )
    if not os.path.exists(reg_file):
        log(f'No mask found for {cal_file_name}, skipping')
        return

    mask_config = stage_config.get('apply_mask', {})
    flag = mask_config.get('mask_flag', 1024)
    set_to_nan = mask_config.get('mask_set_nan', False)

    log(f'Applying mask to {cal_file_name}')
    from jwst.datamodels import ImageModel

    with ImageModel(cal_file) as model:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            wcs = model.get_fits_wcs()
        shape = np.shape(model.data)

        regs = Regions.read(reg_file)
        for reg in regs:
            reg = reg.to_pixel(wcs)
            mask = reg.to_mask(mode='center')
            mask = mask.to_image(shape)
            try:
                mask = mask.astype(bool)
            except (ValueError, TypeError) as e:
                log(f"Warning: skipping region in {reg_file}, could not convert mask to bool: {e}")
                continue

            if set_to_nan:
                model.data[mask] = np.nan

            mask = (mask * flag).astype('uint32')
            model.dq |= mask

        model.save(cal_file)


def sky_subtraction(cal_file, field, stage_config, overwrite=False):
    """Subtract a constant sky pedestal value from the cal file.

    Parameters
    ----------
    cal_file : str
        Full path to the *_cal.fits file.
    field : Field
        Field dataclass providing stage directory paths.
    stage_config : dict
        Stage 2 configuration dictionary.
    overwrite : bool
        Overwrite existing products.
    """
    from stdatamodels import util as stutil
    from jwst.datamodels import ImageModel

    with ImageModel(cal_file) as model:
        for entry in model.history:
            if 'Removed sky' in entry['description'] and not overwrite:
                log(f'Sky subtraction already done for {os.path.basename(cal_file)}, skipping...')
                return

    log(f'Running sky subtraction on {os.path.basename(cal_file)}')

    with ImageModel(cal_file) as model:
        sci = model.data
        dq = model.dq

        # Read in mask created during 1/f correction
        srcmask = cal_file.replace(field.stage2_dir, field.stage1_dir)
        srcmask = srcmask.replace('_cal.fits', '_rate_1fmask.fits')
        log(f'Using existing source mask {os.path.basename(srcmask)}')
        seg = fits.getdata(srcmask)
        w = np.where((dq == 0) & (seg == 0))
        data = sci[w].flatten()

        # Apply a sigma clip to the data
        data = sigma_clip(data, sigma_upper=3, sigma_lower=10, maxiters=5, masked=False)
        data = data[~np.isinf(data) & ~np.isnan(data)]

        # Fit the pedestal
        try:
            sky = _fit_sky_tot(data)
        except:
            print(f'Failed on {cal_file}!!')
            raise

        # Subtract off sky
        model.data -= sky

        # Update header
        model.meta.background.level = sky
        model.meta.background.subtracted = True
        model.meta.background.method = 'local'

        log(f"Saving to {os.path.basename(cal_file)}")
        time = datetime.now()
        stepdescription = f"Removed sky {time.strftime('%Y-%m-%d %H:%M:%S')}"
        substr = stutil.create_history_entry(stepdescription)
        model.history.append(substr)

        model.save(cal_file)


def rescale_variance(cal_file, field, stage_config, overwrite=False):
    """Perform variance map scaling.

    This routine models the 2D background of an individual exposure and rescales
    the variance maps to match the measured variance of background pixels.

     1. Run a background subtraction routine that creates a 2D model of
        the background in an image. This will be used to calculate the
        sky variance.

     2. Rescale the variance maps. Determines a robust sky variance in the
        image and scales the VAR_RNOISE array to reproduce this value. The
        VAR_RNOISE arrays are used for inverse variance weighting during
        drizzling, so this step ensures that the resulting error arrays will
        include the rms sky fluctuations.

    Parameters
    ----------
    cal_file : str
        Full path to the *_cal.fits file.
    field : Field
        Field dataclass (unused here, kept for interface consistency).
    stage_config : dict
        Stage 2 configuration dictionary.
    overwrite : bool
        Overwrite existing products.
    """
    from stdatamodels import util as stutil
    from jwst.datamodels import ImageModel
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    var_config = stage_config.get('bkgsub_var', {})

    with ImageModel(cal_file) as model:
        for entry in model.history:
            if 'Rescaled variance' in entry['description'] and not overwrite:
                log(f'Variance rescaling already done for {os.path.basename(cal_file)}, skipping...')
                return

    log(f'Rescaling variance for {os.path.basename(cal_file)}')

    # Run a full 2D background subtraction routine
    bkg = SubtractBackground(
        ring_radius_in=var_config.get('ring_radius_in', 40),
        ring_width=var_config.get('ring_width', 3),
        ring_clip_max_sigma=var_config.get('ring_clip_max_sigma', 5.0),
        ring_clip_box_size=var_config.get('ring_clip_box_size', 100),
        ring_clip_filter_size=var_config.get('ring_clip_filter_size', 3),
        tier_kernel_size=var_config.get('tier_kernel_size', [25, 15, 5, 2]),
        tier_npixels=var_config.get('tier_npixels', [15, 15, 5, 2]),
        tier_nsigma=var_config.get('tier_nsigma', [3, 3, 3, 3]),
        tier_dilate_size=var_config.get('tier_dilate_size', [0, 0, 0, 3]),
        bg_box_size=var_config.get('bg_box_size', 10),
        bg_filter_size=var_config.get('bg_filter_size', 5),
        bg_exclude_percentile=var_config.get('bg_exclude_percentile', 90),
        bg_sigma=var_config.get('bg_sigma', 3),
        bg_interpolator=var_config.get('bg_interpolator', 'zoom'),
        suffix='bkgsub',
        replace_sci=True,
    )
    try:
        bkg.call(cal_file)
    except:
        print(f"!!! failed on {cal_file}")
        raise

    # rescale variance maps
    block_size = var_config.get('block_size', 7)
    with ImageModel(cal_file) as model:
        sci = model.data
        var_rnoise = model.var_rnoise

        block_mask = block_reduce(bkg.mask_final, block_size)
        unmasked_frac = np.sum(block_mask == 0) / np.sum(block_mask >= 0)

        block_sci = block_reduce(sci, block_size)
        block_mask = block_mask != 0
        unmasked_bins = block_sci[block_mask == 0]
        variance = biweight_midvariance(unmasked_bins)
        skyvar = variance / block_size ** 2

        block_var_rnoise = block_reduce(var_rnoise, block_size)
        unmasked_bins = block_var_rnoise[block_mask == 0]
        mean = biweight_location(unmasked_bins)
        masked_mean_var_rnoise = mean / block_size ** 2  # because block_reduce sums by default

        correction_factor = skyvar / masked_mean_var_rnoise

        predicted_skyvar = correction_factor * var_rnoise

        model.var_rnoise = predicted_skyvar

        log(f"Robust masked mean VAR_RDNOISE: {masked_mean_var_rnoise:.3e}")
        log(f"Robust masked mean SKY_VARIANCE: {skyvar:.3e}")
        log(f"Correction factor: {correction_factor:.2f}")
        log(f"Fraction of pixels unmasked: {unmasked_frac * 100:.1f}%")

        ### fix holes in variance maps, not sure if this is still necessary
        rnoise = model.var_rnoise
        poisson = model.var_poisson
        flat = model.var_flat

        w = np.where(rnoise == 0)
        rnoise[w] = np.inf

        w = np.where(poisson == 0)
        poisson[w] = np.inf

        w = np.where(flat == 0)
        flat[w] = np.inf

        model.var_rnoise = rnoise
        model.var_poisson = poisson
        model.flat = flat

        log(f"Saving to {os.path.basename(cal_file)}")
        time = datetime.now()
        stepdescription = f"Rescaled variance {time.strftime('%Y-%m-%d %H:%M:%S')}"
        substr = stutil.create_history_entry(stepdescription)
        model.history.append(substr)

        model.save(cal_file)

    try:
        os.remove(cal_file.replace('_cal.fits', '_cal_bkgsub.fits'))
    except OSError:
        pass


def plot_cal_rate(cal_file, field=None, stage_config=None):
    """Create a quick-look PNG of a cal file with DQ overlay.

    Parameters
    ----------
    cal_file : str
        Full path to the *_cal.fits file.
    field : Field, optional
        Field dataclass (unused, kept for interface consistency).
    stage_config : dict, optional
        Stage 2 configuration dictionary (unused).
    """
    from PIL import Image
    import matplotlib.cm as cm

    outfile = cal_file.replace('_cal.fits', '_cal.png')
    log(outfile)
    im = fits.getdata(cal_file, 'SCI')
    dq = fits.getdata(cal_file, 'DQ')
    norm = ImageNormalize(im, interval=ZScaleInterval())

    # Normalize image to 0-1 range
    im_normed = norm(im)

    # Handle NaNs/infs that might cause issues
    im_normed = np.nan_to_num(im_normed, nan=0.0, posinf=0.0, neginf=0.0)

    # Apply Greys colormap (returns RGBA)
    greys_cmap = cm.get_cmap('Greys')
    im_rgb = greys_cmap(im_normed)  # Shape: (h, w, 4)

    # Apply pink_r colormap to masked regions
    mask = dq > 0
    if mask.any():
        pink_cmap = cm.get_cmap('pink_r')
        masked_rgb = pink_cmap(im_normed)  # Shape: (h, w, 4)
        im_rgb[mask] = masked_rgb[mask]

    # Convert to 8-bit RGB (drop alpha channel)
    im_rgb_8bit = (im_rgb[:, :, :3] * 255).astype(np.uint8)

    # Flip vertically to match origin='lower'
    im_rgb_8bit = np.flipud(im_rgb_8bit)

    # Downsample by integer factor (e.g., 2x2)
    downsample_factor = 2
    h, w = im_rgb_8bit.shape[:2]
    new_h = h // downsample_factor
    new_w = w // downsample_factor

    # Reshape and average over blocks
    im_downsampled = im_rgb_8bit[:new_h * downsample_factor, :new_w * downsample_factor]
    im_downsampled = im_downsampled.reshape(
        new_h, downsample_factor, new_w, downsample_factor, 3
    )
    im_downsampled = im_downsampled.mean(axis=(1, 3)).astype(np.uint8)

    # Save with PIL
    Image.fromarray(im_downsampled).save(outfile, optimize=True)


# ---------------------------------------------------------------------------
# Diagonal striping removal
# ---------------------------------------------------------------------------

def create_diagonal_bins(image_shape, bin_width, theta):
    """Create diagonal bins across an image at angle theta.

    Parameters
    ----------
    image_shape : tuple
        Shape of the image (height, width).
    bin_width : float
        Width of each diagonal bin in pixels.
    theta : float
        Angle in degrees relative to the x-axis.

    Returns
    -------
    bin_indices : numpy.ndarray
        2D array where each pixel contains its bin index.
    """
    theta = np.radians(theta)  # Convert angle to radians

    height, width = image_shape

    # Create coordinate grids
    y, x = np.meshgrid(np.arange(height), np.arange(width), indexing='ij')

    # Rotate coordinates: project onto axis perpendicular to diagonal direction
    # The perpendicular direction is at angle (theta + pi/2)
    perpendicular_angle = theta + np.pi / 2

    # Project coordinates onto the perpendicular axis
    # This gives us the distance from each pixel to the diagonal lines
    projected_coords = x * np.cos(perpendicular_angle) + y * np.sin(perpendicular_angle)

    # Find the range of projected coordinates to handle negative values
    min_proj = np.min(projected_coords)

    # Shift coordinates to make them all positive, then divide by bin_width
    bin_indices = ((projected_coords - min_proj) / bin_width).astype(int)

    return bin_indices


def get_pixels_in_bin(image, bin_indices, bin_number):
    """Get all pixels that belong to a specific bin."""
    mask = (bin_indices == bin_number)
    return image[mask]


def create_median_bin_image(image, bin_indices, sigma=3, maxiters=5, num_pixel_threshold=0):
    """Create an image where each pixel is replaced with the median value from its bin.

    Parameters
    ----------
    image : numpy.ndarray
        Original image array.
    bin_indices : numpy.ndarray
        2D array of bin indices from create_diagonal_bins().
    sigma : float
        Sigma for sigma-clipped stats.
    maxiters : int
        Maximum iterations for sigma clipping.
    num_pixel_threshold : int
        Minimum number of pixels required in a bin.

    Returns
    -------
    median_image : numpy.ndarray
        Image where each pixel contains the median value from its diagonal bin.
    """
    # Initialize output image with same shape and dtype as input
    median_image = np.zeros_like(image)

    # Get unique bin numbers
    unique_bins = np.unique(bin_indices)

    # Calculate median for each bin and assign to all pixels in that bin
    for bin_num in unique_bins:
        mask = (bin_indices == bin_num)
        bin_pixels = image[mask]
        if len(bin_pixels) < num_pixel_threshold:
            continue
        median_value = sigma_clipped_stats(
            bin_pixels, mask=~np.isfinite(bin_pixels), maxiters=maxiters, sigma=sigma
        )[1]

        median_image[mask] = median_value

    return median_image


def fast_variance_objective(theta, image, bin_width):
    """Fast computation of variance for optimization."""
    theta = np.radians(theta)  # Convert angle to radians

    height, width = image.shape

    # Create coordinate grids
    y, x = np.meshgrid(np.arange(height), np.arange(width), indexing='ij')

    # Project coordinates
    perpendicular_angle = theta + np.pi / 2
    projected_coords = x * np.cos(perpendicular_angle) + y * np.sin(perpendicular_angle)

    # Create bins
    min_proj = np.min(projected_coords)
    bin_indices = ((projected_coords - min_proj) / bin_width).astype(int)

    # Calculate variance efficiently
    total_variance = 0.0
    unique_bins = np.unique(bin_indices)

    for bin_num in unique_bins:
        mask = (bin_indices == bin_num)
        bin_pixels = image[mask]
        if len(bin_pixels) > 1:
            median_val = sigma_clipped_stats(
                bin_pixels, mask=~np.isfinite(bin_pixels), maxiters=5, sigma=3
            )[1]
            variance = _calc_variance(bin_pixels, median_val, 1)
            total_variance += variance

    return total_variance


def remove_diagonal_striping(image, field, stage_config):
    """Subtract diagonal parallel striping features present in observations.

    Implements a similar algorithm to the subtraction of 1/f noise, but uses
    diagonal apertures rather than row-by-row subtraction.

    Parameters
    ----------
    image : str
        Full path to the *_cal.fits file.
    field : Field
        Field dataclass (unused here, kept for interface consistency).
    stage_config : dict
        Stage 2 configuration dictionary.
    """
    diag_config = stage_config.get('remove_diagonal_striping', {})
    do_plot = diag_config.get('plot', False)
    theta_min = diag_config.get('theta_min', 30)
    theta_max = diag_config.get('theta_max', 60)
    theta_step = diag_config.get('theta_step', 1)
    bin_width = diag_config.get('bin_width', 10)

    from jwst.datamodels import ImageModel

    model = ImageModel(image)
    # check that image has not already been corrected
    for entry in model.history:
        if 'Removed diagonal striping' in entry['description']:
            log(f'{image} already corrected for diagonal striping patterns, exiting')
            return

    log('Measuring diagonal striping')
    log(f'Working on {image}')

    mask = np.zeros(model.data.shape, dtype=bool)
    mask[model.dq > 0] = True

    thetas = np.arange(theta_min, theta_max + theta_step, theta_step)
    variance = np.zeros_like(thetas)
    for i, theta_i in enumerate(thetas):
        log(f'Testing {i + 1}/{len(thetas)}: {theta_i:.2f} degrees')
        variance[i] = fast_variance_objective(theta_i, model.data, bin_width)

    min_variance = np.min(variance)
    theta = thetas[np.argmin(variance)]
    log(f"Optimized angle: {theta:.2f} degrees, Variance: {min_variance:.2e}")

    bins = create_diagonal_bins(np.shape(model.data), bin_width, theta)
    med = create_median_bin_image(model.data, bins)

    model.close()

    image_orig = image.replace('.fits', '_before_diag_sub.fits')
    log(f"Copying input to {image_orig}")
    shutil.copy2(image, image_orig)

    # remove striping from science image
    with ImageModel(image) as immodel:
        sci = immodel.data
        # to replace zeros
        wzero = np.where(sci == 0)
        outsci = sci - med
        outsci[wzero] = 0

        # write output
        immodel.data = outsci
        # add history entry
        time = datetime.now()
        stepdescription = f"Removed diagonal striping; {time.strftime('%Y-%m-%d %H:%M:%S')}"
        from stdatamodels import util as stutil
        substr = stutil.create_history_entry(stepdescription)
        immodel.history.append(substr)
        log(f'Saving cleaned image to {image}')
        immodel.save(image)

    if do_plot:
        log(f'Making diagonal striping removal plots')

        plt.figure()
        plt.plot(thetas, variance, marker='o')
        plt.gca().axvline(theta, color='r', linestyle='--')
        plt.xlabel('Theta (degrees)')
        plt.ylabel('Variance')
        output_file = image.replace('_cal.fits', '_diag_striping_variance.pdf')
        plt.savefig(output_file)
        plt.close()
        log(f'Saved plot to {output_file}')

        # NOTE: plot_three was a nircamx utility; if ported, import from
        # campfire_pipeline.nircam.plots and uncomment:
        # image_orig = image.replace('_cal.fits', '_cal_before_diag_sub.fits')
        # output_file = image.replace('_cal.fits', '_diag_striping.pdf')
        # plot_three(image_orig, med, image,
        #            title1='Original', title2='Stripes', title3='Stripes removed',
        #            scaling=3, save_file=output_file)
        # log(f'Saved plot to {output_file}')


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_stage2(field, stage_config, filters=None, n_processes=1, overwrite=False):
    """Orchestrate Stage 2 processing for a NIRCam field.

    Runs enabled steps in order:
      1. image2_step  -- JWST Image2Pipeline calibration
      2. remove_edge  -- flag noisy detector edges
      3. bkgsub_var   -- sky subtraction + variance rescaling
      4. apply_masks  -- apply region-file masks

    Parameters
    ----------
    field : Field
        Field dataclass (must have workspace set up).
    stage_config : dict
        Stage 2 configuration dictionary.
    filters : list of str, optional
        Filters to process. If None, uses ``field.filters``.
    n_processes : int
        Number of parallel workers for dispatch.
    overwrite : bool
        If True, re-run steps even when products exist.
    """
    from campfire_pipeline.common.parallel import dispatch

    if filters is None:
        filters = field.filters

    files_to_skip = stage_config.get('files_to_skip', [])

    # Auto-read exclusions from exposures contract file if present
    excluded = field.get_excluded_exposures()
    if excluded:
        files_to_skip = list(set(files_to_skip + excluded))
        log(f"Auto-excluding {len(excluded)} exposure(s) from contract file")

    log(f"Stage 2 for field '{field.name}': filters={filters}, "
        f"n_processes={n_processes}, overwrite={overwrite}")

    for filtname in filters:
        log(f"--- Processing filter: {filtname} ---")

        # Ensure output directory exists
        output_dir = os.path.join(field.stage2_dir, filtname)
        os.makedirs(output_dir, exist_ok=True)

        # Step 1: Image2Pipeline
        rate_files = field.get_rate_files(filtname, skip=files_to_skip)
        if not rate_files:
            log(f"No rate files found for {filtname}, skipping image2")
        else:
            log(f"Running image2 on {len(rate_files)} files")
            dispatch(
                image2_step,
                rate_files,
                n_processes=n_processes,
                field=field,
                stage_config=stage_config,
                overwrite=overwrite,
            )

        # Step 2: Remove edges
        cal_files = field.get_cal_files(filtname, skip=files_to_skip)
        if not cal_files:
            log(f"No cal files found for {filtname}, skipping remove_edge")
        else:
            log(f"Running remove_edge on {len(cal_files)} files")
            dispatch(
                remove_edge,
                cal_files,
                n_processes=n_processes,
                field=field,
                stage_config=stage_config,
            )

        # Step 3: Background subtraction + variance rescaling
        cal_files = field.get_cal_files(filtname, skip=files_to_skip)
        if not cal_files:
            log(f"No cal files found for {filtname}, skipping bkgsub_var")
        else:
            log(f"Running sky_subtraction on {len(cal_files)} files")
            dispatch(
                sky_subtraction,
                cal_files,
                n_processes=n_processes,
                field=field,
                stage_config=stage_config,
            )

            log(f"Running rescale_variance on {len(cal_files)} files")
            dispatch(
                rescale_variance,
                cal_files,
                n_processes=n_processes,
                field=field,
                stage_config=stage_config,
            )

        # Step 4: Apply masks
        cal_files = field.get_cal_files(filtname, skip=files_to_skip)
        if not cal_files:
            log(f"No cal files found for {filtname}, skipping apply_masks")
        else:
            log(f"Running apply_masks on {len(cal_files)} files")
            dispatch(
                apply_masks,
                cal_files,
                n_processes=n_processes,
                field=field,
                stage_config=stage_config,
            )

        # Step 5: Generate quick-look PNGs
        cal_files = field.get_cal_files(filtname, skip=files_to_skip)
        if cal_files:
            log(f"Running plot_cal_rate on {len(cal_files)} files")
            dispatch(
                plot_cal_rate,
                cal_files,
                n_processes=n_processes,
                field=field,
                stage_config=stage_config,
            )

    log(f"Stage 2 complete for field '{field.name}'")
