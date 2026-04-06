"""
Token management for CAMPFIRE Python client.

Handles token refresh and validation.
"""

from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests

from ..exceptions import AuthenticationError
from .credentials import CredentialManager, StoredCredentials


class TokenManager:
    """
    Manages OAuth tokens including automatic refresh.

    This class handles:
    - Loading tokens from stored credentials
    - Checking token expiration
    - Refreshing tokens when needed
    - Updating stored credentials after refresh

    Examples
    --------
    >>> manager = TokenManager("https://campfire.hollisakins.com/api/v1")
    >>> token = manager.get_valid_token()  # Auto-refreshes if needed
    """

    def __init__(
        self,
        base_url: str,
        credentials_manager: Optional[CredentialManager] = None,
    ):
        """
        Initialize the token manager.

        Parameters
        ----------
        base_url : str
            Base URL for the CAMPFIRE API.
        credentials_manager : CredentialManager, optional
            Custom credential manager. Defaults to standard location.
        """
        self.base_url = base_url.rstrip("/")
        self.refresh_endpoint = f"{self.base_url}/auth/refresh"
        self.creds_manager = credentials_manager or CredentialManager()
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "campfire-python/0.1.0"})

        # Cache the current credentials
        self._cached_creds: Optional[StoredCredentials] = None
        self._load_credentials()

    def _load_credentials(self) -> None:
        """Load credentials from storage."""
        self._cached_creds = self.creds_manager.load()

    def has_credentials(self) -> bool:
        """Check if credentials are available."""
        return self._cached_creds is not None

    def is_oauth(self) -> bool:
        """Check if using OAuth credentials."""
        return self._cached_creds is not None and self._cached_creds.is_oauth()

    def is_api_key(self) -> bool:
        """Check if using API key credentials."""
        return self._cached_creds is not None and self._cached_creds.is_api_key()

    def needs_refresh(self, buffer_minutes: int = 5) -> bool:
        """
        Check if OAuth token needs to be refreshed.

        Parameters
        ----------
        buffer_minutes : int
            Refresh this many minutes before actual expiration.

        Returns
        -------
        bool
            True if token needs refresh, False otherwise.
        """
        if not self.is_oauth():
            return False

        if not self._cached_creds or not self._cached_creds.expires_at:
            return True

        try:
            expires = datetime.fromisoformat(
                self._cached_creds.expires_at.replace("Z", "+00:00")
            )
            threshold = datetime.now(expires.tzinfo) + timedelta(minutes=buffer_minutes)
            return expires <= threshold
        except (ValueError, TypeError):
            return True

    def refresh_tokens(self) -> Tuple[str, str, int]:
        """
        Refresh the OAuth tokens.

        Returns
        -------
        tuple
            (access_token, refresh_token, expires_in)

        Raises
        ------
        AuthenticationError
            If refresh fails.
        """
        if not self.is_oauth():
            raise AuthenticationError("Cannot refresh: not using OAuth credentials")

        if not self._cached_creds or not self._cached_creds.refresh_token:
            raise AuthenticationError("Cannot refresh: no refresh token available")

        try:
            response = self.session.post(
                self.refresh_endpoint,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": self._cached_creds.refresh_token,
                },
            )

            if response.status_code == 400:
                data = response.json()
                error = data.get("error", "unknown_error")
                if error in ("invalid_grant", "expired_token"):
                    raise AuthenticationError(
                        "Session expired. Please run 'campfire login' again."
                    )
                raise AuthenticationError(f"Token refresh failed: {error}")

            response.raise_for_status()
            data = response.json()

            access_token = data["access_token"]
            refresh_token = data["refresh_token"]
            expires_in = data["expires_in"]
            supabase_token = data.get("supabase_token")
            supabase_url = data.get("supabase_url")
            supabase_anon_key = data.get("supabase_anon_key")

            # Update stored credentials
            self.creds_manager.update_oauth_tokens(
                access_token, refresh_token, expires_in,
                supabase_token, supabase_url, supabase_anon_key,
            )

            # Reload cached credentials
            self._load_credentials()

            return access_token, refresh_token, expires_in

        except requests.RequestException as e:
            raise AuthenticationError(f"Failed to refresh token: {e}")

    def get_valid_token(self, auto_refresh: bool = True) -> str:
        """
        Get a valid authentication token.

        For API keys, returns the key directly.
        For OAuth tokens, refreshes if needed.

        Parameters
        ----------
        auto_refresh : bool
            Whether to automatically refresh expired OAuth tokens.

        Returns
        -------
        str
            Valid authentication token.

        Raises
        ------
        AuthenticationError
            If no credentials or token is invalid.
        """
        if not self.has_credentials():
            raise AuthenticationError(
                "No credentials found. Run 'campfire login' or set CAMPFIRE_API_KEY."
            )

        # API keys don't need refresh
        if self.is_api_key():
            return self._cached_creds.api_key

        # OAuth tokens may need refresh
        if self.is_oauth():
            if auto_refresh and self.needs_refresh():
                self.refresh_tokens()

            if not self._cached_creds.access_token:
                raise AuthenticationError("No access token available")

            return self._cached_creds.access_token

        raise AuthenticationError("Unknown credential type")

    def get_supabase_token(self, auto_refresh: bool = True) -> Optional[str]:
        """
        Get a valid Supabase-compatible JWT.

        The supabase_token shares the same expiry as the access token,
        so refreshing one refreshes both.

        Returns
        -------
        str or None
            Supabase JWT, or None if not available (e.g. API key auth).
        """
        if not self.has_credentials() or not self.is_oauth():
            return None

        if auto_refresh and self.needs_refresh():
            self.refresh_tokens()

        return self._cached_creds.supabase_token if self._cached_creds else None

    def get_user_email(self) -> Optional[str]:
        """Get the user's email if available."""
        if self._cached_creds and self._cached_creds.user_email:
            return self._cached_creds.user_email
        return None

    def invalidate(self) -> None:
        """Clear cached credentials (call after logout)."""
        self._cached_creds = None
