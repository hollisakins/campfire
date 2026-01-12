"""
Plotting utilities for CAMPFIRE spectrum visualization.

This module provides Plotly-based plotting functions for visualizing NIRSpec
spectroscopic data, matching the functionality of the CAMPFIRE web interface.
"""

from typing import Dict, List, Optional, Tuple, Union
import numpy as np

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    raise ImportError(
        "Plotting functionality requires plotly. "
        "Install with: pip install plotly"
    )


# =============================================================================
# Constants
# =============================================================================

# Common emission lines with rest wavelengths in microns
# Colors assigned as rainbow from blue (short wavelength) to red (long wavelength)
EMISSION_LINES = [
    {"name": "Lyα", "wave": 0.12157, "color": "#6366f1"},      # indigo (shortest)
    {"name": "CIV", "wave": 0.1549, "color": "#4f46e5"},       # indigo-600
    {"name": "CIII]", "wave": 0.1909, "color": "#4338ca"},     # indigo-700
    {"name": "MgII", "wave": 0.2798, "color": "#2563eb"},      # blue-600
    {"name": "[OII]", "wave": 0.3727, "color": "#0ea5e9"},     # sky-500
    {"name": "Hδ", "wave": 0.4102, "color": "#06b6d4"},        # cyan-500
    {"name": "Hγ", "wave": 0.4341, "color": "#14b8a6"},        # teal-500
    {"name": "Hβ", "wave": 0.4861, "color": "#10b981"},        # emerald-500
    {"name": "[OIII]₁", "wave": 0.4959, "color": "#22c55e"},   # green-500
    {"name": "[OIII]₂", "wave": 0.5007, "color": "#84cc16"},   # lime-500
    {"name": "Hα", "wave": 0.6563, "color": "#eab308"},        # yellow-500
    {"name": "[NII]", "wave": 0.6584, "color": "#f59e0b"},     # amber-500
    {"name": "[SII]₁", "wave": 0.6717, "color": "#f97316"},    # orange-500
    {"name": "[SII]₂", "wave": 0.6731, "color": "#ef4444"},    # red-500
    {"name": "Paβ", "wave": 1.2822, "color": "#dc2626"},       # red-600
    {"name": "Paα", "wave": 1.8751, "color": "#b91c1c"},       # red-700 (longest)
]

# Colormaps matching the web interface
COLORMAPS = {
    "viridis": "Viridis",
    "plasma": "Plasma",
    "inferno": "Inferno",
    "magma": "Magma",
    "cividis": "Cividis",
    "greys": "Greys",
}

# Default accent color for plots
DEFAULT_ACCENT_COLOR = "#ec4899"  # magenta


def _hex_to_rgba(hex_color: str, alpha: float = 1.0) -> str:
    """Convert hex color to rgba string with specified alpha."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


# =============================================================================
# Helper Functions
# =============================================================================

def convert_flux_units(
    fnu: np.ndarray,
    wavelength: np.ndarray,
    to_unit: str = "flambda"
) -> np.ndarray:
    """
    Convert flux between f_nu (μJy) and f_lambda (erg/s/cm²/Å).

    Parameters
    ----------
    fnu : array_like
        Flux density in μJy.
    wavelength : array_like
        Wavelength in μm.
    to_unit : str
        Target unit: 'flambda' or 'fnu'.

    Returns
    -------
    numpy.ndarray
        Converted flux values.

    Notes
    -----
    Conversion formula:
        f_λ = f_ν * 2.998e-19 / λ²

    Where:
        - f_ν is in μJy (1 μJy = 10^-29 erg/s/cm²/Hz)
        - λ is in μm
        - f_λ is in erg/s/cm²/Å
    """
    fnu = np.asarray(fnu, dtype=float)
    wavelength = np.asarray(wavelength, dtype=float)

    if to_unit == "flambda":
        return fnu * 2.998e-19 / (wavelength ** 2)
    elif to_unit == "fnu":
        return fnu
    else:
        raise ValueError(f"Unknown unit: {to_unit}. Use 'fnu' or 'flambda'.")


def get_emission_lines(
    redshift: float,
    wave_min: Optional[float] = None,
    wave_max: Optional[float] = None
) -> List[Dict]:
    """
    Calculate observed wavelengths for emission lines at given redshift.

    Parameters
    ----------
    redshift : float
        Redshift to apply.
    wave_min : float, optional
        Minimum wavelength (μm) to filter lines.
    wave_max : float, optional
        Maximum wavelength (μm) to filter lines.

    Returns
    -------
    list of dict
        List of emission lines with observed wavelengths.
    """
    lines = []
    for line in EMISSION_LINES:
        observed_wave = line["wave"] * (1 + redshift)

        # Filter by wavelength range if specified
        if wave_min is not None and observed_wave < wave_min:
            continue
        if wave_max is not None and observed_wave > wave_max:
            continue

        lines.append({
            "name": line["name"],
            "rest_wave": line["wave"],
            "observed_wave": observed_wave,
            "color": line["color"],
        })

    return lines


def _build_step_coords(
    x: np.ndarray,
    y: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Build step function coordinates for plotting.

    Creates horizontal-vertical step pattern from data points.

    Parameters
    ----------
    x : array_like
        X coordinates.
    y : array_like
        Y coordinates.

    Returns
    -------
    tuple of (x_step, y_step)
        Coordinates for step function plotting.
    """
    x = np.asarray(x)
    y = np.asarray(y)

    n = len(x)
    if n == 0:
        return np.array([]), np.array([])

    # Calculate bin widths (use half spacing on edges)
    if n > 1:
        dx = np.diff(x)
        bin_widths = np.concatenate([[dx[0]], (dx[:-1] + dx[1:]) / 2, [dx[-1]]])
    else:
        bin_widths = np.array([1.0])

    # Build step coordinates
    x_step = []
    y_step = []

    for i in range(n):
        left = x[i] - bin_widths[i] / 2
        right = x[i] + bin_widths[i] / 2

        x_step.extend([left, right])
        y_step.extend([y[i], y[i]])

    return np.array(x_step), np.array(y_step)


