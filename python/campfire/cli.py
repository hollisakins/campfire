"""
CAMPFIRE CLI commands.

Provides commands for authentication, data sync, and observation management:
- campfire login: Authenticate with CAMPFIRE
- campfire logout: Remove stored credentials
- campfire whoami: Show current authenticated user
- campfire status: Check credentials and sync status
- campfire observations: List available observations
- campfire add: Track an observation for syncing
- campfire remove: Stop tracking an observation
- campfire sync: Download/update tracked observations
"""

import json as json_mod
import shutil
import sys
from pathlib import Path
from typing import Optional, Tuple

import click
import requests

from .auth.credentials import CredentialManager
from .auth.device_flow import run_device_flow
from .auth.tokens import TokenManager
from .exceptions import AuthenticationError

# Default API URL
DEFAULT_BASE_URL = "https://campfire.hollisakins.com/api/v1"


def get_base_url() -> str:
    """Get the API base URL from environment, config, or default."""
    import os

    env_url = os.environ.get("CAMPFIRE_API_URL")
    if env_url:
        return env_url

    from .config import Config
    config = Config()
    if config.exists() and config.base_url:
        return config.base_url

    return DEFAULT_BASE_URL


def _require_auth(base_url: str) -> Tuple[TokenManager, str]:
    """Verify credentials and return (token_manager, valid_token). Exits on failure."""
    try:
        tm = TokenManager(base_url)
        if not tm.has_credentials():
            click.echo("✗ Not logged in. Run: campfire login")
            sys.exit(1)
        token = tm.get_valid_token(auto_refresh=True)
        return tm, token
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        click.echo("  Run: campfire login")
        sys.exit(1)


def _prompt_data_dir() -> None:
    """First-time setup: prompt for data directory and save config."""
    from .config import Config

    config = Config()
    if config.exists():
        return

    click.echo()
    default = str(config.data_dir)
    data_dir = click.prompt(
        "Where should CAMPFIRE store downloaded data?",
        default=default,
    )
    config.data_dir = Path(data_dir).expanduser()
    config.ensure_data_dir()
    click.echo(f"  Data directory set to: {config.data_dir}")
    click.echo(f"  Config saved to: {config.config_path}")


@click.group()
@click.version_option(version="0.1.0", prog_name="campfire")
def cli():
    """CAMPFIRE - query, download, and sync NIRSpec spectroscopic data."""
    pass


# ---------------------------------------------------------------------------
# Authentication commands
# ---------------------------------------------------------------------------


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

    # Save the base URL so subsequent commands use the same server
    from .config import Config
    config = Config()
    config.base_url = base_url

    # First-time config setup
    _prompt_data_dir()


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
    """Check credentials and sync status."""
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

    # Show sync status if config exists
    from .config import Config

    config = Config()
    if not config.exists():
        return

    click.echo()
    click.echo(f"Data directory: {config.data_dir}")

    tracked = config.tracked_observations
    if not tracked:
        click.echo("\nNo tracked observations. Run: campfire observations")
        return

    from .state import SyncState
    from .sync import format_size

    state = SyncState(config.data_dir / ".campfire_meta" / "sync_state.db")

    click.echo()
    click.echo("Tracked observations:")
    click.echo(f"  {'OBSERVATION':<25} {'SYNCED':<12} {'SIZE':<12} {'LAST SYNC'}")

    for obs in tracked:
        stats = state.get_observation_stats(obs)
        last = state.get_last_sync(obs)
        synced = stats["synced_count"]
        size = format_size(stats["total_bytes"])
        last_str = last[:16].replace("T", " ") if last else "never"
        click.echo(f"  {obs:<25} {synced:<12} {size:<12} {last_str}")

    state.close()

    # Catalog info
    meta_dir = config.data_dir / ".campfire_meta"
    objects_csv = meta_dir / "objects.csv"
    spectra_csv = meta_dir / "spectra.csv"
    if objects_csv.exists() and spectra_csv.exists():
        obj_count = sum(1 for _ in open(objects_csv)) - 1  # minus header
        spec_count = sum(1 for _ in open(spectra_csv)) - 1
        click.echo(f"\nCatalog: objects.csv ({obj_count} objects), spectra.csv ({spec_count} spectra)")

    # Disk usage
    if config.data_dir.exists():
        total = sum(f.stat().st_size for f in config.data_dir.rglob("*") if f.is_file())
        click.echo(f"Disk usage: {format_size(total)}")


