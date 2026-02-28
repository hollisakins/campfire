"""
Instrument-agnostic spectral math: wavelength conversion, resampling, LSF convolution, blackbody.

Includes both the legacy 3-step pipeline (resample → convolve → resample) and the
sfhz pre-convolution engine for memory-efficient redshift fitting.
"""

import numpy as np
from astropy.io import fits
from scipy.ndimage import gaussian_filter1d
from spectres import spectres
from numba import njit, prange
import warnings


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
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
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


# ---------------------------------------------------------------------------
# sfhz pre-convolution engine
# ---------------------------------------------------------------------------

def load_r_curve(r_curve_file):
    """Load an R-curve FITS file and return (wavelength, R) arrays in microns.

    Parameters
    ----------
    r_curve_file : str
        Path to R-curve FITS file

    Returns
    -------
    r_wav : ndarray — wavelength in microns
    r_val : ndarray — spectral resolution R
    """
    data = fits.open(r_curve_file)[1].data
    return data['WAVELENGTH'].astype(np.float64), data['R'].astype(np.float64)


def r_curve_R_range(r_wav, r_val):
    """Return (R_min, R_max) from an R-curve."""
    return float(np.min(r_val)), float(np.max(r_val))


def preconvolve_at_discrete_R(templates, dloglam, R_min, R_max, K):
    """Pre-convolve templates at K log-spaced spectral resolution values.

    On the log-lambda grid, convolution with a Gaussian LSF of resolution R
    is a fixed-width Gaussian with sigma_pix = 1 / (R * 2.355 * dloglam).

    Parameters
    ----------
    templates : ndarray
        2D (n_templ, n_rest) or 3D (n_bins, n_templ, n_rest) rest-frame templates
    dloglam : float
        Log-lambda pixel scale of the rest-frame grid
    R_min, R_max : float
        Range of effective R (instrument R / f_LSF) to cover
    K : int
        Number of R knots (log-spaced between R_min and R_max)

    Returns
    -------
    templates_conv : ndarray
        One extra axis inserted before the last: (..., K, n_rest)
    r_knots : ndarray, shape (K,)
        The R values at each knot
    """
    r_knots = np.exp(np.linspace(np.log(R_min), np.log(R_max), K))

    ndim = templates.ndim
    if ndim == 2:
        n_templ, n_rest = templates.shape
        out = np.zeros((n_templ, K, n_rest), dtype=np.float32)
        for k in range(K):
            sigma_pix = 1.0 / (r_knots[k] * 2.355 * dloglam)
            if sigma_pix < 0.3:
                # Negligible convolution — just copy
                out[:, k, :] = templates
            else:
                for i in range(n_templ):
                    out[i, k, :] = gaussian_filter1d(
                        templates[i].astype(np.float64), sigma_pix, mode='constant')
    elif ndim == 3:
        n_bins, n_templ, n_rest = templates.shape
        out = np.zeros((n_bins, n_templ, K, n_rest), dtype=np.float32)
        for k in range(K):
            sigma_pix = 1.0 / (r_knots[k] * 2.355 * dloglam)
            if sigma_pix < 0.3:
                out[:, :, k, :] = templates
            else:
                for b in range(n_bins):
                    for i in range(n_templ):
                        out[b, i, k, :] = gaussian_filter1d(
                            templates[b, i].astype(np.float64), sigma_pix, mode='constant')
    else:
        raise ValueError(f"templates must be 2D or 3D, got {ndim}D")

    return out, r_knots


