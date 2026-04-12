"""
CAMPFIRE CLI commands.

Provides commands for authentication, catalog sync, and FITS downloading:
- campfire login: Authenticate with CAMPFIRE
- campfire logout: Remove stored credentials
- campfire whoami: Show current authenticated user
- campfire status: Check credentials, catalog, and download status
- campfire observations: List available observations
- campfire sync: Sync the full object catalog (metadata only)
- campfire download: Download FITS spectrum files
"""

import shutil
import sys
from pathlib import Path
from typing import Optional

import click
import requests

from .api.session import APISession, resolve_base_url
from .api.client import APIClient
from .auth.credentials import CredentialManager
from .auth.device_flow import run_device_flow
from .auth.tokens import TokenManager
from .exceptions import AuthenticationError
from .output import (
    console,
    make_table,
    print_error,
    print_msg,
    print_success,
    print_warning,
)


def _require_auth(base_url: str) -> APISession:
    """Verify credentials and return an APISession. Exits on failure."""
    try:
        return APISession(base_url=base_url)
    except AuthenticationError as e:
        print_error(str(e))
        print_msg("  Run: campfire login")
        sys.exit(1)


def _open_store():
    """Open the LocalStore, creating it if needed.

    If the on-disk schema is outdated, prompts the user to delete and
    recreate the database.
    """
    from .db.store import LocalStore, SchemaMismatchError
    from .config import ensure_data_dir, meta_dir

    ensure_data_dir()
    db_path = meta_dir() / "campfire.db"
    try:
        return LocalStore(db_path)
    except SchemaMismatchError as e:
        print_warning(str(e))
        print_msg("   The database must be recreated to match the current client version.")
        if click.confirm("   Delete and rebuild on next sync?", default=True):
            db_path.unlink(missing_ok=True)
            # Also remove WAL/SHM files left by SQLite
            db_path.with_suffix(".db-wal").unlink(missing_ok=True)
            db_path.with_suffix(".db-shm").unlink(missing_ok=True)
            print_msg("   Deleted. Creating fresh database…")
            return LocalStore(db_path)
        else:
            print_msg("   Aborting.")
            sys.exit(1)


def _check_client_version(base_url: str) -> None:
    """Check if a newer client version is available. Never raises."""
    from . import __version__
    from packaging.version import Version

    try:
        resp = requests.get(f"{base_url}/version", timeout=5)
        resp.raise_for_status()
        data = resp.json()

        current = Version(__version__)
        latest = Version(data.get("latest", __version__))
        minimum = Version(data.get("minimum", "0.0.0"))

        if current < minimum:
            print_warning(f"campfire v{__version__} is no longer supported "
                          f"(minimum: v{minimum}). Please update:")
            print_msg("  pip install -U git+https://github.com/hollisakins/campfire.git#subdirectory=python")
        elif current < latest:
            print_msg(f"\n  Update available: v{__version__} → v{latest}")
            print_msg("  pip install -U git+https://github.com/hollisakins/campfire.git#subdirectory=python")
    except Exception:
        pass  # Never block sync for a version check failure


@click.group(invoke_without_command=True)
@click.version_option(version="0.3.0", prog_name="campfire")
@click.pass_context
def cli(ctx):
    """CAMPFIRE — Python client and deployment tools for NIRSpec spectroscopic data."""
    if ctx.invoked_subcommand is None:
        if sys.stdout.isatty():
            from .tui import launch_tui
            launch_tui()
        else:
            click.echo(ctx.get_help())


def _register_deploy_group():
    """Register deploy subgroup. Imported lazily to avoid loading deploy deps for non-deploy commands."""
    try:
        from campfire.deploy.cli import deploy_group
        cli.add_command(deploy_group, name='deploy')
    except ImportError:
        @cli.command('deploy', hidden=False)
        def deploy_stub():
            """Deploy CAMPFIRE pipeline products to Supabase + R2. (Requires: pip install campfire[deploy])"""
            click.echo("Deploy dependencies not installed. Run: pip install campfire[deploy]")
            sys.exit(1)


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
    base_url = base_url or resolve_base_url()
    creds = CredentialManager()

    # Check if already logged in
    if creds.exists():
        existing = creds.load()
        if existing:
            if existing.is_oauth() and existing.user_email:
                print_msg(f"Already logged in as {existing.user_email}")
            elif existing.is_api_key():
                print_msg("Already authenticated with API key")

            if not click.confirm("Do you want to re-authenticate?"):
                return

    # Determine authentication method
    if browser is None:
        print_msg("\nHow would you like to authenticate?")
        print_msg("  1. Login with web browser (recommended)")
        print_msg("  2. Paste an API key")
        choice = click.prompt("Choice", type=click.IntRange(1, 2), default=1)
        browser = choice == 1

    if browser:
        _browser_login(base_url, creds)
    else:
        _api_key_login(base_url, creds)


