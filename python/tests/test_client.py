"""Tests for CAMPFIRE API client."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from campfire import Campfire
from campfire.exceptions import (
    AuthenticationError,
    ValidationError,
    NotFoundError,
    APIError,
)


def _make_mock_response(status_code=200, json_data=None, text=""):
    """Create a mock requests.Response."""
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


@pytest.fixture
def mock_api_session():
    """Create a mock APISession that bypasses credential loading."""
    with patch("campfire.client.APISession") as MockSession:
        session_instance = MockSession.return_value
        session_instance.base_url = "https://campfire.hollisakins.com/api/v1"
        session_instance._auto_refresh = True
        session_instance._ensure_valid_token = Mock()
        # The underlying requests session
        session_instance._session = Mock()
        session_instance.session = session_instance._session
        yield session_instance


class TestClientInitialization:
    """Test client initialization and configuration."""

    @patch("campfire.client.APISession")
    def test_init_creates_api_session(self, MockSession):
        """Client creates an APISession on init."""
        MockSession.return_value.base_url = "https://campfire.hollisakins.com/api/v1"
        client = Campfire()
        MockSession.assert_called_once()

    @patch("campfire.client.APISession")
    def test_init_custom_base_url(self, MockSession):
        """Client passes custom base URL to APISession."""
        MockSession.return_value.base_url = "https://custom.com/api/v1"
        custom_url = "https://custom.com/api/v1"
        client = Campfire(base_url=custom_url)
        MockSession.assert_called_once_with(base_url=custom_url, auto_refresh=True)
        assert client.base_url == custom_url

    @patch("campfire.client.APISession")
    def test_default_base_url(self, MockSession):
        """Client uses default production URL."""
        MockSession.return_value.base_url = "https://campfire.hollisakins.com/api/v1"
        client = Campfire()
        assert "campfire.hollisakins.com" in client.base_url

    @patch("campfire.client.APISession")
    def test_init_no_credentials_raises(self, MockSession):
        """Client raises AuthenticationError when no credentials exist."""
        MockSession.side_effect = AuthenticationError("No credentials found.")
        with pytest.raises(AuthenticationError):
            Campfire()

    @patch("campfire.client.APISession")
    def test_auto_refresh_passed_through(self, MockSession):
        """auto_refresh parameter is passed to APISession."""
        MockSession.return_value.base_url = "https://campfire.hollisakins.com/api/v1"
        Campfire(auto_refresh=False)
        MockSession.assert_called_once_with(base_url=None, auto_refresh=False)


class TestQueryObjects:
    """Test query_objects method."""

    def test_query_objects_success(self, mock_api_session, sample_objects_response):
        """query_objects returns astropy Table on success."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_objects_response
        )

        client = Campfire()
        result = client.query_objects()

        assert len(result) == 1
        assert result[0]["object_id"] == "ember_uds_p4_123456"

    def test_query_objects_empty(self, mock_api_session):
        """query_objects returns empty Table when no results."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data={"data": [], "pagination": {"total": 0}}
        )

        client = Campfire()
        result = client.query_objects()

        assert len(result) == 0

    def test_query_objects_auth_error(self, mock_api_session):
        """query_objects raises AuthenticationError on 401."""
        mock_api_session.get.return_value = _make_mock_response(
            status_code=401, text="Invalid API key"
        )

        client = Campfire()
        with pytest.raises(AuthenticationError):
            client.query_objects()

    def test_query_objects_access_denied(self, mock_api_session):
        """query_objects raises AuthenticationError on 403."""
        mock_api_session.get.return_value = _make_mock_response(
            status_code=403, text="Access denied"
        )

        client = Campfire()
        with pytest.raises(AuthenticationError):
            client.query_objects()

    def test_query_objects_with_filters(self, mock_api_session, sample_objects_response):
        """query_objects passes filter parameters correctly."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_objects_response
        )

        client = Campfire()
        client.query_objects(
            programs=[1, 2],
            fields=["COSMOS", "UDS"],
            gratings=["PRISM"],
            redshift_range=(2.0, 4.0),
            redshift_quality=[2, 3],
            inspected_only=True,
            limit=100,
        )

        # Check that the session.get was called with params
        call_args = mock_api_session.get.call_args
        # APIClient calls session.get(path, params=...) or session.get(path, **kwargs)
        # The path is "/objects" and params are passed
        params = call_args.kwargs.get("params", {})

        assert "programs" in params
        assert "fields" in params
        assert "gratings" in params
        assert "redshift_min" in params
        assert "redshift_max" in params
        assert "redshift_quality" in params
        assert "inspected_only" in params
        assert params["limit"] == 100

    def test_query_objects_cone_search(self, mock_api_session, sample_objects_response):
        """query_objects passes cone search parameters."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_objects_response
        )

        client = Campfire()
        client.query_objects(cone_search=(150.0, 2.5, 5.0))

        call_args = mock_api_session.get.call_args
        params = call_args.kwargs.get("params", {})

        assert params["ra"] == 150.0
        assert params["dec"] == 2.5
        assert params["radius"] == 5.0


class TestMetadataMethods:
    """Test metadata fetching methods."""

    def test_get_metadata(self, mock_api_session, sample_metadata_response):
        """get_metadata returns metadata dict."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )

        client = Campfire()
        result = client.get_metadata()

        assert "programs" in result
        assert "fields" in result
        assert "gratings" in result
        assert "observations" in result

    def test_get_programs(self, mock_api_session, sample_metadata_response):
        """get_programs returns astropy Table of programs."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )

        client = Campfire()
        result = client.get_programs()

        assert len(result) == 2
        assert result[0]["program_name"] == "EMBER-UDS"

    def test_get_fields(self, mock_api_session, sample_metadata_response):
        """get_fields returns list of field names."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )

        client = Campfire()
        result = client.get_fields()

        assert result == ["COSMOS", "UDS", "EGS"]

    def test_get_gratings(self, mock_api_session, sample_metadata_response):
        """get_gratings returns list of grating names."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )

        client = Campfire()
        result = client.get_gratings()

        assert "PRISM" in result
        assert "G395M" in result

    def test_get_observations(self, mock_api_session, sample_metadata_response):
        """get_observations returns list of observation names."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_metadata_response
        )

        client = Campfire()
        result = client.get_observations()

        assert "ember_uds_p4" in result