def _get_flux_label(unit: str) -> str:
    """Get axis label for flux unit."""
    if unit == "fnu":
        return "fν (μJy)"
    elif unit == "flambda":
        return "fλ (erg/s/cm²/Å)"
    else:
        return "Flux"


# =============================================================================
# Main Plotting Functions
# =============================================================================

def plot_spectrum(
    spectrum_data: Dict,
    redshift: float = 0.0,
    flux_unit: str = "fnu",
    show_errors: bool = True,
    show_emission_lines: bool = False,
    colormap: str = "viridis",
    snr_range: Tuple[float, float] = (-5, 10),
    accent_color: str = DEFAULT_ACCENT_COLOR,
    title: Optional[str] = None,
    width: int = 1000,
    height: int = 700,
) -> go.Figure:
    """
    Create a multi-panel spectrum plot matching the CAMPFIRE web interface.

    The plot includes:
    - Top-left: 2D S/N heatmap (wavelength vs spatial position)
    - Top-right: Cross-dispersion profile with extraction weight
    - Bottom: 1D spectrum with error band

    Parameters
    ----------
    spectrum_data : dict
        Spectrum data from client.get_spectrum_data() with keys:
        wave, fnu, fnu_err, snr_2d, n_spatial, n_wave, profile, profile_fit, profile_pix.
    redshift : float, optional
        Redshift for emission line overlay (default: 0.0).
    flux_unit : str, optional
        Flux unit: 'fnu' (μJy) or 'flambda' (erg/s/cm²/Å) (default: 'fnu').
    show_errors : bool, optional
        Show error band around spectrum (default: True).
    show_emission_lines : bool, optional
        Show emission line markers (default: False).
    colormap : str, optional
        Colormap for 2D heatmap (default: 'viridis').
        Options: viridis, plasma, inferno, magma, cividis, greys.
    snr_range : tuple, optional
        (min, max) S/N range for heatmap colorbar (default: (-5, 10)).
    accent_color : str, optional
        Accent color for spectrum line and error band (default: magenta).
    title : str, optional
        Plot title.
    width : int, optional
        Figure width in pixels (default: 1000).
    height : int, optional
        Figure height in pixels (default: 700).

    Returns
    -------
    plotly.graph_objects.Figure
        Interactive Plotly figure.

    Examples
    --------
    >>> from campfire import Campfire
    >>> from campfire.plotting import plot_spectrum
    >>>
    >>> cf = Campfire()
    >>> data = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')
    >>> fig = plot_spectrum(data, redshift=2.5, show_emission_lines=True)
    >>> fig.show()
    """
    # Extract data
    wave = np.array(spectrum_data["wave"])
    fnu = np.array(spectrum_data["fnu"], dtype=float)
    fnu_err = np.array(spectrum_data.get("fnu_err", []), dtype=float)
    snr_2d = np.array(spectrum_data.get("snr_2d", []))
    profile = np.array(spectrum_data.get("profile", []))
    profile_fit = np.array(spectrum_data.get("profile_fit", []))
    profile_pix = np.array(spectrum_data.get("profile_pix", []))

    # Handle null values
    fnu = np.where(np.isnan(fnu) | (fnu is None), np.nan, fnu)
    if len(fnu_err) > 0:
        fnu_err = np.where(np.isnan(fnu_err) | (fnu_err is None), np.nan, fnu_err)

    # Convert flux units if needed
    if flux_unit == "flambda":
        fnu = convert_flux_units(fnu, wave, "flambda")
        if len(fnu_err) > 0:
            fnu_err = convert_flux_units(fnu_err, wave, "flambda")

    # Create subplots
    fig = make_subplots(
        rows=2, cols=2,
        column_widths=[0.8, 0.2],
        row_heights=[0.35, 0.65],
        specs=[
            [{"type": "heatmap"}, {"type": "scatter"}],
            [{"type": "scatter", "colspan": 2}, None],
        ],
        horizontal_spacing=0.02,
        vertical_spacing=0.08,
        shared_xaxes=False,
    )

    # -------------------------------------------------------------------------
    # 2D S/N Heatmap (top-left)
    # -------------------------------------------------------------------------
    if len(snr_2d) > 0:
        colorscale = COLORMAPS.get(colormap, "Viridis")

        fig.add_trace(
            go.Heatmap(
                z=snr_2d,
                x=wave,
                colorscale=colorscale,
                zmin=snr_range[0],
                zmax=snr_range[1],
                colorbar=dict(
                    title="S/N",
                    len=0.35,
                    y=0.82,
                    thickness=15,
                ),
                hovertemplate="λ: %{x:.4f} μm<br>S/N: %{z:.2f}<extra></extra>",
            ),
            row=1, col=1
        )

    # -------------------------------------------------------------------------
    # Cross-dispersion profile (top-right)
    # -------------------------------------------------------------------------
    if len(profile) > 0 and len(profile_pix) > 0:
        # Step function for profile
        x_step, y_step = _build_step_coords(profile_pix, profile)

        fig.add_trace(
            go.Scatter(
                x=y_step,  # Profile values on x-axis
                y=x_step,  # Pixel positions on y-axis
                mode="lines",
                line=dict(color=accent_color, width=1.5),
                name="Profile",
                showlegend=False,
                hovertemplate="Profile: %{x:.3f}<br>Pixel: %{y:.1f}<extra></extra>",
            ),
            row=1, col=2
        )

        # Extraction weight fill
        if len(profile_fit) > 0:
            x_fit_step, y_fit_step = _build_step_coords(profile_pix, profile_fit)
            fig.add_trace(
                go.Scatter(
                    x=y_fit_step,
                    y=x_fit_step,
                    mode="lines",
                    fill="tozerox",
                    fillcolor="rgba(239, 68, 68, 0.3)",  # red with transparency
                    line=dict(color="rgba(239, 68, 68, 0.5)", width=1),
                    name="Extraction",
                    showlegend=False,
                ),
                row=1, col=2
            )

    # -------------------------------------------------------------------------
    # 1D Spectrum (bottom)
    # -------------------------------------------------------------------------

    # Valid data mask
    valid = ~np.isnan(fnu)

    # Error band
    if show_errors and len(fnu_err) > 0:
        valid_err = valid & ~np.isnan(fnu_err)
        wave_valid = wave[valid_err]
        fnu_valid = fnu[valid_err]
        err_valid = fnu_err[valid_err]

        # Create filled region for error band
        fig.add_trace(
            go.Scatter(
                x=np.concatenate([wave_valid, wave_valid[::-1]]),
                y=np.concatenate([fnu_valid + err_valid, (fnu_valid - err_valid)[::-1]]),
                fill="toself",
                fillcolor=_hex_to_rgba(accent_color, 0.15),  # 15% opacity
                line=dict(width=0),
                name="1σ error",
                showlegend=True,
                hoverinfo="skip",
            ),
            row=2, col=1
        )

    # Spectrum line
    fig.add_trace(
        go.Scatter(
            x=wave[valid],
            y=fnu[valid],
            mode="lines",
            line=dict(color=accent_color, width=1.5),
            name="Spectrum",
            hovertemplate="λ: %{x:.4f} μm<br>" + _get_flux_label(flux_unit).split()[0] + ": %{y:.3g}<extra></extra>",
        ),
        row=2, col=1
    )

    # -------------------------------------------------------------------------
    # Emission Lines
    # -------------------------------------------------------------------------
    if show_emission_lines and redshift > 0:
        wave_min, wave_max = wave.min(), wave.max()
        flux_min = np.nanmin(fnu[valid])
        flux_max = np.nanmax(fnu[valid])

        lines = get_emission_lines(redshift, wave_min, wave_max)

        for line in lines:
            fig.add_trace(
                go.Scatter(
                    x=[line["observed_wave"], line["observed_wave"]],
                    y=[flux_min * 0.9, flux_max * 1.1],
                    mode="lines",
                    line=dict(color=line["color"], width=1.5, dash="dash"),
                    name=line["name"],
                    legendgroup="emission_lines",
                    hovertemplate=(
                        f"{line['name']}<br>"
                        f"λ_rest: {line['rest_wave']:.4f} μm<br>"
                        f"λ_obs: {line['observed_wave']:.4f} μm<extra></extra>"
                    ),
                ),
                row=2, col=1
            )

    # -------------------------------------------------------------------------
    # Layout
    # -------------------------------------------------------------------------
    fig.update_layout(
        title=dict(text=title, x=0.5) if title else None,
        width=width,
        height=height,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
        template="plotly_white",
        hovermode="closest",
    )

    # Axis labels
    fig.update_xaxes(title_text="Wavelength (μm)", row=2, col=1)
    fig.update_yaxes(title_text=_get_flux_label(flux_unit), row=2, col=1)
    fig.update_xaxes(title_text="Wavelength (μm)", row=1, col=1)
    fig.update_yaxes(title_text="Spatial (pix)", row=1, col=1)
    fig.update_xaxes(title_text="", row=1, col=2)
    fig.update_yaxes(title_text="", row=1, col=2)

    # Link x-axes for heatmap and spectrum
    fig.update_xaxes(matches="x", row=2, col=1)

    return fig


