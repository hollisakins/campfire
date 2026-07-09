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


def _maybe_migrate_layout(store, migrate_layout: Optional[bool]) -> None:
    """Detect a pre-#212 local layout and (optionally) migrate it in place.

    Runs before the metadata sync so the relocated files are at their canonical
    ``products/nirspec/<obs>/`` paths when ``verify_local_objects`` discovers
    them — turning "nothing downloaded" back into recognized local files without
    re-downloading. A ``_meta`` marker short-circuits the scan once migrated.
    """
    from .config import resolve_data_dir
    from . import migrate as _mig

    if store.get_meta(_mig.LAYOUT_MARKER_KEY) == _mig.LAYOUT_MARKER_VALUE:
        return  # already migrated, or born on the new layout

    root = resolve_data_dir()
    plan_result = _mig.plan(root)
    if not plan_result["pending"]:
        # Fresh or already-migrated tree — stamp so we never scan again.
        store.set_meta(_mig.LAYOUT_MARKER_KEY, _mig.LAYOUT_MARKER_VALUE)
        return

    counts = plan_result["counts"]
    click.echo("\n⚠  Local data uses the old layout (products/<obs>/ instead of "
               "products/nirspec/<obs>/).")
    click.echo("   Files there won't be recognized as downloaded until the tree is migrated.")
    summary = ", ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
    click.echo(f"   Planned actions (dry-run): {summary}")
    if plan_result["warnings"]:
        click.echo(f"   ({len(plan_result['warnings'])} item(s) will need manual review.)")

    if migrate_layout is False:
        click.echo("   Skipping (--no-migrate-layout). Run it yourself with:")
        click.echo("     python pipeline/scripts/migrate_layout_212.py --apply")
        return
    if migrate_layout is None:
        if not sys.stdin.isatty():
            click.echo("   Non-interactive session — not migrating automatically. Re-run with "
                       "--migrate-layout, or use pipeline/scripts/migrate_layout_212.py --apply.")
            return
        if not click.confirm("   Migrate the local layout now?", default=False):
            click.echo("   Skipped — will be offered again on the next sync.")
            return

    click.echo("   Migrating…")
    _mig.apply(root, echo=click.echo)
    # Only stamp the marker if nothing material remains (an unresolved
    # "BOTH exist" case needs a human; re-offering next sync is safe + idempotent).
    if not _mig.plan(root)["pending"]:
        store.set_meta(_mig.LAYOUT_MARKER_KEY, _mig.LAYOUT_MARKER_VALUE)
        click.echo("   ✓ Local layout migrated.")
    else:
        click.echo("   ⚠ Migration applied; some items still need manual review "
                   "(see warnings/manifest above).")


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
@click.version_option(version="0.4.0", prog_name="campfire")
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


