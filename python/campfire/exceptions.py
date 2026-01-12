"""Custom exceptions for the CAMPFIRE API client."""


class CampfireError(Exception):
    """Base exception for all CAMPFIRE API errors."""
    pass


class AuthenticationError(CampfireError):
    """Raised when API key authentication fails."""
    pass


class NotFoundError(CampfireError):
    """Raised when a requested resource is not found."""
    pass


class DownloadError(CampfireError):
    """Raised when a file download fails."""
    pass


class ValidationError(CampfireError):
    """Raised when input validation fails."""
    pass


class APIError(CampfireError):
    """Raised when the API returns an unexpected error."""
    pass
