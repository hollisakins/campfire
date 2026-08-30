"""
Token management for CAMPFIRE Python client.

Handles token refresh and validation.
"""

import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Tuple

import requests

from ..exceptions import AuthenticationError
from ._jwt import get_exp
from .credentials import CredentialManager, StoredCredentials


class _RefreshRejected(Exception):
    """Internal: the server rejected the refresh token we presented.

    Distinct from :class:`AuthenticationError` because it is often *not* fatal —
    a concurrent refresh (another thread or process) rotates the token, and
    retrying with the freshly-stored one succeeds. Never escapes this module.
    """


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

        # Refresh is serialised: the server ROTATES the refresh token, so two
        # threads refreshing at once means the loser POSTs a token the winner
        # already invalidated and gets `invalid_grant` back. Deploy runs 16
        # concurrent upload workers off one manager, so this is reached whenever
        # a long transfer crosses the refresh threshold (issue #474).
        # `_refresh_generation` lets a thread that waited on the lock notice
        # that someone else already refreshed, and use that result instead of
        # issuing a second (doomed) refresh.
        self._refresh_lock = threading.RLock()
        self._refresh_generation = 0

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

    def _cached_expires_in(self) -> int:
        """Seconds until the cached access token expires (>= 0, 0 if unknown)."""
        creds = self._cached_creds
        if not creds or not creds.expires_at:
            return 0
        try:
            expires = datetime.fromisoformat(
                creds.expires_at.replace("Z", "+00:00")
            )
            delta = (expires - datetime.now(expires.tzinfo)).total_seconds()
            return max(0, int(delta))
        except (ValueError, TypeError):
            return 0

    def _post_refresh(self, refresh_token: str) -> Tuple[str, str, int]:
        """One refresh round-trip. Caller must hold ``_refresh_lock``.

        Raises ``_RefreshRejected`` when the server rejects the token itself, so
        the caller can decide whether a reload-and-retry is worth attempting;
        every other failure raises ``AuthenticationError`` directly.
        """
        try:
            response = self.session.post(
                self.refresh_endpoint,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
            )

            if response.status_code == 400:
                data = response.json()
                error = data.get("error", "unknown_error")
                if error in ("invalid_grant", "expired_token"):
                    raise _RefreshRejected(error)
                raise AuthenticationError(f"Token refresh failed: {error}")

            response.raise_for_status()
            data = response.json()

            access_token = data["access_token"]
            new_refresh_token = data["refresh_token"]
            expires_in = data["expires_in"]
            supabase_token = data.get("supabase_token")
            supabase_url = data.get("supabase_url")
            supabase_anon_key = data.get("supabase_anon_key")

            # Update stored credentials
            self.creds_manager.update_oauth_tokens(
                access_token, new_refresh_token, expires_in,
                supabase_token, supabase_url, supabase_anon_key,
            )

            # Reload cached credentials
            self._load_credentials()
            self._refresh_generation += 1

            return access_token, new_refresh_token, expires_in

        except requests.RequestException as e:
            raise AuthenticationError(f"Failed to refresh token: {e}")

    def refresh_tokens(self) -> Tuple[str, str, int]:
        """
        Refresh the OAuth tokens.

        Thread-safe: concurrent callers are serialised, and a caller that waited
        on the lock returns the winner's freshly-rotated token rather than
        re-refreshing with the one the winner just invalidated (issue #474).

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

        # Sampled before acquiring: if it moves while we wait, another thread
        # refreshed and our view of the refresh token is stale.
        seen_generation = self._refresh_generation

        with self._refresh_lock:
            if self._refresh_generation != seen_generation:
                creds = self._cached_creds
                if creds and creds.access_token and creds.refresh_token:
                    return (
                        creds.access_token,
                        creds.refresh_token,
                        self._cached_expires_in(),
                    )

            if not self._cached_creds or not self._cached_creds.refresh_token:
                raise AuthenticationError(
                    "Cannot refresh: no refresh token available"
                )

            try:
                return self._post_refresh(self._cached_creds.refresh_token)
            except _RefreshRejected:
                # The token we sent was rejected. Another *process* may have
                # rotated it on disk since we cached it, so reload and retry
                # once with whatever is stored now. Only give up — and only
                # then tell the user to log in again — if the stored token is
                # unchanged (nothing to retry with) or is itself rejected.
                stale = self._cached_creds.refresh_token
                self._load_credentials()
                current = (
                    self._cached_creds.refresh_token
                    if self._cached_creds else None
                )
                if current and current != stale:
                    try:
                        return self._post_refresh(current)
                    except _RefreshRejected:
                        pass
                raise AuthenticationError(
                    "Session expired. Please run 'campfire login' again."
                )

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

        When ``auto_refresh`` is set, this refreshes based on the *access*
        token's expiry (:meth:`needs_refresh`). For refresh decisions keyed on
        the Supabase JWT's own ``exp``, use :meth:`supabase_token_needs_refresh`
        and :meth:`force_refresh_supabase_token` instead.

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

    def supabase_token_needs_refresh(self, buffer_minutes: int = 10) -> bool:
        """
        Whether the cached Supabase JWT is at or near expiry.

        Unlike :meth:`needs_refresh` (which inspects the *access* token's
        ``expires_at``), this decodes the Supabase JWT's own ``exp`` claim, so
        refresh decisions don't depend on the access and Supabase tokens sharing
        a lifetime. Falls back to :meth:`needs_refresh` when the token can't be
        decoded (safe: both tokens are currently minted with the same 1 h TTL).
        """
        if not self.is_oauth():
            return False
        if not self._cached_creds or not self._cached_creds.supabase_token:
            return True
        exp = get_exp(self._cached_creds.supabase_token)
        if exp is None:
            return self.needs_refresh(buffer_minutes)
        return time.time() + buffer_minutes * 60 >= exp

    def force_refresh_supabase_token(self) -> Optional[str]:
        """
        Unconditionally refresh and return the new Supabase JWT.

        ``get_supabase_token(auto_refresh=True)`` only refreshes when the
        *access* token's :meth:`needs_refresh` fires — the wrong signal when the
        Supabase JWT is the one near expiry. This forces a refresh via
        :meth:`refresh_tokens` and returns the freshly-minted Supabase token.
        """
        if not self.is_oauth():
            return None
        self.refresh_tokens()
        return self.get_supabase_token(auto_refresh=False)

    def get_user_email(self) -> Optional[str]:
        """Get the user's email if available."""
        if self._cached_creds and self._cached_creds.user_email:
            return self._cached_creds.user_email
        return None

    def invalidate(self) -> None:
        """Clear cached credentials (call after logout)."""
        self._cached_creds = None
