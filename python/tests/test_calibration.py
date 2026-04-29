"""Tests for calibration + stacking and new model dataclasses."""

import numpy as np
import pytest

from campfire.calibration import stack_spectra
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
                    "signal_to_noise": 7.0,
                    "exposure_time": 1500.0,
                },
                {
                    "spectrum_id": "ember_cosmos_p1_g395m_f290lp_100",
                    "object_id": "CAMPFIRE-J0001+0001",
                    "grating": "G395M",
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
