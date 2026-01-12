"""Tests for CAMPFIRE exception classes."""

import pytest

from campfire.exceptions import (
    CampfireError,
    AuthenticationError,
    NotFoundError,
    DownloadError,
    ValidationError,
    APIError,
)


class TestExceptionHierarchy:
    """Test exception class hierarchy."""

    def test_base_exception(self):
        """CampfireError is the base exception."""
        with pytest.raises(CampfireError):
            raise CampfireError("test error")

    def test_authentication_error_inherits(self):
        """AuthenticationError inherits from CampfireError."""
        err = AuthenticationError("auth failed")
        assert isinstance(err, CampfireError)
        assert isinstance(err, Exception)

    def test_not_found_error_inherits(self):
        """NotFoundError inherits from CampfireError."""
        err = NotFoundError("not found")
        assert isinstance(err, CampfireError)

    def test_download_error_inherits(self):
        """DownloadError inherits from CampfireError."""
        err = DownloadError("download failed")
        assert isinstance(err, CampfireError)

    def test_validation_error_inherits(self):
        """ValidationError inherits from CampfireError."""
        err = ValidationError("invalid input")
        assert isinstance(err, CampfireError)

    def test_api_error_inherits(self):
        """APIError inherits from CampfireError."""
        err = APIError("api error")
        assert isinstance(err, CampfireError)


class TestExceptionMessages:
    """Test exception message handling."""

    def test_exception_message(self):
        """Exceptions preserve their message."""
        msg = "Test error message"
        err = CampfireError(msg)
        assert str(err) == msg

    def test_authentication_error_message(self):
        """AuthenticationError preserves message."""
        msg = "Invalid API key"
        err = AuthenticationError(msg)
        assert str(err) == msg

    def test_not_found_error_with_resource(self):
        """NotFoundError can include resource info."""
        msg = "Spectrum not found: ember_uds_p4_123456"
        err = NotFoundError(msg)
        assert "ember_uds_p4_123456" in str(err)


class TestExceptionCatching:
    """Test catching exceptions at different levels."""

    def test_catch_specific_exception(self):
        """Can catch specific exception type."""
        def raise_auth_error():
            raise AuthenticationError("auth failed")

        with pytest.raises(AuthenticationError):
            raise_auth_error()

    def test_catch_base_exception(self):
        """Can catch any CAMPFIRE exception via base class."""
        def raise_auth_error():
            raise AuthenticationError("auth failed")

        with pytest.raises(CampfireError):
            raise_auth_error()

    def test_catch_correct_type(self):
        """Different exceptions can be distinguished."""
        def handle_error(err):
            if isinstance(err, AuthenticationError):
                return "auth"
            elif isinstance(err, NotFoundError):
                return "not_found"
            elif isinstance(err, DownloadError):
                return "download"
            elif isinstance(err, ValidationError):
                return "validation"
            elif isinstance(err, APIError):
                return "api"
            else:
                return "unknown"

        assert handle_error(AuthenticationError("")) == "auth"
        assert handle_error(NotFoundError("")) == "not_found"
        assert handle_error(DownloadError("")) == "download"
        assert handle_error(ValidationError("")) == "validation"
        assert handle_error(APIError("")) == "api"