def _register_fitsgl_group():
    """Register the fitsgl subgroup lazily (epic #337). The group itself needs no
    producer deps — it imports FitsGL only inside `build` — so registration rarely
    fails; the stub guards a genuinely broken/absent subpackage."""
    try:
        from campfire.fitsgl.cli import fitsgl_group
        cli.add_command(fitsgl_group, name='fitsgl')
    except ImportError:
        @cli.command('fitsgl', hidden=False)
        def fitsgl_stub():
            """Build & deploy FitsGL tile-pyramid datasets. (Requires: pip install campfire[fitsgl])"""
            click.echo("FitsGL dependencies not installed. Run: pip install campfire[fitsgl]")
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

    # Per-observation cloud-vs-local view, from the storage_objects mirror. Each
    # cell is local/available; intermediates only appear where the cloud has them
    # (admins reviewing a draft, or after --intermediate). No network call.
    summary = store.get_object_summary()
    materialized = [r for r in summary if r["finals_local"] or r["intermediates_local"]]
    if materialized:
        click.echo()
        click.echo(f"  {'OBSERVATION':<25} {'FINALS':<12} {'INTERMEDIATES':<15} {'SIZE':<12}")
        for r in materialized:
            finals = f"{r['finals_local']}/{r['finals_available']}"
            if r["intermediates_available"]:
                inter = f"{r['intermediates_local']}/{r['intermediates_available']}"
            else:
                inter = "—"
            size = format_size(r["local_bytes"])
            click.echo(f"  {r['observation']:<25} {finals:<12} {inter:<15} {size:<12}")
    else:
        click.echo()
        click.echo("  No files downloaded yet. Run: campfire download --obs <name>")

    # Global cloud storage budget (admin-only; best-effort, skipped otherwise).
    try:
        api = APIClient(APISession(base_url=base_url))
        budget = api.get_storage_budget()
        if budget:
            used = format_size(int(budget.get("total_bytes", 0)))
            cap = format_size(int(budget.get("cap_bytes", 0)))
            pct = budget.get("pct_used", 0)
            click.echo(f"\nCloud storage: {used} of {cap} ({pct}%)")
    except Exception:
        pass

    # Stale files (server hash != local hash, via the mirror)
    stale = store.get_stale_objects()
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
@click.option("--migrate-layout/--no-migrate-layout", "migrate_layout", default=None,
              help="Migrate a pre-#212 local layout without prompting (or skip it). "
                   "Default: prompt when an old layout is detected on a TTY.")
def sync_cmd(full: bool, base_url: Optional[str], migrate_layout: Optional[bool]):
    """Sync the object catalog from the server (metadata only).

    On first run, pulls the full catalog. On subsequent runs, only
    fetches objects modified since the last sync (incremental).
    Use --full to force a complete re-sync.

    If the local data directory still uses the pre-#212 layout, sync detects it
    and offers to migrate it in place (products/<obs>/ -> products/nirspec/<obs>/,
    plus raw/ and reference/ when observations.toml is present).
    """
    base_url = base_url or resolve_base_url()

    from .config import meta_dir as _meta_dir, products_dir as _products_dir
    from .sync import sync_metadata

    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)

    store = _open_store()

    try:
        # Migrate a pre-#212 local layout before anything reads the tree, so the
        # verify step below discovers the relocated files instead of missing them.
        _maybe_migrate_layout(store, migrate_layout)

        is_incremental = not full and store.get_max_objects_updated_at() is not None
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
                        f"{result['objects']} objects, {result['spectra']} spectra updated.")
        else:
            click.echo(f"✓ Full sync complete: {result['observations']} observations, "
                        f"{result['objects']} objects, {result['spectra']} spectra.")

        if result.get("objects_purged"):
            click.echo(f"  Removed {result['objects_purged']} objects deleted from server.")
        if result.get("purged_spectra"):
            click.echo(f"  Removed {result['purged_spectra']} spectra deleted from server.")

        # Verify local files so status reports correct counts immediately
        pd = _products_dir()
        if pd.exists():
            verify = store.verify_local_objects(pd, show_progress=True)
            if verify["cleared"]:
                click.echo(f"  Detected {verify['cleared']} missing local file(s).")
            if verify["rehashed"]:
                click.echo(f"  Re-verified {verify['rehashed']} modified local file(s).")
            if verify["discovered"]:
                click.echo(f"  Found {verify['discovered']} existing local file(s).")

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


class _VariadicOption(click.Option):
    """Click option that consumes multiple space-separated values after a single flag.

    Allows e.g. ``--obs obs1 obs2 obs3`` in addition to the standard
    ``--obs obs1 --obs obs2 --obs obs3`` syntax.
    """

    def add_to_parser(self, parser, ctx):
        super().add_to_parser(parser, ctx)
        name = self.opts[-1]
        opt = parser._long_opt.get(name)
        if opt is None:
            return
        original_process = opt.process

        def _eat_remaining(value, state):
            original_process(value, state)
            while state.rargs and not state.rargs[0].startswith('-'):
                original_process(state.rargs.pop(0), state)

        opt.process = _eat_remaining


