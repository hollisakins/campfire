"""
CAMPFIRE authentication CLI commands.

Provides minimal CLI commands for authentication:
- campfire login: Authenticate with CAMPFIRE
- campfire logout: Remove stored credentials
- campfire whoami: Show current authenticated user
- campfire status: Check if credentials are valid
"""

import sys
from typing import Optional

import click
import requests

from .auth.credentials import CredentialManager
from .auth.device_flow import run_device_flow
from .auth.tokens import TokenManager
from .exceptions import AuthenticationError

# Default API URL
DEFAULT_BASE_URL = "https://campfire.hollisakins.com/api/v1"


def get_base_url() -> str:
    """Get the API base URL from environment or default."""
    import os

    return os.environ.get("CAMPFIRE_API_URL", DEFAULT_BASE_URL)


@click.group()
@click.version_option(version="0.1.0", prog_name="campfire")
def cli():
    """CAMPFIRE authentication commands."""
    pass


@cli.command()
@click.option(
    "--browser/--api-key",
    default=None,
    help="Authentication method: --browser for device flow, --api-key for manual paste",
)
@click.option(
    "--base-url",
    default=None,
    help="API base URL (default: production)",
)
def login(browser: Optional[bool], base_url: Optional[str]):
    """
    Authenticate with CAMPFIRE.

    By default, opens a browser for secure authentication.
    Use --api-key for headless environments.
    """
    base_url = base_url or get_base_url()
    creds = CredentialManager()

    # Check if already logged in
    if creds.exists():
        existing = creds.load()
        if existing:
            if existing.is_oauth() and existing.user_email:
                click.echo(f"Already logged in as {existing.user_email}")
            elif existing.is_api_key():
                click.echo("Already authenticated with API key")

            if not click.confirm("Do you want to re-authenticate?"):
                return

    # Determine authentication method
    if browser is None:
        # Interactive choice
        click.echo("\nHow would you like to authenticate?")
        click.echo("  1. Login with web browser (recommended)")
        click.echo("  2. Paste an API key")
        choice = click.prompt("Choice", type=click.IntRange(1, 2), default=1)
        browser = choice == 1

    if browser:
        _browser_login(base_url, creds)
    else:
        _api_key_login(base_url, creds)


def _browser_login(base_url: str, creds: CredentialManager):
    """Handle browser-based OAuth flow."""
    click.echo("\nStarting browser authentication...")

    try:
        tokens = run_device_flow(base_url, open_browser=True, show_progress=True)

        # Get user email from whoami endpoint
        user_email = _get_user_email(base_url, tokens.access_token)

        # Save credentials
        creds.save_oauth(
            tokens.access_token,
            tokens.refresh_token,
            tokens.expires_in,
            user_email,
        )

        click.echo(f"\n✓ Logged in successfully!")
        if user_email:
            click.echo(f"  Authenticated as: {user_email}")
        click.echo(f"  Credentials saved to: ~/.campfire/credentials")

    except AuthenticationError as e:
        click.echo(f"\n✗ Authentication failed: {e}", err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\n\nAuthentication cancelled.")
        sys.exit(1)


def _api_key_login(base_url: str, creds: CredentialManager):
    """Handle manual API key entry."""
    click.echo("\nGenerate an API key at:")
    click.echo(f"  {base_url.replace('/api/v1', '')}/profile/api-keys")
    click.echo()

    api_key = click.prompt("Paste your API key", hide_input=True)

    if not api_key:
        click.echo("✗ No API key provided", err=True)
        sys.exit(1)

    if not api_key.startswith("sk_"):
        click.echo("✗ Invalid API key format (should start with 'sk_')", err=True)
        sys.exit(1)

    # Validate the key by making a test request
    click.echo("Validating...", nl=False)

    try:
        response = requests.get(
            f"{base_url}/auth/whoami",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )

        if response.status_code == 401:
            click.echo(" ✗")
            click.echo("Invalid API key", err=True)
            sys.exit(1)

        response.raise_for_status()
        click.echo(" ✓")

        # Save credentials
        creds.save_api_key(api_key)

        click.echo(f"\n✓ API key saved successfully!")
        click.echo(f"  Credentials saved to: ~/.campfire/credentials")

    except requests.RequestException as e:
        click.echo(" ✗")
        click.echo(f"Failed to validate API key: {e}", err=True)
        sys.exit(1)


def _get_user_email(base_url: str, access_token: str) -> Optional[str]:
    """Fetch user email from whoami endpoint."""
    try:
        response = requests.get(
            f"{base_url}/auth/whoami",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("email")
    except requests.RequestException:
        pass
    return None


@cli.command()
def logout():
    """Remove stored credentials."""
    creds = CredentialManager()

    if not creds.exists():
        click.echo("Not logged in.")
        return

    creds.delete()
    click.echo("✓ Logged out successfully")


@cli.command()
@click.option("--base-url", default=None, help="API base URL")
def whoami(base_url: Optional[str]):
    """Show current authenticated user."""
    base_url = base_url or get_base_url()
    creds = CredentialManager()

    if not creds.exists():
        click.echo("Not logged in. Run: campfire login")
        sys.exit(1)

    loaded = creds.load()
    if not loaded:
        click.echo("Invalid credentials. Run: campfire login")
        sys.exit(1)

    if loaded.is_api_key():
        # For API keys, we need to fetch user info
        click.echo("Authentication: API key")
        if loaded.api_key:
            click.echo(f"Key prefix: {loaded.api_key[:20]}...")

        # Try to get user info
        try:
            response = requests.get(
                f"{base_url}/auth/whoami",
                headers={"Authorization": f"Bearer {loaded.api_key}"},
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                click.echo(f"Email: {data.get('email', 'unknown')}")
        except requests.RequestException:
            pass

    elif loaded.is_oauth():
        click.echo("Authentication: OAuth (device flow)")
        if loaded.user_email:
            click.echo(f"Email: {loaded.user_email}")
        if loaded.expires_at:
            click.echo(f"Token expires: {loaded.expires_at}")


@cli.command()
@click.option("--base-url", default=None, help="API base URL")
def status(base_url: Optional[str]):
    """Check if credentials are valid."""
    base_url = base_url or get_base_url()

    try:
        token_manager = TokenManager(base_url)

        if not token_manager.has_credentials():
            click.echo("✗ No credentials found")
            click.echo("  Run: campfire login")
            sys.exit(1)

        # Try to get a valid token (will refresh if needed)
        token = token_manager.get_valid_token(auto_refresh=True)

        # Verify the token works
        response = requests.get(
            f"{base_url}/auth/whoami",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        if response.status_code == 200:
            click.echo("✓ Credentials valid")
            data = response.json()
            if data.get("email"):
                click.echo(f"  User: {data['email']}")
        else:
            click.echo("✗ Credentials invalid or expired")
            click.echo("  Run: campfire login")
            sys.exit(1)

    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        click.echo("  Run: campfire login")
        sys.exit(1)
    except requests.RequestException as e:
        click.echo(f"✗ Failed to verify credentials: {e}")
        sys.exit(1)


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