class TestSpectrumDataMethods:
    """Test spectrum data fetching methods."""

    def test_get_spectrum_data(self, mock_api_session, sample_spectrum_data):
        """get_spectrum_data returns spectrum data dict."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_spectrum_data
        )

        client = Campfire()
        result = client.get_spectrum_data("ember_uds_p4_123456", "PRISM")

        assert "wave" in result
        assert "fnu" in result
        assert "snr_2d" in result

    def test_get_spectrum_data_not_found(self, mock_api_session):
        """get_spectrum_data raises NotFoundError on 404."""
        mock_api_session.get.return_value = _make_mock_response(
            status_code=404, text="Not found"
        )

        client = Campfire()
        with pytest.raises(NotFoundError):
            client.get_spectrum_data("nonexistent", "PRISM")

    def test_get_redshift_fit_data(self, mock_api_session, sample_redshift_fit_data):
        """get_redshift_fit_data returns fit data dict."""
        mock_api_session.get.return_value = _make_mock_response(
            json_data=sample_redshift_fit_data
        )

        client = Campfire()
        result = client.get_redshift_fit_data("ember_uds_p4_123456", "PRISM")

        assert "redshift" in result
        assert "chi2_grid" in result
        assert result["redshift"] == 2.5

    def test_get_redshift_fit_data_not_available(self, mock_api_session):
        """get_redshift_fit_data raises NotFoundError when fit not available."""
        mock_api_session.get.return_value = _make_mock_response(
            status_code=404, text="Fit not available"
        )

        client = Campfire()
        with pytest.raises(NotFoundError):
            client.get_redshift_fit_data("object_without_fit", "PRISM")