@cli.command()
@click.option("--obs", "obs_filter", multiple=True, cls=_VariadicOption, help="Download by observation name")
@click.option("--program", "program_filter", multiple=True, cls=_VariadicOption, help="Download by program slug")
@click.option("--field", "field_filter", multiple=True, cls=_VariadicOption, help="Download by field name")
@click.option("--grating", "grating_filter", multiple=True, cls=_VariadicOption,
              help="NIRSpec: narrow finals to these gratings (e.g. PRISM G395M)")
@click.option("--filters", "filter_filter", multiple=True, cls=_VariadicOption,
              help="NIRCam: narrow to these filters (e.g. f277w f356w f444w)")
@click.option("--stale", is_flag=True, help="Re-download files updated on the server")
@click.option("--intermediate", "include_intermediate", is_flag=True,
              help="Also fetch canonical intermediates (NIRSpec spectrum-exposures, NIRCam exposures + expmaps)")
@click.option("--all", "download_all", is_flag=True, help="Download everything accessible")
@click.option("--workers", default=4, help="Parallel download workers")
@click.option("--yes", is_flag=True, help="Skip confirmation")
@click.option("--dry-run", is_flag=True, help="Show plan without downloading")
@click.option("--base-url", default=None, help="API base URL")
def download(obs_filter, program_filter, field_filter, grating_filter, filter_filter,
             stale, include_intermediate, download_all, workers, yes, dry_run, base_url):
    """Download data products (NIRSpec spectra + NIRCam field products).

    Requires a prior 'campfire sync' to populate the local catalog. Select what
    to download with --obs / --program (NIRSpec) or --field (NIRSpec observations
    in a field, or NIRCam field products), then optionally scope within a
    selection:

    \b
      --grating   NIRSpec: narrow finals to these gratings (e.g. PRISM G395M)
      --filters   NIRCam:  narrow to these filters (e.g. f277w f356w f444w)

    By default only finals are fetched (NIRSpec spectra, NIRCam mosaics);
    --intermediate also pulls the canonical intermediates (NIRSpec spectrum-
    exposures, NIRCam exposures + expmaps) so you can restore a deleted-local
    observation or inspect a stage-1/2 draft.

    \b
    Examples:
      campfire download --obs ember_uds_p4
      campfire download --obs ember_uds_p4 ember_uds_p5
      campfire download --program EMBER-UDS --grating PRISM
      campfire download --obs ember_egs_p1 --intermediate
      campfire download --field egs --filters f277w f356w f444w --intermediate
      campfire download --field cosmos
      campfire download --stale
      campfire download --all
    """
    from .config import products_dir as _products_dir, meta_dir as _meta_dir
    from .sync import (
        download_objects,
        sync_metadata,
        format_size,
    )
    from .db.store import FINAL_PRODUCT_TYPES, INTERMEDIATE_PRODUCT_TYPES
    from .api.session import create_download_session

    product_types = list(FINAL_PRODUCT_TYPES)
    if include_intermediate:
        product_types += list(INTERMEDIATE_PRODUCT_TYPES)

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
        is_first_sync = store.get_max_objects_updated_at() is None
        if is_first_sync:
            click.echo("Syncing catalog for the first time...")
        else:
            click.echo("Syncing catalog...")

        result = sync_metadata(api, store, _meta_dir(), show_progress=is_first_sync)

        if result.get("needs_full_sync"):
            click.echo("  Local catalog out of sync with server, running full sync...")
            result = sync_metadata(api, store, _meta_dir(), show_progress=True, full=True)

        parts = []
        if result.get("objects"):
            parts.append(f"{result['objects']} objects updated")
        if result.get("spectra"):
            parts.append(f"{result['spectra']} spectra updated")
        if result.get("objects_purged"):
            parts.append(f"{result['objects_purged']} removed")
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

    # Determine which observations (NIRSpec) + fields (NIRCam) to download
    target_fields = set()
    if stale:
        stale_files = store.get_stale_objects()
        if not stale_files:
            click.echo("All local files are up to date.")
            store.close()
            return
        # Group stale files by observation
        stale_obs = set(f["observation"] for f in stale_files if f.get("observation"))
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
                spectra = store.query_spectra(programs=[prog], limit=999999)
                obs_for_prog = set(s["observation"] for s in spectra if s.get("observation"))
                if not obs_for_prog:
                    click.echo(f"✗ No observations found for program '{prog}'", err=True)
                    store.close()
                    sys.exit(1)
                target_obs.update(obs_for_prog)

        if field_filter:
            for fld in field_filter:
                spectra = store.query_spectra(fields=[fld], limit=999999)
                obs_for_field = set(s["observation"] for s in spectra if s.get("observation"))
                if obs_for_field:
                    target_obs.update(obs_for_field)  # NIRSpec field → its observations
                else:
                    # NIRCam (or spectra-less) field: field-scoped storage objects
                    # (observation NULL), selected directly by field below.
                    target_fields.add(fld)

        target_obs = sorted(target_obs)

    target_fields = sorted(target_fields)
    if not target_obs and not target_fields:
        click.echo("Nothing to download.")
        store.close()
        return

    filter_list = list(filter_filter) if filter_filter else None
    if filter_list and not target_fields:
        click.echo("Note: --filters scopes NIRCam field products; this selection "
                   "has no NIRCam field, so it has no effect.")

    # Reconcile DB with filesystem before planning
    verify = store.verify_local_objects(
        _products_dir(), product_types=product_types, show_progress=True
    )
    if verify["cleared"]:
        click.echo(f"  Detected {verify['cleared']} missing local file(s), will re-download.")
    if verify.get("rehashed"):
        click.echo(f"  Re-verified {verify['rehashed']} modified local file(s).")
    if verify["discovered"]:
        click.echo(f"  Found {verify['discovered']} existing local file(s), skipping download.")

    # Compute download plan locally (no HTTP requests)
    grating_list = list(grating_filter) if grating_filter else None
    click.echo("Checking files...")

    pending = store.get_pending_objects(
        observations=list(target_obs),
        product_types=product_types,
        gratings=grating_list,
        fields=list(target_fields),
        filters=filter_list,
    )

    # Show per-scope status (observation for NIRSpec, field for NIRCam)
    obs_with_downloads = []
    total_download = 0
    total_files = 0

    for obs in list(target_obs) + list(target_fields):
        obs_pending = pending.get(obs, [])
        if not obs_pending:
            click.echo(f"  {obs}: up to date")
            continue

        new_count = sum(1 for s in obs_pending if s["status"] == "new")
        updated_count = sum(1 for s in obs_pending if s["status"] == "updated")
        download_bytes = sum(s.get("size_bytes") or 0 for s in obs_pending)

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

    # Execute downloads — one product-type-agnostic pass over the planned set.
    # Plan + URLs are computed locally then presigned; the generic engine fetches,
    # verifies content_hash, and records local state in the storage_objects mirror.
    click.echo()
    dl_session = create_download_session(max_workers=workers)
    api_session._ensure_valid_token()

    stats = download_objects(
        api,
        list(target_obs),
        product_types,
        store,
        _products_dir(),
        max_workers=workers,
        download_session=dl_session,
        gratings=grating_list,
        fields=list(target_fields),
        filters=filter_list,
    )

    click.echo(f"\n✓ Download complete")
    click.echo(f"  Files downloaded: {stats.get('downloaded', 0)}")
    if stats.get("failed"):
        click.echo(f"  Files failed: {stats['failed']}")
    if stats.get("unauthorized"):
        click.echo(f"  Files skipped (no access): {stats['unauthorized']}")
    click.echo(f"  Total size: {format_size(total_download)}")

    store.close()


def main():
    """Entry point for the CLI."""
    _register_deploy_group()
    _register_fitsgl_group()
    cli()


if __name__ == "__main__":
    main()