def plot_redshift_fit(
    fit_data: Dict,
    spectrum_data: Optional[Dict] = None,
    flux_unit: str = "fnu",
    show_emission_lines: bool = True,
    accent_color: str = DEFAULT_ACCENT_COLOR,
    title: Optional[str] = None,
    width: int = 900,
    height: int = 600,
) -> go.Figure:
    """
    Create a redshift fitting plot with chi-squared curve.

    The plot includes:
    - Top: Observed spectrum with best-fit model overlay
    - Bottom: Chi-squared vs redshift curve

    Parameters
    ----------
    fit_data : dict
        Fit data from client.get_redshift_fit_data() with keys:
        redshift, chi2_min, confidence, z_grid, chi2_grid, model_wave, model_fnu.
    spectrum_data : dict, optional
        Spectrum data from client.get_spectrum_data() for observed spectrum overlay.
    flux_unit : str, optional
        Flux unit: 'fnu' (μJy) or 'flambda' (erg/s/cm²/Å) (default: 'fnu').
    show_emission_lines : bool, optional
        Show emission line markers at best-fit redshift (default: True).
    accent_color : str, optional
        Accent color for observed spectrum (default: magenta).
    title : str, optional
        Plot title. If None, auto-generates from fit results.
    width : int, optional
        Figure width in pixels (default: 900).
    height : int, optional
        Figure height in pixels (default: 600).

    Returns
    -------
    plotly.graph_objects.Figure
        Interactive Plotly figure.

    Examples
    --------
    >>> from campfire import Campfire
    >>> from campfire.plotting import plot_redshift_fit
    >>>
    >>> cf = Campfire()
    >>> fit = cf.get_redshift_fit_data('ember_uds_p4_123456', 'PRISM')
    >>> spec = cf.get_spectrum_data('ember_uds_p4_123456', 'PRISM')
    >>> fig = plot_redshift_fit(fit, spectrum_data=spec)
    >>> fig.show()
    """
    # Extract fit data
    redshift = fit_data["redshift"]
    chi2_min = fit_data["chi2_min"]
    confidence = fit_data.get("confidence", 0)
    z_grid = np.array(fit_data["z_grid"])
    chi2_grid = np.array(fit_data["chi2_grid"])
    model_wave = np.array(fit_data.get("model_wave", []))
    model_fnu = np.array(fit_data.get("model_fnu", []))

    # Auto-generate title if not provided
    if title is None:
        title = f"z = {redshift:.4f} | χ² = {chi2_min:.2f} | {confidence:.1f}% confidence"

    # Create subplots
    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.1,
        subplot_titles=("Spectrum + Model", "χ² vs Redshift"),
    )

    # -------------------------------------------------------------------------
    # Top panel: Spectrum + Model
    # -------------------------------------------------------------------------
    if spectrum_data is not None:
        wave = np.array(spectrum_data["wave"])
        fnu = np.array(spectrum_data["fnu"], dtype=float)
        fnu_err = np.array(spectrum_data.get("fnu_err", []), dtype=float)

        # Handle null values
        fnu = np.where(np.isnan(fnu), np.nan, fnu)

        # Convert units if needed
        if flux_unit == "flambda":
            fnu = convert_flux_units(fnu, wave, "flambda")
            if len(fnu_err) > 0:
                fnu_err = convert_flux_units(fnu_err, wave, "flambda")

        valid = ~np.isnan(fnu)

        # Error band
        if len(fnu_err) > 0:
            valid_err = valid & ~np.isnan(fnu_err)
            wave_valid = wave[valid_err]
            fnu_valid = fnu[valid_err]
            err_valid = fnu_err[valid_err]

            fig.add_trace(
                go.Scatter(
                    x=np.concatenate([wave_valid, wave_valid[::-1]]),
                    y=np.concatenate([fnu_valid + err_valid, (fnu_valid - err_valid)[::-1]]),
                    fill="toself",
                    fillcolor=_hex_to_rgba(accent_color, 0.15),
                    line=dict(width=0),
                    name="1σ error",
                    showlegend=True,
                    hoverinfo="skip",
                ),
                row=1, col=1
            )

        # Observed spectrum
        fig.add_trace(
            go.Scatter(
                x=wave[valid],
                y=fnu[valid],
                mode="lines",
                line=dict(color=accent_color, width=1.5),
                name="Observed",
            ),
            row=1, col=1
        )

    # Model spectrum
    if len(model_wave) > 0 and len(model_fnu) > 0:
        model_flux = model_fnu
        if flux_unit == "flambda":
            model_flux = convert_flux_units(model_fnu, model_wave, "flambda")

        fig.add_trace(
            go.Scatter(
                x=model_wave,
                y=model_flux,
                mode="lines",
                line=dict(color="#f97316", width=2),  # orange
                name="Model",
            ),
            row=1, col=1
        )

    # Emission lines
    if show_emission_lines and spectrum_data is not None:
        wave = np.array(spectrum_data["wave"])
        fnu = np.array(spectrum_data["fnu"], dtype=float)
        valid = ~np.isnan(fnu)

        if flux_unit == "flambda":
            fnu = convert_flux_units(fnu, wave, "flambda")

        wave_min, wave_max = wave.min(), wave.max()
        flux_min = np.nanmin(fnu[valid])
        flux_max = np.nanmax(fnu[valid])

        lines = get_emission_lines(redshift, wave_min, wave_max)

        for line in lines:
            fig.add_trace(
                go.Scatter(
                    x=[line["observed_wave"], line["observed_wave"]],
                    y=[flux_min * 0.9, flux_max * 1.1],
                    mode="lines",
                    line=dict(color=line["color"], width=1.5, dash="dash"),
                    name=line["name"],
                    legendgroup="emission_lines",
                    showlegend=False,
                ),
                row=1, col=1
            )

    # -------------------------------------------------------------------------
    # Bottom panel: Chi-squared curve
    # -------------------------------------------------------------------------
    fig.add_trace(
        go.Scatter(
            x=z_grid,
            y=chi2_grid,
            mode="lines",
            line=dict(color="#64748b", width=1.5),
            name="χ²",
            showlegend=False,
            hovertemplate="z: %{x:.4f}<br>χ²: %{y:.2f}<extra></extra>",
        ),
        row=2, col=1
    )

    # Best-fit redshift marker
    fig.add_trace(
        go.Scatter(
            x=[redshift, redshift],
            y=[chi2_grid.min() * 0.8, chi2_grid.max()],
            mode="lines",
            line=dict(color="#ef4444", width=2, dash="dash"),
            name=f"z = {redshift:.4f}",
            showlegend=True,
        ),
        row=2, col=1
    )

    # -------------------------------------------------------------------------
    # Layout
    # -------------------------------------------------------------------------
    fig.update_layout(
        title=dict(text=title, x=0.5),
        width=width,
        height=height,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
        template="plotly_white",
        hovermode="closest",
    )

    # Axis labels
    fig.update_xaxes(title_text="Wavelength (μm)", row=1, col=1)
    fig.update_yaxes(title_text=_get_flux_label(flux_unit), row=1, col=1)
    fig.update_xaxes(title_text="Redshift", row=2, col=1)
    fig.update_yaxes(title_text="χ²", type="log", row=2, col=1)

    return fig


