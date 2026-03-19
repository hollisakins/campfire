"""Tests for local-first routing in Campfire client and SpectrumData model."""

import pytest
import numpy as np
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

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

    objects = [
        {
            "id": 1,
            "object_id": "test_obj_100",
            "program_slug": "test-prog",
            "program_name": "TEST",
            "field": "COSMOS",
            "observation": "test_obs",
            "ra": 150.0,
            "dec": 2.0,
            "redshift": 2.5,
            "redshift_auto": 2.5,
            "redshift_inspected": 2.5,
            "redshift_quality": 3,
            "spectral_features": 0,
            "object_flags": 1,
            "dq_flags": 0,
            "max_snr": 15.0,
            "spectra": [
                {
                    "id": 10,
                    "object_id": "test_obj_100",
                    "grating": "PRISM",
                    "fits_path": "spectra/test_obs/test_obj_100_PRISM_spec.fits",
                    "signal_to_noise": 15.0,
                },
            ],
        },
        {
            "id": 2,
            "object_id": "test_obj_200",
            "program_slug": "test-prog",
            "program_name": "TEST",
            "field": "COSMOS",
            "observation": "test_obs",
            "ra": 150.1,
            "dec": 2.1,
            "redshift": 0.5,
            "redshift_auto": 0.5,
            "redshift_inspected": None,
            "redshift_quality": 0,
            "spectral_features": 0,
            "object_flags": 0,
            "dq_flags": 0,
            "max_snr": 5.0,
            "spectra": [
                {
                    "id": 20,
                    "object_id": "test_obj_200",
                    "grating": "PRISM",
                    "fits_path": "spectra/test_obs/test_obj_200_PRISM_spec.fits",
                    "signal_to_noise": 5.0,
                },
            ],
        },
    ]
    store.upsert_objects(objects)
    yield store, tmp_path
    store.close()


@pytest.fixture
def local_client(local_store):
    """Create a Campfire client backed by a local store."""
    store, tmp_path = local_store

    with patch("campfire.client.APISession") as MockSession:
        mock_session = MockSession.return_value
        mock_session.base_url = "https://test.com/api/v1"
        mock_session._ensure_valid_token = Mock()
        mock_session.get = Mock()

        client = Campfire(data_dir=tmp_path)

        # Verify it detected local data
        assert client._local is not None
        yield client, mock_session, store, tmp_path


class TestLocalFirstQueryObjects:
    """Test that query_objects routes to local store when data is available."""

    def test_uses_local_store(self, local_client):
        """query_objects uses SQLite when observations are synced."""
        client, mock_session, store, _ = local_client

        results = client.query_objects(observations=["test_obs"])

        # Should NOT have hit the API
        mock_session.get.assert_not_called()
        assert len(results) == 2

    def test_local_query_with_filters(self, local_client):
        """Local queries support the same filters as remote."""
        client, mock_session, _, _ = local_client

        results = client.query_objects(
            observations=["test_obs"],
            redshift_range=(2.0, 3.0),
        )

        mock_session.get.assert_not_called()
        assert len(results) == 1
        assert results[0]["object_id"] == "test_obj_100"

    def test_queries_local_for_unknown_obs(self, local_client):
        """query_objects queries local store even for unknown observations.

        After full catalog sync, local store has the complete catalog.
        Unknown observations simply return no results from SQLite.
        """
        client, mock_session, _, _ = local_client

        results = client.query_objects(observations=["unknown_obs"])

        # Should NOT have hit the API — local store handles it
        mock_session.get.assert_not_called()
        assert len(results) == 0

    def test_remote_flag_forces_api(self, local_client):
        """remote=True bypasses local store."""
        client, mock_session, _, _ = local_client

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [], "pagination": {"total": 0}}
        mock_session.get.return_value = mock_response

        results = client.query_objects(observations=["test_obs"], remote=True)

        mock_session.get.assert_called()

    def test_is_local_property(self, local_client):
        """is_local returns True when local store is available."""
        client, _, _, _ = local_client
        assert client.is_local is True

    def test_no_local_store(self):
        """Client works without local store."""
        with patch("campfire.client.APISession") as MockSession:
            mock_session = MockSession.return_value
            mock_session.base_url = "https://test.com/api/v1"

            client = Campfire(data_dir="/nonexistent/path")
            assert client.is_local is False


