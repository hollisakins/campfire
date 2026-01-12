"""Tests for CAMPFIRE plotting utilities."""

import pytest
import numpy as np

# Skip all tests if plotly is not installed
plotly = pytest.importorskip("plotly")

from campfire.plotting import (
    convert_flux_units,
    get_emission_lines,
    _build_step_coords,
    plot_spectrum,
    plot_redshift_fit,
    plot_spectrum_simple,
    EMISSION_LINES,
)


class TestFluxConversion:
    """Test flux unit conversion functions."""

    def test_convert_to_flambda(self):
        """Convert f_nu to f_lambda correctly."""
        fnu = np.array([1.0])  # 1 μJy
        wavelength = np.array([1.0])  # 1 μm

        result = convert_flux_units(fnu, wavelength, "flambda")

        # f_λ = f_ν * 2.998e-19 / λ²
        expected = 1.0 * 2.998e-19 / 1.0
        assert np.isclose(result[0], expected)

    def test_convert_to_flambda_wavelength_dependence(self):
        """f_lambda scales inversely with λ²."""
        fnu = np.array([1.0, 1.0])
        wavelength = np.array([1.0, 2.0])

        result = convert_flux_units(fnu, wavelength, "flambda")

        # At 2 μm, flux should be 1/4 of flux at 1 μm
        assert np.isclose(result[1], result[0] / 4.0)

    def test_convert_to_fnu_passthrough(self):
        """Converting to fnu returns original values."""
        fnu = np.array([1.5, 2.5, 3.5])
        wavelength = np.array([1.0, 2.0, 3.0])

        result = convert_flux_units(fnu, wavelength, "fnu")

        np.testing.assert_array_equal(result, fnu)

    def test_convert_invalid_unit(self):
        """Invalid unit raises ValueError."""
        with pytest.raises(ValueError):
            convert_flux_units(np.array([1.0]), np.array([1.0]), "invalid")

    def test_convert_preserves_shape(self):
        """Conversion preserves array shape."""
        fnu = np.random.rand(100)
        wavelength = np.linspace(0.6, 5.3, 100)

        result = convert_flux_units(fnu, wavelength, "flambda")

        assert result.shape == fnu.shape


class TestEmissionLines:
    """Test emission line utilities."""

    def test_emission_lines_defined(self):
        """EMISSION_LINES contains expected lines."""
        names = [line["name"] for line in EMISSION_LINES]

        assert "Lyα" in names
        assert "Hα" in names
        assert "[OIII]₂" in names

    def test_emission_lines_have_required_keys(self):
        """Each emission line has required keys."""
        for line in EMISSION_LINES:
            assert "name" in line
            assert "wave" in line
            assert "color" in line
            assert line["wave"] > 0

    def test_get_emission_lines_redshift(self):
        """get_emission_lines applies redshift correctly."""
        redshift = 2.0

        lines = get_emission_lines(redshift)

        for line in lines:
            expected_obs = line["rest_wave"] * (1 + redshift)
            assert np.isclose(line["observed_wave"], expected_obs)

    def test_get_emission_lines_filter_by_wavelength(self):
        """get_emission_lines filters by wavelength range."""
        redshift = 2.0
        wave_min = 1.0
        wave_max = 3.0

        lines = get_emission_lines(redshift, wave_min, wave_max)

        for line in lines:
            assert wave_min <= line["observed_wave"] <= wave_max

    def test_get_emission_lines_zero_redshift(self):
        """At z=0, observed wavelength equals rest wavelength."""
        lines = get_emission_lines(0.0)

        for line in lines:
            assert np.isclose(line["observed_wave"], line["rest_wave"])


class TestStepCoordinates:
    """Test step function coordinate builder."""

    def test_build_step_coords_basic(self):
        """Build step coords produces expected shape."""
        x = np.array([0, 1, 2])
        y = np.array([1, 2, 3])

        x_step, y_step = _build_step_coords(x, y)

        # Each point becomes 2 points (left and right edges)
        assert len(x_step) == 2 * len(x)
        assert len(y_step) == 2 * len(y)

    def test_build_step_coords_y_values(self):
        """Step coords repeat y values for each step."""
        x = np.array([0, 1, 2])
        y = np.array([1, 2, 3])

        x_step, y_step = _build_step_coords(x, y)

        # Y values should appear in pairs
        assert y_step[0] == y_step[1] == 1
        assert y_step[2] == y_step[3] == 2
        assert y_step[4] == y_step[5] == 3

    def test_build_step_coords_empty(self):
        """Empty arrays produce empty result."""
        x_step, y_step = _build_step_coords(np.array([]), np.array([]))

        assert len(x_step) == 0
        assert len(y_step) == 0


