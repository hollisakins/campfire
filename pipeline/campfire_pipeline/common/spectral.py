"""
Instrument-agnostic spectral math: wavelength conversion, resampling, LSF convolution, blackbody.
"""

import numpy as np
from astropy.io import fits
from spectres import spectres


def air_to_vac(wav_air):
    """Convert air wavelengths to vacuum wavelengths using SDSS formula."""
    # from SDSS:
    # AIR = VAC / (1.0 + 2.735182E-4 + 131.4182 / VAC^2 + 2.76249E8 / VAC^4)
    vac_wavs = np.logspace(2, 4.5, 1000)
    air_wavs = vac_wavs / (1.0 + 2.735182E-4 + 131.4182 / vac_wavs**2 + 2.76249E8 / vac_wavs**4)
    return np.interp(wav_air, air_wavs, vac_wavs)


def get_wavelength_sampling(wav_min, wav_max, R_curve_file, oversample=4):
    """Generate non-uniform wavelength sampling based on spectral resolution curve."""
    R_curve = fits.open(R_curve_file)[1].data
    x = [wav_min]
    while x[-1] <= wav_max:
        R_val = np.interp(x[-1], R_curve['WAVELENGTH'], R_curve['R'])
        dwav = x[-1] / R_val / oversample
        x.append(x[-1] + dwav)
    return np.array(x)


def planck(lam, T):  # lam in um, T in K, return blackbody in f_nu
    nu = 299792458e6 / lam
    bb = 2 * 6.626e-34 * nu**3 / (3e5)**2 / (np.exp(6.626e-34 * nu / 1.381e-23 / T) - 1)
    bb[~np.isfinite(bb)] = 0
    return bb


def MBB(lam, T, pivot, beta):  # modified black body with hard H_infinity cut
    temp = planck(lam, T) * (pivot / lam)**beta
    for i in range(6, 20):  # create smoother balmer break
        temp[lam < 4 * .0912 * (i**2 / (i**2 - 4))] = temp[lam < 4 * .0912 * (i**2 / (i**2 - 4))] * 0.85
    temp[lam < 4 * .0912] = 0
    return temp


def resample_to_nonuniform_grid(old_wav, old_grid, R_curve_file, oversample=4):
    """
    Step 1: Resample template grid to non-uniform wavelength sampling based on R-curve.

    Parameters
    ----------
    old_wav : array
        Template wavelength grid
    old_grid : array
        Template grid (n_templates, n_redshifts, n_wavelengths)
    R_curve_file : str
        Path to spectral resolution curve file
    oversample : int
        Oversampling factor for LSF convolution

    Returns
    -------
    tuple : (temp_wav, temp_grid)
        temp_wav: Non-uniform wavelength grid
        temp_grid: Resampled template grid
    """
    old_wav = old_wav.astype(np.float64)
    old_grid = old_grid.astype(np.float64)

    temp_wav = get_wavelength_sampling(old_wav.min(), old_wav.max(), R_curve_file, oversample=oversample)
    temp_grid = spectres(temp_wav, old_wav, old_grid, fill=0, verbose=False)

    return temp_wav, temp_grid


def convolve_with_lsf(temp_wav, temp_grid, oversample=4, f_LSF=1.3):
    """
    Step 2: Convolve templates with Line Spread Function.

    Parameters
    ----------
    temp_wav : array
        Non-uniform wavelength grid
    temp_grid : array
        Template grid on non-uniform wavelength grid
    oversample : int
        Oversampling factor for LSF convolution
    f_LSF : float
        LSF fudge factor

    Returns
    -------
    array : LSF-convolved template grid
    """
    sigma_pix = oversample / 2.35 / f_LSF  # sigma width of kernel in pixels
    k_size = 4 * int(sigma_pix + 1)
    x_kernel_pix = np.arange(-k_size, k_size + 1)

    kernel = np.exp(-(x_kernel_pix**2) / (2 * sigma_pix**2))
    kernel /= np.trapezoid(kernel)  # Explicitly normalise kernel

    # Disperse non-uniformly sampled spectrum
    n_templ, n_z, _ = np.shape(temp_grid)
    convolved_grid = np.zeros_like(temp_grid)
    for i in range(n_templ):
        for j in range(n_z):
            convolved_grid[i, j, :] = np.convolve(temp_grid[i, j, :], kernel, mode='same')
    return convolved_grid


def resample_to_observed_grid(temp_wav, convolved_grid, observed_wav):
    """
    Step 3: Final resampling to object's observed wavelength grid.

    Parameters
    ----------
    temp_wav : array
        Non-uniform wavelength grid
    convolved_grid : array
        LSF-convolved template grid
    observed_wav : array
        Observed wavelength grid

    Returns
    -------
    array : Template grid resampled to observed wavelength grid
    """
    observed_wav = observed_wav.astype(np.float64)
    new_grid = spectres(observed_wav, temp_wav, convolved_grid, fill=0, verbose=False)
    return new_grid