class TestOpenSpectrum:
    """Test that open_spectrum checks local files and caches downloads."""

    def test_returns_local_file(self, local_client):
        """open_spectrum returns local SpectrumData when file exists."""
        from astropy.io import fits
        from astropy.table import Table as FitsTable

        client, mock_session, store, tmp_path = local_client

        # Create a real FITS file locally (under products/)
        obs_dir = tmp_path / "products" / "test_obs"
        obs_dir.mkdir(parents=True, exist_ok=True)
        fits_file = obs_dir / "test_obj_100_PRISM_spec.fits"

        wave = np.linspace(0.6, 5.3, 50)
        flux = np.ones(50)
        err = np.ones(50) * 0.1
        t = FitsTable([wave, flux, err], names=["WAVELENGTH", "FLUX", "FLUX_ERR"])
        hdul = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU(t)])
        hdul.writeto(str(fits_file))

        # Mark it as synced in the store
        store.mark_synced(
            10, "test_obj_100", "test_obs", "PRISM",
            "spectra/test_obs/test_obj_100_PRISM_spec.fits",
            "test_obs/test_obj_100_PRISM_spec.fits",
            "sha256:abc", fits_file.stat().st_size,
        )

        spec = client.open_spectrum("test_obj_100", "PRISM")

        # Should NOT have hit the API
        mock_session.get.assert_not_called()
        assert isinstance(spec, SpectrumData)
        assert spec.wavelength.shape == (50,)
        assert spec.object_id == "test_obj_100"
        assert spec.grating == "PRISM"

    def test_downloads_and_caches(self, local_client):
        """open_spectrum downloads from API and caches in managed dir."""
        from astropy.io import fits
        from astropy.table import Table as FitsTable
        import io

        client, mock_session, store, tmp_path = local_client

        # Build a valid FITS file in memory for the mock response
        wave = np.linspace(0.6, 5.3, 30)
        flux = np.ones(30)
        err = np.ones(30) * 0.1
        t = FitsTable([wave, flux, err], names=["WAVELENGTH", "FLUX", "FLUX_ERR"])
        hdul = fits.HDUList([fits.PrimaryHDU(), fits.BinTableHDU(t)])
        buf = io.BytesIO()
        hdul.writeto(buf)
        fits_bytes = buf.getvalue()

        # Mock get_signed_url
        client._api.get_signed_url = Mock(return_value="https://r2.example.com/signed")

        # Mock the download response
        mock_response = MagicMock()
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_response.raise_for_status = Mock()
        mock_response.iter_content = Mock(return_value=[fits_bytes])

        with patch("campfire.client.requests.get", return_value=mock_response):
            spec = client.open_spectrum("test_obj_100", "PRISM")

        assert isinstance(spec, SpectrumData)
        assert spec.wavelength.shape == (30,)

        # File should now be cached in products dir
        cached = tmp_path / "products" / "test_obs" / "test_obj_100_PRISM_spec.fits"
        assert cached.exists()

        # Store should be updated
        local_path = store.find_local_path("test_obj_100", "PRISM")
        assert local_path is not None


class TestIterObjects:
    """Test iter_objects auto-pagination."""

    def test_iter_local(self, local_client):
        """iter_objects yields from local store."""
        client, mock_session, _, _ = local_client

        objects = list(client.iter_objects(observations=["test_obs"]))

        mock_session.get.assert_not_called()
        assert len(objects) == 2

    def test_iter_local_with_filters(self, local_client):
        """iter_objects applies filters locally."""
        client, _, _, _ = local_client

        objects = list(client.iter_objects(
            observations=["test_obs"],
            redshift_range=(2.0, 3.0),
        ))

        assert len(objects) == 1
        assert objects[0]["object_id"] == "test_obj_100"


class TestSpectrumData:
    """Test SpectrumData dataclass."""

    def test_basic_creation(self):
        """SpectrumData can be created with arrays."""
        wave = np.linspace(0.6, 5.3, 100)
        flux = np.random.normal(1.0, 0.1, 100)
        flux_err = np.abs(np.random.normal(0.05, 0.01, 100))

        spec = SpectrumData(
            wavelength=wave,
            flux=flux,
            flux_err=flux_err,
            header={"OBJECT": "test"},
            grating="PRISM",
            object_id="test_123",
        )

        assert spec.wavelength.shape == (100,)
        assert spec.flux.shape == (100,)
        assert spec.grating == "PRISM"
        assert spec.object_id == "test_123"
        assert spec.fits_path is None

    def test_repr(self):
        """SpectrumData has a readable repr."""
        wave = np.linspace(0.6, 5.3, 100)
        spec = SpectrumData(
            wavelength=wave,
            flux=np.ones(100),
            flux_err=np.zeros(100),
            header={},
            grating="G395M",
            object_id="obj_456",
        )

        r = repr(spec)
        assert "obj_456" in r
        assert "G395M" in r
        assert "100 pixels" in r

    def test_from_fits_table_hdu(self, tmp_path):
        """SpectrumData.from_fits reads a table-format FITS file."""
        from astropy.io import fits
        from astropy.table import Table

        # Create a minimal FITS file with a BinTable
        wave = np.linspace(0.6, 5.3, 50)
        flux = np.random.normal(1.0, 0.1, 50)
        err = np.abs(np.random.normal(0.05, 0.01, 50))

        t = Table([wave, flux, err], names=["WAVELENGTH", "FLUX", "FLUX_ERR"])
        hdu_primary = fits.PrimaryHDU()
        hdu_primary.header["OBJECT"] = "test_obj"
        hdu_primary.header["GRATING"] = "PRISM"
        hdu_table = fits.BinTableHDU(t)
        hdul = fits.HDUList([hdu_primary, hdu_table])

        fits_path = tmp_path / "test_spectrum.fits"
        hdul.writeto(str(fits_path))

        spec = SpectrumData.from_fits(str(fits_path))

        assert spec.wavelength.shape == (50,)
        assert spec.flux.shape == (50,)
        assert spec.flux_err.shape == (50,)
        assert spec.object_id == "test_obj"
        assert spec.grating == "PRISM"
        assert spec.fits_path == str(fits_path)

    def test_from_fits_explicit_params(self, tmp_path):
        """SpectrumData.from_fits uses explicit object_id/grating over header."""
        from astropy.io import fits
        from astropy.table import Table

        t = Table(
            [np.linspace(0.6, 5.3, 10), np.ones(10), np.zeros(10)],
            names=["WAVELENGTH", "FLUX", "FLUX_ERR"],
        )
        hdul = fits.HDUList([
            fits.PrimaryHDU(),
            fits.BinTableHDU(t),
        ])

        fits_path = tmp_path / "test.fits"
        hdul.writeto(str(fits_path))

        spec = SpectrumData.from_fits(
            str(fits_path), object_id="custom_id", grating="G395M"
        )

        assert spec.object_id == "custom_id"
        assert spec.grating == "G395M"
