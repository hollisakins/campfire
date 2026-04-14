"""Spectral calibration and stacking.

Provides utilities for flux-calibrating spectra against photometric
measurements and stacking multiple spectra of the same object.

Example
-------
::

    from campfire.calibration import calibrate_to_photometry, calibrate_and_stack

    cf = Campfire()
    obj = cf.get_object('J100025.32+021520.1')

    # Single spectrum calibration
    calib = calibrate_to_photometry(obj.spectra[0], obj.photometry)
    calib.plot()

    # Calibrate + stack all PRISM spectra
    prism = obj.spectra[obj.spectra.grating == 'PRISM']
    stacked = calibrate_and_stack(prism, obj.photometry)
    stacked.plot()
"""

import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

import numpy as np

from .models import Photometry, Spectrum, SpectrumCollection, SpectrumData


# ---------------------------------------------------------------------------
# SVO Filter Profile Service — band name → SVO ID mapping
# ---------------------------------------------------------------------------

SVO_FILTER_MAP = {
    # JWST NIRCam
    "f090w": "JWST/NIRCam.F090W",
    "f115w": "JWST/NIRCam.F115W",
    "f140m": "JWST/NIRCam.F140M",
    "f150w": "JWST/NIRCam.F150W",
    "f182m": "JWST/NIRCam.F182M",
    "f200w": "JWST/NIRCam.F200W",
    "f210m": "JWST/NIRCam.F210M",
    "f250m": "JWST/NIRCam.F250M",
    "f277w": "JWST/NIRCam.F277W",
    "f300m": "JWST/NIRCam.F300M",
    "f335m": "JWST/NIRCam.F335M",
    "f356w": "JWST/NIRCam.F356W",
    "f360m": "JWST/NIRCam.F360M",
    "f410m": "JWST/NIRCam.F410M",
    "f430m": "JWST/NIRCam.F430M",
    "f444w": "JWST/NIRCam.F444W",
    "f460m": "JWST/NIRCam.F460M",
    "f480m": "JWST/NIRCam.F480M",
    "f770w": "JWST/NIRCam.F770W",
    # HST ACS/WFC
    "f435w": "HST/ACS_WFC.F435W",
    "f606w": "HST/ACS_WFC.F606W",
    "f814w": "HST/ACS_WFC.F814W",
    # HST WFC3/IR
    "f098m": "HST/WFC3_IR.F098M",
    # Euclid
    "vis": "Euclid/VIS.vis",
    # Ground-based (COSMOS defaults)
    "u": "CFHT/MegaCam.u",
    "g": "Subaru/HSC.g",
    "r": "Subaru/HSC.r",
    "i": "Subaru/HSC.i",
    "z": "Subaru/HSC.z",
    "y": "Subaru/HSC.y",
    "Y": "Paranal/VISTA.Y",
    "J": "Paranal/VISTA.J",
    "H": "Paranal/VISTA.H",
    "Ks": "Paranal/VISTA.Ks",
}

SVO_BASE_URL = "http://svo2.cab.inta-csic.es/theory/fps/getdata.php"

# Fallback band edges (microns) from deploy/photometry.py
_FILTER_EDGES = {
    "f090w": (0.788550, 1.023550),
    "f115w": (0.998200, 1.305200),
    "f140m": (1.304350, 1.505350),
    "f150w": (1.303790, 1.693790),
    "f182m": (1.695500, 2.000500),
    "f200w": (1.723400, 2.258400),
    "f210m": (1.961600, 2.232600),
    "f250m": (2.393530, 2.616900),
    "f277w": (2.365900, 3.216190),
    "f300m": (2.770356, 3.250592),
    "f335m": (3.118640, 3.642920),
    "f356w": (3.070000, 4.078020),
    "f360m": (3.322680, 3.902360),
    "f410m": (3.775340, 4.402310),
    "f430m": (4.122610, 4.444200),
    "f444w": (3.802370, 5.099550),
    "f460m": (4.465820, 4.813090),
    "f480m": (4.582030, 5.088740),
    "f770w": (6.475000, 8.830000),
    "f435w": (0.359500, 0.488300),
    "f606w": (0.462700, 0.717900),
    "f814w": (0.686800, 0.962600),
    "f098m": (0.889000, 1.084297),
    "vis": (0.495885, 0.930629),
    "u": (0.3100, 0.4000),
    "g": (0.3950, 0.5600),
    "r": (0.5500, 0.7000),
    "i": (0.6900, 0.8400),
    "z": (0.8200, 1.0000),
    "y": (0.9300, 1.0600),
    "Y": (0.9600, 1.0900),
    "J": (1.1500, 1.3500),
    "H": (1.4900, 1.8000),
    "Ks": (1.9900, 2.3200),
}


