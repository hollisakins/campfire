"""Tests for local-first routing in Campfire client and SpectrumData model."""

import io
import numpy as np
import pytest
from unittest.mock import MagicMock, Mock, patch

from astropy.io import fits
from astropy.table import Table as FitsTable

from campfire.client import Campfire
from campfire.db.store import LocalStore
from campfire.models import SpectrumData


@pytest.fixture
def local_store(tmp_path):
    """Create a populated LocalStore in a temp directory."""
    (tmp_path / "meta").mkdir()
    (tmp_path / "products").mkdir()
    db_path = tmp_path / "meta" / "campfire.db"
    store = LocalStore(db_path)

    store.upsert_objects([
        {
            "id": 1,
            "object_id": "TEST-OBJ-100",
            "field": "cosmos",
            "ra": 150.0,
            "dec": 2.0,
            "redshift": 2.5,
            "redshift_auto": 2.5,
            "redshift_inspected": 2.5,
            "redshift_quality": 3,
            "n_targets": 1,
            "n_spectra": 1,
            "programs": ["test-prog"],
            "gratings": ["PRISM"],
            "observations": ["test_obs"],
            "member_target_ids": ["test_obj_100"],
            "max_snr": 15.0,
            "has_photometry": False,
            "is_active": True,
        },
        {
            "id": 2,
            "object_id": "TEST-OBJ-200",
            "field": "cosmos",
            "ra": 150.1,
            "dec": 2.1,
            "redshift": 0.5,
            "redshift_auto": 0.5,
            "redshift_inspected": None,
            "redshift_quality": 0,
            "n_targets": 1,
            "n_spectra": 1,
            "programs": ["test-prog"],
            "gratings": ["PRISM"],
            "observations": ["test_obs"],
            "member_target_ids": ["test_obj_200"],
            "max_snr": 5.0,
            "has_photometry": False,
            "is_active": True,
        },
    ])

    store.upsert_spectra([
        {
            "id": 10,
            "spectrum_id": "test_obs_prism_100",
            "target_id": "test_obj_100",
            "object_id": "TEST-OBJ-100",
            "grating": "PRISM",
            "fits_path": "test_obs/test_obs_prism_100_spec.fits",
            "file_hash": "sha256:abc",
            "file_size": 1024,
            "signal_to_noise": 15.0,
            "program_slug": "test-prog",
            "observation": "test_obs",
            "field": "cosmos",
        },
        {
            "id": 20,
            "spectrum_id": "test_obs_prism_200",
            "target_id": "test_obj_200",
            "object_id": "TEST-OBJ-200",
            "grating": "PRISM",
            "fits_path": "test_obs/test_obs_prism_200_spec.fits",
            "file_hash": "sha256:def",
            "file_size": 1024,
            "signal_to_noise": 5.0,
            "program_slug": "test-prog",
            "observation": "test_obs",
            "field": "cosmos",
        },
    ])

    yield store, tmp_path
    store.close()


@pytest.fixture
def local_client(local_store):
    store, tmp_path = local_store

    with patch("campfire.client.APISession") as MockSession:
        mock_session = MockSession.return_value
        mock_session.base_url = "https://test.com/api/v1"
        mock_session._ensure_valid_token = Mock()
        mock_session.get = Mock()

        client = Campfire(data_dir=tmp_path)
        assert client._local is not None
        yield client, mock_session, store, tmp_path


class TestLocalFirstQueryObjects:
    def test_uses_local_store(self, local_client):
        client, mock_session, _, _ = local_client
        results = client.query_objects(observations=["test_obs"])
        mock_session.get.assert_not_called()
        assert len(results) == 2

    def test_local_query_with_filters(self, local_client):
        client, mock_session, _, _ = local_client
        results = client.query_objects(
            observations=["test_obs"],
            redshift_range=(2.0, 3.0),
        )
        mock_session.get.assert_not_called()
        assert len(results) == 1
        assert results[0]["object_id"] == "TEST-OBJ-100"

    def test_queries_local_for_unknown_obs(self, local_client):
        client, mock_session, _, _ = local_client
        results = client.query_objects(observations=["unknown_obs"])
        mock_session.get.assert_not_called()
        assert len(results) == 0

    def test_remote_flag_forces_api(self, local_client):
        client, mock_session, _, _ = local_client
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [], "pagination": {"total": 0}}
        mock_session.get.return_value = mock_response
        client.query_objects(observations=["test_obs"], remote=True)
        mock_session.get.assert_called()

    def test_is_local_property(self, local_client):
        client, _, _, _ = local_client
        assert client.is_local is True

    def test_no_local_store(self):
        with patch("campfire.client.APISession") as MockSession:
            MockSession.return_value.base_url = "https://test.com/api/v1"
            client = Campfire(data_dir="/nonexistent/path")
            assert client.is_local is False


