"""
Spectral extraction: profile functions and 1D spectral combination.
"""

import warnings
import numpy as np

from campfire_pipeline.common.io import log


def boxcar_profile(start, end, n_pixels):
    """
    Generate a boxcar extraction profile with fractional pixel weights.

    Parameters:
    -----------
    start : float
        Starting position (can be fractional)
    end : float
        Ending position (can be fractional)
    n_pixels : int
        Total number of pixels in the profile

    Returns:
    --------
    profile : ndarray
        1D array of weights for each pixel
    """
    profile = np.zeros(n_pixels)

    # Clip start and end to valid range [0, n_pixels]
    start = np.clip(start, 0, n_pixels)
    end = np.clip(end, 0, n_pixels)

    # Handle edge case where start >= end after clipping
    if start >= end:
        return profile

    # Get integer bounds
    start_int = int(np.floor(start))
    end_int = int(np.floor(end))

    # Clip integer bounds to valid indices
    start_int = np.clip(start_int, 0, n_pixels - 1)
    end_int = np.clip(end_int, 0, n_pixels - 1)

    # Calculate fractional contributions
    start_frac = 1.0 - (start - np.floor(start))  # fraction of first pixel
    end_frac = end - np.floor(end)  # fraction of last pixel

    # Fill in the profile
    if start_int == end_int:
        # Entire extraction is within a single pixel
        profile[start_int] = end - start
    else:
        # Multiple pixels involved
        profile[start_int] = start_frac
        if start_int + 1 <= end_int - 1:
            profile[start_int+1:end_int] = 1.0  # fully included pixels
        if end_frac > 0:  # Only add end contribution if there's a fractional part
            profile[end_int] = end_frac

    return profile


def optext_profile(collapsed, start, end):

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)

        x = np.arange(len(collapsed)+1)
        profile = np.zeros_like(collapsed)
        profile[(x[:-1] > start)&(x[1:] <= end)] = collapsed[(x[:-1] > start)&(x[1:] <= end)]
        profile[profile < 0] = 0
        profile /= np.nansum(profile)

    return profile


def optext_profile_is_corrupted(collapsed, start, end,
                                min_positive_pixels=3,
                                min_positive_fraction=0.5):
    """Diagnose whether the in-aperture cross-dispersion profile is too
    corrupted (e.g. by background over-subtraction) to support an optimal
    extraction. Returns ``(corrupted, n_positive, positive_fraction)``.

    The profile is considered corrupted if fewer than ``min_positive_pixels``
    finite, positive pixels lie inside the aperture, or if the ratio of
    positive flux to total |flux| in the aperture is below
    ``min_positive_fraction``.
    """
    x = np.arange(len(collapsed) + 1)
    in_ap = (x[:-1] > start) & (x[1:] <= end)
    aper = collapsed[in_ap]
    aper = aper[np.isfinite(aper)]
    if aper.size == 0:
        return True, 0, 0.0
    n_positive = int(np.sum(aper > 0))
    abs_sum = float(np.sum(np.abs(aper)))
    pos_sum = float(np.sum(aper[aper > 0]))
    positive_fraction = pos_sum / abs_sum if abs_sum > 0 else 0.0
    corrupted = (n_positive < min_positive_pixels) or (positive_fraction < min_positive_fraction)
    return corrupted, n_positive, positive_fraction


def extract_with_profile(profile, data, error, mask=None, ivw=False):
    variance = error**2
    variance[np.isnan(data)] = np.nan

    if np.ndim(profile)==1:
        profile = profile[:,np.newaxis]

    if mask is not None:
        data[mask] = np.nan
        variance[mask] = np.nan

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        if ivw:
            fnu = np.nansum(profile*data/variance,axis=0)/np.nansum(profile**2/variance, axis=0)
            fnu_err = np.sqrt(np.nansum(profile, axis=0)/np.nansum(profile**2/variance,axis=0))
        else:
            fnu = np.nansum(profile*data, axis=0)
            fnu_err = np.sqrt(np.nansum(profile*variance, axis=0))

    return fnu, fnu_err


def combine_1d_spectra(wavelengths, fluxes, errors, exposure_times, common_wave,
                       sigma_clip_enabled=True, sigma_clip_low=3.0,
                       sigma_clip_high=3.0, sigma_clip_maxiters=5):
    """
    Combine multiple 1D spectra onto a common wavelength grid via exposure-time weighting + sigma clipping.

    Parameters
    ----------
    wavelengths : list of ndarray  — per-spectrum wavelength arrays
    fluxes : list of ndarray       — per-spectrum flux arrays (fnu in uJy)
    errors : list of ndarray       — per-spectrum error arrays
    exposure_times : list of float — per-spectrum effective exposure times (seconds)
    common_wave : ndarray          — target wavelength grid

    Returns
    -------
    combined_flux : ndarray
    combined_error : ndarray
    n_combined : ndarray (int) — number of spectra contributing per pixel
    """
    from spectres import spectres
    from astropy.stats import sigma_clip

    n_spec = len(fluxes)
    n_wave = len(common_wave)
    exposure_times = np.asarray(exposure_times, dtype=float)

    # Resample each spectrum onto the common wavelength grid
    resampled_flux = np.full((n_spec, n_wave), np.nan)
    resampled_err = np.full((n_spec, n_wave), np.nan)

    for i in range(n_spec):
        try:
            resampled_flux[i], resampled_err[i] = spectres(
                common_wave, wavelengths[i], fluxes[i],
                spec_errs=errors[i], fill=np.nan,
            )
        except Exception as e:
            log(f"Warning: spectres failed for spectrum {i}: {e}")
            continue

    # Exposure-time-weighted combination per wavelength pixel
    combined_flux = np.full(n_wave, np.nan)
    combined_error = np.full(n_wave, np.nan)
    n_combined = np.zeros(n_wave, dtype=int)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)

        for j in range(n_wave):
            f_col = resampled_flux[:, j]
            e_col = resampled_err[:, j]

            # Mask invalid pixels
            valid = np.isfinite(f_col) & np.isfinite(e_col) & (e_col > 0)
            if not np.any(valid):
                continue

            f_valid = f_col[valid]
            e_valid = e_col[valid]
            w_valid = exposure_times[valid]

            # Sigma clipping (only if >= 3 spectra)
            if sigma_clip_enabled and len(f_valid) >= 3:
                clipped = sigma_clip(f_valid, sigma_lower=sigma_clip_low,
                                     sigma_upper=sigma_clip_high,
                                     maxiters=sigma_clip_maxiters,
                                     masked=True)
                mask = ~clipped.mask
                f_valid = f_valid[mask]
                e_valid = e_valid[mask]
                w_valid = w_valid[mask]

            if len(f_valid) == 0:
                continue

            w = w_valid / np.sum(w_valid)
            combined_flux[j] = np.sum(w * f_valid)
            combined_error[j] = np.sqrt(np.sum((w * e_valid)**2))
            n_combined[j] = len(f_valid)

    return combined_flux, combined_error, n_combined