def compute_pixel_edges(wav):
    """Compute pixel boundaries as midpoints between wavelength centers.

    Parameters
    ----------
    wav : ndarray, shape (n_pix,)
        Pixel center wavelengths

    Returns
    -------
    wav_lo : ndarray, shape (n_pix,)
        Lower edge of each pixel
    wav_hi : ndarray, shape (n_pix,)
        Upper edge of each pixel
    """
    midpts = 0.5 * (wav[:-1] + wav[1:])
    wav_lo = np.empty_like(wav)
    wav_hi = np.empty_like(wav)
    wav_lo[0] = wav[0] - 0.5 * (wav[1] - wav[0])
    wav_lo[1:] = midpts
    wav_hi[:-1] = midpts
    wav_hi[-1] = wav[-1] + 0.5 * (wav[-1] - wav[-2])
    return wav_lo, wav_hi


# ---------------------------------------------------------------------------
# Numba kernel: per-spectrum template assembly with pixel-integration
# ---------------------------------------------------------------------------

@njit(cache=True)
def _interp_r_knot(R_val, r_knots):
    """Find bracketing R-knot indices and interpolation weight.

    Returns (k_lo, alpha) where the interpolated value is:
        (1 - alpha) * data[k_lo] + alpha * data[k_lo + 1]
    """
    K = len(r_knots)
    if R_val <= r_knots[0]:
        return 0, 0.0
    if R_val >= r_knots[K - 1]:
        return K - 2, 1.0
    # Binary search
    lo, hi = 0, K - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if r_knots[mid] <= R_val:
            lo = mid
        else:
            hi = mid
    alpha = (R_val - r_knots[lo]) / (r_knots[hi] - r_knots[lo])
    return lo, alpha


@njit(parallel=True, cache=True)
def _assemble_templates_pixint(
    cont_conv,       # (n_bins, n_cont, K, n_rest) float32
    line_conv,       # (n_line, K, n_rest) float32
    r_knots,         # (K,) float64
    r_at_obs,        # (n_obs,) float64 — R(λ_obs)
    log_obs_lo,      # (n_obs,) float64
    log_obs_hi,      # (n_obs,) float64
    log_rest_start,  # scalar float64
    dloglam,         # scalar float64
    log_shifts,      # (n_z,) float64 — log(1+z)
    bin_indices,     # (n_z,) int64
    n_rest,          # int
):
    """Assemble pixel-integrated templates for all trial redshifts.

    For each (z, observed pixel), integrates the pre-convolved, R-interpolated
    rest-frame template over the pixel boundaries using the trapezoidal rule.

    Returns
    -------
    grid : ndarray, shape (n_templ_total, n_z, n_obs) float64
    """
    n_cont = cont_conv.shape[1]
    n_line = line_conv.shape[0]
    n_templ = n_cont + n_line
    n_z = len(log_shifts)
    n_obs = len(r_at_obs)

    grid = np.zeros((n_templ, n_z, n_obs))

    for iz in prange(n_z):
        log_shift = log_shifts[iz]
        b = bin_indices[iz]

        for ip in range(n_obs):
            # R-knot interpolation weights for this observed pixel
            k_lo, alpha = _interp_r_knot(r_at_obs[ip], r_knots)
            k_hi = k_lo + 1
            if k_hi >= len(r_knots):
                k_hi = k_lo

            # Rest-frame log-lambda range for this observed pixel
            rest_lo = log_obs_lo[ip] - log_shift
            rest_hi = log_obs_hi[ip] - log_shift

            # Convert to fractional rest-frame pixel indices
            frac_lo = (rest_lo - log_rest_start) / dloglam
            frac_hi = (rest_hi - log_rest_start) / dloglam

            # Integer pixel range to integrate over
            i_lo = int(np.floor(frac_lo))
            i_hi = int(np.ceil(frac_hi))

            if i_lo < 0:
                i_lo = 0
            if i_hi >= n_rest:
                i_hi = n_rest - 1
            if i_lo >= i_hi:
                continue

            # Number of sub-pixels
            n_sub = i_hi - i_lo

            # Trapezoidal integration for each template
            for t in range(n_cont):
                val = 0.0
                # Interpolate between R-knots
                v_prev = ((1.0 - alpha) * cont_conv[b, t, k_lo, i_lo]
                          + alpha * cont_conv[b, t, k_hi, i_lo])

                for isub in range(1, n_sub + 1):
                    idx = i_lo + isub
                    if idx >= n_rest:
                        break
                    v_cur = ((1.0 - alpha) * cont_conv[b, t, k_lo, idx]
                             + alpha * cont_conv[b, t, k_hi, idx])
                    val += 0.5 * (v_prev + v_cur)
                    v_prev = v_cur

                # Normalize by number of sub-pixels to get mean value
                if n_sub > 0:
                    grid[t, iz, ip] = val / n_sub

            for t in range(n_line):
                val = 0.0
                v_prev = ((1.0 - alpha) * line_conv[t, k_lo, i_lo]
                          + alpha * line_conv[t, k_hi, i_lo])

                for isub in range(1, n_sub + 1):
                    idx = i_lo + isub
                    if idx >= n_rest:
                        break
                    v_cur = ((1.0 - alpha) * line_conv[t, k_lo, idx]
                             + alpha * line_conv[t, k_hi, idx])
                    val += 0.5 * (v_prev + v_cur)
                    v_prev = v_cur

                if n_sub > 0:
                    grid[n_cont + t, iz, ip] = val / n_sub

    return grid


