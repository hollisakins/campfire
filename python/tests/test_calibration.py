"""Tests for calibration + stacking and new model dataclasses."""

import warnings

import numpy as np
import pytest

from campfire.calibration import (
    _fit_chebyshev,
    _fit_flat,
    calibrate_and_stack,
    stack_spectra,
)
from campfire.models import (
    Band,
    Object,
    Photometry,
    Spectrum,
    SpectrumCollection,
    SpectrumData,
)


# ---------------------------------------------------------------------------
# SpectrumData — auto-flam and plot
# ---------------------------------------------------------------------------

class TestSpectrumDataAutoFlam:
    def test_flam_auto_computed(self):
        wave = np.linspace(1.0, 5.0, 100)
        fnu = np.ones(100) * 2.0
        spec = SpectrumData(
            wavelength=wave,
            fnu=fnu,
            fnu_err=np.ones(100) * 0.1,
            header={},
            grating="PRISM",
            spectrum_id="t_prism_1",
        )
        assert spec.flam is not None
        # flam = fnu * 2.998e-19 / wave^2
        expected = fnu * 2.998e-19 / wave**2
        np.testing.assert_allclose(spec.flam, expected)

    def test_flam_preserved_when_given(self):
        wave = np.linspace(1.0, 5.0, 50)
        supplied_flam = np.full(50, 7.0)
        spec = SpectrumData(
            wavelength=wave,
            fnu=np.ones(50),
            fnu_err=np.ones(50) * 0.1,
            header={},
            grating="PRISM",
            spectrum_id="t_prism_1",
            flam=supplied_flam,
            flam_err=np.ones(50) * 0.5,
        )
        np.testing.assert_array_equal(spec.flam, supplied_flam)


# ---------------------------------------------------------------------------
# stack_spectra — id format and weighted-mean behaviour
# ---------------------------------------------------------------------------

class TestStackSpectra:
    def _make_spec(self, spectrum_id, fnu_value=1.0):
        wave = np.linspace(1.0, 5.0, 20)
        return SpectrumData(
            wavelength=wave,
            fnu=np.full(20, fnu_value),
            fnu_err=np.full(20, 0.1),
            header={},
            grating="PRISM",
            spectrum_id=spectrum_id,
        )

    def test_stacked_id_with_object_id(self):
        a = self._make_spec("a_prism_1", 2.0)
        b = self._make_spec("a_prism_2", 3.0)
        stacked = stack_spectra([a, b], object_id="obj_xyz")
        assert stacked.spectrum_id == "stack:obj_xyz:PRISM"
        # weighted-mean of two equal-err inputs is the arithmetic mean
        assert np.isclose(stacked.fnu[5], 2.5)

    def test_stacked_id_fallback(self):
        a = self._make_spec("a_prism_1", 2.0)
        b = self._make_spec("a_prism_2", 2.0)
        stacked = stack_spectra([a, b])
        assert stacked.spectrum_id == "stack:PRISM:2spectra"

    def test_stack_requires_two(self):
        a = self._make_spec("a_prism_1")
        with pytest.raises(ValueError, match="at least 2"):
            stack_spectra([a])


# ---------------------------------------------------------------------------
# Photometry
# ---------------------------------------------------------------------------

class TestPhotometry:
    def test_from_record(self):
        rec = {
            "catalog_name": "cosmos2020",
            "catalog_id": "12345",
            "match_distance_arcsec": 0.3,
            "photo_z": 2.1,
            "photo_z_err_lo": 1.9,
            "photo_z_err_hi": 2.3,
            "photometry": {
                "flux_unit": "uJy",
                "bands": {
                    "f444w": {"flux": 0.42, "flux_err": 0.03, "wav": 4.44},
                    "f150w": {"flux": 0.10, "flux_err": 0.01, "wav": 1.50},
                    "f277w": {"flux": 0.25, "flux_err": 0.02, "wav": 2.77},
                },
            },
        }
        phot = Photometry.from_record(rec)
        # Bands are sorted by wavelength
        assert phot.bands == ["f150w", "f277w", "f444w"]
        np.testing.assert_allclose(phot.wavelength, [1.50, 2.77, 4.44])
        np.testing.assert_allclose(phot.flux, [0.10, 0.25, 0.42])
        assert phot.photo_z == 2.1

        # Band access by name
        b = phot["f444w"]
        assert isinstance(b, Band)
        assert b.flux == 0.42
        assert b.wavelength == 4.44

        with pytest.raises(KeyError):
            _ = phot["no_such_band"]