class TestLocalFirstQuerySpectra:
    def test_uses_local_store(self, local_client):
        client, mock_session, _, _ = local_client
        results = client.query_spectra(observations=["test_obs"])
        mock_session.get.assert_not_called()
        assert len(results) == 2

    def test_grating_filter(self, local_client):
        client, _, _, _ = local_client
        results = client.query_spectra(gratings=["PRISM"])
        assert len(results) == 2


class TestOpenSpectrum:
    def _make_fits_bytes(self, n=30):
        wave = np.linspace(0.6, 5.3, n)
        flux = np.ones(n)
        err = np.ones(n) * 0.1
        t = FitsTable([wave, flux, err], names=["WAVELENGTH", "FLUX", "FLUX_ERR"])
        hdul = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU(t)])
        buf = io.BytesIO()
        hdul.writeto(buf)
        return buf.getvalue()

    def test_returns_local_file(self, local_client):
        client, mock_session, store, tmp_path = local_client

        obs_dir = tmp_path / "products" / "test_obs"
        obs_dir.mkdir(parents=True, exist_ok=True)
        fits_file = obs_dir / "test_obs_prism_100_spec.fits"
        fits_file.write_bytes(self._make_fits_bytes(50))

        store.mark_synced(
            spectrum_id="test_obs_prism_100",
            local_path="test_obs/test_obs_prism_100_spec.fits",
            file_hash="sha256:abc",
            file_size=fits_file.stat().st_size,
        )

        spec = client.open_spectrum("test_obs_prism_100")
        mock_session.get.assert_not_called()
        assert isinstance(spec, SpectrumData)
        assert spec.spectrum_id == "test_obs_prism_100"
        assert spec.grating == "PRISM"

    def test_downloads_and_caches(self, local_client):
        client, mock_session, store, tmp_path = local_client
        fits_bytes = self._make_fits_bytes(30)

        client._api.get_signed_url = Mock(return_value="https://r2.example.com/signed")

        mock_response = MagicMock()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_response.raise_for_status = Mock()
        mock_response.iter_content = Mock(return_value=[fits_bytes])

        with patch("campfire.client.requests.get", return_value=mock_response):
            spec = client.open_spectrum("test_obs_prism_100")

        assert isinstance(spec, SpectrumData)
        assert spec.wavelength.shape == (30,)

        cached = tmp_path / "products" / "test_obs" / "test_obs_prism_100_spec.fits"
        assert cached.exists()
        assert store.find_local_path("test_obs_prism_100") is not None


class TestSpectrumData:
    def test_basic_creation(self):
        wave = np.linspace(0.6, 5.3, 100)
        flux = np.random.normal(1.0, 0.1, 100)
        flux_err = np.abs(np.random.normal(0.05, 0.01, 100))

        spec = SpectrumData(
            wavelength=wave,
            flux=flux,
            flux_err=flux_err,
            header={"OBJECT": "test"},
            grating="PRISM",
            spectrum_id="test_prism_123",
        )
        assert spec.wavelength.shape == (100,)
        assert spec.grating == "PRISM"
        assert spec.spectrum_id == "test_prism_123"

    def test_repr(self):
        wave = np.linspace(0.6, 5.3, 100)
        spec = SpectrumData(
            wavelength=wave,
            flux=np.ones(100),
            flux_err=np.zeros(100),
            header={},
            grating="G395M",
            spectrum_id="obj_g395m_456",
        )
        r = repr(spec)
        assert "obj_g395m_456" in r
        assert "G395M" in r

    def test_from_fits_table_hdu(self, tmp_path):
        t = FitsTable(
            [np.linspace(0.6, 5.3, 50), np.ones(50), np.zeros(50)],
            names=["WAVELENGTH", "FLUX", "FLUX_ERR"],
        )
        hdu_primary = fits.PrimaryHDU()
        hdu_primary.header["GRATING"] = "PRISM"
        hdu_primary.header["FILENAME"] = "ember_uds_p4_prism_clear_100_spec.fits"
        hdul = fits.HDUList([hdu_primary, fits.BinTableHDU(t)])

        fits_path = tmp_path / "ember_uds_p4_prism_clear_100_spec.fits"
        hdul.writeto(str(fits_path))

        spec = SpectrumData.from_fits(str(fits_path))
        assert spec.wavelength.shape == (50,)
        assert spec.grating == "PRISM"
        assert spec.spectrum_id == "ember_uds_p4_prism_clear_100"

    def test_from_fits_explicit_params(self, tmp_path):
        t = FitsTable(
            [np.linspace(0.6, 5.3, 10), np.ones(10), np.zeros(10)],
            names=["WAVELENGTH", "FLUX", "FLUX_ERR"],
        )
        hdul = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU(t)])
        fits_path = tmp_path / "test.fits"
        hdul.writeto(str(fits_path))
        spec = SpectrumData.from_fits(
            str(fits_path), spectrum_id="custom_id", grating="G395M",
        )
        assert spec.spectrum_id == "custom_id"
        assert spec.grating == "G395M"