@njit(cache=True)
def _cgm_sigma_a(nu_rest):
    """Ly-alpha absorption cross-section (Lorentzian damping wing). Returns cm^2."""
    Lam_a = 6.255486e8
    nu_lya = 2.46607e15
    C = 6.9029528e22
    nu_ratio = nu_rest / nu_lya
    sig = C * nu_ratio**4 / (4.0 * np.pi**2 * (nu_rest - nu_lya)**2
                             + Lam_a**2 * nu_ratio**6 / 4.0)
    return sig * 1e-16


@njit(cache=True)
def _bilinear_igm(lam_rest_um, z, igm_wav, igm_z, igm_trans):
    """Bilinear interpolation on the IGM transmission grid."""
    n_wav = len(igm_wav)
    n_z = len(igm_z)

    # Wavelength bracket
    iw = np.searchsorted(igm_wav, lam_rest_um) - 1
    if iw < 0:
        iw = 0
    if iw >= n_wav - 1:
        iw = n_wav - 2
    iw_hi = iw + 1
    dw = igm_wav[iw_hi] - igm_wav[iw]
    if dw > 0:
        w_alpha = (lam_rest_um - igm_wav[iw]) / dw
    else:
        w_alpha = 0.0
    w_alpha = max(0.0, min(1.0, w_alpha))

    # Redshift bracket
    iz = np.searchsorted(igm_z, z) - 1
    if iz < 0:
        iz = 0
    if iz >= n_z - 1:
        iz = n_z - 2
    iz_hi = iz + 1
    dz = igm_z[iz_hi] - igm_z[iz]
    if dz > 0:
        z_alpha = (z - igm_z[iz]) / dz
    else:
        z_alpha = 0.0
    z_alpha = max(0.0, min(1.0, z_alpha))

    T = ((1 - w_alpha) * (1 - z_alpha) * igm_trans[iw, iz]
         + w_alpha * (1 - z_alpha) * igm_trans[iw_hi, iz]
         + (1 - w_alpha) * z_alpha * igm_trans[iw, iz_hi]
         + w_alpha * z_alpha * igm_trans[iw_hi, iz_hi])
    return T


