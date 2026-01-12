"""Tests for CAMPFIRE API client."""

import pytest
from unittest.mock import Mock, patch, MagicMock
import json

from campfire import Campfire
from campfire.exceptions import (
    AuthenticationError,
    ValidationError,
    NotFoundError,
    APIError,
)


class TestClientInitialization:
    """Test client initialization and configuration."""

    def test_init_with_api_key(self, sample_api_key):
        """Client initializes with provided API key."""
        client = Campfire(api_key=sample_api_key)
        assert client.api_key == sample_api_key

    def test_init_from_environment(self, monkeypatch, sample_api_key):
        """Client reads API key from environment variable."""
        monkeypatch.setenv("CAMPFIRE_API_KEY", sample_api_key)
        client = Campfire()
        assert client.api_key == sample_api_key

    def test_init_without_api_key_raises(self, monkeypatch):
        """Client raises error when no API key is provided."""
        monkeypatch.delenv("CAMPFIRE_API_KEY", raising=False)
        with pytest.raises(AuthenticationError) as exc_info:
            Campfire()
        assert "API key required" in str(exc_info.value)

    def test_init_invalid_api_key_format(self):
        """Client validates API key format."""
        with pytest.raises(ValidationError) as exc_info:
            Campfire(api_key="invalid_key_format")
        assert "Invalid API key format" in str(exc_info.value)

    def test_init_custom_base_url(self, sample_api_key):
        """Client accepts custom base URL."""
        custom_url = "https://custom.campfire.com/api/v1"
        client = Campfire(api_key=sample_api_key, base_url=custom_url)
        assert client.base_url == custom_url

    def test_default_base_url(self, sample_api_key):
        """Client uses default production URL."""
        client = Campfire(api_key=sample_api_key)
        assert "campfire.vercel.app" in client.base_url

    def test_session_headers(self, sample_api_key):
        """Client session includes auth headers."""
        client = Campfire(api_key=sample_api_key)
        assert "Authorization" in client.session.headers
        assert f"Bearer {sample_api_key}" in client.session.headers["Authorization"]
        assert "User-Agent" in client.session.headers


class TestQueryObjects:
    """Test query_objects method."""

    @patch("campfire.client.requests.Session.get")
    def test_query_objects_success(self, mock_get, sample_api_key, sample_objects_response):
        """query_objects returns astropy Table on success."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_objects_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.query_objects()

        assert len(result) == 1
        assert result[0]["object_id"] == "ember_uds_p4_123456"

    @patch("campfire.client.requests.Session.get")
    def test_query_objects_empty(self, mock_get, sample_api_key):
        """query_objects returns empty Table when no results."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": [], "pagination": {"total": 0}}

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.query_objects()

        assert len(result) == 0

    @patch("campfire.client.requests.Session.get")
    def test_query_objects_auth_error(self, mock_get, sample_api_key):
        """query_objects raises AuthenticationError on 401."""
        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Invalid API key"

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)

        with pytest.raises(AuthenticationError):
            client.query_objects()

    @patch("campfire.client.requests.Session.get")
    def test_query_objects_access_denied(self, mock_get, sample_api_key):
        """query_objects raises AuthenticationError on 403."""
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.text = "Access denied"

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)

        with pytest.raises(AuthenticationError):
            client.query_objects()

    @patch("campfire.client.requests.Session.get")
    def test_query_objects_with_filters(self, mock_get, sample_api_key, sample_objects_response):
        """query_objects passes filter parameters correctly."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_objects_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        client.query_objects(
            programs=[1, 2],
            fields=["COSMOS", "UDS"],
            gratings=["PRISM"],
            redshift_range=(2.0, 4.0),
            redshift_quality=[2, 3],
            inspected_only=True,
            limit=100,
        )

        # Check that params were passed
        call_args = mock_get.call_args
        params = call_args.kwargs.get("params", call_args[1].get("params", {}))

        assert "programs" in params
        assert "fields" in params
        assert "gratings" in params
        assert "redshift_min" in params
        assert "redshift_max" in params
        assert "redshift_quality" in params
        assert "inspected_only" in params
        assert params["limit"] == 100

    @patch("campfire.client.requests.Session.get")
    def test_query_objects_cone_search(self, mock_get, sample_api_key, sample_objects_response):
        """query_objects passes cone search parameters."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_objects_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        client.query_objects(cone_search=(150.0, 2.5, 5.0))

        call_args = mock_get.call_args
        params = call_args.kwargs.get("params", call_args[1].get("params", {}))

        assert params["ra"] == 150.0
        assert params["dec"] == 2.5
        assert params["radius"] == 5.0