# ---------------------------------------------------------------------------
# SpectrumCollection — boolean indexing
# ---------------------------------------------------------------------------

class TestSpectrumCollection:
    def test_boolean_index_by_grating(self):
        spectra = [
            Spectrum(spectrum_id="s1", object_id="o", grating="PRISM", signal_to_noise=5),
            Spectrum(spectrum_id="s2", object_id="o", grating="G395M", signal_to_noise=8),
            Spectrum(spectrum_id="s3", object_id="o", grating="PRISM", signal_to_noise=12),
        ]
        col = SpectrumCollection(spectra)
        prism = col[col.grating == "PRISM"]
        assert isinstance(prism, SpectrumCollection)
        assert len(prism) == 2
        assert prism.gratings == ["PRISM"]

        high = col[col.signal_to_noise > 6]
        assert len(high) == 2

    def test_boolean_index_by_program_and_observation(self):
        spectra = [
            Spectrum(
                spectrum_id="s1", object_id="o", grating="PRISM",
                program="ember", observation="ember_cosmos_p1", field="cosmos",
            ),
            Spectrum(
                spectrum_id="s2", object_id="o", grating="G395M",
                program="rubies", observation="rubies_uds_p2", field="uds",
            ),
            Spectrum(
                spectrum_id="s3", object_id="o", grating="G395M",
                program="ember", observation="ember_cosmos_p1", field="cosmos",
            ),
        ]
        col = SpectrumCollection(spectra)

        ember = col[col.program == "ember"]
        assert len(ember) == 2
        assert set(ember.spectrum_id) == {"s1", "s3"}

        uds = col[col.field == "uds"]
        assert len(uds) == 1
        assert uds[0].spectrum_id == "s2"

        assert col.programs == ["ember", "rubies"]
        assert col.observations == ["ember_cosmos_p1", "rubies_uds_p2"]
        assert col.fields == ["cosmos", "uds"]


# ---------------------------------------------------------------------------
# Object.from_dict
# ---------------------------------------------------------------------------

class TestObjectFromDict:
    def test_builds_with_spectra_and_tags(self):
        d = {
            "object_id": "CAMPFIRE-J0001+0001",
            "ra": 1.23,
            "dec": 4.56,
            "field": "cosmos",
            "redshift": 3.2,
            "redshift_quality": 3,
            "programs": ["ember"],
            "tags": ["lrd", "blagn"],
            "has_photometry": True,
            "n_spectra": 2,
            "spectra": [
                {
                    "spectrum_id": "ember_cosmos_p1_prism_clear_100",
                    "object_id": "CAMPFIRE-J0001+0001",
                    "grating": "PRISM",
                    "program_slug": "ember",
                    "observation": "ember_cosmos_p1",
                    "field": "cosmos",
                    "target_id": "t1",
                    "signal_to_noise": 7.0,
                    "exposure_time": 1500.0,
                },
                {
                    "spectrum_id": "ember_cosmos_p1_g395m_f290lp_100",
                    "object_id": "CAMPFIRE-J0001+0001",
                    "grating": "G395M",
                    "program_slug": "ember",
                    "observation": "ember_cosmos_p1",
                    "field": "cosmos",
                    "target_id": "t1",
                    "signal_to_noise": 4.0,
                    "exposure_time": 3000.0,
                },
            ],
        }
        obj = Object.from_dict(d)
        assert obj.object_id == "CAMPFIRE-J0001+0001"
        assert obj.tags == ["lrd", "blagn"]
        assert obj.has_photometry is True
        assert len(obj.spectra) == 2
        assert obj.spectra.gratings == ["G395M", "PRISM"]
        assert obj.photometry is None
        # program_slug from store rows should be exposed as `.program`
        assert all(s.program == "ember" for s in obj.spectra)
        assert obj.spectra[0].observation == "ember_cosmos_p1"
        assert obj.spectra[0].target_id == "t1"
        assert len(obj.spectra[obj.spectra.program == "ember"]) == 2

    def test_opener_wired(self):
        called = {}

        def fake_opener(spectrum_id):
            called["id"] = spectrum_id
            return "opened"

        d = {
            "object_id": "o",
            "ra": 0.0,
            "dec": 0.0,
            "spectra": [
                {"spectrum_id": "sid_1", "object_id": "o", "grating": "PRISM"}
            ],
        }
        obj = Object.from_dict(d, opener=fake_opener)
        result = obj.spectra[0].open()
        assert result == "opened"
        assert called["id"] == "sid_1"


