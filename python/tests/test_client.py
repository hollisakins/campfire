"""Tests for CAMPFIRE API client."""

import pytest
from unittest.mock import Mock, patch

from campfire import Campfire
from campfire.exceptions import AuthenticationError, NotFoundError


def _make_mock_response(status_code=200, json_data=None, text=""):
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


@pytest.fixture
def mock_api_session():
    """Mock APISession that bypasses credential loading."""
    with patch("campfire.client.APISession") as MockSession:
        session_instance = MockSession.return_value
        session_instance.base_url = "https://campfire.hollisakins.com/api/v1"
        session_instance._auto_refresh = True
        session_instance._ensure_valid_token = Mock()
        session_instance._session = Mock()
        session_instance.session = session_instance._session
        yield session_instance


class TestClientInitialization:
    @patch("campfire.client.APISession")
    def test_init_creates_api_session(self, MockSession):
        MockSession.return_value.base_url = "https://campfire.hollisakins.com/api/v1"
        Campfire()
        MockSession.assert_called_once()

    @patch("campfire.client.APISession")
    def test_init_custom_base_url(self, MockSession):
        MockSession.return_value.base_url = "https://custom.com/api/v1"
        custom_url = "https://custom.com/api/v1"
        client = Campfire(base_url=custom_url)
        MockSession.assert_called_once_with(base_url=custom_url, auto_refresh=True)
        assert client.base_url == custom_url


class TestQueryObjects:
    def test_query_objects_success(self, mock_api_session, sample_objects_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_objects_response
        )
        client = Campfire()
        result = client.query_objects(remote=True)
        assert len(result) == 1
        assert result[0]["object_id"] == "CAMPFIRE-J023629.63-053243.2"

    def test_query_objects_empty(self, mock_api_session):
        mock_api_session.get.return_value = _make_mock_response(
            json_data={"data": [], "pagination": {"total": 0}}
        )
        client = Campfire()
        result = client.query_objects(remote=True)
        assert len(result) == 0

    def test_query_objects_auth_error(self, mock_api_session):
        mock_api_session.get.return_value = _make_mock_response(
            status_code=401, text="Invalid API key"
        )
        client = Campfire()
        with pytest.raises(AuthenticationError):
            client.query_objects(remote=True)

    def test_query_objects_with_filters(self, mock_api_session, sample_objects_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_objects_response
        )
        client = Campfire()
        client.query_objects(
            programs=["ember-uds"],
            fields=["COSMOS", "UDS"],
            gratings=["PRISM"],
            redshift_range=(2.0, 4.0),
            redshift_quality=[2, 3],
            inspected_only=True,
            limit=100,
            remote=True,
        )
        call_args = mock_api_session.get.call_args
        params = call_args.kwargs.get("params", {})
        assert "programs" in params
        assert "fields" in params
        assert "gratings" in params
        assert "redshift_min" in params
        assert "redshift_quality" in params
        assert "inspected_only" in params
        assert params["limit"] == 100

    def test_query_objects_cone_search(self, mock_api_session, sample_objects_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_objects_response
        )
        client = Campfire()
        client.query_objects(cone_search=(150.0, 2.5, 5.0), remote=True)
        params = mock_api_session.get.call_args.kwargs.get("params", {})
        assert params["ra"] == 150.0
        assert params["dec"] == 2.5
        assert params["radius"] == 5.0


class TestQuerySpectra:
    def test_query_spectra_success(self, mock_api_session, sample_spectra_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_spectra_response
        )
        client = Campfire()
        result = client.query_spectra(remote=True)
        assert len(result) == 1
        assert result[0]["spectrum_id"] == "ember_uds_p4_prism_clear_123456"

    def test_query_spectra_hits_list_endpoint(self, mock_api_session, sample_spectra_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_spectra_response
        )
        client = Campfire()
        client.query_spectra(remote=True)
        url = mock_api_session.get.call_args.args[0]
        assert url == "/spectra/list"

    def test_query_spectra_dq_flags_param(self, mock_api_session, sample_spectra_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_spectra_response
        )
        from campfire.flags import DQFlags
        client = Campfire()
        client.query_spectra(dq_flags=~DQFlags.CONTAMINATION, remote=True)
        params = mock_api_session.get.call_args.kwargs.get("params", {})
        assert "dq_flags_exclude" in params


class TestMetadataMethods:
    def test_get_metadata(self, mock_api_session, sample_metadata_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )
        client = Campfire()
        result = client.get_metadata()
        assert "programs" in result
        assert "fields" in result

    def test_get_programs(self, mock_api_session, sample_metadata_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )
        client = Campfire()
        result = client.get_programs()
        assert len(result) == 2

    def test_get_fields(self, mock_api_session, sample_metadata_response):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )
        client = Campfire()
        result = client.get_fields()
        assert result == ["COSMOS", "UDS", "EGS"]


class TestSpectrumDataMethods:
    def test_get_spectrum_data(self, mock_api_session, sample_spectrum_data):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_spectrum_data
        )
        client = Campfire()
        result = client.get_spectrum_data("ember_uds_p4_prism_clear_123456")
        assert "wave" in result
        params = mock_api_session.get.call_args.kwargs.get("params", {})
        assert params["spectrum_id"] == "ember_uds_p4_prism_clear_123456"

    def test_get_spectrum_data_not_found(self, mock_api_session):
        mock_api_session.get.return_value = _make_mock_response(
            status_code=404, text="Not found"
        )
        client = Campfire()
        with pytest.raises(NotFoundError):
            client.get_spectrum_data("nonexistent")

    def test_get_redshift_fit_data(self, mock_api_session, sample_redshift_fit_data):
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_redshift_fit_data
        )
        client = Campfire()
        result = client.get_redshift_fit_data("ember_uds_p4_prism_clear_123456")
        assert result["redshift"] == 2.5


class TestImagingMethods:
    def test_get_cutout_object_id_param(self, mock_api_session, tmp_path):
        mock_api_session.get.return_value = _make_mock_response(json_data={})
        mock_api_session.get.return_value.content = b"PNGDATA"
        with patch("campfire.config.resolve_data_dir", return_value=tmp_path):
            client = Campfire()
            client.get_cutout("CAMPFIRE-J0001+0001", fov=3.0, cache=False)
        params = mock_api_session.get.call_args.kwargs.get("params", {})
        assert params["object_id"] == "CAMPFIRE-J0001+0001"

    def test_get_shutters_object_id_param(self, mock_api_session, tmp_path):
        mock_api_session.get.return_value = _make_mock_response(
            json_data={"shutters": [], "meta": {}}
        )
        with patch("campfire.config.resolve_data_dir", return_value=tmp_path):
            client = Campfire()
            client.get_shutters("CAMPFIRE-J0001+0001", fov=3.0, cache=False)
        params = mock_api_session.get.call_args.kwargs.get("params", {})
        assert params["object_id"] == "CAMPFIRE-J0001+0001"
