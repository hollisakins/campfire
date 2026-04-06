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


def _require_auth(base_url: str) -> APISession:
    """Verify credentials and return an APISession. Exits on failure."""
    try:
        return APISession(base_url=base_url)
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        click.echo("  Run: campfire login")
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
        click.echo(f"⚠  {e}")
        click.echo("   The database must be recreated to match the current client version.")
        if click.confirm("   Delete and rebuild on next sync?", default=True):
            db_path.unlink(missing_ok=True)
            # Also remove WAL/SHM files left by SQLite
            db_path.with_suffix(".db-wal").unlink(missing_ok=True)
            db_path.with_suffix(".db-shm").unlink(missing_ok=True)
            click.echo("   Deleted. Creating fresh database…")
            return LocalStore(db_path)
        else:
            click.echo("   Aborting.")
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
            click.echo(f"\n⚠ campfire v{__version__} is no longer supported "
                        f"(minimum: v{minimum}). Please update:")
            click.echo("  pip install -U git+https://github.com/hollisakins/campfire.git#subdirectory=python")
        elif current < latest:
            click.echo(f"\n  Update available: v{__version__} → v{latest}")
            click.echo("  pip install -U git+https://github.com/hollisakins/campfire.git#subdirectory=python")
    except Exception:
        pass  # Never block sync for a version check failure


@click.group()
@click.version_option(version="0.3.0", prog_name="campfire")
def cli():
    """CAMPFIRE — Python client and deployment tools for NIRSpec spectroscopic data."""
    pass


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
                click.echo(f"Already logged in as {existing.user_email}")
            elif existing.is_api_key():
                click.echo("Already authenticated with API key")

            if not click.confirm("Do you want to re-authenticate?"):
                return

    # Determine authentication method
    if browser is None:
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
@click.option("--base-url", default=None, help="API base URL")
def logout(base_url: Optional[str]):
    """Remove stored credentials and revoke server session."""
    base_url = base_url or resolve_base_url()
    creds = CredentialManager()

    if not creds.exists():
        click.echo("Not logged in.")
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
    click.echo("✓ Logged out successfully")