def _cache_dir() -> Path:
    """Return (and create) the local filter curve cache directory."""
    d = Path.home() / ".cache" / "campfire" / "filters"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _download_filter_curve(band: str) -> Optional[tuple]:
    """Download a filter transmission curve from the SVO Filter Profile Service.

    Returns (wavelength_um, transmission) arrays, or None on failure.
    """
    svo_id = SVO_FILTER_MAP.get(band)
    if svo_id is None:
        return None

    import requests

    try:
        resp = requests.get(
            SVO_BASE_URL,
            params={"format": "ascii", "id": svo_id},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        return None

    # Parse the 2-column ASCII: wavelength (Angstrom), transmission
    wavelengths = []
    transmissions = []
    for line in resp.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                w = float(parts[0])
                t = float(parts[1])
                wavelengths.append(w)
                transmissions.append(t)
            except ValueError:
                continue

    if len(wavelengths) < 3:
        return None

    wave_um = np.array(wavelengths) * 1e-4  # Angstrom → micron
    trans = np.array(transmissions)
    return wave_um, trans


def get_filter_curve(band: str) -> tuple:
    """Get filter transmission curve (wavelength_um, transmission).

    Checks the local cache first, downloads from SVO if needed,
    falls back to a top-hat approximation using known band edges.

    Parameters
    ----------
    band : str
        Band name (e.g. ``'f444w'``).

    Returns
    -------
    wavelength : np.ndarray
        Wavelength in microns.
    transmission : np.ndarray
        Transmission (0–1).
    """
    cache = _cache_dir() / f"{band}.npz"

    # Try cache
    if cache.exists():
        data = np.load(cache)
        return data["wavelength"], data["transmission"]

    # Try SVO download
    result = _download_filter_curve(band)
    if result is not None:
        wave, trans = result
        np.savez_compressed(cache, wavelength=wave, transmission=trans)
        return wave, trans

    # Fallback: top-hat from known band edges
    if band in _FILTER_EDGES:
        blue, red = _FILTER_EDGES[band]
        warnings.warn(
            f"Could not download filter curve for '{band}' from SVO. "
            f"Using top-hat approximation ({blue:.3f}–{red:.3f} μm).",
            stacklevel=2,
        )
        wave = np.array([blue - 0.001, blue, red, red + 0.001])
        trans = np.array([0.0, 1.0, 1.0, 0.0])
        return wave, trans

    raise ValueError(
        f"Unknown band '{band}': not in SVO_FILTER_MAP or _FILTER_EDGES. "
        f"Available bands: {sorted(SVO_FILTER_MAP.keys())}"
    )


# ---------------------------------------------------------------------------
# Synthetic photometry
# ---------------------------------------------------------------------------

def synthetic_photometry(
    wavelength: np.ndarray,
    flux: np.ndarray,
    flux_err: np.ndarray,
    band: str,
) -> tuple:
    """Compute AB synthetic photometry for a single band.

    Uses the standard AB formula for f_ν:
    ``<f_ν> = ∫ f_ν(λ) T(λ) λ dλ / ∫ T(λ) λ dλ``

    Parameters
    ----------
    wavelength : np.ndarray
        Spectrum wavelength in microns.
    flux : np.ndarray
        Spectrum flux density (f_ν) in μJy.
    flux_err : np.ndarray
        Flux error in μJy.
    band : str
        Band name (e.g. ``'f444w'``).

    Returns
    -------
    synth_flux : float
        Synthetic flux in μJy.
    synth_err : float
        Propagated error in μJy.
    """
    filt_wave, filt_trans = get_filter_curve(band)

    # Interpolate filter onto spectrum wavelength grid
    T = np.interp(wavelength, filt_wave, filt_trans, left=0.0, right=0.0)

    # Mask non-finite flux pixels
    valid = np.isfinite(flux) & np.isfinite(flux_err)
    T_v = T.copy()
    T_v[~valid] = 0.0
    flux_v = np.where(valid, flux, 0.0)
    err_v = np.where(valid, flux_err, 0.0)

    denom = np.trapz(T_v * wavelength, wavelength)
    if denom == 0:
        return np.nan, np.nan

    synth_flux = np.trapz(flux_v * T_v * wavelength, wavelength) / denom
    synth_err = np.sqrt(np.trapz((err_v * T_v * wavelength) ** 2, wavelength)) / denom

    return float(synth_flux), float(synth_err)


# ---------------------------------------------------------------------------
# CalibrationResult
# ---------------------------------------------------------------------------

@dataclass
class CalibrationResult:
    """Result of spectral calibration to photometry.

    Attributes
    ----------
    spectrum : SpectrumData
        Calibrated spectrum (new object with corrected flux).
    original : SpectrumData
        Original uncalibrated spectrum.
    multiplier : np.ndarray
        Correction curve applied to flux (same length as wavelength).
    method : str
        Calibration method used (``'chebyshev'`` or ``'flat'``).
    bands_used : list of str
        Bands that contributed to the fit.
    observed_flux : np.ndarray
        Photometric flux for ``bands_used`` (μJy).
    observed_flux_err : np.ndarray
        Photometric flux error (μJy).
    synthetic_flux : np.ndarray
        Synthetic photometry from original spectrum (μJy).
    synthetic_flux_err : np.ndarray
        Synthetic photometry error (μJy).
    band_wavelengths : np.ndarray
        Pivot wavelengths for ``bands_used`` (microns).
    """

    spectrum: SpectrumData
    original: SpectrumData
    multiplier: np.ndarray
    method: str
    bands_used: List[str]
    observed_flux: np.ndarray
    observed_flux_err: np.ndarray
    synthetic_flux: np.ndarray
    synthetic_flux_err: np.ndarray
    band_wavelengths: np.ndarray

    def plot(self, axes=None):
        """Diagnostic plot showing calibration results.

        Two-panel figure:
        - **Top**: Before/after spectra with photometric points.
        - **Bottom**: Calibration multiplier curve with per-band ratios.

        Parameters
        ----------
        axes : array-like of matplotlib.axes.Axes, optional
            Two Axes to draw on. If *None*, a new figure is created.

        Returns
        -------
        tuple of matplotlib.axes.Axes
        """
        import matplotlib.pyplot as plt

        if axes is None:
            fig, axes = plt.subplots(
                2, 1, figsize=(10, 7), height_ratios=[3, 1],
                sharex=True, constrained_layout=True,
            )

        ax_spec, ax_mult = axes

        wave = self.original.wavelength
        valid = np.isfinite(self.original.fnu)

        # Top panel: spectra + photometry
        ax_spec.plot(
            wave[valid], self.original.fnu[valid],
            color="0.5", lw=0.8, alpha=0.7, label="Original",
        )
        ax_spec.plot(
            wave[valid], self.spectrum.fnu[valid],
            color="C0", lw=1, label="Calibrated",
        )
        ax_spec.errorbar(
            self.band_wavelengths, self.observed_flux,
            yerr=self.observed_flux_err,
            fmt="o", color="C3", ms=6, zorder=10, label="Photometry",
        )
        # Synthetic photometry of calibrated spectrum
        synth_after = np.array([
            synthetic_photometry(wave, self.spectrum.fnu, self.spectrum.fnu_err, b)[0]
            for b in self.bands_used
        ])
        ax_spec.scatter(
            self.band_wavelengths, synth_after,
            marker="x", color="C0", s=60, zorder=11, label="Synth (after)",
        )
        ax_spec.scatter(
            self.band_wavelengths, self.synthetic_flux,
            marker="x", color="0.5", s=40, zorder=9, label="Synth (before)",
        )

        ax_spec.set_ylabel("f_ν (μJy)")
        ax_spec.legend(fontsize=8)
        ax_spec.set_title(
            f"{self.original.target_id}  {self.original.grating}  —  "
            f"{self.method} calibration"
        )

        # Bottom panel: multiplier curve + per-band ratios
        ax_mult.plot(wave, self.multiplier, color="C0", lw=1.5)

        ratio = self.observed_flux / self.synthetic_flux
        frac_err = np.sqrt(
            (self.observed_flux_err / self.observed_flux) ** 2
            + (self.synthetic_flux_err / self.synthetic_flux) ** 2
        )
        ratio_err = ratio * frac_err

        ax_mult.errorbar(
            self.band_wavelengths, ratio, yerr=ratio_err,
            fmt="o", color="C3", ms=6, zorder=10,
        )
        ax_mult.axhline(1.0, color="0.5", ls="--", lw=0.8)
        ax_mult.set_xlabel("Wavelength (μm)")
        ax_mult.set_ylabel("Multiplier")

        return ax_spec, ax_mult


# ---------------------------------------------------------------------------
# Main calibration function
# ---------------------------------------------------------------------------

def calibrate_to_photometry(
    spectrum: Union[Spectrum, SpectrumData],
    photometry: Photometry,
    method: str = "chebyshev",
    bands: Optional[List[str]] = None,
    degree: Optional[int] = None,
    min_snr: float = 0.5,
) -> CalibrationResult:
    """Flux-calibrate a spectrum against broadband photometry.

    Computes synthetic photometry from the spectrum for each band,
    compares to the observed photometry, and fits a smooth correction
    curve to bring the spectrum into agreement.

    Parameters
    ----------
    spectrum : Spectrum or SpectrumData
        The spectrum to calibrate. If a :class:`Spectrum` handle,
        ``.open()`` is called to load the data.
    photometry : Photometry
        Broadband photometric measurements (flux in μJy).
    method : str
        ``'chebyshev'`` (default) fits a Chebyshev polynomial.
        ``'flat'`` applies a single scalar correction.
    bands : list of str, optional
        Bands to use. Default: auto-select bands overlapping the
        spectrum with sufficient SNR.
    degree : int, optional
        Chebyshev polynomial degree. Default: ``min(3, n_bands - 1)``.
    min_snr : float
        Minimum SNR in both spectrum and photometry for a band to
        be included in the fit (default 0.5).

    Returns
    -------
    CalibrationResult
        Contains the calibrated spectrum, correction curve, and
        diagnostic data. Call ``.plot()`` for a visual summary.
    """
    # Resolve Spectrum → SpectrumData
    if isinstance(spectrum, Spectrum):
        spec_data = spectrum.open()
    else:
        spec_data = spectrum

    wave = spec_data.wavelength
    flux = spec_data.fnu
    flux_err = spec_data.fnu_err

    # Determine which bands to use
    if bands is None:
        bands = _auto_select_bands(wave, flux, flux_err, photometry, min_snr)
    else:
        # Validate requested bands exist in photometry
        for b in bands:
            if b not in photometry.bands:
                raise ValueError(
                    f"Band '{b}' not found in photometry. "
                    f"Available: {', '.join(photometry.bands)}"
                )

    if len(bands) == 0:
        raise ValueError(
            "No bands overlap with the spectrum wavelength range. "
            "Check that the photometry bands cover the spectral range."
        )

    # Compute synthetic photometry for each band
    synth_fluxes = []
    synth_errs = []
    obs_fluxes = []
    obs_errs = []
    band_waves = []

    for b in bands:
        sf, se = synthetic_photometry(wave, flux, flux_err, b)
        band_info = photometry[b]
        synth_fluxes.append(sf)
        synth_errs.append(se)
        obs_fluxes.append(band_info.flux)
        obs_errs.append(band_info.flux_err)
        band_waves.append(band_info.wavelength)

    synth_fluxes = np.array(synth_fluxes)
    synth_errs = np.array(synth_errs)
    obs_fluxes = np.array(obs_fluxes)
    obs_errs = np.array(obs_errs)
    band_waves = np.array(band_waves)

    # Filter out NaN synthetic photometry
    valid = (
        np.isfinite(synth_fluxes)
        & np.isfinite(obs_fluxes)
        & (synth_fluxes > 0)
        & (obs_fluxes > 0)
    )
    if valid.sum() == 0:
        raise ValueError(
            "No valid band ratios could be computed. Synthetic photometry "
            "returned NaN or zero for all bands."
        )

    if valid.sum() < len(bands):
        dropped = [b for b, v in zip(bands, valid) if not v]
        warnings.warn(
            f"Dropped bands with invalid synthetic photometry: {dropped}",
            stacklevel=2,
        )
        bands = [b for b, v in zip(bands, valid) if v]
        synth_fluxes = synth_fluxes[valid]
        synth_errs = synth_errs[valid]
        obs_fluxes = obs_fluxes[valid]
        obs_errs = obs_errs[valid]
        band_waves = band_waves[valid]

    # Compute correction multiplier
    if method == "chebyshev":
        multiplier = _fit_chebyshev(
            wave, band_waves, obs_fluxes, obs_errs,
            synth_fluxes, synth_errs, degree,
        )
    elif method == "flat":
        multiplier = _fit_flat(
            wave, obs_fluxes, obs_errs, synth_fluxes, synth_errs,
        )
    else:
        raise ValueError(f"Unknown method '{method}'. Use 'chebyshev' or 'flat'.")

    # Apply calibration — create new SpectrumData
    cal_fnu = spec_data.fnu * multiplier
    cal_fnu_err = spec_data.fnu_err * multiplier
    cal_flam = spec_data.flam * multiplier
    cal_flam_err = spec_data.flam_err * multiplier

    calibrated = SpectrumData(
        wavelength=spec_data.wavelength.copy(),
        fnu=cal_fnu,
        fnu_err=cal_fnu_err,
        header=dict(spec_data.header),
        grating=spec_data.grating,
        target_id=spec_data.target_id,
        flam=cal_flam,
        flam_err=cal_flam_err,
        fits_path=spec_data.fits_path,
    )

    return CalibrationResult(
        spectrum=calibrated,
        original=spec_data,
        multiplier=multiplier,
        method=method,
        bands_used=bands,
        observed_flux=obs_fluxes,
        observed_flux_err=obs_errs,
        synthetic_flux=synth_fluxes,
        synthetic_flux_err=synth_errs,
        band_wavelengths=band_waves,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _auto_select_bands(
    wave: np.ndarray,
    flux: np.ndarray,
    flux_err: np.ndarray,
    photometry: Photometry,
    min_snr: float,
) -> List[str]:
    """Select photometry bands that overlap the spectrum with sufficient SNR."""
    wmin = wave[np.isfinite(flux)].min()
    wmax = wave[np.isfinite(flux)].max()

    selected = []
    for b in photometry.bands:
        # Check band overlaps spectrum
        if b in _FILTER_EDGES:
            blue, red = _FILTER_EDGES[b]
        elif b in SVO_FILTER_MAP:
            # Use photometry pivot wavelength as rough check
            band_info = photometry[b]
            blue = band_info.wavelength * 0.8
            red = band_info.wavelength * 1.2
        else:
            continue

        if red < wmin or blue > wmax:
            continue

        # Check photometric SNR
        band_info = photometry[b]
        if band_info.flux_err > 0 and band_info.flux / band_info.flux_err < min_snr:
            continue

        # Check synthetic photometry SNR
        sf, se = synthetic_photometry(wave, flux, flux_err, b)
        if not np.isfinite(sf) or sf <= 0:
            continue
        if se > 0 and sf / se < min_snr:
            continue

        selected.append(b)

    return selected


def _fit_chebyshev(
    wave: np.ndarray,
    band_waves: np.ndarray,
    obs_flux: np.ndarray,
    obs_err: np.ndarray,
    synth_flux: np.ndarray,
    synth_err: np.ndarray,
    degree: Optional[int],
) -> np.ndarray:
    """Fit a Chebyshev polynomial calibration curve."""
    ratio = obs_flux / synth_flux
    frac_err = np.sqrt(
        (obs_err / obs_flux) ** 2 + (synth_err / synth_flux) ** 2
    )
    ratio_err = ratio * frac_err

    # Map wavelengths to [-1, 1] using spectrum bounds
    wmin, wmax = wave.min(), wave.max()
    cheb_band = (band_waves - wmin) / (wmax - wmin) * 2 - 1
    cheb_wave = (wave - wmin) / (wmax - wmin) * 2 - 1

    # Auto degree: min(3, n_bands - 1)
    n = len(band_waves)
    if degree is None:
        degree = min(3, n - 1)
    degree = max(0, min(degree, n - 1))

    # Weighted Chebyshev fit
    weights = 1.0 / ratio_err
    coeffs = np.polynomial.chebyshev.chebfit(cheb_band, ratio, degree, w=weights)
    multiplier = np.polynomial.chebyshev.chebval(cheb_wave, coeffs)

    # Clamp to prevent runaway corrections
    multiplier = np.clip(multiplier, 0.1, 50.0)

    # Freeze beyond the reddest fitted band (constant extrapolation)
    red_idx = wave > band_waves.max()
    if red_idx.any():
        multiplier[red_idx] = multiplier[~red_idx][-1] if (~red_idx).any() else 1.0

    # Freeze below the bluest fitted band
    blue_idx = wave < band_waves.min()
    if blue_idx.any():
        multiplier[blue_idx] = multiplier[~blue_idx][0] if (~blue_idx).any() else 1.0

    return multiplier


def _fit_flat(
    wave: np.ndarray,
    obs_flux: np.ndarray,
    obs_err: np.ndarray,
    synth_flux: np.ndarray,
    synth_err: np.ndarray,
) -> np.ndarray:
    """Compute a flat (scalar) calibration correction."""
    ratio = obs_flux / synth_flux
    frac_err = np.sqrt(
        (obs_err / obs_flux) ** 2 + (synth_err / synth_flux) ** 2
    )
    ratio_err = ratio * frac_err

    # Inverse-variance weighted mean
    weights = 1.0 / ratio_err**2
    flat_ratio = np.sum(weights * ratio) / np.sum(weights)

    return np.full_like(wave, flat_ratio)


# ---------------------------------------------------------------------------
# Spectral resampling
# ---------------------------------------------------------------------------

def _resample(
    new_wave: np.ndarray,
    old_wave: np.ndarray,
    flux: np.ndarray,
    flux_err: np.ndarray,
) -> tuple:
    """Resample a spectrum onto a new wavelength grid.

    Uses ``spectres`` for flux-conserving resampling if installed,
    otherwise falls back to linear interpolation.

    Returns (resampled_flux, resampled_flux_err).
    """
    try:
        from spectres import spectres
        new_flux = spectres(new_wave, old_wave, flux, spec_errs=flux_err, fill=np.nan, verbose=False)
        # spectres returns (flux, err) when spec_errs is given
        if isinstance(new_flux, tuple):
            return new_flux[0], new_flux[1]
        # If only flux returned, resample errors separately
        new_err = spectres(new_wave, old_wave, flux_err, fill=np.nan, verbose=False)
        return new_flux, new_err
    except ImportError:
        pass

    # Fallback: linear interpolation (set out-of-range to NaN)
    valid = np.isfinite(flux) & np.isfinite(flux_err)
    new_flux = np.interp(new_wave, old_wave[valid], flux[valid], left=np.nan, right=np.nan)
    new_err = np.interp(new_wave, old_wave[valid], flux_err[valid], left=np.nan, right=np.nan)
    return new_flux, new_err


# ---------------------------------------------------------------------------
# Spectrum stacking
# ---------------------------------------------------------------------------

def stack_spectra(
    spectra: List[SpectrumData],
    method: str = "weighted_mean",
    wavelength_grid: Optional[np.ndarray] = None,
) -> SpectrumData:
    """Stack multiple spectra onto a common wavelength grid.

    Resamples all input spectra to a shared wavelength grid, then
    combines them using inverse-variance weighting (default) or
    median.

    Parameters
    ----------
    spectra : list of SpectrumData
        Spectra to stack. Should generally share the same grating.
    method : str
        ``'weighted_mean'`` (default): inverse-variance weighted mean.
        ``'median'``: pixel-wise median (errors from MAD).
        ``'mean'``: unweighted mean.
    wavelength_grid : np.ndarray, optional
        Common wavelength grid (microns). Default: use the grid from
        the spectrum with the most pixels.

    Returns
    -------
    SpectrumData
        Stacked spectrum. ``target_id`` is taken from the first input;
        ``grating`` from the first input.

    Raises
    ------
    ValueError
        If fewer than 2 spectra are provided.
    """
    if len(spectra) < 2:
        raise ValueError("Need at least 2 spectra to stack.")

    # Choose reference wavelength grid
    if wavelength_grid is None:
        ref = max(spectra, key=lambda s: len(s.wavelength))
        wavelength_grid = ref.wavelength.copy()

    n_pix = len(wavelength_grid)
    n_spec = len(spectra)

    # Resample all spectra onto the common grid
    flux_cube = np.full((n_spec, n_pix), np.nan)
    err_cube = np.full((n_spec, n_pix), np.nan)

    for i, s in enumerate(spectra):
        if np.array_equal(s.wavelength, wavelength_grid):
            flux_cube[i] = s.fnu
            err_cube[i] = s.fnu_err
        else:
            flux_cube[i], err_cube[i] = _resample(
                wavelength_grid, s.wavelength, s.fnu, s.fnu_err,
            )

    # Stack
    if method == "weighted_mean":
        stacked_flux, stacked_err = _stack_weighted_mean(flux_cube, err_cube)
    elif method == "median":
        stacked_flux, stacked_err = _stack_median(flux_cube)
    elif method == "mean":
        stacked_flux, stacked_err = _stack_mean(flux_cube, err_cube)
    else:
        raise ValueError(
            f"Unknown stacking method '{method}'. "
            f"Use 'weighted_mean', 'median', or 'mean'."
        )

    # Also stack flam
    flam_cube = np.full((n_spec, n_pix), np.nan)
    flam_err_cube = np.full((n_spec, n_pix), np.nan)
    for i, s in enumerate(spectra):
        if np.array_equal(s.wavelength, wavelength_grid):
            flam_cube[i] = s.flam
            flam_err_cube[i] = s.flam_err
        else:
            flam_cube[i], flam_err_cube[i] = _resample(
                wavelength_grid, s.wavelength, s.flam, s.flam_err,
            )
    if method == "weighted_mean":
        stacked_flam, stacked_flam_err = _stack_weighted_mean(flam_cube, flam_err_cube)
    elif method == "median":
        stacked_flam, stacked_flam_err = _stack_median(flam_cube)
    else:
        stacked_flam, stacked_flam_err = _stack_mean(flam_cube, flam_err_cube)

    # Build merged header
    header = dict(spectra[0].header)
    header["NSTACK"] = n_spec
    header["STACKMTH"] = method

    target_ids = list(dict.fromkeys(s.target_id for s in spectra))

    return SpectrumData(
        wavelength=wavelength_grid,
        fnu=stacked_flux,
        fnu_err=stacked_err,
        header=header,
        grating=spectra[0].grating,
        target_id=" + ".join(target_ids) if len(target_ids) > 1 else target_ids[0],
        flam=stacked_flam,
        flam_err=stacked_flam_err,
    )


def _stack_weighted_mean(
    flux_cube: np.ndarray,
    err_cube: np.ndarray,
) -> tuple:
    """Inverse-variance weighted mean along axis 0."""
    with np.errstate(divide="ignore", invalid="ignore"):
        weights = np.where(
            np.isfinite(flux_cube) & np.isfinite(err_cube) & (err_cube > 0),
            1.0 / err_cube**2,
            0.0,
        )
    w_sum = np.sum(weights, axis=0)
    safe = w_sum > 0

    stacked = np.full(flux_cube.shape[1], np.nan)
    stacked_err = np.full(flux_cube.shape[1], np.nan)

    flux_filled = np.where(np.isfinite(flux_cube), flux_cube, 0.0)
    stacked[safe] = np.sum(weights * flux_filled, axis=0)[safe] / w_sum[safe]
    stacked_err[safe] = 1.0 / np.sqrt(w_sum[safe])

    return stacked, stacked_err


def _stack_median(flux_cube: np.ndarray) -> tuple:
    """Median stack with MAD-based error estimate."""
    stacked = np.nanmedian(flux_cube, axis=0)
    # MAD → sigma: σ ≈ 1.4826 * MAD
    mad = np.nanmedian(np.abs(flux_cube - stacked[np.newaxis, :]), axis=0)
    n_good = np.sum(np.isfinite(flux_cube), axis=0)
    # Error on median ≈ 1.4826 * MAD / sqrt(n)
    with np.errstate(divide="ignore", invalid="ignore"):
        stacked_err = np.where(n_good > 0, 1.4826 * mad / np.sqrt(n_good), np.nan)
    return stacked, stacked_err


def _stack_mean(
    flux_cube: np.ndarray,
    err_cube: np.ndarray,
) -> tuple:
    """Unweighted mean with propagated errors."""
    stacked = np.nanmean(flux_cube, axis=0)
    n_good = np.sum(np.isfinite(flux_cube) & np.isfinite(err_cube), axis=0)
    err_filled = np.where(np.isfinite(err_cube), err_cube, 0.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        stacked_err = np.where(
            n_good > 0,
            np.sqrt(np.nansum(err_filled**2, axis=0)) / n_good,
            np.nan,
        )
    return stacked, stacked_err


# ---------------------------------------------------------------------------
# Calibrate + stack convenience
# ---------------------------------------------------------------------------

@dataclass
class StackResult:
    """Result of calibrating and stacking multiple spectra.

    Attributes
    ----------
    spectrum : SpectrumData
        The final stacked spectrum.
    calibrations : list of CalibrationResult
        Per-spectrum calibration results (empty if no calibration was
        performed).
    input_spectra : list of SpectrumData
        The individual (calibrated) spectra before stacking.
    stacking_method : str
        Stacking method used.
    """

    spectrum: SpectrumData
    calibrations: List[CalibrationResult]
    input_spectra: List[SpectrumData]
    stacking_method: str

    def plot(self, axes=None):
        """Diagnostic plot showing stacked spectrum and inputs.

        Three-panel figure:
        - **Top**: Individual calibrated spectra (translucent) with
          stacked result overlaid.
        - **Middle**: Stacked spectrum with error band.
        - **Bottom**: Per-spectrum calibration multipliers (if available).

        Parameters
        ----------
        axes : array-like of matplotlib.axes.Axes, optional
            Axes to draw on. If *None*, a new figure is created.
            Pass 2 Axes if no calibrations, 3 if calibrations exist.

        Returns
        -------
        tuple of matplotlib.axes.Axes
        """
        import matplotlib.pyplot as plt

        has_calibrations = len(self.calibrations) > 0
        n_panels = 3 if has_calibrations else 2

        if axes is None:
            ratios = [3, 2, 1] if has_calibrations else [3, 2]
            fig, axes = plt.subplots(
                n_panels, 1, figsize=(10, 3 * n_panels),
                height_ratios=ratios,
                sharex=True, constrained_layout=True,
            )

        ax_overlay = axes[0]
        ax_stacked = axes[1]

        wave = self.spectrum.wavelength

        # Top: individual spectra + stack overlay
        for i, s in enumerate(self.input_spectra):
            valid = np.isfinite(s.fnu)
            ax_overlay.plot(
                s.wavelength[valid], s.fnu[valid],
                alpha=0.4, lw=0.7, label=f"Spec {i+1}" if i < 5 else None,
            )
        valid = np.isfinite(self.spectrum.fnu)
        ax_overlay.plot(
            wave[valid], self.spectrum.fnu[valid],
            color="black", lw=1.5, label="Stacked",
        )
        ax_overlay.set_ylabel("f_ν (μJy)")
        ax_overlay.legend(fontsize=8)
        ax_overlay.set_title(
            f"{self.spectrum.target_id}  {self.spectrum.grating}  —  "
            f"{self.stacking_method} stack of {len(self.input_spectra)} spectra"
        )

        # Middle: stacked with error band
        ax_stacked.plot(
            wave[valid], self.spectrum.fnu[valid],
            color="C0", lw=1,
        )
        err = self.spectrum.fnu_err
        ax_stacked.fill_between(
            wave[valid],
            self.spectrum.fnu[valid] - err[valid],
            self.spectrum.fnu[valid] + err[valid],
            alpha=0.15, color="C0",
        )
        ax_stacked.set_ylabel("f_ν (μJy)")

        # Bottom: calibration multipliers
        if has_calibrations:
            ax_calib = axes[2]
            for i, c in enumerate(self.calibrations):
                ax_calib.plot(
                    c.original.wavelength, c.multiplier,
                    alpha=0.7, lw=1, label=f"Spec {i+1}" if i < 5 else None,
                )
            ax_calib.axhline(1.0, color="0.5", ls="--", lw=0.8)
            ax_calib.set_ylabel("Multiplier")
            ax_calib.legend(fontsize=8)

        axes[-1].set_xlabel("Wavelength (μm)")

        return tuple(axes)


def calibrate_and_stack(
    spectra: Union[SpectrumCollection, List[Spectrum], List[SpectrumData]],
    photometry: Optional[Photometry] = None,
    calibration_method: str = "chebyshev",
    stacking_method: str = "weighted_mean",
    wavelength_grid: Optional[np.ndarray] = None,
    **calibration_kwargs,
) -> StackResult:
    """Calibrate multiple spectra to photometry and stack them.

    Convenience function that combines :func:`calibrate_to_photometry`
    and :func:`stack_spectra` into a single call.

    Parameters
    ----------
    spectra : SpectrumCollection, list of Spectrum, or list of SpectrumData
        Spectra to calibrate and stack.
    photometry : Photometry, optional
        Photometric measurements. Required unless *spectra* are already
        :class:`SpectrumData` (in which case stacking is done without
        calibration).
    calibration_method : str
        Passed to :func:`calibrate_to_photometry` as ``method``.
    stacking_method : str
        Passed to :func:`stack_spectra` as ``method``.
    wavelength_grid : np.ndarray, optional
        Common wavelength grid for stacking.
    **calibration_kwargs
        Additional keyword arguments passed to
        :func:`calibrate_to_photometry` (e.g. ``bands``, ``degree``).

    Returns
    -------
    StackResult
        Contains the stacked spectrum and per-spectrum calibration
        results. Call ``.plot()`` for a visual summary.
    """
    spec_list = list(spectra)

    if not spec_list:
        raise ValueError("No spectra provided.")

    # Determine if we need calibration
    calibrations = []
    calibrated_data = []

    if isinstance(spec_list[0], SpectrumData):
        # Already loaded — stack directly (no calibration)
        calibrated_data = spec_list
    elif photometry is not None:
        # Calibrate each spectrum
        for s in spec_list:
            calib = calibrate_to_photometry(
                s, photometry,
                method=calibration_method,
                **calibration_kwargs,
            )
            calibrations.append(calib)
            calibrated_data.append(calib.spectrum)
    else:
        # No photometry — just load and stack uncalibrated
        for s in spec_list:
            if isinstance(s, Spectrum):
                calibrated_data.append(s.open())
            else:
                calibrated_data.append(s)

    if len(calibrated_data) < 2:
        raise ValueError("Need at least 2 spectra to stack.")

    stacked = stack_spectra(
        calibrated_data,
        method=stacking_method,
        wavelength_grid=wavelength_grid,
    )

    return StackResult(
        spectrum=stacked,
        calibrations=calibrations,
        input_spectra=calibrated_data,
        stacking_method=stacking_method,
    )