class TestPlotSpectrum:
    """Test plot_spectrum function."""

    def test_plot_spectrum_returns_figure(self, sample_spectrum_data):
        """plot_spectrum returns a Plotly Figure."""
        fig = plot_spectrum(sample_spectrum_data)

        assert isinstance(fig, plotly.graph_objects.Figure)

    def test_plot_spectrum_has_traces(self, sample_spectrum_data):
        """plot_spectrum creates expected traces."""
        fig = plot_spectrum(sample_spectrum_data)

        # Should have at least heatmap and spectrum traces
        assert len(fig.data) >= 2

    def test_plot_spectrum_with_errors(self, sample_spectrum_data):
        """plot_spectrum shows error band when enabled."""
        fig = plot_spectrum(sample_spectrum_data, show_errors=True)

        # Should have more traces with errors
        trace_types = [trace.name for trace in fig.data if hasattr(trace, "name")]
        assert any("error" in str(name).lower() for name in trace_types if name)

    def test_plot_spectrum_emission_lines(self, sample_spectrum_data):
        """plot_spectrum shows emission lines when enabled."""
        fig = plot_spectrum(
            sample_spectrum_data,
            redshift=2.0,
            show_emission_lines=True
        )

        # Should have more traces with emission lines
        assert len(fig.data) > 3

    def test_plot_spectrum_flux_units(self, sample_spectrum_data):
        """plot_spectrum respects flux_unit parameter."""
        fig_fnu = plot_spectrum(sample_spectrum_data, flux_unit="fnu")
        fig_flambda = plot_spectrum(sample_spectrum_data, flux_unit="flambda")

        # Y-axis labels should differ (yaxis3 is the main spectrum panel)
        assert fig_fnu.layout.yaxis3.title.text != fig_flambda.layout.yaxis3.title.text
        assert "fν" in fig_fnu.layout.yaxis3.title.text
        assert "fλ" in fig_flambda.layout.yaxis3.title.text

    def test_plot_spectrum_custom_size(self, sample_spectrum_data):
        """plot_spectrum respects size parameters."""
        fig = plot_spectrum(sample_spectrum_data, width=1200, height=800)

        assert fig.layout.width == 1200
        assert fig.layout.height == 800


class TestPlotRedshiftFit:
    """Test plot_redshift_fit function."""

    def test_plot_redshift_fit_returns_figure(self, sample_redshift_fit_data):
        """plot_redshift_fit returns a Plotly Figure."""
        fig = plot_redshift_fit(sample_redshift_fit_data)

        assert isinstance(fig, plotly.graph_objects.Figure)

    def test_plot_redshift_fit_has_chi2_curve(self, sample_redshift_fit_data):
        """plot_redshift_fit shows chi-squared curve."""
        fig = plot_redshift_fit(sample_redshift_fit_data)

        # Should have chi2 trace and best-fit marker
        assert len(fig.data) >= 2

    def test_plot_redshift_fit_with_spectrum(
        self, sample_redshift_fit_data, sample_spectrum_data
    ):
        """plot_redshift_fit shows spectrum when provided."""
        fig = plot_redshift_fit(
            sample_redshift_fit_data,
            spectrum_data=sample_spectrum_data
        )

        # More traces with spectrum data
        assert len(fig.data) >= 4

    def test_plot_redshift_fit_auto_title(self, sample_redshift_fit_data):
        """plot_redshift_fit generates title from fit data."""
        fig = plot_redshift_fit(sample_redshift_fit_data)

        assert fig.layout.title is not None
        assert "z = " in fig.layout.title.text
        assert "χ²" in fig.layout.title.text


class TestPlotSpectrumSimple:
    """Test plot_spectrum_simple function."""

    def test_plot_spectrum_simple_returns_figure(self, sample_spectrum_data):
        """plot_spectrum_simple returns a Plotly Figure."""
        fig = plot_spectrum_simple(sample_spectrum_data)

        assert isinstance(fig, plotly.graph_objects.Figure)

    def test_plot_spectrum_simple_fewer_traces(self, sample_spectrum_data):
        """plot_spectrum_simple has fewer traces than full version."""
        fig_simple = plot_spectrum_simple(sample_spectrum_data)
        fig_full = plot_spectrum(sample_spectrum_data)

        # Simple version shouldn't have 2D heatmap
        assert len(fig_simple.data) < len(fig_full.data)

    def test_plot_spectrum_simple_with_emission_lines(self, sample_spectrum_data):
        """plot_spectrum_simple shows emission lines when enabled."""
        fig = plot_spectrum_simple(
            sample_spectrum_data,
            redshift=2.0,
            show_emission_lines=True
        )

        # Should have more traces with emission lines
        assert len(fig.data) > 2


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_plot_spectrum_with_nan_values(self):
        """plot_spectrum handles NaN values gracefully."""
        data = {
            "wave": [0.6, 0.8, 1.0, 1.2],
            "fnu": [1.0, float("nan"), 1.5, 2.0],
            "fnu_err": [0.1, 0.1, float("nan"), 0.1],
            "snr_2d": [],
            "profile": [],
            "profile_fit": [],
            "profile_pix": [],
        }

        # Should not raise
        fig = plot_spectrum(data)
        assert isinstance(fig, plotly.graph_objects.Figure)

    def test_plot_spectrum_without_2d_data(self):
        """plot_spectrum works without 2D heatmap data."""
        data = {
            "wave": [0.6, 0.8, 1.0, 1.2],
            "fnu": [1.0, 1.2, 1.5, 2.0],
            "fnu_err": [0.1, 0.1, 0.1, 0.1],
            "snr_2d": [],
            "profile": [],
            "profile_fit": [],
            "profile_pix": [],
        }

        fig = plot_spectrum(data)
        assert isinstance(fig, plotly.graph_objects.Figure)

    def test_plot_spectrum_empty_data(self):
        """plot_spectrum handles minimal data."""
        data = {
            "wave": [1.0],
            "fnu": [1.0],
            "fnu_err": [],
            "snr_2d": [],
            "profile": [],
            "profile_fit": [],
            "profile_pix": [],
        }

        fig = plot_spectrum(data)
        assert isinstance(fig, plotly.graph_objects.Figure)