def plot_spectrum_simple(
    spectrum_data: Dict,
    redshift: float = 0.0,
    flux_unit: str = "fnu",
    show_errors: bool = True,
    show_emission_lines: bool = False,
    accent_color: str = DEFAULT_ACCENT_COLOR,
    title: Optional[str] = None,
    width: int = 800,
    height: int = 400,
) -> go.Figure:
    """
    Create a simple 1D spectrum plot without 2D heatmap.

    A lightweight alternative to plot_spectrum() for quick visualization.

    Parameters
    ----------
    spectrum_data : dict
        Spectrum data with keys: wave, fnu, fnu_err.
    redshift : float, optional
        Redshift for emission line overlay (default: 0.0).
    flux_unit : str, optional
        Flux unit: 'fnu' or 'flambda' (default: 'fnu').
    show_errors : bool, optional
        Show error band (default: True).
    show_emission_lines : bool, optional
        Show emission line markers (default: False).
    accent_color : str, optional
        Color for spectrum line (default: magenta).
    title : str, optional
        Plot title.
    width : int, optional
        Figure width in pixels (default: 800).
    height : int, optional
        Figure height in pixels (default: 400).

    Returns
    -------
    plotly.graph_objects.Figure
        Interactive Plotly figure.
    """
    # Extract data
    wave = np.array(spectrum_data["wave"])
    fnu = np.array(spectrum_data["fnu"], dtype=float)
    fnu_err = np.array(spectrum_data.get("fnu_err", []), dtype=float)

    # Handle null values
    fnu = np.where(np.isnan(fnu), np.nan, fnu)
    if len(fnu_err) > 0:
        fnu_err = np.where(np.isnan(fnu_err), np.nan, fnu_err)

    # Convert flux units if needed
    if flux_unit == "flambda":
        fnu = convert_flux_units(fnu, wave, "flambda")
        if len(fnu_err) > 0:
            fnu_err = convert_flux_units(fnu_err, wave, "flambda")

    # Create figure
    fig = go.Figure()

    valid = ~np.isnan(fnu)

    # Error band
    if show_errors and len(fnu_err) > 0:
        valid_err = valid & ~np.isnan(fnu_err)
        wave_valid = wave[valid_err]
        fnu_valid = fnu[valid_err]
        err_valid = fnu_err[valid_err]

        fig.add_trace(
            go.Scatter(
                x=np.concatenate([wave_valid, wave_valid[::-1]]),
                y=np.concatenate([fnu_valid + err_valid, (fnu_valid - err_valid)[::-1]]),
                fill="toself",
                fillcolor=_hex_to_rgba(accent_color, 0.15),
                line=dict(width=0),
                name="1σ error",
                hoverinfo="skip",
            )
        )

    # Spectrum line
    fig.add_trace(
        go.Scatter(
            x=wave[valid],
            y=fnu[valid],
            mode="lines",
            line=dict(color=accent_color, width=1.5),
            name="Spectrum",
            hovertemplate="λ: %{x:.4f} μm<br>Flux: %{y:.3g}<extra></extra>",
        )
    )

    # Emission lines
    if show_emission_lines and redshift > 0:
        wave_min, wave_max = wave.min(), wave.max()
        flux_min = np.nanmin(fnu[valid])
        flux_max = np.nanmax(fnu[valid])

        lines = get_emission_lines(redshift, wave_min, wave_max)

        for line in lines:
            fig.add_trace(
                go.Scatter(
                    x=[line["observed_wave"], line["observed_wave"]],
                    y=[flux_min * 0.9, flux_max * 1.1],
                    mode="lines",
                    line=dict(color=line["color"], width=1.5, dash="dash"),
                    name=line["name"],
                    legendgroup="emission_lines",
                )
            )

    # Layout
    fig.update_layout(
        title=dict(text=title, x=0.5) if title else None,
        width=width,
        height=height,
        xaxis_title="Wavelength (μm)",
        yaxis_title=_get_flux_label(flux_unit),
        template="plotly_white",
        hovermode="closest",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
    )

    return fig
