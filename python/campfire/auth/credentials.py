"""
Credential storage for CAMPFIRE Python client.

Stores credentials in ~/.campfire/credentials with secure file permissions.
Supports both OAuth tokens and API keys.
"""

import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Literal

# Default paths
CAMPFIRE_DIR = Path.home() / ".campfire"
CREDENTIALS_FILE = CAMPFIRE_DIR / "credentials"


@dataclass
class StoredCredentials:
    """Represents stored authentication credentials."""

    type: Literal["oauth", "api_key"]

    # OAuth fields
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    supabase_token: Optional[str] = None
    supabase_url: Optional[str] = None
    supabase_anon_key: Optional[str] = None
    expires_at: Optional[str] = None  # ISO format datetime string
    user_email: Optional[str] = None

    # API key field
    api_key: Optional[str] = None

    def is_oauth(self) -> bool:
        """Check if credentials are OAuth-based."""
        return self.type == "oauth"

    def is_api_key(self) -> bool:
        """Check if credentials are API key-based."""
        return self.type == "api_key"

    def is_expired(self) -> bool:
        """Check if OAuth access token is expired."""
        if not self.is_oauth() or not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
            # Consider expired if within 5 minutes of expiration
            return datetime.now(expires.tzinfo) >= expires
        except (ValueError, TypeError):
            return True

    def get_token(self) -> Optional[str]:
        """Get the authentication token (access token or API key)."""
        if self.is_api_key():
            return self.api_key
        return self.access_token


class CredentialManager:
    """
    Manages stored credentials for CAMPFIRE authentication.

    Credentials are stored in ~/.campfire/credentials as JSON.
    File permissions are set to 0600 (owner read/write only).

    Examples
    --------
    >>> creds = CredentialManager()
    >>> creds.save_oauth("access_token", "refresh_token", 3600, "user@example.com")
    >>> loaded = creds.load()
    >>> print(loaded.user_email)
    user@example.com
    """

    def __init__(self, credentials_dir: Optional[Path] = None):
        """
        Initialize the credential manager.

        Parameters
        ----------
        credentials_dir : Path, optional
            Custom directory for credentials. Defaults to ~/.campfire
        """
        self.credentials_dir = credentials_dir or CAMPFIRE_DIR
        self.credentials_file = self.credentials_dir / "credentials"

    def _ensure_dir(self) -> None:
        """Create credentials directory with secure permissions."""
        if not self.credentials_dir.exists():
            self.credentials_dir.mkdir(mode=0o700, parents=True)
        else:
            # Ensure permissions are correct
            os.chmod(self.credentials_dir, 0o700)

    def _secure_write(self, data: dict) -> None:
        """Write data to credentials file with secure permissions."""
        self._ensure_dir()

        # Write to a temp file first, then rename (atomic operation)
        temp_file = self.credentials_file.with_suffix(".tmp")

        try:
            with open(temp_file, "w") as f:
                json.dump(data, f, indent=2)

            # Set permissions before renaming
            os.chmod(temp_file, 0o600)

            # Atomic rename
            temp_file.rename(self.credentials_file)
        except Exception:
            # Clean up temp file on error
            if temp_file.exists():
                temp_file.unlink()
            raise

    def save_oauth(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        user_email: Optional[str] = None,
        supabase_token: Optional[str] = None,
        supabase_url: Optional[str] = None,
        supabase_anon_key: Optional[str] = None,
    ) -> None:
        """
        Save OAuth credentials.

        Parameters
        ----------
        access_token : str
            The JWT access token.
        refresh_token : str
            The refresh token for obtaining new access tokens.
        expires_in : int
            Access token lifetime in seconds.
        user_email : str, optional
            User's email address for display purposes.
        supabase_token : str, optional
            Supabase-compatible JWT for direct database access.
        supabase_url : str, optional
            Supabase project URL (returned by server at login).
        supabase_anon_key : str, optional
            Supabase anon key (returned by server at login).
        """
        expires_at = datetime.utcnow().isoformat() + "Z"
        if expires_in > 0:
            from datetime import timedelta

            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat() + "Z"

        creds = StoredCredentials(
            type="oauth",
            access_token=access_token,
            refresh_token=refresh_token,
            supabase_token=supabase_token,
            supabase_url=supabase_url,
            supabase_anon_key=supabase_anon_key,
            expires_at=expires_at,
            user_email=user_email,
        )

        self._secure_write(asdict(creds))

    def save_api_key(self, api_key: str) -> None:
        """
        Save API key credentials.

        Parameters
        ----------
        api_key : str
            The API key (format: sk_live_...).
        """
        creds = StoredCredentials(type="api_key", api_key=api_key)
        self._secure_write(asdict(creds))

    def load(self) -> Optional[StoredCredentials]:
        """
        Load stored credentials.

        Returns
        -------
        StoredCredentials or None
            The stored credentials, or None if no credentials exist.
        """
        if not self.credentials_file.exists():
            return None

        try:
            with open(self.credentials_file, "r") as f:
                data = json.load(f)

            return StoredCredentials(**data)
        except (json.JSONDecodeError, TypeError, KeyError):
            # Invalid credentials file
            return None

    def delete(self) -> bool:
        """
        Delete stored credentials.

        Returns
        -------
        bool
            True if credentials were deleted, False if they didn't exist.
        """
        if self.credentials_file.exists():
            self.credentials_file.unlink()
            return True
        return False

    def update_oauth_tokens(
        self,
        access_token: str,
        refresh_token: str,
        expires_in: int,
        supabase_token: Optional[str] = None,
        supabase_url: Optional[str] = None,
        supabase_anon_key: Optional[str] = None,
    ) -> None:
        """
        Update OAuth tokens after a refresh.

        Preserves existing user_email, supabase_url, and supabase_anon_key
        from stored credentials when new values are not provided.

        Parameters
        ----------
        access_token : str
            New access token.
        refresh_token : str
            New refresh token.
        expires_in : int
            Token lifetime in seconds.
        supabase_token : str, optional
            New Supabase-compatible JWT.
        supabase_url : str, optional
            Supabase project URL (preserved from existing if not provided).
        supabase_anon_key : str, optional
            Supabase anon key (preserved from existing if not provided).
        """
        existing = self.load()
        user_email = existing.user_email if existing else None
        # Preserve Supabase connection info across refreshes
        if not supabase_url and existing:
            supabase_url = existing.supabase_url
        if not supabase_anon_key and existing:
            supabase_anon_key = existing.supabase_anon_key
        self.save_oauth(
            access_token, refresh_token, expires_in, user_email,
            supabase_token, supabase_url, supabase_anon_key,
        )

    def exists(self) -> bool:
        """Check if credentials file exists."""
        return self.credentials_file.exists()