class TestMetadataMethods:
    """Test metadata fetching methods."""

    @patch("campfire.client.requests.Session.get")
    def test_get_metadata(self, mock_get, sample_api_key, sample_metadata_response):
        """get_metadata returns metadata dict."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_metadata_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.get_metadata()

        assert "programs" in result
        assert "fields" in result
        assert "gratings" in result
        assert "observations" in result

    @patch("campfire.client.requests.Session.get")
    def test_get_programs(self, mock_get, sample_api_key, sample_metadata_response):
        """get_programs returns astropy Table of programs."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_metadata_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.get_programs()

        assert len(result) == 2
        assert result[0]["program_name"] == "EMBER-UDS"

    @patch("campfire.client.requests.Session.get")
    def test_get_fields(self, mock_get, sample_api_key, sample_metadata_response):
        """get_fields returns list of field names."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_metadata_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.get_fields()

        assert result == ["COSMOS", "UDS", "EGS"]

    @patch("campfire.client.requests.Session.get")
    def test_get_gratings(self, mock_get, sample_api_key, sample_metadata_response):
        """get_gratings returns list of grating names."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_metadata_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.get_gratings()

        assert "PRISM" in result
        assert "G395M" in result

    @patch("campfire.client.requests.Session.get")
    def test_get_observations(self, mock_get, sample_api_key, sample_metadata_response):
        """get_observations returns list of observation names."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_metadata_response

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.get_observations()

        assert "ember_uds_p4" in result


class TestSpectrumDataMethods:
    """Test spectrum data fetching methods."""

    @patch("campfire.client.requests.Session.get")
    def test_get_spectrum_data(self, mock_get, sample_api_key, sample_spectrum_data):
        """get_spectrum_data returns spectrum data dict."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_spectrum_data

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.get_spectrum_data("ember_uds_p4_123456", "PRISM")

        assert "wave" in result
        assert "fnu" in result
        assert "snr_2d" in result

    @patch("campfire.client.requests.Session.get")
    def test_get_spectrum_data_not_found(self, mock_get, sample_api_key):
        """get_spectrum_data raises NotFoundError on 404."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Not found"

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)

        with pytest.raises(NotFoundError):
            client.get_spectrum_data("nonexistent", "PRISM")

    @patch("campfire.client.requests.Session.get")
    def test_get_redshift_fit_data(self, mock_get, sample_api_key, sample_redshift_fit_data):
        """get_redshift_fit_data returns fit data dict."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = sample_redshift_fit_data

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)
        result = client.get_redshift_fit_data("ember_uds_p4_123456", "PRISM")

        assert "redshift" in result
        assert "chi2_grid" in result
        assert result["redshift"] == 2.5

    @patch("campfire.client.requests.Session.get")
    def test_get_redshift_fit_data_not_available(self, mock_get, sample_api_key):
        """get_redshift_fit_data raises NotFoundError when fit not available."""
        mock_response = Mock()
        mock_response.status_code = 404
        mock_response.text = "Fit not available"

        mock_get.return_value = mock_response

        client = Campfire(api_key=sample_api_key)

        with pytest.raises(NotFoundError):
            client.get_redshift_fit_data("object_without_fit", "PRISM")