@cli.command()
@click.option("--base-url", default=None, help="API base URL")
def whoami(base_url: Optional[str]):
    """Show current authenticated user."""
    base_url = base_url or resolve_base_url()
    creds = CredentialManager()

    if not creds.exists():
        click.echo("Not logged in. Run: campfire login")
        sys.exit(1)

    loaded = creds.load()
    if not loaded:
        click.echo("Invalid credentials. Run: campfire login")
        sys.exit(1)

    if loaded.is_api_key():
        click.echo("Authentication: API key")
        if loaded.api_key:
            click.echo(f"Key prefix: {loaded.api_key[:20]}...")

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
    """Check credentials, catalog, and download status."""
    base_url = base_url or resolve_base_url()

    try:
        token_manager = TokenManager(base_url)

        if not token_manager.has_credentials():
            click.echo("✗ No credentials found")
            click.echo("  Run: campfire login")
            sys.exit(1)

        token = token_manager.get_valid_token(auto_refresh=True)

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

    # Show catalog and download status
    from .config import resolve_data_dir, meta_dir as _meta_dir
    from .sync import format_size

    data_dir = resolve_data_dir()
    click.echo()
    click.echo(f"Data directory: {data_dir}")

    db_path = _meta_dir(data_dir) / "campfire.db"
    if not db_path.exists():
        click.echo("\nNo local catalog. Run: campfire sync")
        return

    from .db.store import LocalStore, SchemaMismatchError

    try:
        store = LocalStore(db_path)
    except SchemaMismatchError:
        click.echo("\n⚠  Local catalog has an outdated schema. Run: campfire sync")
        return

    # Catalog stats
    obs_list = store.get_synced_observations()
    last_synced = store.get_last_synced_at()
    if last_synced:
        last_str = last_synced[:16].replace("T", " ")
        click.echo(f"Catalog: {len(obs_list)} observations (last synced {last_str})")
    else:
        click.echo(f"Catalog: {len(obs_list)} observations")

    # Download stats per observation
    if obs_list:
        click.echo()
        click.echo(f"  {'OBSERVATION':<25} {'DOWNLOADED':<14} {'SIZE':<12}")

        total_downloaded = 0
        total_bytes = 0
        for obs in obs_list:
            stats = store.get_observation_stats(obs)
            downloaded = stats["synced_count"]
            size = format_size(stats["total_bytes"])
            total_downloaded += downloaded
            total_bytes += stats["total_bytes"]
            if downloaded > 0:
                click.echo(f"  {obs:<25} {downloaded:<14} {size:<12}")

        if total_downloaded == 0:
            click.echo("  No FITS files downloaded yet. Run: campfire download --obs <name>")

    # Stale files
    stale = store.get_stale_files()
    if stale:
        click.echo(f"\n⚠ {len(stale)} local file(s) updated on server. Run: campfire download --stale")

    # Disk usage
    if data_dir.exists():
        total = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
        click.echo(f"\nDisk usage: {format_size(total)}")

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

    from .config import meta_dir as _meta_dir
    from .sync import sync_metadata

    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)

    store = _open_store()

    try:
        is_incremental = not full and store.get_max_updated_at() is not None
        if is_incremental:
            click.echo("Syncing catalog (updating existing)...")
        else:
            click.echo("Syncing full CAMPFIRE catalog (may take a while)...")

        result = sync_metadata(
            api, store, _meta_dir(),
            show_progress=True,
            full=full,
        )

        if result.get("incremental"):
            click.echo(f"✓ Incremental sync complete: {result['observations']} observations, "
                        f"{result['targets']} targets, {result['spectra']} spectra, "
                        f"{result['sky_objects']} sky objects updated.")
        else:
            click.echo(f"✓ Full sync complete: {result['observations']} observations, "
                        f"{result['targets']} targets, {result['spectra']} spectra, "
                        f"{result['sky_objects']} sky objects.")

        if result.get("purged_objects") or result.get("purged_spectra"):
            click.echo(f"  Removed {result['purged_objects']} targets and "
                        f"{result['purged_spectra']} spectra deleted from server.")
        if result.get("sky_objects_purged"):
            click.echo(f"  Removed {result['sky_objects_purged']} sky objects deleted from server.")

        if result["stale_count"] > 0:
            click.echo(f"\n⚠ {result['stale_count']} local file(s) have been updated on the server.")
            click.echo("  Run: campfire download --stale")

        # Check for client updates (non-blocking)
        _check_client_version(base_url)
    except Exception as e:
        click.echo(f"✗ Sync failed: {e}", err=True)
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
        click.echo(f"✗ {e}")
        sys.exit(1)

    store = _open_store()

    # Auto-sync catalog before download
    try:
        is_first_sync = store.get_max_updated_at() is None
        if is_first_sync:
            click.echo("Syncing catalog for the first time...")
        else:
            click.echo("Syncing catalog...")

        result = sync_metadata(api, store, _meta_dir(), show_progress=is_first_sync)

        if result.get("needs_full_sync"):
            click.echo("  Local catalog out of sync with server, running full sync...")
            result = sync_metadata(api, store, _meta_dir(), show_progress=True, full=True)

        parts = []
        if result["targets"]:
            parts.append(f"{result['targets']} targets updated")
        if result.get("purged_objects"):
            parts.append(f"{result['purged_objects']} removed")
        if parts:
            click.echo(f"  {', '.join(parts)}")
        else:
            click.echo("  Up to date.")
    except Exception as e:
        click.echo(f"  ⚠ Sync failed: {e}", err=True)
        click.echo("  Continuing with existing catalog data.")

    catalog_obs = store.get_synced_observations()
    if not catalog_obs:
        click.echo("✗ No catalog data.")
        store.close()
        sys.exit(1)

    # No filters specified — show available observations and exit
    if not obs_filter and not program_filter and not field_filter and not stale and not download_all:
        summary = store.get_observation_summary()
        store.close()

        lines = []
        lines.append("")
        lines.append(f"Available observations ({len(summary)} total):")
        if len(summary) > 30:
            lines.append("(scroll with arrow keys, q to quit)")
        lines.append("")
        lines.append(f"  {'OBSERVATION':<25} {'PROGRAM':<15} {'FIELD':<10} {'SPECTRA':>8}   LOCAL")
        for row in summary:
            downloaded = row["downloaded_count"]
            total = row["spectrum_count"]
            if downloaded >= total and total > 0:
                local_str = f"{downloaded} (complete)"
            elif downloaded > 0:
                local_str = f"{downloaded}/{total}"
            else:
                local_str = ""
            lines.append(
                f"  {row['observation']:<25} "
                f"{row['program_slug']:<15} "
                f"{row['field']:<10} "
                f"{total:>8}   {local_str}"
            )

        lines.append("")
        lines.append("Use --obs, --program, or --field to download, or --all for everything.")
        lines.append("")

        output = "\n".join(lines)
        if len(summary) > 30:
            click.echo_via_pager(output)
        else:
            click.echo(output)
        return

    # Determine which observations to download
    if stale:
        stale_files = store.get_stale_files()
        if not stale_files:
            click.echo("All local files are up to date.")
            store.close()
            return
        # Group stale files by observation
        stale_obs = set(f["observation"] for f in stale_files)
        target_obs = sorted(stale_obs)
        click.echo(f"Found {len(stale_files)} stale file(s) across {len(target_obs)} observation(s)")
    else:
        target_obs = set()

        if download_all:
            target_obs = set(catalog_obs)

        if obs_filter:
            for name in obs_filter:
                if name not in catalog_obs:
                    click.echo(f"✗ Observation '{name}' not in catalog. Run: campfire sync", err=True)
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
                    click.echo(f"✗ No observations found for program '{prog}'", err=True)
                    store.close()
                    sys.exit(1)
                target_obs.update(obs_for_prog)

        if field_filter:
            for fld in field_filter:
                results = store.query_targets(fields=[fld], limit=999999)
                obs_for_field = set(r["observation"] for r in results)
                if not obs_for_field:
                    click.echo(f"✗ No observations found for field '{fld}'", err=True)
                    store.close()
                    sys.exit(1)
                target_obs.update(obs_for_field)

        target_obs = sorted(target_obs)

    if not target_obs:
        click.echo("Nothing to download.")
        store.close()
        return

    # Reconcile DB with filesystem before planning
    verify = store.verify_local_files(_products_dir())
    if verify["cleared"]:
        click.echo(f"  Detected {verify['cleared']} missing local file(s), will re-download.")
    if verify.get("rehashed"):
        click.echo(f"  Re-verified {verify['rehashed']} modified local file(s).")
    if verify["discovered"]:
        click.echo(f"  Found {verify['discovered']} existing local file(s), skipping download.")

    # Compute download plan locally (no HTTP requests)
    grating_list = list(grating_filter) if grating_filter else None
    click.echo("Checking files...")

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
            click.echo(f"  {obs}: up to date")
            continue

        new_count = sum(1 for s in obs_pending if s["status"] == "new")
        updated_count = sum(1 for s in obs_pending if s["status"] == "updated")
        download_bytes = sum(s.get("file_size") or 0 for s in obs_pending)

        parts = []
        if new_count:
            parts.append(f"{new_count} new")
        if updated_count:
            parts.append(f"{updated_count} updated")
        click.echo(f"  {obs}: {', '.join(parts)} ({format_size(download_bytes)})")

        obs_with_downloads.append(obs)
        total_download += download_bytes
        total_files += len(obs_pending)

    if total_files == 0:
        click.echo("\nAll files up to date.")
        store.close()
        return

    if dry_run:
        click.echo(f"\nDry run: would download {total_files} files ({format_size(total_download)})")
        store.close()
        return

    if not yes:
        click.echo(f"\nDownload {total_files} files ({format_size(total_download)})?")
        if not click.confirm("Proceed?", default=True):
            store.close()
            return

    # Execute downloads — only fetch manifests for observations that need them
    click.echo()
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
            click.echo(f"✗ Failed to download {obs}: {e}")

    # Summary
    total_downloaded = sum(s.get("downloaded", 0) for s in all_stats)
    total_failed = sum(s.get("failed", 0) for s in all_stats)
    click.echo(f"\n✓ Download complete")
    click.echo(f"  Files downloaded: {total_downloaded}")
    if total_failed:
        click.echo(f"  Files failed: {total_failed}")
    click.echo(f"  Total size: {format_size(total_download)}")

    store.close()


def main():
    """Entry point for the CLI."""
    _register_deploy_group()
    cli()


if __name__ == "__main__":
    main()
