"""
OAuth 2.0 Device Authorization Grant implementation for CAMPFIRE.

This module implements RFC 8628 device flow for CLI authentication.
"""

import time
import webbrowser
from dataclasses import dataclass
from typing import Optional

import requests

from ..exceptions import AuthenticationError


@dataclass
class DeviceCodeResponse:
    """Response from device authorization initiation."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


@dataclass
class TokenResponse:
    """Response from successful token exchange."""

    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str


class DeviceFlowAuth:
    """
    Handle OAuth 2.0 Device Authorization Grant flow.

    This implements the RFC 8628 device flow, which is ideal for CLI applications
    where browser-based authentication is preferred but the CLI cannot receive
    redirects.

    Examples
    --------
    >>> flow = DeviceFlowAuth("https://campfire.hollisakins.com/api/v1")
    >>> device = flow.initiate()
    >>> print(f"Enter code {device.user_code} at {device.verification_uri}")
    >>> tokens = flow.poll_for_token(device.device_code)
    """

    def __init__(self, base_url: str):
        """
        Initialize the device flow handler.

        Parameters
        ----------
        base_url : str
            Base URL for the CAMPFIRE API (e.g., "https://campfire.hollisakins.com/api/v1").
        """
        self.base_url = base_url.rstrip("/")
        self.device_endpoint = f"{self.base_url}/auth/device"
        self.token_endpoint = f"{self.base_url}/auth/device/token"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "campfire-python/0.1.0"})

    def initiate(self) -> DeviceCodeResponse:
        """
        Start the device authorization flow.

        Returns
        -------
        DeviceCodeResponse
            Contains device_code, user_code, and verification URIs.

        Raises
        ------
        AuthenticationError
            If the request fails.
        """
        try:
            response = self.session.post(self.device_endpoint)
            response.raise_for_status()
            data = response.json()

            return DeviceCodeResponse(
                device_code=data["device_code"],
                user_code=data["user_code"],
                verification_uri=data["verification_uri"],
                verification_uri_complete=data["verification_uri_complete"],
                expires_in=data["expires_in"],
                interval=data["interval"],
            )
        except requests.RequestException as e:
            raise AuthenticationError(f"Failed to initiate device flow: {e}")

    def open_browser(self, url: str) -> bool:
        """
        Open the verification URL in the default browser.

        Parameters
        ----------
        url : str
            The verification URL to open.

        Returns
        -------
        bool
            True if browser was opened successfully, False otherwise.
        """
        try:
            webbrowser.open(url)
            return True
        except Exception:
            return False

    def poll_for_token(
        self,
        device_code: str,
        interval: int = 5,
        timeout: int = 900,
        on_pending: Optional[callable] = None,
    ) -> TokenResponse:
        """
        Poll the token endpoint until authorization completes.

        Parameters
        ----------
        device_code : str
            The device code from initiate().
        interval : int, optional
            Initial polling interval in seconds (default: 5).
        timeout : int, optional
            Maximum time to wait in seconds (default: 900 = 15 minutes).
        on_pending : callable, optional
            Callback function called on each pending response.
            Useful for updating progress indicators.

        Returns
        -------
        TokenResponse
            Access and refresh tokens.

        Raises
        ------
        AuthenticationError
            If authorization fails, times out, or is denied.
        """
        start_time = time.time()
        current_interval = interval

        while time.time() - start_time < timeout:
            time.sleep(current_interval)

            try:
                response = self.session.post(
                    self.token_endpoint,
                    json={
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                        "device_code": device_code,
                    },
                )

                data = response.json()

                if response.status_code == 200:
                    # Success!
                    return TokenResponse(
                        access_token=data["access_token"],
                        refresh_token=data["refresh_token"],
                        expires_in=data["expires_in"],
                        token_type=data.get("token_type", "Bearer"),
                    )

                # Handle error responses
                error = data.get("error")

                if error == "authorization_pending":
                    # User hasn't authorized yet, keep polling
                    if on_pending:
                        on_pending()
                    continue

                elif error == "slow_down":
                    # Increase polling interval
                    current_interval = data.get("interval", current_interval + 5)
                    continue

                elif error == "expired_token":
                    raise AuthenticationError(
                        "Authorization timed out. Please run 'campfire login' again."
                    )

                elif error == "access_denied":
                    raise AuthenticationError("Authorization was denied by the user.")

                else:
                    # Unknown error
                    error_desc = data.get("error_description", error or "Unknown error")
                    raise AuthenticationError(f"Authorization failed: {error_desc}")

            except requests.RequestException as e:
                # Network error - continue polling with backoff
                current_interval = min(current_interval * 2, 30)
                continue

        # Timeout
        raise AuthenticationError(
            "Authorization timed out after 15 minutes. Please try again."
        )


def run_device_flow(
    base_url: str,
    open_browser: bool = True,
    show_progress: bool = True,
) -> TokenResponse:
    """
    Run the complete device authorization flow.

    This is a convenience function that handles the entire flow including
    browser opening and progress display.

    Parameters
    ----------
    base_url : str
        Base URL for the CAMPFIRE API.
    open_browser : bool, optional
        Whether to automatically open the browser (default: True).
    show_progress : bool, optional
        Whether to show progress messages (default: True).

    Returns
    -------
    TokenResponse
        Access and refresh tokens.
    """
    flow = DeviceFlowAuth(base_url)

    # Initiate flow
    device = flow.initiate()

    if show_progress:
        print(f"\nOpening browser to: {device.verification_uri}")
        print(f"Enter code: {device.user_code}")
        print("\nWaiting for authorization...", end="", flush=True)

    # Open browser
    if open_browser:
        flow.open_browser(device.verification_uri_complete)

    # Poll for token with progress indicator
    def on_pending():
        if show_progress:
            print(".", end="", flush=True)

    tokens = flow.poll_for_token(
        device.device_code,
        interval=device.interval,
        timeout=device.expires_in,
        on_pending=on_pending,
    )

    if show_progress:
        print(" done!")

    return tokens