def _browser_login(base_url: str, creds: CredentialManager):
    """Handle browser-based OAuth flow."""
    print_msg("\nStarting browser authentication...")

    try:
        tokens = run_device_flow(base_url, open_browser=True, show_progress=True)
        user_email = _get_user_email(base_url, tokens.access_token)

        creds.save_oauth(
            tokens.access_token,
            tokens.refresh_token,
            tokens.expires_in,
            user_email,
            supabase_token=tokens.supabase_token,
            supabase_url=tokens.supabase_url,
            supabase_anon_key=tokens.supabase_anon_key,
        )

        print_success("Logged in successfully!")
        if user_email:
            print_msg(f"  Authenticated as: {user_email}")
        print_msg("  Credentials saved to: ~/.campfire/credentials")

    except AuthenticationError as e:
        print_error(f"Authentication failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print_msg("\n\nAuthentication cancelled.")
        sys.exit(1)


def _api_key_login(base_url: str, creds: CredentialManager):
    """Handle manual API key entry."""
    print_msg("\nGenerate an API key at:")
    print_msg(f"  {base_url.replace('/api/v1', '')}/profile/api-keys")
    print_msg()

    api_key = click.prompt("Paste your API key", hide_input=True)

    if not api_key:
        print_error("No API key provided")
        sys.exit(1)

    if not api_key.startswith("sk_"):
        print_error("Invalid API key format (should start with 'sk_')")
        sys.exit(1)

    with console.status("Validating..."):
        try:
            response = requests.get(
                f"{base_url}/auth/whoami",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )

            if response.status_code == 401:
                print_error("Invalid API key")
                sys.exit(1)

            response.raise_for_status()

        except requests.RequestException as e:
            print_error(f"Failed to validate API key: {e}")
            sys.exit(1)

    creds.save_api_key(api_key)

    print_success("API key saved successfully!")
    print_msg("  Credentials saved to: ~/.campfire/credentials")


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
@click.option("--base-url", default=None, help="API base URL")
def logout(base_url: Optional[str]):
    """Remove stored credentials and revoke server session."""
    base_url = base_url or resolve_base_url()
    creds = CredentialManager()

    if not creds.exists():
        print_msg("Not logged in.")
        return

    # Revoke server-side refresh token before deleting local credentials
    loaded = creds.load()
    if loaded and loaded.type == "oauth" and loaded.refresh_token:
        try:
            requests.post(
                f"{base_url}/auth/revoke",
                json={"token": loaded.refresh_token},
                timeout=5,
            )
        except Exception:
            pass  # Best-effort; still delete local creds

    creds.delete()
    print_success("Logged out successfully")


@cli.command()
@click.option("--base-url", default=None, help="API base URL")
def whoami(base_url: Optional[str]):
    """Show current authenticated user."""
    base_url = base_url or resolve_base_url()
    creds = CredentialManager()

    if not creds.exists():
        print_msg("Not logged in. Run: campfire login")
        sys.exit(1)

    loaded = creds.load()
    if not loaded:
        print_msg("Invalid credentials. Run: campfire login")
        sys.exit(1)

    if loaded.is_api_key():
        print_msg("Authentication: API key")
        if loaded.api_key:
            print_msg(f"Key prefix: {loaded.api_key[:20]}...")

        try:
            response = requests.get(
                f"{base_url}/auth/whoami",
                headers={"Authorization": f"Bearer {loaded.api_key}"},
                timeout=10,
            )
            if response.status_code == 200:
                data = response.json()
                print_msg(f"Email: {data.get('email', 'unknown')}")
        except requests.RequestException:
            pass

    elif loaded.is_oauth():
        print_msg("Authentication: OAuth (device flow)")
        if loaded.user_email:
            print_msg(f"Email: {loaded.user_email}")
        if loaded.expires_at:
            print_msg(f"Token expires: {loaded.expires_at}")


@cli.command()
@click.option("--base-url", default=None, help="API base URL")
def status(base_url: Optional[str]):
    """Check credentials, catalog, and download status."""
    base_url = base_url or resolve_base_url()

    try:
        token_manager = TokenManager(base_url)

        if not token_manager.has_credentials():
            print_error("No credentials found")
            print_msg("  Run: campfire login")
            sys.exit(1)

        token = token_manager.get_valid_token(auto_refresh=True)

        response = requests.get(
            f"{base_url}/auth/whoami",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )

        if response.status_code == 200:
            print_success("Credentials valid")
            data = response.json()
            if data.get("email"):
                print_msg(f"  User: {data['email']}")
        else:
            print_error("Credentials invalid or expired")
            print_msg("  Run: campfire login")
            sys.exit(1)

    except AuthenticationError as e:
        print_error(str(e))
        print_msg("  Run: campfire login")
        sys.exit(1)
    except requests.RequestException as e:
        print_error(f"Failed to verify credentials: {e}")
        sys.exit(1)

    # Show catalog and download status
    from .config import resolve_data_dir, meta_dir as _meta_dir
    from .sync import format_size

    data_dir = resolve_data_dir()
    print_msg()
    print_msg(f"Data directory: {data_dir}")

    db_path = _meta_dir(data_dir) / "campfire.db"
    if not db_path.exists():
        print_msg("\nNo local catalog. Run: campfire sync")
        return

    from .db.store import LocalStore, SchemaMismatchError

    try:
        store = LocalStore(db_path)
    except SchemaMismatchError:
        print_warning("Local catalog has an outdated schema. Run: campfire sync")
        return

    # Catalog stats
    obs_list = store.get_synced_observations()
    last_synced = store.get_last_synced_at()
    if last_synced:
        last_str = last_synced[:16].replace("T", " ")
        print_msg(f"Catalog: {len(obs_list)} observations (last synced {last_str})")
    else:
        print_msg(f"Catalog: {len(obs_list)} observations")

    # Download stats per observation
    if obs_list:
        table = make_table("OBSERVATION", "DOWNLOADED", "SIZE")

        total_downloaded = 0
        total_bytes = 0
        for obs in obs_list:
            stats = store.get_observation_stats(obs)
            downloaded = stats["synced_count"]
            size = format_size(stats["total_bytes"])
            total_downloaded += downloaded
            total_bytes += stats["total_bytes"]
            if downloaded > 0:
                table.add_row(obs, str(downloaded), size)

        if total_downloaded == 0:
            print_msg("  No FITS files downloaded yet. Run: campfire download --obs <name>")
        else:
            console.print(table)

    # Stale files
    stale = store.get_stale_files()
    if stale:
        print_warning(f"{len(stale)} local file(s) updated on server. Run: campfire download --stale")

    # Disk usage
    if data_dir.exists():
        total = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
        print_msg(f"\nDisk usage: {format_size(total)}")

    store.close()


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Sync command (metadata only)
# ---------------------------------------------------------------------------


@cli.command(name="sync")
@click.option("--full", is_flag=True, help="Force full sync (skip incremental)")
@click.option("--base-url", default=None, help="API base URL")
def sync_cmd(full: bool, base_url: Optional[str]):
    """Sync the object catalog from the server (metadata only).

    On first run, pulls the full catalog. On subsequent runs, only
    fetches objects modified since the last sync (incremental).
    Use --full to force a complete re-sync.
    """
    base_url = base_url or resolve_base_url()

    from .config import meta_dir as _meta_dir, products_dir as _products_dir
    from .sync import sync_metadata

    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError as e:
        print_error(str(e))
        sys.exit(1)

    store = _open_store()

    try:
        is_incremental = not full and store.get_max_updated_at() is not None
        if is_incremental:
            print_msg("Syncing catalog (updating existing)...")
        else:
            print_msg("Syncing full CAMPFIRE catalog (may take a while)...")

        result = sync_metadata(
            api, store, _meta_dir(),
            show_progress=True,
            full=full,
        )

        if result.get("incremental"):
            print_success(f"Incremental sync complete: {result['observations']} observations, "
                          f"{result['targets']} targets, {result['spectra']} spectra, "
                          f"{result['sky_objects']} sky objects updated.")
        else:
            print_success(f"Full sync complete: {result['observations']} observations, "
                          f"{result['targets']} targets, {result['spectra']} spectra, "
                          f"{result['sky_objects']} sky objects.")

        if result.get("purged_objects") or result.get("purged_spectra"):
            print_msg(f"  Removed {result['purged_objects']} targets and "
                      f"{result['purged_spectra']} spectra deleted from server.")
        if result.get("sky_objects_purged"):
            print_msg(f"  Removed {result['sky_objects_purged']} sky objects deleted from server.")

        # Verify local files so status reports correct counts immediately
        pd = _products_dir()
        if pd.exists():
            verify = store.verify_local_files(pd, show_progress=True)
            if verify["cleared"]:
                print_msg(f"  Detected {verify['cleared']} missing local file(s).")
            if verify["rehashed"]:
                print_msg(f"  Re-verified {verify['rehashed']} modified local file(s).")
            if verify["discovered"]:
                print_msg(f"  Found {verify['discovered']} existing local file(s).")

        if result["stale_count"] > 0:
            print_warning(f"{result['stale_count']} local file(s) have been updated on the server.")
            print_msg("  Run: campfire download --stale")

        # Check for client updates (non-blocking)
        _check_client_version(base_url)
    except Exception as e:
        print_error(f"Sync failed: {e}")
        sys.exit(1)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Download command (FITS files)
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--obs", "obs_filter", multiple=True, help="Download by observation name")
@click.option("--program", "program_filter", multiple=True, help="Download by program slug")
@click.option("--field", "field_filter", multiple=True, help="Download by field name")
@click.option("--grating", "grating_filter", multiple=True, help="Filter by grating type")
@click.option("--stale", is_flag=True, help="Re-download files updated on the server")
@click.option("--all", "download_all", is_flag=True, help="Download everything accessible")
@click.option("--workers", default=4, help="Parallel download workers")
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.option("--dry-run", is_flag=True, help="Show plan without downloading")
@click.option("--base-url", default=None, help="API base URL")
def download(obs_filter, program_filter, field_filter, grating_filter,
             stale, download_all, workers, yes, dry_run, base_url):
    """Download FITS spectrum files.

    Requires a prior 'campfire sync' to populate the local catalog.
    Use filters to select which observations to download.

    \b
    Examples:
      campfire download --obs ember_uds_p4
      campfire download --program EMBER-UDS --grating PRISM
      campfire download --field COSMOS
      campfire download --stale
      campfire download --all
    """
    from .config import products_dir as _products_dir, meta_dir as _meta_dir
    from .sync import (
        download_observation,
        sync_metadata,
        format_size,
    )
    from .api.session import create_download_session

    base_url = base_url or resolve_base_url()

    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError as e:
        print_error(str(e))
        sys.exit(1)

    store = _open_store()

    # Auto-sync catalog before download
    try:
        is_first_sync = store.get_max_updated_at() is None
        if is_first_sync:
            print_msg("Syncing catalog for the first time...")
        else:
            print_msg("Syncing catalog...")

        result = sync_metadata(api, store, _meta_dir(), show_progress=is_first_sync)

        if result.get("needs_full_sync"):
            print_msg("  Local catalog out of sync with server, running full sync...")
            result = sync_metadata(api, store, _meta_dir(), show_progress=True, full=True)

        parts = []
        if result["targets"]:
            parts.append(f"{result['targets']} targets updated")
        if result.get("purged_objects"):
            parts.append(f"{result['purged_objects']} removed")
        if parts:
            print_msg(f"  {', '.join(parts)}")
        else:
            print_msg("  Up to date.")
    except Exception as e:
        print_warning(f"Sync failed: {e}")
        print_msg("  Continuing with existing catalog data.")

    catalog_obs = store.get_synced_observations()
    if not catalog_obs:
        print_error("No catalog data.")
        store.close()
        sys.exit(1)

    # No filters specified — show available observations and exit
    if not obs_filter and not program_filter and not field_filter and not stale and not download_all:
        summary = store.get_observation_summary()
        store.close()

        print_msg(f"\nAvailable observations ({len(summary)} total):\n")

        table = make_table("OBSERVATION", "PROGRAM", "FIELD", "SPECTRA", "LOCAL")
        for row in summary:
            downloaded = row["downloaded_count"]
            total = row["spectrum_count"]
            if downloaded >= total and total > 0:
                local_str = f"{downloaded} (complete)"
            elif downloaded > 0:
                local_str = f"{downloaded}/{total}"
            else:
                local_str = ""
            table.add_row(
                row["observation"],
                row["program_slug"],
                row["field"],
                str(total),
                local_str,
            )

        if len(summary) > 30:
            with console.pager():
                console.print(table)
        else:
            console.print(table)

        print_msg("\nUse --obs, --program, or --field to download, or --all for everything.")
        return

    # Determine which observations to download
    if stale:
        stale_files = store.get_stale_files()
        if not stale_files:
            print_msg("All local files are up to date.")
            store.close()
            return
        # Group stale files by observation
        stale_obs = set(f["observation"] for f in stale_files)
        target_obs = sorted(stale_obs)
        print_msg(f"Found {len(stale_files)} stale file(s) across {len(target_obs)} observation(s)")
    else:
        target_obs = set()

        if download_all:
            target_obs = set(catalog_obs)

        if obs_filter:
            for name in obs_filter:
                if name not in catalog_obs:
                    print_error(f"Observation '{name}' not in catalog. Run: campfire sync")
                    store.close()
                    sys.exit(1)
                target_obs.add(name)

        if program_filter:
            for prog in program_filter:
                matching = store.get_distinct_values("observation")
                # Query store for observations matching this program
                results = store.query_targets(programs=[prog], limit=999999)
                obs_for_prog = set(r["observation"] for r in results)
                if not obs_for_prog:
                    print_error(f"No observations found for program '{prog}'")
                    store.close()
                    sys.exit(1)
                target_obs.update(obs_for_prog)

        if field_filter:
            for fld in field_filter:
                results = store.query_targets(fields=[fld], limit=999999)
                obs_for_field = set(r["observation"] for r in results)
                if not obs_for_field:
                    print_error(f"No observations found for field '{fld}'")
                    store.close()
                    sys.exit(1)
                target_obs.update(obs_for_field)

        target_obs = sorted(target_obs)

    if not target_obs:
        print_msg("Nothing to download.")
        store.close()
        return

    # Reconcile DB with filesystem before planning
    verify = store.verify_local_files(_products_dir(), show_progress=True)
    if verify["cleared"]:
        print_msg(f"  Detected {verify['cleared']} missing local file(s), will re-download.")
    if verify.get("rehashed"):
        print_msg(f"  Re-verified {verify['rehashed']} modified local file(s).")
    if verify["discovered"]:
        print_msg(f"  Found {verify['discovered']} existing local file(s), skipping download.")

    # Compute download plan locally (no HTTP requests)
    grating_list = list(grating_filter) if grating_filter else None
    print_msg("Checking files...")

    pending = store.get_pending_downloads(
        observations=list(target_obs),
        gratings=grating_list,
    )

    # Show per-observation status
    obs_with_downloads = []
    total_download = 0
    total_files = 0

    for obs in target_obs:
        obs_pending = pending.get(obs, [])
        if not obs_pending:
            print_msg(f"  {obs}: up to date")
            continue

        new_count = sum(1 for s in obs_pending if s["status"] == "new")
        updated_count = sum(1 for s in obs_pending if s["status"] == "updated")
        download_bytes = sum(s.get("file_size") or 0 for s in obs_pending)

        parts = []
        if new_count:
            parts.append(f"{new_count} new")
        if updated_count:
            parts.append(f"{updated_count} updated")
        print_msg(f"  {obs}: {', '.join(parts)} ({format_size(download_bytes)})")

        obs_with_downloads.append(obs)
        total_download += download_bytes
        total_files += len(obs_pending)

    if total_files == 0:
        print_msg("\nAll files up to date.")
        store.close()
        return

    if dry_run:
        print_msg(f"\nDry run: would download {total_files} files ({format_size(total_download)})")
        store.close()
        return

    if not yes:
        print_msg(f"\nDownload {total_files} files ({format_size(total_download)})?")
        if not click.confirm("Proceed?", default=True):
            store.close()
            return

    # Execute downloads — only fetch manifests for observations that need them
    print_msg()
    dl_session = create_download_session(max_workers=workers)
    all_stats = []

    for obs in obs_with_downloads:
        api_session._ensure_valid_token()

        try:
            stats = download_observation(
                api, obs,
                _products_dir(), store,
                max_workers=workers,
                download_session=dl_session,
                grating_filter=grating_list,
            )
            all_stats.append(stats)
        except Exception as e:
            print_error(f"Failed to download {obs}: {e}")

    # Summary
    total_downloaded = sum(s.get("downloaded", 0) for s in all_stats)
    total_failed = sum(s.get("failed", 0) for s in all_stats)
    print_success("Download complete")
    print_msg(f"  Files downloaded: {total_downloaded}")
    if total_failed:
        print_msg(f"  Files failed: {total_failed}")
    print_msg(f"  Total size: {format_size(total_download)}")

    store.close()


def main():
    """Entry point for the CLI."""
    _register_deploy_group()
    cli()


if __name__ == "__main__":
    main()
