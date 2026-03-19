"""Shared session management for CAMPFIRE API access.

Provides authenticated HTTP sessions used by both the CLI and the Python client,
consolidating session creation that was previously duplicated across modules.
"""

import os
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..auth.tokens import TokenManager
from ..exceptions import AuthenticationError

__version__ = "0.1.0"

# Default API URL
DEFAULT_BASE_URL = "https://campfire.hollisakins.com/api/v1"


def resolve_base_url(base_url: Optional[str] = None) -> str:
    """Resolve the API base URL from argument, environment, config, or default."""
    if base_url:
        return base_url

    env_url = os.environ.get("CAMPFIRE_API_URL")
    if env_url:
        return env_url

    try:
        from ..config import Config
        config = Config()
        if config.exists() and config.base_url:
            return config.base_url
    except Exception:
        pass

    return DEFAULT_BASE_URL


class APISession:
    """Authenticated HTTP session for the CAMPFIRE API.

    Wraps a ``requests.Session`` with automatic credential loading and
    OAuth token refresh. Used by both ``APIClient`` and the CLI.

    Parameters
    ----------
    base_url : str, optional
        API base URL. Resolved from env/config/default if not provided.
    auto_refresh : bool, optional
        Automatically refresh OAuth tokens when they expire (default True).
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        auto_refresh: bool = True,
    ):
        self.base_url = resolve_base_url(base_url)
        self._auto_refresh = auto_refresh
        self._token_manager: Optional[TokenManager] = None
        self._auth_type: Optional[str] = None
        self._auth_token: Optional[str] = None

        # Load credentials
        self._load_credentials()

        # Create session
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": f"campfire-python/{__version__}",
        })
        self._update_auth_header()

    def _load_credentials(self) -> None:
        """Load credentials from ~/.campfire/credentials."""
        self._token_manager = TokenManager(self.base_url)

        if not self._token_manager.has_credentials():
            raise AuthenticationError(
                "No credentials found. Run 'campfire login' to authenticate."
            )

        if self._token_manager.is_api_key():
            self._auth_token = self._token_manager.get_valid_token(auto_refresh=False)
            self._auth_type = "api_key"
        else:
            self._auth_token = self._token_manager.get_valid_token(
                auto_refresh=self._auto_refresh
            )
            self._auth_type = "oauth"

    def _update_auth_header(self) -> None:
        """Update the session's Authorization header."""
        self._session.headers["Authorization"] = f"Bearer {self._auth_token}"

    def _ensure_valid_token(self) -> None:
        """Ensure we have a valid token, refreshing if necessary."""
        if self._auth_type != "oauth" or not self._auto_refresh:
            return

        if self._token_manager and self._token_manager.needs_refresh():
            try:
                self._auth_token = self._token_manager.get_valid_token(auto_refresh=True)
                self._update_auth_header()
            except AuthenticationError:
                pass

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make an authenticated request with automatic token refresh.

        Parameters
        ----------
        method : str
            HTTP method (GET, POST, etc.).
        path : str
            URL path relative to base_url (e.g., '/objects'), or an absolute URL.
        **kwargs
            Passed to ``requests.Session.request()``.
        """
        self._ensure_valid_token()
        if path.startswith("http"):
            url = path
        else:
            url = f"{self.base_url}{path}"
        return self._session.request(method, url, **kwargs)

    def get(self, path: str, **kwargs) -> requests.Response:
        """Authenticated GET request."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        """Authenticated POST request."""
        return self.request("POST", path, **kwargs)

    @property
    def token_manager(self) -> Optional[TokenManager]:
        """Access the underlying TokenManager for credential inspection."""
        return self._token_manager

    @property
    def session(self) -> requests.Session:
        """Access the underlying requests.Session for direct use."""
        return self._session


def create_download_session(max_workers: int = 4) -> requests.Session:
    """Create a requests.Session with connection pooling and retry for downloads.

    Presigned R2 URLs are self-authenticating, so no auth headers are needed.
    The pool is sized to match the number of workers so each thread gets a
    persistent connection without contention.
    """
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=max_workers,
        pool_maxsize=max_workers,
    )
    session.mount("https://", adapter)
    return session
