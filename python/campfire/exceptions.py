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


class DRModeError(CampfireError):
    """Raised when a client opened on a data release (``Campfire(dr=...)``) is
    asked for something only the live service answers: a remote query, a
    sync, a download, a cutout. A release is read from its local store and
    never falls back to live routes (docs/design-data-releases.md §4.7)."""
    pass


class DRLoadError(CampfireError):
    """Raised when a release's export files cannot be loaded: a file that does
    not verify against the sha256 / size / row count the files route gave, or
    a release of a format version this client does not know."""
    pass