@njit(parallel=True, cache=True)
def apply_igm_to_grid(grid, obs_wav, zgrid,
                       igm_wav, igm_z, igm_trans,
                       cgm_A=3.5918, cgm_a=1.8414, cgm_c=18.001):
    """Apply IGM + CGM attenuation to an assembled template grid in-place.

    For each (z, observed pixel), computes the rest-frame wavelength
    and multiplies all templates by the Inoue+2014 IGM transmission.
    For z >= 6, also applies the CGM damping wing (Asada+24).

    Parameters
    ----------
    grid : (n_templ, n_z, n_obs) float64 — modified in-place
    obs_wav : (n_obs,) float64 — observed wavelengths in microns
    zgrid : (n_z,) float64 — trial redshifts
    igm_wav : (n_igm,) float64 — IGM grid wavelengths in microns
    igm_z : (n_igm_z,) float64 — IGM grid redshifts
    igm_trans : (n_igm, n_igm_z) float64 — IGM transmission values
    cgm_A, cgm_a, cgm_c : float — CGM sigmoid parameters
    """
    n_templ = grid.shape[0]
    n_z = grid.shape[1]
    n_obs = grid.shape[2]
    max_igm_lam = igm_wav[-1]   # ~0.1225 um
    c_ang = 2.99792458e18
    cgm_lam_max = 0.15  # um — cutoff above which CGM wing is negligible

    for iz in prange(n_z):
        z = zgrid[iz]

        # Pre-compute CGM N_HI for this z
        do_cgm = z >= 6.0
        N_HI = 0.0
        if do_cgm:
            log10_NHI = cgm_A / (1.0 + np.exp(-cgm_a * (z - 6.0))) + cgm_c
            N_HI = 10.0 ** log10_NHI

        for ip in range(n_obs):
            lam_rest = obs_wav[ip] / (1.0 + z)

            T = 1.0

            # IGM from Inoue+2014 grid (rest-frame < ~1225 A)
            if lam_rest <= max_igm_lam:
                T = _bilinear_igm(lam_rest, z, igm_wav, igm_z, igm_trans)

            # CGM damping wing (z >= 6, extends above Ly-alpha)
            if do_cgm and lam_rest < cgm_lam_max:
                lam_rest_ang = lam_rest * 1.0e4
                nu_rest = c_ang / lam_rest_ang
                sig = _cgm_sigma_a(nu_rest)
                T *= np.exp(-N_HI * sig)

            if T < 1.0:
                for t in range(n_templ):
                    grid[t, iz, ip] *= T


def assemble_templates_for_spectrum(cont_conv, line_conv, r_knots, r_wav, r_val,
                                     obs_wav, zgrid, z_bin_edges, dloglam, wave_rest):
    """Python wrapper around the numba pixel-integration kernel.

    Parameters
    ----------
    cont_conv : ndarray, shape (n_bins, n_cont, K, n_rest)
    line_conv : ndarray, shape (n_line, K, n_rest)
    r_knots : ndarray, shape (K,)
    r_wav, r_val : ndarray — R-curve wavelengths and values
    obs_wav : ndarray, shape (n_obs,) — observed wavelengths in microns
    zgrid : ndarray, shape (n_z,) — trial redshifts
    z_bin_edges : ndarray — sfhz bin edges
    dloglam : float
    wave_rest : ndarray, shape (n_rest,)

    Returns
    -------
    grid : ndarray, shape (n_templ, n_z, n_obs) float64
    """
    obs_wav = obs_wav.astype(np.float64)
    wav_lo, wav_hi = compute_pixel_edges(obs_wav)

    r_at_obs = np.interp(obs_wav, r_wav, r_val)
    log_obs_lo = np.log(wav_lo)
    log_obs_hi = np.log(wav_hi)
    log_rest_start = np.log(wave_rest[0])
    log_shifts = np.log(1.0 + zgrid)
    bin_indices = np.clip(np.digitize(zgrid, z_bin_edges) - 1,
                          0, len(z_bin_edges) - 2).astype(np.int64)
    n_rest = len(wave_rest)

    return _assemble_templates_pixint(
        cont_conv.astype(np.float32),
        line_conv.astype(np.float32),
        r_knots.astype(np.float64),
        r_at_obs, log_obs_lo, log_obs_hi,
        log_rest_start, dloglam, log_shifts, bin_indices, n_rest,
    )