# ---------------------------------------------------------------------------
# SpectrumData.valid — the canonical pixel mask (issue #197, finding 4)
# ---------------------------------------------------------------------------

class TestSpectrumDataValid:
    def _make(self, fnu, fnu_err):
        n = len(fnu)
        return SpectrumData(
            wavelength=np.linspace(1.0, 5.0, n),
            fnu=np.asarray(fnu, dtype=float),
            fnu_err=np.asarray(fnu_err, dtype=float),
            header={},
            grating="PRISM",
            spectrum_id="t_prism_1",
        )

    def test_valid_excludes_nonfinite_flux_and_nonpositive_error(self):
        spec = self._make(
            fnu=[1.0, np.nan, 2.0, 3.0, 4.0, 5.0],
            fnu_err=[0.1, 0.1, 0.0, -0.5, np.nan, np.inf],
        )
        # idx 0 good; 1 (nan flux), 2 (zero err), 3 (neg err),
        # 4 (nan err), 5 (inf err) all invalid.
        np.testing.assert_array_equal(
            spec.valid, [True, False, False, False, False, False]
        )

    def test_valid_matches_recipe_idiom(self):
        rng = np.random.default_rng(0)
        fnu = rng.normal(1.0, 0.2, 50)
        fnu_err = np.abs(rng.normal(0.1, 0.02, 50))
        fnu[5] = np.nan
        fnu_err[10] = 0.0
        spec = self._make(fnu, fnu_err)
        expected = np.isfinite(fnu) & (fnu_err > 0)
        np.testing.assert_array_equal(spec.valid, expected)

    def test_plot_uses_valid_mask(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        spec = self._make(
            fnu=[1.0, 2.0, 3.0, 4.0],
            fnu_err=[0.1, 0.0, 0.1, 0.1],  # idx 1 has zero error -> invalid
        )
        _, ax = plt.subplots()
        spec.plot(ax=ax)
        # bare isfinite would plot 4 points; valid drops the zero-error pixel.
        line = ax.lines[0]
        assert len(line.get_xdata()) == 3
        plt.close("all")


# ---------------------------------------------------------------------------
# from_fits — missing error column warns + fills NaN (not zeros) (finding 4)
# ---------------------------------------------------------------------------

class TestFromFitsMissingError:
    def test_missing_error_column_fills_nan_and_warns(self, tmp_path):
        from astropy.io import fits

        wave = np.linspace(1.0, 5.0, 10)
        fnu = np.ones(10)
        hdu0 = fits.PrimaryHDU()
        hdu0.header["GRATING"] = "PRISM"
        cols = fits.ColDefs([
            fits.Column(name="wave", format="D", array=wave),
            fits.Column(name="fnu", format="D", array=fnu),
        ])
        hdu1 = fits.BinTableHDU.from_columns(cols)
        path = tmp_path / "nocols_spec.fits"
        fits.HDUList([hdu0, hdu1]).writeto(path)

        with pytest.warns(UserWarning, match="No error column"):
            spec = SpectrumData.from_fits(str(path))

        # Filled with NaN (not zeros), so every pixel is masked-out as invalid.
        assert np.all(np.isnan(spec.fnu_err))
        assert not spec.valid.any()


# ---------------------------------------------------------------------------
# to_specutils — specutils export carries mask + uncertainty (finding 4)
# ---------------------------------------------------------------------------

class TestToSpecutils:
    def test_export_carries_mask_and_uncertainty(self):
        pytest.importorskip("specutils")
        import astropy.units as u

        fnu = np.array([1.0, 2.0, np.nan, 4.0])
        fnu_err = np.array([0.1, 0.0, 0.1, 0.2])  # idx 1 zero err, idx 2 nan flux
        spec = SpectrumData(
            wavelength=np.linspace(1.0, 4.0, 4),
            fnu=fnu,
            fnu_err=fnu_err,
            header={},
            grating="PRISM",
            spectrum_id="t_prism_1",
        )
        s1d = spec.to_specutils()

        # mask True == masked (invalid); matches ~spec.valid
        np.testing.assert_array_equal(s1d.mask, ~spec.valid)
        assert s1d.uncertainty is not None
        np.testing.assert_allclose(s1d.flux.to_value(u.uJy), fnu, equal_nan=True)
        np.testing.assert_allclose(
            s1d.spectral_axis.to_value(u.um), spec.wavelength
        )


# ---------------------------------------------------------------------------
# Grating guard for stacking (issue #197, finding 7)
# ---------------------------------------------------------------------------

class TestGratingGuard:
    def _make_spec(self, grating, n=20, fnu_value=1.0):
        wave = np.linspace(1.0, 5.0, n)
        return SpectrumData(
            wavelength=wave,
            fnu=np.full(n, fnu_value),
            fnu_err=np.full(n, 0.1),
            header={},
            grating=grating,
            spectrum_id=f"a_{grating.lower()}_1",
        )

    def test_stack_mixed_gratings_raises_without_grid(self):
        a = self._make_spec("PRISM")
        b = self._make_spec("G395H")
        with pytest.raises(ValueError, match="multiple gratings"):
            stack_spectra([a, b])

    def test_stack_same_grating_ok(self):
        a = self._make_spec("PRISM", fnu_value=2.0)
        b = self._make_spec("PRISM", fnu_value=4.0)
        stacked = stack_spectra([a, b])
        assert stacked.grating == "PRISM"

    def test_stack_mixed_gratings_warns_with_explicit_grid(self):
        a = self._make_spec("PRISM")
        b = self._make_spec("G395H")
        grid = np.linspace(1.0, 5.0, 20)
        with pytest.warns(UserWarning, match="multiple gratings"):
            stacked = stack_spectra([a, b], wavelength_grid=grid)
        assert stacked is not None

    def test_calibrate_and_stack_mixed_gratings_raises_early(self):
        # SpectrumData inputs => no calibration; should still fail fast.
        a = self._make_spec("PRISM")
        b = self._make_spec("G140M")
        with pytest.raises(ValueError, match="multiple gratings"):
            calibrate_and_stack([a, b])


# ---------------------------------------------------------------------------
# Zero-guard in calibration weight computation (issue #197, finding 4)
# ---------------------------------------------------------------------------

class TestFitWeightZeroGuard:
    def test_fit_flat_handles_zero_ratio_err(self):
        # obs_err and synth_err both zero -> ratio_err == 0 -> would div-by-zero
        wave = np.linspace(1.0, 5.0, 20)
        obs_flux = np.array([2.0, 4.0])
        synth_flux = np.array([1.0, 2.0])
        zero_err = np.zeros(2)
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any RuntimeWarning becomes a failure
            mult = _fit_flat(wave, obs_flux, zero_err, synth_flux, zero_err)
        assert np.all(np.isfinite(mult))
        # ratio is 2.0 for both bands -> unweighted mean fallback == 2.0
        np.testing.assert_allclose(mult, 2.0)

    def test_fit_chebyshev_handles_zero_ratio_err(self):
        wave = np.linspace(1.0, 5.0, 20)
        band_waves = np.array([1.5, 3.0, 4.5])
        obs_flux = np.array([2.0, 2.0, 2.0])
        synth_flux = np.array([1.0, 1.0, 1.0])
        zero_err = np.zeros(3)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            mult = _fit_chebyshev(
                wave, band_waves, obs_flux, zero_err, synth_flux, zero_err, degree=1
            )
        assert np.all(np.isfinite(mult))
        # constant ratio 2.0 everywhere -> multiplier ~2.0
        np.testing.assert_allclose(mult, 2.0, rtol=1e-6)