# ---------------------------------------------------------------------------
# Observation management commands
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--tracked", is_flag=True, help="Only show tracked observations")
@click.option("--json", "json_out", is_flag=True, help="JSON output for scripting")
@click.option("--base-url", default=None, help="API base URL")
def observations(tracked: bool, json_out: bool, base_url: Optional[str]):
    """List available observations with stats."""
    base_url = base_url or get_base_url()
    _, token = _require_auth(base_url)

    try:
        response = requests.get(
            f"{base_url}/observations",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if response.status_code == 401:
            click.echo("✗ Authentication failed. Run: campfire login", err=True)
            sys.exit(1)
        response.raise_for_status()
    except requests.RequestException as e:
        click.echo(f"✗ Failed to fetch observations: {e}", err=True)
        sys.exit(1)

    obs_list = response.json().get("observations", [])

    from .config import Config
    from .sync import format_size

    config = Config()
    tracked_set = set(config.tracked_observations) if config.exists() else set()

    if tracked:
        obs_list = [o for o in obs_list if o["observation"] in tracked_set]

    if json_out:
        click.echo(json_mod.dumps(obs_list, indent=2))
        return

    if not obs_list:
        click.echo("No observations available.")
        return

    # Determine tracking/sync status for each observation
    from .state import SyncState

    state = None
    if config.exists() and config.data_dir.exists():
        db_path = config.data_dir / ".campfire_meta" / "sync_state.db"
        if db_path.exists():
            state = SyncState(db_path)

    click.echo()
    click.echo(f"  {'OBSERVATION':<25} {'PROGRAM':<12} {'FIELD':<10} {'OBJECTS':>8} {'SPECTRA':>8} {'SIZE':>10}   STATUS")
    for obs in obs_list:
        name = obs["observation"]
        prog = obs.get("program_name", "")
        field = obs.get("field", "")
        n_obj = obs.get("object_count", 0)
        n_spec = obs.get("spectrum_count", 0)
        size = format_size(obs.get("total_size_bytes", 0))

        if name in tracked_set:
            if state:
                local_stats = state.get_observation_stats(name)
                synced = local_stats["synced_count"]
                if synced >= n_spec and n_spec > 0:
                    status_str = "tracked (synced)"
                elif synced > 0:
                    diff = n_spec - synced
                    status_str = f"tracked ({diff} new)"
                else:
                    status_str = "tracked (not synced)"
            else:
                status_str = "tracked"
        else:
            status_str = "not tracked"

        click.echo(f"  {name:<25} {prog:<12} {field:<10} {n_obj:>8} {n_spec:>8} {size:>10}   {status_str}")

    if state:
        state.close()


@cli.command()
@click.argument("obs_names", nargs=-1)
@click.option("--all", "add_all", is_flag=True, help="Track all available observations")
@click.option("--base-url", default=None, help="API base URL")
def add(obs_names: tuple, add_all: bool, base_url: Optional[str]):
    """Track observations for syncing.

    Pass one or more observation names, or use --all to track everything.
    """
    if not obs_names and not add_all:
        click.echo("Usage: campfire add <obs_name> [obs_name ...] or campfire add --all", err=True)
        sys.exit(1)

    explicit_base_url = base_url is not None
    base_url = base_url or get_base_url()
    _, token = _require_auth(base_url)

    # Fetch available observations
    try:
        response = requests.get(
            f"{base_url}/observations",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as e:
        click.echo(f"✗ Failed to fetch observations: {e}", err=True)
        sys.exit(1)

    obs_list = response.json().get("observations", [])
    obs_by_name = {o["observation"]: o for o in obs_list}

    from .config import Config
    from .sync import format_size

    config = Config()

    # Persist the base URL if explicitly provided via --base-url
    if explicit_base_url:
        config.base_url = base_url

    if add_all:
        to_add = [o for o in obs_list if o["observation"] not in config.tracked_observations]
        if not to_add:
            click.echo("Already tracking all available observations.")
            return
    else:
        # Validate all requested names
        to_add = []
        for name in obs_names:
            if name in config.tracked_observations:
                click.echo(f"  Already tracking: {name}")
                continue
            if name not in obs_by_name:
                click.echo(f"✗ Observation '{name}' not found or you don't have access", err=True)
                available = list(obs_by_name.keys())
                if available:
                    click.echo(f"  Available: {', '.join(available)}")
                sys.exit(1)
            to_add.append(obs_by_name[name])

        if not to_add:
            return

    config.ensure_data_dir()
    total_obj = 0
    total_spec = 0
    total_bytes = 0

    for obs_info in to_add:
        name = obs_info["observation"]
        config.add_observation(name)
        n_obj = obs_info.get("object_count", 0)
        n_spec = obs_info.get("spectrum_count", 0)
        size_bytes = obs_info.get("total_size_bytes", 0)
        total_obj += n_obj
        total_spec += n_spec
        total_bytes += size_bytes
        click.echo(f"  + {name} ({n_obj} objects, {n_spec} spectra, {format_size(size_bytes)})")

    click.echo(f"\n✓ Now tracking {len(to_add)} observation(s) ({total_obj} objects, {total_spec} spectra, {format_size(total_bytes)})")
    click.echo(f"  Run 'campfire sync' to download.")


@cli.command()
@click.argument("obs_name")
@click.option("--delete", is_flag=True, help="Also delete local files")
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.option("--base-url", default=None, help="API base URL")
def remove(obs_name: str, delete: bool, yes: bool, base_url: Optional[str]):
    """Stop tracking an observation."""
    from .config import Config
    from .sync import format_size

    config = Config()

    if obs_name not in config.tracked_observations:
        click.echo(f"Not tracking: {obs_name}")
        return

    # Check local state
    from .state import SyncState

    state = SyncState(config.data_dir / ".campfire_meta" / "sync_state.db")
    stats = state.get_observation_stats(obs_name)
    synced = stats["synced_count"]

    if not yes:
        click.echo(f"\nRemove {obs_name} from tracking?")
        if synced > 0 and not delete:
            click.echo(f"  {synced} local files will be kept.")
        if delete and synced > 0:
            click.echo(f"  Also delete {synced} local files ({format_size(stats['total_bytes'])})?")
        if not click.confirm("Proceed?"):
            return

    config.remove_observation(obs_name)
    state.remove_observation(obs_name)
    state.close()

    if delete:
        obs_dir = config.data_dir / obs_name
        if obs_dir.exists():
            shutil.rmtree(obs_dir)
            click.echo(f"✓ Stopped tracking: {obs_name} (local files deleted)")
        else:
            click.echo(f"✓ Stopped tracking: {obs_name}")
    else:
        click.echo(f"✓ Stopped tracking: {obs_name}")
        if synced > 0:
            click.echo(f"  Local files kept in: {config.data_dir / obs_name}/")


# ---------------------------------------------------------------------------
# Sync command
# ---------------------------------------------------------------------------


@cli.command(name="sync")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
@click.option("--workers", default=10, help="Parallel download workers")
@click.option("--observation", "obs_filter", multiple=True, help="Only sync specific observation(s)")
@click.option("--dry-run", is_flag=True, help="Show plan without downloading")
@click.option("--base-url", default=None, help="API base URL")
def sync_cmd(yes: bool, workers: int, obs_filter: tuple, dry_run: bool, base_url: Optional[str]):
    """Download and update tracked observations."""
    base_url = base_url or get_base_url()

    from .config import Config
    from .state import SyncState
    from .sync import (
        create_authenticated_session,
        refresh_session_token,
        sync_observation,
        format_size,
    )
    from .catalog import generate_catalogs

    config = Config()
    if not config.exists():
        click.echo("✗ Not configured. Run: campfire login")
        sys.exit(1)

    tracked = config.tracked_observations
    if not tracked:
        click.echo("No tracked observations. Run: campfire add <obs_name>")
        sys.exit(1)

    # Filter to specific observations if requested
    if obs_filter:
        tracked = [o for o in tracked if o in obs_filter]
        missing = [o for o in obs_filter if o not in config.tracked_observations]
        for m in missing:
            click.echo(f"Warning: '{m}' is not tracked, skipping")
        if not tracked:
            click.echo("No matching tracked observations.")
            sys.exit(1)

    config.ensure_data_dir()
    state = SyncState(config.data_dir / ".campfire_meta" / "sync_state.db")

    # Create authenticated session
    try:
        session = create_authenticated_session(base_url)
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)

    # Gather download plans for all observations
    click.echo("Checking tracked observations...")
    plans = []
    total_download = 0
    total_files = 0

    for obs in tracked:
        try:
            from .sync import fetch_manifest, compute_download_plan

            manifest = fetch_manifest(session, base_url, obs)
            synced = state.get_synced_files(obs)
            new_files, updated_files, up_to_date = compute_download_plan(manifest, synced)
            to_download = new_files + updated_files
            download_bytes = sum(s.get("file_size") or 0 for s in to_download)

            if not to_download:
                click.echo(f"  {obs}: {len(up_to_date)} files synced, up to date")
            else:
                parts = []
                if new_files:
                    parts.append(f"{len(new_files)} new")
                if updated_files:
                    parts.append(f"{len(updated_files)} updated")
                click.echo(f"  {obs}: {', '.join(parts)} ({format_size(download_bytes)})")

            plans.append({
                "observation": obs,
                "manifest": manifest,
                "to_download": to_download,
                "up_to_date": up_to_date,
                "download_bytes": download_bytes,
            })
            total_download += download_bytes
            total_files += len(to_download)
        except Exception as e:
            click.echo(f"  {obs}: ✗ {e}")

    if total_files == 0:
        click.echo("\nAll observations up to date.")
        state.close()
        return

    if dry_run:
        click.echo(f"\nDry run: would download {total_files} files ({format_size(total_download)})")
        state.close()
        return

    # Confirm
    if not yes:
        click.echo(f"\nDownload {total_files} files ({format_size(total_download)})?")
        if not click.confirm("Proceed?", default=True):
            state.close()
            return

    # Execute sync for each observation
    click.echo()
    all_stats = []
    for plan in plans:
        obs = plan["observation"]
        if not plan["to_download"]:
            continue

        # Refresh token between observations for long syncs
        refresh_session_token(session, base_url)

        try:
            stats = sync_observation(
                session, base_url, obs,
                config.data_dir, state,
                max_workers=workers,
            )
            all_stats.append(stats)
        except Exception as e:
            click.echo(f"✗ Failed to sync {obs}: {e}")

    # Regenerate catalogs
    click.echo("\nUpdating catalog...")
    try:
        # Fetch full object data for all tracked observations
        all_objects = []
        for obs in config.tracked_observations:
            refresh_session_token(session, base_url)
            offset = 0
            while True:
                resp = session.get(
                    f"{base_url}/objects",
                    params={"observations": obs, "limit": 1000, "offset": offset},
                    timeout=60,
                )
                if resp.status_code != 200:
                    break
                data = resp.json()
                objects = data.get("data", [])
                all_objects.extend(objects)
                pagination = data.get("pagination", {})
                total = pagination.get("total", 0)
                offset += len(objects)
                if offset >= total or not objects:
                    break

        obj_count, spec_count = generate_catalogs(all_objects, config.data_dir)
        click.echo(f"  Catalog updated: objects.csv ({obj_count} objects), spectra.csv ({spec_count} spectra)")
    except Exception as e:
        click.echo(f"  Warning: Failed to update catalog: {e}")

    # Print summary
    total_downloaded = sum(s.get("downloaded", 0) for s in all_stats)
    total_failed = sum(s.get("failed", 0) for s in all_stats)
    click.echo(f"\n✓ Sync complete")
    click.echo(f"  Files downloaded: {total_downloaded}")
    if total_failed:
        click.echo(f"  Files failed: {total_failed}")
    click.echo(f"  Total size: {format_size(total_download)}")

    state.close()


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