def assemble_templates_spectres(cont_conv, line_conv, r_knots, r_wav, r_val,
                                 obs_wav, zgrid, z_bin_edges, dloglam, wave_rest,
                                 igm_data=None):
    """Non-optimized template assembly using spectres for pixel-integration.

    Used as the validation/fallback path. For each z-chunk, builds templates
    on an oversampled non-uniform grid, then uses spectres for pixel-integration.

    Parameters
    ----------
    (same as assemble_templates_for_spectrum)
    igm_data : dict, optional
        Output of load_igm_grid(). If provided, IGM attenuation is applied
        in the rest frame before spectres resampling (more accurate than
        post-processing).

    Returns
    -------
    grid : ndarray, shape (n_templ, n_z, n_obs) float64
    """
    n_cont = cont_conv.shape[1]
    n_line = line_conv.shape[0]
    n_templ = n_cont + n_line
    n_z = len(zgrid)
    n_obs = len(obs_wav)

    obs_wav_f64 = obs_wav.astype(np.float64)
    n_rest = len(wave_rest)
    log_rest_start = np.log(wave_rest[0])
    bin_indices = np.clip(np.digitize(zgrid, z_bin_edges) - 1,
                          0, len(z_bin_edges) - 2)

    grid = np.zeros((n_templ, n_z, n_obs))

    for iz in range(n_z):
        z = zgrid[iz]
        b = bin_indices[iz]

        # Shift rest-frame grid to observed frame
        obs_template_wav = wave_rest * (1 + z)

        # R-interpolation at each template wavelength (using observed R)
        r_at_templ = np.interp(obs_template_wav, r_wav, r_val)

        # Build R-interpolated templates for this z
        templates_iz = np.zeros((n_templ, n_rest))

        for t in range(n_cont):
            for ip in range(n_rest):
                R_val = r_at_templ[ip]
                k_lo = np.searchsorted(r_knots, R_val, side='right') - 1
                k_lo = max(0, min(k_lo, len(r_knots) - 2))
                k_hi = k_lo + 1
                alpha = (R_val - r_knots[k_lo]) / (r_knots[k_hi] - r_knots[k_lo])
                alpha = max(0.0, min(1.0, alpha))
                templates_iz[t, ip] = ((1 - alpha) * cont_conv[b, t, k_lo, ip]
                                       + alpha * cont_conv[b, t, k_hi, ip])

        for t in range(n_line):
            for ip in range(n_rest):
                R_val = r_at_templ[ip]
                k_lo = np.searchsorted(r_knots, R_val, side='right') - 1
                k_lo = max(0, min(k_lo, len(r_knots) - 2))
                k_hi = k_lo + 1
                alpha = (R_val - r_knots[k_lo]) / (r_knots[k_hi] - r_knots[k_lo])
                alpha = max(0.0, min(1.0, alpha))
                templates_iz[n_cont + t, ip] = ((1 - alpha) * line_conv[t, k_lo, ip]
                                                 + alpha * line_conv[t, k_hi, ip])

        # Apply IGM attenuation in rest frame (before pixel-integration)
        if igm_data is not None:
            from campfire_pipeline.common.igm import igm_transmission
            T_igm = igm_transmission(wave_rest, z, igm_data)
            templates_iz *= T_igm[np.newaxis, :]

        # Spectres pixel-integration from oversampled template to observed grid
        # Only use wavelength range that overlaps
        mask_templ = ((obs_template_wav >= obs_wav_f64[0] * 0.95) &
                      (obs_template_wav <= obs_wav_f64[-1] * 1.05))
        if np.sum(mask_templ) < 2:
            continue

        resampled = spectres(obs_wav_f64, obs_template_wav[mask_templ].astype(np.float64),
                             templates_iz[:, mask_templ].astype(np.float64),
                             fill=0, verbose=False)
        grid[:, iz, :] = resampled

    return grid
