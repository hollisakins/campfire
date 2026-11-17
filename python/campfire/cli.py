"""
CAMPFIRE CLI commands.

Provides commands for authentication, catalog sync, and FITS downloading:
- campfire login: Authenticate with CAMPFIRE
- campfire logout: Remove stored credentials
- campfire whoami: Show current authenticated user
- campfire status: Check credentials, catalog, and download status
- campfire sync: Sync the full object catalog (metadata only)
- campfire pull spec|im: Download data products by modality (`download` is a back-compat alias)
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
from ._version import __version__
from .modality import (
    IMAGING,
    SPECTROSCOPY,
    ModalityGroup,
    normalize_names,
    resolve_instruments,
    resolve_products,
)


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
    from packaging.version import Version

    try:
        resp = requests.get(f"{base_url}/version", timeout=5)
        resp.raise_for_status()
        data = resp.json()

        current = Version(__version__)
        latest = Version(data.get("latest", __version__))
        minimum = Version(data.get("minimum", "0.0.0"))

        # Two supported install paths, and the CLI can't tell which one it came
        # from: a repo checkout updates via install.py, while a pip-from-git
        # install must supply campfire-layout alongside the client (it isn't on
        # PyPI, so the single-package form can never resolve).
        upgrade_lines = (
            "  repo checkout:  git pull && python3 install.py\n"
            "  pip install:    pip install -U "
            '"campfire-layout @ git+https://github.com/hollisakins/campfire.git#subdirectory=layout" '
            '"campfire @ git+https://github.com/hollisakins/campfire.git#subdirectory=python"'
        )
        if current < minimum:
            click.echo(f"\n⚠ campfire v{__version__} is no longer supported "
                        f"(minimum: v{minimum}). Please update:")
            click.echo(upgrade_lines)
        elif current < latest:
            click.echo(f"\n  Update available: v{__version__} → v{latest}")
            click.echo(upgrade_lines)
    except Exception:
        pass  # Never block sync for a version check failure


_ACCESS_META = "access_fingerprint"      # the caller's program set at the last full sync
_LATEST_DR_META = "latest_dr_seen"       # the newest published release the tail announced


def _sync_tail(api, store, *, full: bool, say=click.echo) -> None:
    """The tail of ``campfire sync`` (P5-3, design §4.6): the newest published
    data release, and whether the caller's program set moved since the last
    full sync (an incremental sync keys on ``updated_at``, which a grant or a
    revocation never bumps). Reads ``/auth/whoami``; never raises."""
    try:
        me = api.whoami()
    except Exception:  # noqa: BLE001 - a notice, never a reason to fail a sync
        return
    latest = me.get("latest_dr") or {}
    tag = latest.get("tag")
    if tag:
        if store.get_meta(_LATEST_DR_META) != tag:
            say(f"\n  {tag} published {str(latest.get('published_at') or '')[:10]}; you are on live. "
                f"`campfire pull spec --dr {tag}` fetches the release.")
            store.set_meta(_LATEST_DR_META, tag)
        else:
            say(f"  Latest data release: {tag}; you are on live.")
    fingerprint = me.get("access_fingerprint")
    if fingerprint:
        seen = store.get_meta(_ACCESS_META)
        if full or seen is None:
            store.set_meta(_ACCESS_META, fingerprint)
        elif seen != fingerprint:
            say("\n⚠ Your program access changed since the last full sync. Run: campfire sync --full")


@click.group()
@click.version_option(version=__version__, prog_name="campfire")
def cli():
    """CAMPFIRE — Python client and deployment tools for NIRSpec spectroscopic data."""
    pass


def _register_deploy_group():
    """Register the operator-side commands lazily (deploy deps stay optional).

    ``deploy`` (publication), ``push`` (bytes-only local→cloud transfer), and
    ``drop-local`` (delete local files verified in cloud) all live in the deploy
    package; consumers without the extra get an install-hint stub instead.
    """
    try:
        from campfire.deploy.cli import delete_local, deploy_group, push_cmd
        from campfire.deploy.config_cli import config_group
        cli.add_command(deploy_group, name='deploy')
        cli.add_command(push_cmd, name='push')
        # Same command as `campfire deploy delete-local`, surfaced top-level as
        # the storage-plane verb paired with pull/push.
        cli.add_command(delete_local, name='drop-local')
        # The config plane (issue #303): push/pull/diff for the three
        # data-management TOMLs, mirroring the storage-plane verbs.
        cli.add_command(config_group, name='config')
    except ImportError:
        @cli.command('deploy', hidden=False)
        def deploy_stub():
            """Deploy CAMPFIRE pipeline products to Supabase + OSN. (Requires: pip install campfire[deploy])"""
            click.echo("Deploy dependencies not installed. Run: pip install campfire[deploy]")
            sys.exit(1)

        @cli.command('push', hidden=False)
        def push_stub():
            """Push local products to cloud storage. (Requires: pip install campfire[deploy])"""
            click.echo("Deploy dependencies not installed. Run: pip install campfire[deploy]")
            sys.exit(1)

        @cli.command('config', hidden=False)
        def config_stub():
            """Sync data-management config with the cloud. (Requires: pip install campfire[deploy])"""
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
        if not sys.stdin.isatty():
            raise click.ClickException(
                "No interactive terminal to choose an authentication method. "
                "Run 'campfire login --browser' (device flow; works over SSH — "
                "open the printed URL on any machine) or "
                "'campfire login --api-key' to paste an API key."
            )
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

    shown_email = False
    if loaded.is_api_key():
        click.echo("Authentication: API key")
        if loaded.api_key:
            click.echo(f"Key prefix: {loaded.api_key[:20]}...")
    elif loaded.is_oauth():
        click.echo("Authentication: OAuth (device flow)")
        if loaded.user_email:
            click.echo(f"Email: {loaded.user_email}")
            shown_email = True
        if loaded.expires_at:
            click.echo(f"Token expires: {loaded.expires_at}")

    try:
        me = APIClient(APISession(base_url=base_url)).whoami()
    except Exception as e:  # noqa: BLE001 - offline or expired: the credential facts above still stand
        click.echo(f"(server not reached: {e})")
        return
    if not shown_email and me.get("email"):
        click.echo(f"Email: {me['email']}")
    if me.get("is_admin"):
        click.echo("Admin: yes")
    latest = me.get("latest_dr")
    if latest and latest.get("tag"):
        click.echo(f"Latest data release: {latest['tag']} "
                   f"(published {str(latest.get('published_at') or '')[:10]})")
    else:
        click.echo("Latest data release: none published yet")


def format_budget_lines(budget: dict) -> list[str]:
    """The `campfire status` cloud-budget lines from a get_storage_budget() payload.

    First line: bytes at rest against the cap. Second line (when the server
    reports the P2 classes): how the registry splits into live pointers,
    retained archive copies, GC-pending tombstones (superseded / revoked keys
    whose bytes are still in the bucket) and collected archive rows (hash +
    size kept, no bytes). Older servers without ``by_class`` print the first
    line only.
    """
    from .sync import format_size

    used = format_size(int(budget.get("total_bytes", 0)))
    cap = format_size(int(budget.get("cap_bytes", 0)))
    pct = budget.get("pct_used", 0)
    lines = [f"\nCloud storage: {used} of {cap} ({pct}%)"]
    classes = budget.get("by_class") or {}
    if classes:
        live = classes.get("live") or {}
        archive = classes.get("archive") or {}
        collected = classes.get("collected") or {}
        gc_pending = classes.get("gc_pending") or {}
        parts = [f"live {format_size(int(live.get('bytes', 0)))}",
                 f"archive {format_size(int(archive.get('bytes', 0)))}"]
        n_gc = int(gc_pending.get("count", 0))
        if n_gc:
            parts.append(f"gc-pending {format_size(int(gc_pending.get('bytes', 0)))} ({n_gc})")
        n_coll = int(collected.get("count", 0))
        if n_coll:
            parts.append(f"{n_coll} collected (no bytes)")
        lines.append("  " + " · ".join(parts))
    return lines


@cli.command()
# Removed in 0.8: the scoped push-side diff is `campfire push spec|im ... --dry-run`.
@click.option("--obs", "obs_scope", multiple=True, cls=_VariadicOption, hidden=True)
@click.option("--field", "field_scope", multiple=True, cls=_VariadicOption, hidden=True)
@click.option("--base-url", default=None, help="API base URL")
def status(obs_scope, field_scope, base_url: Optional[str]):
    """Check credentials, catalog, and the local↔cloud storage state.

    What the cloud has vs what is materialized locally, plus staleness and
    (for admins) the storage budget. What a push would transfer is
    `campfire push spec --obs X --dry-run` / `campfire push im --field Y --dry-run`.
    """
    if obs_scope or field_scope:
        lines = ["`campfire status` no longer takes a scope; what a push would transfer is a "
                 "push dry run:"]
        lines += [f"  campfire push spec --obs {' '.join(obs_scope)} --dry-run"] if obs_scope else []
        lines += [f"  campfire push im --field {' '.join(field_scope)} --dry-run"] if field_scope else []
        raise click.UsageError("\n".join(lines))
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
        click.echo("  No files downloaded yet. Run: campfire pull spec --obs <name>  (or: campfire pull im --field <name>)")

    # Global cloud storage budget (admin-only; best-effort, skipped otherwise).
    try:
        api = APIClient(APISession(base_url=base_url))
        budget = api.get_storage_budget()
        if budget:
            for line in format_budget_lines(budget):
                click.echo(line)
    except Exception:
        pass

    # Stale files (server hash != local hash, via the mirror)
    stale = store.get_stale_objects()
    if stale:
        click.echo(f"\n⚠ {len(stale)} local file(s) updated on server. Run: campfire pull --stale")

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
@click.option("--no-snapshot", is_flag=True,
              help="On a full sync, page every row from the server instead of "
                   "starting from the nightly catalog snapshot")
@click.option("--base-url", default=None, help="API base URL")
@click.option("--migrate-layout/--no-migrate-layout", "migrate_layout", default=None,
              help="Migrate a pre-#212 local layout without prompting (or skip it). "
                   "Default: prompt when an old layout is detected on a TTY.")
def sync_cmd(full: bool, no_snapshot: bool, base_url: Optional[str],
             migrate_layout: Optional[bool]):
    """Sync the object catalog from the server (metadata only).

    On first run, pulls the full catalog -- from the server's nightly catalog
    snapshot when it has one, topped up with your non-public programs and
    everything changed since. On subsequent runs, only fetches objects
    modified since the last sync (incremental). Use --full to force a
    complete re-sync.

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
            use_snapshot=not no_snapshot,
        )

        if result.get("snapshot_id"):
            click.echo(f"✓ Full sync complete (from catalog snapshot {result['snapshot_id']}): "
                        f"{result['observations']} observations.")
        elif result.get("incremental"):
            click.echo(f"✓ Incremental sync complete: {result['observations']} observations, "
                        f"{result['objects']} objects, {result['spectra']} spectra updated.")
        else:
            click.echo(f"✓ Full sync complete: {result['observations']} observations, "
                        f"{result['objects']} objects, {result['spectra']} spectra.")

        if result.get("objects_purged"):
            click.echo(f"  Removed {result['objects_purged']} objects deleted from server.")
        if result.get("purged_spectra"):
            click.echo(f"  Removed {result['purged_spectra']} spectra deleted from server.")

        # Verify local files so status reports correct counts immediately.
        # Cheap: one bulk directory scan + size-match adoption, no file reads —
        # frequent drift-checking is the point, so this must stay fast even on
        # a reducer tree over NFS (run `campfire verify --deep` for true
        # content hashing).
        pd = _products_dir()
        if pd.exists():
            verify = store.verify_local_objects(pd, show_progress=True)
            if verify["cleared"]:
                click.echo(f"  Detected {verify['cleared']} missing local file(s).")
            if verify["rehashed"]:
                click.echo(f"  Refreshed {verify['rehashed']} modified local file(s).")
            if verify["discovered"]:
                click.echo(f"  Found {verify['discovered']} existing local file(s).")
            if verify.get("mismatched"):
                click.echo(f"  {verify['mismatched']} local file(s) don't match the "
                           f"cloud object (partial/foreign) — left as pending.")

        if result["stale_count"] > 0:
            click.echo(f"\n⚠ {result['stale_count']} local file(s) have been updated on the server.")
            click.echo("  Run: campfire pull --stale")

        # Check for client updates (non-blocking)
        _check_client_version(base_url)
        # The newest data release; a changed program set (non-blocking)
        _sync_tail(api, store, full=not is_incremental)
    except Exception as e:
        click.echo(f"✗ Sync failed: {e}", err=True)
        sys.exit(1)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Pull command (cloud→local data products; alias: download)
# ---------------------------------------------------------------------------
#
# `campfire pull <modality>`: the modality decides how data is selected and
# what the product names mean (campfire/modality.py). Bare `campfire pull`
# keeps only what spans every modality: --stale and --provenance.

_PULL_LEGACY_FLAGS = ("--obs", "--program", "--field", "--grating", "--filters",
                      "--intermediate", "--all", "--dr")


def _transfer_options(f):
    """--workers / --yes / --dry-run / --base-url, shared by every pull form."""
    f = click.option("--base-url", default=None, help="API base URL")(f)
    f = click.option("--dry-run", is_flag=True, help="Show the plan without downloading")(f)
    f = click.option("--yes", is_flag=True, help="Skip confirmation")(f)
    f = click.option("--workers", default=4, show_default=True, help="Parallel download workers")(f)
    return f


def _instrument_option(modality):
    return click.option(
        "--instrument", "instruments", multiple=True, cls=_VariadicOption,
        help=f"Only these instruments ({', '.join(modality.instruments)}; default: all)")


def _products_option(modality):
    names = ", ".join(modality.product_names())
    finals = ", ".join(modality.finals())
    return click.option(
        "--products", multiple=True, cls=_VariadicOption,
        help=f"What to fetch: {names} (space or comma separated; default: {finals})")


@cli.group(name="pull", cls=ModalityGroup, legacy_flags=_PULL_LEGACY_FLAGS,
           invoke_without_command=True)
@click.option("--stale", is_flag=True,
              help="Re-download every local file the server has updated, any modality")
@click.option("--provenance", "provenance_file", default=None, metavar="FILE",
              type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="Fetch the bytes a provenance file records: into the live tree while live "
                   "still carries them, else from the release holding them (into dr/TAG/products)")
@click.option("--no-annotations", "no_annotations", is_flag=True,
              help="Skip regenerating review annotations (admins with the deploy extra)")
@_transfer_options
@click.pass_context
def pull_group(ctx, stale, provenance_file, no_annotations, workers, yes, dry_run, base_url):
    """Pull cloud data products into the local tree (alias: download).

    The cloud→local half of the storage plane. Pick a modality, then a scope:

    \b
      campfire pull spectroscopy (spec)   --obs / --program / --field / --all
      campfire pull imaging (im)          --field / --all

    Each takes --products (default: the finals), --instrument, its own
    narrowers (spec: --grating; im: --filters, --tile) and --dr TAG for a
    data release. Run a subcommand with no scope to list what is available.

    \b
    Examples:
      campfire pull spec --obs ember_uds_p4
      campfire pull spec --program EMBER-UDS --grating PRISM
      campfire pull spec --obs ember_egs_p1 --products spectra,exposures
      campfire pull im --field nep --products exposures,expmaps
      campfire pull im --field cosmos --filters f444w --tile A1
      campfire pull spec --dr DR2026.1 --obs ember_uds_p4
      campfire pull --stale
      campfire pull --provenance paper.campfire.json
    """
    if ctx.invoked_subcommand:
        if stale or provenance_file:
            raise click.UsageError("--stale and --provenance span every modality; "
                                   "use them without a subcommand.", ctx)
        # Transfer flags given before the subcommand still apply to it.
        ctx.obj = dict(no_annotations=no_annotations, workers=workers, yes=yes,
                       dry_run=dry_run, base_url=base_url)
        return
    if provenance_file and stale:
        raise click.UsageError("--provenance takes its selection from the file; "
                               "it does not combine with --stale.", ctx)
    if provenance_file:
        _pull_provenance(provenance_file, yes, dry_run, base_url)
        return
    if stale:
        _pull_live(None, stale=True, no_annotations=no_annotations, workers=workers,
                   yes=yes, dry_run=dry_run, base_url=base_url)
        return
    raise click.UsageError("Pick a modality: `campfire pull spec ...` or `campfire pull im ...` "
                           "(run either with no scope to list what is available).", ctx)


def _merged_transfer(ctx, **local):
    """Subcommand transfer flags, OR'd with any given before the subcommand."""
    parent = ctx.obj or {}
    out = dict(local)
    for k in ("no_annotations", "yes", "dry_run"):
        out[k] = bool(local.get(k)) or bool(parent.get(k))
    out["base_url"] = local.get("base_url") or parent.get("base_url")
    if parent.get("workers") not in (None, 4) and local.get("workers") == 4:
        out["workers"] = parent["workers"]
    return out


@click.command(name="spectroscopy")
@click.option("--obs", "obs_filter", multiple=True, cls=_VariadicOption, help="Observation name(s)")
@click.option("--program", "program_filter", multiple=True, cls=_VariadicOption, help="Program slug(s)")
@click.option("--field", "field_filter", multiple=True, cls=_VariadicOption,
              help="Field name(s): every observation in the field")
@click.option("--all", "download_all", is_flag=True, help="Every observation you can read")
@_instrument_option(SPECTROSCOPY)
@_products_option(SPECTROSCOPY)
@click.option("--grating", "grating_filter", multiple=True, cls=_VariadicOption,
              help="Narrow spectra to these gratings (e.g. PRISM G395M)")
@click.option("--dr", "dr_tag", default=None, metavar="TAG",
              help="From a data release: its catalog into dr/TAG/meta, its frozen spectra "
                   "into dr/TAG/products (never the live tree)")
@click.option("--no-annotations", "no_annotations", is_flag=True,
              help="Skip regenerating review annotations and inspected redshifts")
@_transfer_options
@click.pass_context
def pull_spectroscopy(ctx, obs_filter, program_filter, field_filter, download_all, instruments,
                      products, grating_filter, dr_tag, no_annotations, workers, yes, dry_run,
                      base_url):
    """Spectra and their exposures, by observation, program or field.

    \b
    Products:  spectra (default), exposures
    Scope:     --obs, --program, --field, --all  (combine freely)
    Narrow:    --grating (spectra only)

    A scoped pull also writes the inspected redshifts to
    reference/nirspec/<obs>/redshifts.toml, and for admins with the deploy
    extra the review annotations (rate masks, stuck shutters, bkg overrides).

    \b
    Examples:
      campfire pull spec --obs ember_uds_p4 ember_uds_p5
      campfire pull spec --program EMBER-UDS --grating PRISM
      campfire pull spec --obs ember_egs_p1 --products spectra,exposures
      campfire pull spec --dr DR2026.1 --program EMBER-UDS
    """
    t = _merged_transfer(ctx, no_annotations=no_annotations, workers=workers, yes=yes,
                         dry_run=dry_run, base_url=base_url)
    sel = _Selection(
        modality=SPECTROSCOPY,
        instruments=resolve_instruments(SPECTROSCOPY, instruments),
        obs=normalize_names(obs_filter), programs=normalize_names(program_filter),
        fields=normalize_names(field_filter), all=download_all,
        gratings=normalize_names(grating_filter))
    sel.products = resolve_products(SPECTROSCOPY, sel.instruments, products, verb="pull")
    if dr_tag:
        _pull_release(dr_tag, sel, t["workers"], t["yes"], t["dry_run"], t["base_url"])
        return
    _pull_live(sel, **t)


@click.command(name="imaging")
@click.option("--field", "field_filter", multiple=True, cls=_VariadicOption, help="Field name(s)")
@click.option("--all", "download_all", is_flag=True, help="Every field you can read")
@_instrument_option(IMAGING)
@_products_option(IMAGING)
@click.option("--filters", "filter_filter", multiple=True, cls=_VariadicOption,
              help="Narrow to these filters (e.g. f277w f356w f444w)")
@click.option("--tile", "tile_filter", multiple=True, cls=_VariadicOption,
              help="Narrow mosaics to these tiles (fields.toml tile names)")
@click.option("--dr", "dr_tag", default=None, metavar="TAG",
              help="From a data release: its frozen mosaics into dr/TAG/products")
@click.option("--no-annotations", "no_annotations", is_flag=True,
              help="Skip regenerating review annotations (masks, exclusions)")
@_transfer_options
@click.pass_context
def pull_imaging(ctx, field_filter, download_all, instruments, products, filter_filter,
                 tile_filter, dr_tag, no_annotations, workers, yes, dry_run, base_url):
    """Mosaics, exposures and exposure maps, by field.

    \b
    Products:  mosaics (default), exposures, expmaps
    Scope:     --field, --all
    Narrow:    --filters (every product), --tile (mosaics only; exposures
               and expmaps cover the whole field)

    For admins with the deploy extra, a scoped pull also regenerates the
    review annotations (masks, exclusions) into reference/.

    \b
    Examples:
      campfire pull im --field cosmos
      campfire pull im --field nep --products exposures,expmaps
      campfire pull im --field egs --filters f277w f356w f444w --products mosaics,exposures
      campfire pull im --field cosmos --tile A1 A2 --filters f444w
    """
    t = _merged_transfer(ctx, no_annotations=no_annotations, workers=workers, yes=yes,
                         dry_run=dry_run, base_url=base_url)
    sel = _Selection(
        modality=IMAGING,
        instruments=resolve_instruments(IMAGING, instruments),
        fields=normalize_names(field_filter), all=download_all,
        filters=[f.lower() for f in normalize_names(filter_filter)],
        tiles=normalize_names(tile_filter))
    sel.products = resolve_products(IMAGING, sel.instruments, products, verb="pull")
    if sel.tiles and "mosaics" not in sel.products:
        raise click.UsageError("--tile narrows mosaics; exposures and expmaps cover the whole "
                               "field. Add mosaics to --products, or drop --tile.", ctx)
    if dr_tag:
        _pull_release(dr_tag, sel, t["workers"], t["yes"], t["dry_run"], t["base_url"])
        return
    _pull_live(sel, **t)


pull_group.add_modality(pull_spectroscopy, SPECTROSCOPY.alias)
pull_group.add_modality(pull_imaging, IMAGING.alias)


class _Selection:
    """A resolved pull request: modality, instruments, products, scope, narrowers."""

    def __init__(self, modality, instruments, obs=(), programs=(), fields=(), all=False,
                 gratings=(), filters=(), tiles=(), products=()):
        self.modality = modality
        self.instruments = list(instruments)
        self.obs, self.programs, self.fields = list(obs), list(programs), list(fields)
        self.all = all
        self.gratings, self.filters, self.tiles = list(gratings), list(filters), list(tiles)
        self.products = list(products)

    @property
    def scoped(self) -> bool:
        return bool(self.obs or self.programs or self.fields or self.all)

    @property
    def product_types(self) -> list:
        return self.modality.pull_types(self.products, self.instruments)


def _sync_before_pull(api, store) -> None:
    from .config import meta_dir as _meta_dir
    from .sync import sync_metadata
    try:
        is_first_sync = store.get_max_objects_updated_at() is None
        click.echo("Syncing catalog for the first time..." if is_first_sync else "Syncing catalog...")
        result = sync_metadata(api, store, _meta_dir(), show_progress=is_first_sync)
        if result.get("needs_full_sync"):
            click.echo("  Local catalog out of sync with server, running full sync...")
            # A mismatch right after a snapshot bootstrap would only repeat
            # with the same snapshot: walk live instead.
            result = sync_metadata(api, store, _meta_dir(), show_progress=True, full=True,
                                   use_snapshot=not result.get("snapshot_id"))
        parts = []
        if result.get("objects"):
            parts.append(f"{result['objects']} objects updated")
        if result.get("spectra"):
            parts.append(f"{result['spectra']} spectra updated")
        if result.get("objects_purged"):
            parts.append(f"{result['objects_purged']} removed")
        click.echo(f"  {', '.join(parts)}" if parts else "  Up to date.")
    except Exception as e:
        click.echo(f"  ⚠ Sync failed: {e}", err=True)
        click.echo("  Continuing with existing catalog data.")


def _list_spectroscopy(store, *, tag: Optional[str] = None) -> None:
    summary = store.get_observation_summary()
    head = (f"Release {tag}: {len(summary)} observation(s) you may read" if tag
            else f"Available observations ({len(summary)} total):")
    lines = ["", head]
    if len(summary) > 30:
        lines.append("(scroll with arrow keys, q to quit)")
    lines += ["", f"  {'OBSERVATION':<25} {'PROGRAM':<15} {'FIELD':<10} {'SPECTRA':>8}   LOCAL"]
    for row in summary:
        downloaded, total = row["downloaded_count"], row["spectrum_count"]
        local_str = (f"{downloaded} (complete)" if total and downloaded >= total
                     else f"{downloaded}/{total}" if downloaded else "")
        lines.append(f"  {row['observation']:<25} {row['program_slug'] or '':<15} "
                     f"{row['field'] or '':<10} {total:>8}   {local_str}")
    dr = f" --dr {tag}" if tag else ""
    lines += ["", f"Pull with: campfire pull spec{dr} --obs / --program / --field ..., or --all.", ""]
    output = "\n".join(lines)
    click.echo_via_pager(output) if len(summary) > 30 else click.echo(output)


def _list_imaging(store, *, tag: Optional[str] = None) -> None:
    from .sync import format_size
    rows = store._conn.execute(
        """SELECT field, COUNT(DISTINCT LOWER(filter)) AS n_filters,
                  COUNT(*) AS n, SUM(size_bytes) AS bytes,
                  SUM(local_file_hash IS NOT NULL) AS local
           FROM storage_objects
           WHERE product_type = 'nircam_mosaic' AND status = 'active' AND field IS NOT NULL
           GROUP BY field ORDER BY field""").fetchall()
    head = f"Release {tag}: imaging" if tag else "Available imaging (mosaics):"
    lines = ["", head, "", f"  {'FIELD':<14} {'FILTERS':>7} {'MOSAIC FILES':>13} {'SIZE':>10}   LOCAL"]
    for r in rows:
        local = r["local"] or 0
        local_str = (f"{local} (complete)" if local and local >= r["n"]
                     else f"{local}/{r['n']}" if local else "")
        lines.append(f"  {r['field']:<14} {r['n_filters']:>7} {r['n']:>13} "
                     f"{format_size(r['bytes'] or 0):>10}   {local_str}")
    if not rows:
        lines.append("  (none)")
    dr = f" --dr {tag}" if tag else ""
    lines += ["", f"Pull with: campfire pull im{dr} --field ... [--products mosaics,exposures,expmaps], "
                  "or --all.", ""]
    click.echo("\n".join(lines))


def _resolve_scope(sel, store, catalog_obs, *, where: str = "the catalog"):
    """(observations, fields) of a selection; exits on a name that resolves to nothing."""
    target_obs, target_fields = set(), set()
    if sel.modality is SPECTROSCOPY:
        if sel.all:
            target_obs.update(catalog_obs)
        for name in sel.obs:
            if name not in catalog_obs:
                click.echo(f"✗ Observation '{name}' is not in {where}. Run: campfire sync", err=True)
                sys.exit(1)
            target_obs.add(name)
        for prog in sel.programs:
            found = {s["observation"] for s in store.query_spectra(programs=[prog], limit=999999)
                     if s.get("observation")}
            if not found:
                click.echo(f"✗ No observations of program '{prog}' in {where}.", err=True)
                sys.exit(1)
            target_obs.update(found)
        for fld in sel.fields:
            found = {s["observation"] for s in store.query_spectra(fields=[fld], limit=999999)
                     if s.get("observation")}
            if not found:
                click.echo(f"✗ No spectroscopy in field '{fld}' in {where}"
                           f" (imaging? try: campfire pull im --field {fld}).", err=True)
                sys.exit(1)
            target_obs.update(found)
    else:
        known = {r["field"] for r in store._conn.execute(
            "SELECT DISTINCT field FROM storage_objects "
            "WHERE observation IS NULL AND field IS NOT NULL AND product_type LIKE 'nircam%'")}
        if sel.all:
            target_fields.update(known)
        for fld in sel.fields:
            target_fields.add(fld)   # checked against the indexed rows once intermediates are in
    return sorted(target_obs), sorted(target_fields)


def _check_narrowers(sel, store, target_obs, target_fields) -> None:
    """A narrower that matches nothing in the selection is an error, never a note."""
    types = sel.product_types
    ph = ",".join("?" * len(types))
    if sel.modality is IMAGING:
        for fld in target_fields:
            n = store._conn.execute(
                f"SELECT COUNT(*) FROM storage_objects WHERE field = ? AND status = 'active' "
                f"AND product_type IN ({ph})", [fld, *types]).fetchone()[0]
            if not n:
                click.echo(f"✗ No {', '.join(sel.products)} for field '{fld}'.", err=True)
                sys.exit(1)
        fph = ",".join("?" * len(target_fields)) or "NULL"
        if sel.filters:
            have = {r[0].lower() for r in store._conn.execute(
                f"SELECT DISTINCT filter FROM storage_objects WHERE field IN ({fph}) "
                f"AND product_type IN ({ph}) AND filter IS NOT NULL AND status = 'active'",
                [*target_fields, *types])}
            missing = [f for f in sel.filters if f not in have]
            if missing:
                click.echo(f"✗ No {', '.join(sel.products)} in filter(s) {', '.join(missing)} here; "
                           f"available: {', '.join(sorted(have)) or 'none'}.", err=True)
                sys.exit(1)
        if sel.tiles:
            from .modality import mosaic_tile
            have = set()
            for r in store._conn.execute(
                    f"SELECT field, storage_key FROM storage_objects WHERE field IN ({fph}) "
                    f"AND product_type = 'nircam_mosaic' AND status = 'active'", list(target_fields)):
                t = mosaic_tile(r["field"], r["storage_key"].rsplit("/", 1)[-1])
                if t:
                    have.add(t)
            missing = [t for t in sel.tiles if t not in have]
            if missing:
                click.echo(f"✗ No mosaics for tile(s) {', '.join(missing)} here; "
                           f"available: {', '.join(sorted(have)) or 'none'}.", err=True)
                sys.exit(1)
    elif sel.gratings:
        if "spectra" not in sel.products:
            click.echo("✗ --grating narrows spectra; exposures carry no grating in the index. "
                       "Add spectra to --products, or drop --grating.", err=True)
            sys.exit(2)
        oph = ",".join("?" * len(target_obs)) or "NULL"
        have = {r[0].upper() for r in store._conn.execute(
            f"SELECT DISTINCT grating FROM spectra WHERE observation IN ({oph}) AND grating IS NOT NULL",
            list(target_obs))}
        missing = [g for g in sel.gratings if g.upper() not in have]
        if missing:
            click.echo(f"✗ No spectra in grating(s) {', '.join(missing)} here; "
                       f"available: {', '.join(sorted(have)) or 'none'}.", err=True)
            sys.exit(1)


def _show_plan(pending, scopes, *, verb_new="new") -> tuple:
    """Print the per-scope and per-product plan; returns (files, bytes)."""
    from .sync import format_size
    total_files = total_bytes = 0
    by_type: dict = {}
    for scope in scopes:
        rows = pending.get(scope, [])
        if not rows:
            click.echo(f"  {scope}: up to date")
            continue
        new = sum(1 for r in rows if r["status"] == "new")
        upd = len(rows) - new
        size = sum(r.get("size_bytes") or 0 for r in rows)
        parts = ([f"{new} {verb_new}"] if new else []) + ([f"{upd} updated"] if upd else [])
        click.echo(f"  {scope}: {', '.join(parts)} ({format_size(size)})")
        total_files += len(rows)
        total_bytes += size
        for r in rows:
            n, b = by_type.get(r["product_type"], (0, 0))
            by_type[r["product_type"]] = (n + 1, b + (r.get("size_bytes") or 0))
    if len(by_type) > 1:
        click.echo("  by product:")
        for ptype, (n, b) in sorted(by_type.items()):
            click.echo(f"    {ptype:<28} {n:>7} files  {format_size(b):>10}")
    return total_files, total_bytes


def _pull_live(sel, *, stale=False, no_annotations=False, workers=4, yes=False, dry_run=False,
               base_url=None):
    """The live tree: one selection (or every stale file), planned locally from
    the storage_objects mirror and fetched by the product-agnostic engine."""
    from .config import products_dir as _products_dir
    from .sync import download_objects, format_size, refresh_storage_scope
    from .db.store import FINAL_PRODUCT_TYPES, DOWNLOADABLE_PRODUCT_TYPES
    from .api.session import create_download_session

    base_url = base_url or resolve_base_url()
    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)

    store = _open_store()
    _sync_before_pull(api, store)

    catalog_obs = store.get_synced_observations()
    if not catalog_obs:
        click.echo("✗ No catalog data.")
        store.close()
        sys.exit(1)

    if stale:
        stale_files = store.get_stale_objects()
        if not stale_files:
            click.echo("All local files are up to date.")
            store.close()
            return
        target_obs = sorted({f["observation"] for f in stale_files if f.get("observation")})
        target_fields = sorted({f["field"] for f in stale_files
                                if not f.get("observation") and f.get("field")})
        product_types = sorted({f["product_type"] for f in stale_files
                                if f.get("product_type") in DOWNLOADABLE_PRODUCT_TYPES})
        click.echo(f"Found {len(stale_files)} stale file(s) across "
                   f"{len(target_obs) + len(target_fields)} scope(s)")
        gratings = filters = tiles = None
    else:
        if not sel.scoped:
            (_list_spectroscopy if sel.modality is SPECTROSCOPY else _list_imaging)(store)
            store.close()
            return
        target_obs, target_fields = _resolve_scope(sel, store, catalog_obs)
        product_types = sel.product_types
        gratings = sel.gratings or None
        filters = sel.filters or None
        tiles = sel.tiles or None

        # The catalog sync mirrors finals only; the other products of this
        # selection are indexed here, on demand, so nobody pages through them otherwise.
        to_index = [t for t in product_types if t not in FINAL_PRODUCT_TYPES]
        if to_index:
            names = [p for p in sel.products if p not in sel.modality.finals(sel.instruments)]
            click.echo(f"Indexing {', '.join(names)} for this selection...")
            try:
                refreshed = refresh_storage_scope(
                    api, store, product_types=to_index,
                    observations=target_obs or None, fields=target_fields or None,
                    show_progress=True)
                click.echo(f"  {refreshed['rows']} object(s) indexed.")
            except Exception as e:
                click.echo(f"  ⚠ Could not index: {e}", err=True)
                click.echo("  Continuing with what is already indexed locally.")
        _check_narrowers(sel, store, target_obs, target_fields)

    if not target_obs and not target_fields:
        click.echo("Nothing to download.")
        store.close()
        return

    # Reconcile DB with filesystem before planning — scoped to this pull's
    # selection so the pre-flight stays O(selection), not O(tree).
    verify = store.verify_local_objects(
        _products_dir(), product_types=product_types, show_progress=True,
        observations=target_obs or None, fields=target_fields or None)
    if verify["cleared"]:
        click.echo(f"  Detected {verify['cleared']} missing local file(s), will re-download.")
    if verify.get("rehashed"):
        click.echo(f"  Re-verified {verify['rehashed']} modified local file(s).")
    if verify["discovered"]:
        click.echo(f"  Found {verify['discovered']} existing local file(s), skipping download.")
    if verify.get("mismatched"):
        click.echo(f"  {verify['mismatched']} local file(s) don't match the cloud object "
                   f"(partial/foreign); will re-download.")

    click.echo("Checking files...")
    pending = store.get_pending_objects(
        observations=target_obs, product_types=product_types, gratings=gratings,
        fields=target_fields, filters=filters, tiles=tiles)
    total_files, total_bytes = _show_plan(pending, target_obs + target_fields)

    if total_files == 0:
        click.echo("\nAll files up to date.")
        store.close()
        # Annotations are not hash-tracked — regenerate them even when the
        # products are current (an inspector may have edited masks since).
        _maybe_pull_annotations(target_obs, target_fields, no_annotations)
        return
    if dry_run:
        click.echo(f"\nDry run: would download {total_files} files ({format_size(total_bytes)})")
        store.close()
        return
    if not yes:
        click.echo(f"\nDownload {total_files} files ({format_size(total_bytes)})?")
        if not click.confirm("Proceed?", default=True):
            store.close()
            return

    # One product-type-agnostic pass over the planned set: presigned, fetched,
    # verified against content_hash, recorded in the storage_objects mirror.
    click.echo()
    dl_session = create_download_session(max_workers=workers)
    api_session._ensure_valid_token()
    stats = download_objects(
        api, target_obs, product_types, store, _products_dir(),
        max_workers=workers, download_session=dl_session,
        gratings=gratings, fields=target_fields, filters=filters, tiles=tiles)

    click.echo("\n✓ Download complete")
    click.echo(f"  Files downloaded: {stats.get('downloaded', 0)}")
    if stats.get("failed"):
        click.echo(f"  Files failed: {stats['failed']}")
    if stats.get("unauthorized"):
        click.echo(f"  Files skipped (no access): {stats['unauthorized']}")
    click.echo(f"  Total size: {format_size(total_bytes)}")
    store.close()
    _maybe_pull_annotations(target_obs, target_fields, no_annotations)


def _pull_release(tag, sel, workers, yes, dry_run, base_url):
    """`campfire pull <modality> --dr TAG` (P5-2): the release's export files into its own
    store (campfire.dr_store.load_release), then the archived bytes of the
    selection through the release-aware presign into dr/TAG/products/,
    verified against the release manifest and recorded on the release store,
    so an interrupted pull resumes at file granularity. The live tree and the
    live mirror are never touched."""
    from .config import resolve_data_dir
    from .dr_store import load_release, open_store, release_dir
    from .exceptions import DRLoadError
    from .sync import download_objects, format_size
    from .api.session import create_download_session

    finals = sel.modality.finals(sel.instruments)
    extra = [p for p in sel.products if p not in finals]
    if extra:
        click.echo(f"✗ A data release holds its finals ({', '.join(finals)}), frozen; "
                   f"{', '.join(extra)} are not in it.", err=True)
        sys.exit(2)

    base_url = base_url or resolve_base_url()
    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)

    data_dir = resolve_data_dir()
    click.echo(f"Loading release {tag}...")
    try:
        report = load_release(api_session, tag, data_dir=data_dir, log=click.echo)
    except (DRLoadError, RuntimeError) as e:
        click.echo(f"✗ {e}", err=True)
        sys.exit(1)
    if report.rebuilt:
        counts = ", ".join(f"{v} {k}" for k, v in report.counts.items())
        click.echo(f"  store built from {len(report.fetched) + len(report.kept)} file(s): {counts}")
    else:
        click.echo(f"  store up to date ({len(report.kept)} file(s) verified)")

    root = release_dir(data_dir, tag)
    products = root / "products"
    store = open_store(root)
    try:
        if not sel.scoped:
            (_list_spectroscopy if sel.modality is SPECTROSCOPY else _list_imaging)(store, tag=tag)
            return
        target_obs, target_fields = _resolve_scope(
            sel, store, store.get_synced_observations(), where=f"release {tag} (or not in your programs)")
        product_types = sel.product_types
        _check_narrowers(sel, store, target_obs, target_fields)

        # deep: a present file is hashed, never adopted on its size alone. A
        # live final copied into the release tree can have the stamped copy's
        # exact size (the CMPFRDR card usually fits in the header padding), so
        # a size match says nothing about which bytes these are.
        verify = store.verify_local_objects(products, product_types=product_types, show_progress=True,
                                            observations=target_obs or None, fields=target_fields or None,
                                            deep=True)
        if verify.get("discovered"):
            click.echo(f"  Found {verify['discovered']} existing local file(s), skipping download.")
        if verify.get("cleared") or verify.get("mismatched"):
            click.echo(f"  {verify.get('cleared', 0) + verify.get('mismatched', 0)} local file(s) missing or "
                       f"not the release's bytes; will fetch.")

        grating_list = sel.gratings or None
        filter_list = sel.filters or None
        tile_list = sel.tiles or None
        pending = store.get_pending_objects(observations=target_obs, product_types=product_types,
                                            gratings=grating_list, fields=target_fields,
                                            filters=filter_list, tiles=tile_list)
        total_files, total_bytes = _show_plan(pending, target_obs + target_fields, verb_new="to fetch")
        if total_files == 0:
            click.echo(f"\nRelease {tag}: everything selected is on disk under {products}.")
            return
        if dry_run:
            click.echo(f"\nDry run: would fetch {total_files} file(s) ({format_size(total_bytes)}) into {products}")
            return
        if not yes and not click.confirm(f"\nFetch {total_files} file(s) ({format_size(total_bytes)})?", default=True):
            return

        click.echo()
        api_session._ensure_valid_token()
        stats = download_objects(api, target_obs, product_types, store, products,
                                 max_workers=workers, download_session=create_download_session(workers),
                                 gratings=grating_list, fields=target_fields, filters=filter_list,
                                 tiles=tile_list, dr=tag)
        click.echo(f"\n✓ Release {tag}: {stats.get('downloaded', 0)} file(s) fetched into {products}")
        if stats.get("failed"):
            click.echo(f"  Files failed: {stats['failed']} (run the same pull again)")
        if stats.get("unauthorized"):
            click.echo(f"  Files skipped: {stats['unauthorized']} (not archived by the release, or not yours)")
    finally:
        store.close()


def _pull_provenance(file, yes, dry_run, base_url):
    """`campfire pull --provenance FILE` (design §4.8): the bytes a provenance
    file records, from wherever still holds them. The server is asked what it
    knows about each recorded hash (identify); a file live still carries
    exactly lands in the live tree, one a release holds lands in that
    release's tree after its catalog is loaded, and a file nothing holds is
    reported (exit 1). A destination holding other bytes is set aside under
    .superseded/ first."""
    import json as _json

    from .config import resolve_data_dir
    from .dr_store import load_release
    from .provenance import ProvenanceError, load, pull

    try:
        doc = load(file)
    except (ProvenanceError, ValueError) as e:
        click.echo(f"✗ {e}", err=True)
        sys.exit(2)
    if not doc.get("files"):
        click.echo("Nothing to fetch: the file records no spectra.")
        return
    base_url = base_url or resolve_base_url()
    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)
    data_dir = resolve_data_dir()

    def _loader(tag):
        click.echo(f"Loading release {tag}...")
        load_release(api_session, tag, data_dir=data_dir, log=click.echo)

    def _show(result):
        names = [p.get("spectrum_id") or p.get("storage_key") or "?" for p in result["files"]]
        width = min(max((len(n) for n in names), default=1), 50)
        for name, p in zip(names, result["files"]):
            if p["source"] is None:
                click.echo(f"{name:<{width}}  unresolved: {p['reason']}")
                continue
            where = "live" if p["source"] == "live" else p["tag"]
            line = f"{name:<{width}}  {where}, {p['match']}  →  {Path(p['dest']).relative_to(data_dir)}  [{p['status']}]"
            if p.get("set_aside"):
                line += f"  (previous bytes kept at {Path(p['set_aside']).relative_to(data_dir)})"
            if p.get("error"):
                line += f"  {p['error']}"
            click.echo(line)

    result = pull(doc, data_dir, api=api, dry_run=True)
    _show(result)
    s = result["summary"]
    todo = s["to-fetch"]
    if s["unresolved"]:
        click.echo(f"\n{s['unresolved']} file(s) resolve nowhere you can see: not in live, not in a release "
                   f"(bytes not kept).")
    if dry_run:
        click.echo(f"\nDry run: would fetch {todo} file(s) ({s['present']} already on disk).")
        sys.exit(1 if result["problems"] else 0)
    if todo and not yes and not click.confirm(f"\nFetch {todo} file(s)?", default=True):
        return
    if any(p["source"] for p in result["files"]):
        # the real run, whenever anything resolved: fetch, load the catalog of
        # every release drawn on, and record present files on their store
        # (a rebuilt mirror or a copied tree learns of them here)
        click.echo()
        api_session._ensure_valid_token()
        result = pull(doc, data_dir, api=api, release_loader=_loader, log=click.echo)
        s = result["summary"]
        for name, p in zip([p.get("spectrum_id") or p.get("storage_key") or "?" for p in result["files"]],
                           result["files"]):
            if p["status"] in ("refused", "failed"):
                click.echo(f"  {name}: {p['status']}" + (f": {p['error']}" if p.get("error") else ""))
            if p.get("set_aside"):
                click.echo(f"  {name}: previous bytes kept at {Path(p['set_aside']).relative_to(data_dir)}")
    click.echo(f"\n{'✓' if not result['problems'] else '✗'} {s['fetched']} file(s) fetched, {s['present']} already on disk"
               + (f", {s['refused']} refused by the server" if s["refused"] else "")
               + (f", {s['failed']} failed (run the same pull again)" if s["failed"] else "")
               + (f", {s['unresolved']} unresolved" if s["unresolved"] else "")
               + (f", {s['catalog_not_loaded']} in a release whose catalog did not load (run again)"
                  if s.get("catalog_not_loaded") else "") + ".")
    if result["problems"]:
        sys.exit(1)


# Back-compat alias: `campfire download` ≡ `campfire pull` (the same group).
cli.add_command(cli.commands["pull"], name="download")


@cli.command(name="identify")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--offline", is_flag=True, help="Only the stores on this machine; do not ask the server")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable: hashes, every match, the sentence")
@click.option("--base-url", default=None, help="API base URL")
def identify(files, offline, as_json, base_url):
    """What the archive knows about FILES, from their bytes (P5-2).

    Hashes each file (whole-file sha256, plus the science hash when the
    pipeline is installed), looks the hashes up in the live mirror and every
    release store on this machine, then asks the server under your program
    scope. One line per file: "in DR2026.1, still current", "in DR2026.1,
    superseded in live on <date>", "live only, deployed <date>, not in any
    DR", "archived, collected on <date>", or unknown.
    """
    import json as _json

    from .config import resolve_data_dir
    from .identify import identify_paths, subject

    api = None
    if not offline:
        try:
            api = APIClient(APISession(base_url=base_url or resolve_base_url()))
        except AuthenticationError as e:
            click.echo(f"  (not asking the server: {e})", err=True)
    results = identify_paths(files, data_dir=resolve_data_dir(), api=api)
    if as_json:
        click.echo(_json.dumps([r.as_dict() for r in results], indent=2, default=str))
        return
    width = min(max(len(r.path.name) for r in results), 60)
    for r in results:
        if r.error:
            click.echo(f"{r.path.name:<{width}}  ✗ {r.error}")
            continue
        what = subject(r)
        click.echo(f"{r.path.name:<{width}}  {r.sentence}" + (f"  [{what}]" if what else ""))


@cli.command(name="cite")
@click.argument("tag")
@click.option("--program", "programs", multiple=True, metavar="SLUG",
              help="Cite the release as read through these programs only")
@click.option("--obs", "observations", multiple=True, metavar="NAME",
              help="... through the programs of these observations (needs the pulled release)")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable: tag, DOI, programs, the sentence")
@click.option("--base-url", default=None, help="API base URL")
def cite(tag, programs, observations, as_json, base_url):
    """The citation sentence for data release TAG (P5-3).

    One DOI per release; proprietary programs are described, not cited:
    "CAMPFIRE DR2026.1 (doi:...), plus programs ember and zenith reduced with
    cfpipe 0.7.0 and CRDS context jwst_1535.pmap as in that release." Read
    from the pulled release under dr/TAG/ when there is one (no network),
    else from the server. Work from live has no citation yet: write a
    provenance file (campfire provenance).
    """
    import json as _json

    from .cite import cite_sentence, reader_programs, versions_from_store
    from .config import resolve_data_dir
    from .dr_store import open_store, read_release_json, release_dir
    from .exceptions import CampfireError

    try:
        root = release_dir(resolve_data_dir(), tag)
    except ValueError as e:
        click.echo(f"✗ {e}", err=True)
        sys.exit(2)
    release = read_release_json(root)
    if release is None:
        try:
            api = APIClient(APISession(base_url=base_url or resolve_base_url()))
            release = api.release_files(tag)
        except CampfireError as e:
            click.echo(f"✗ release {tag} is not on this machine and the server did not answer: {e}", err=True)
            sys.exit(1)
    chosen = set(programs)
    sentence = None
    if (root / "meta" / "campfire.db").exists():
        store = open_store(root)
        try:
            for obs in observations:
                progs = {s["program_slug"] for s in store.query_spectra(observations=[obs], limit=None)
                         if s.get("program_slug")}
                if not progs:
                    click.echo(f"✗ Observation '{obs}' is not in release {tag} (or not in your programs).", err=True)
                    sys.exit(1)
                chosen |= progs
            selection = sorted(chosen) if chosen else None
            sentence = cite_sentence(release, selection, versions_of=lambda progs: versions_from_store(store, progs))
        finally:
            store.close()
    elif observations:
        click.echo(f"✗ --obs needs the pulled release (campfire pull spec --dr {tag}); select with --program.", err=True)
        sys.exit(2)
    selection = sorted(chosen) if chosen else None
    if sentence is None:
        sentence = cite_sentence(release, selection)
    if as_json:
        click.echo(_json.dumps({"tag": release.get("tag", tag), "doi": release.get("doi"),
                                "programs": selection or reader_programs(release), "sentence": sentence}, indent=2))
    else:
        click.echo(sentence)


@cli.group(name="provenance", invoke_without_command=True)
@click.option("--list", "lists", multiple=True, metavar="SLUG", help="The objects of this list")
@click.option("--obs", "observations", multiple=True, metavar="NAME", help="The objects of this observation")
@click.option("--program", "programs", multiple=True, metavar="SLUG", help="The objects of this program")
@click.option("--object", "objects", multiple=True, metavar="NAME", help="One object by name")
@click.option("-o", "--output", type=click.Path(dir_okay=False, path_type=Path), default=None,
              help="Write the file here (default: stdout)")
@click.pass_context
def provenance_group(ctx, lists, observations, programs, objects, output):
    """Write a provenance file for work done from live (P5-3).

    paper.campfire.json records the objects of a selection as they are now
    (version, redshift, quality) and every spectrum's file with the sha256 of
    the bytes on this machine beside the registry's, the pipeline version,
    and the data releases on this machine already holding it. Built from the
    local ledgers only; never sent anywhere. `campfire provenance check`
    re-reads one later.
    """
    if ctx.invoked_subcommand is not None:
        return
    import json as _json

    from .config import resolve_data_dir
    from .provenance import ProvenanceError, build

    data_dir = resolve_data_dir()
    db = data_dir / "meta" / "campfire.db"
    if not db.exists():
        click.echo("✗ No local catalog; run `campfire sync` first.", err=True)
        sys.exit(1)
    from .db.store import LocalStore
    store = LocalStore(db)
    try:
        doc = build(store, data_dir, lists=lists, observations=observations, programs=programs, objects=objects,
                    base_url=resolve_base_url(), latest_dr=store.get_meta(_LATEST_DR_META),
                    log=lambda m: click.echo(m, err=True))
    except ProvenanceError as e:
        click.echo(f"✗ {e}", err=True)
        sys.exit(2)
    finally:
        store.close()
    text = _json.dumps(doc, indent=2)
    if output:
        output.write_text(text + "\n", encoding="utf-8")
        s = doc["summary"]
        click.echo(f"✓ {output}: {s['objects']} object(s), {s['files']} file(s) ({s['files_on_disk']} on disk)")
    else:
        click.echo(text)


@provenance_group.command(name="check")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--dr", "dr_tag", default=None, metavar="TAG",
              help="Also say whether this pulled release holds each file's science")
@click.option("--offline", is_flag=True, help="Only the stores on this machine; do not ask the server")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable")
@click.option("--base-url", default=None, help="API base URL")
def provenance_check(file, dr_tag, offline, as_json, base_url):
    """Re-read a provenance FILE: are the files still those bytes, what does
    the archive say about them now, and (with --dr) does that release hold
    the same science. Exit 1 when a file was modified or no longer resolves."""
    import json as _json

    from .config import resolve_data_dir
    from .db.store import LocalStore, SchemaMismatchError
    from .provenance import ProvenanceError, check, load

    try:
        doc = load(file)
    except (ProvenanceError, ValueError) as e:
        click.echo(f"✗ {e}", err=True)
        sys.exit(2)
    data_dir = resolve_data_dir()
    api = None
    if not offline:
        try:
            api = APIClient(APISession(base_url=base_url or resolve_base_url()))
        except AuthenticationError as e:
            click.echo(f"  (not asking the server: {e})", err=True)
    store = None
    db = data_dir / "meta" / "campfire.db"
    if db.exists():
        try:
            store = LocalStore(db)
        except SchemaMismatchError:
            store = None
    try:
        result = check(doc, data_dir, store=store, api=api, dr=dr_tag)
    except ProvenanceError as e:
        click.echo(f"✗ {e}", err=True)
        sys.exit(1)
    finally:
        if store is not None:
            store.close()
    if as_json:
        click.echo(_json.dumps(result, indent=2, default=str))
    else:
        names = [r.get("spectrum_id") or r.get("storage_key") or "?" for r in result["files"]]
        width = min(max((len(n) for n in names), default=1), 50)
        for name, r in zip(names, result["files"]):
            line = f"{name:<{width}}  local: {r['local']:<12} archive: {r['archive']}"
            if "release" in r:
                line += f"  |  {r['release']}"
            click.echo(line)
        for o in result["objects"]:
            if o["state"] != "unchanged":
                click.echo(f"  object {o['object_id']}: {o['state']}")
        s = result["summary"]
        click.echo(f"\n{s['files']} file(s): {s['ok']} unchanged on disk, {s['modified']} modified, "
                   f"{s['missing']} not on disk; {len(result['objects']) - s['objects_changed']} object(s) "
                   f"unchanged, {s['objects_changed']} changed.")
    if result["problems"]:
        click.echo(f"⚠ {result['problems']} file(s) no longer resolve as recorded.", err=True)
        sys.exit(1)


def _maybe_pull_redshifts(obs_names, config) -> None:
    """Materialize inspected redshifts to reference/nirspec/<obs>/redshifts.toml.

    Any user with program access can read them (RLS on targets/objects), so
    this runs for every scoped pull; failures print a note, never fail the pull.
    """
    try:
        from campfire.deploy.nirspec_redshifts import pull_redshifts
    except ImportError:
        return
    for obs in obs_names:
        try:
            pull_redshifts(obs, config)
        except Exception as e:
            click.echo(f"  (redshifts for {obs} skipped: {e})")


def _maybe_pull_annotations(obs_names, nircam_fields, disabled: bool) -> None:
    """Regenerate review annotations for a scoped pull (admins, deploy extra).

    The annotation half of `campfire pull`: web-authored review state (masks,
    stuck shutters, bkg overrides, exclusions) regenerated into reference/ for
    the pipeline, plus the inspected redshifts (any user with program access;
    consumed by `cfpipe nirspec linefit`). Best-effort and quiet by design —
    consumers without the deploy extra (or, for the admin-only annotations,
    without admin) skip silently; a real failure prints a note but never fails
    the pull.
    """
    if disabled or (not obs_names and not nircam_fields):
        return
    try:
        from campfire.deploy.annotations import (
            pull_field_annotations,
            pull_observation_annotations,
        )
        from campfire.deploy.config import load_config
        from campfire.deploy.supabase import get_supabase_client
    except ImportError:
        return  # consumer install — annotations are an operator concern

    try:
        config = load_config(None)
        sb = get_supabase_client(config)
        # Inspected redshifts (for `cfpipe nirspec linefit`) need only program
        # access, so they are pulled before — and regardless of — the admin gate
        # that guards the reducer-side annotations below.
        if obs_names:
            _maybe_pull_redshifts(obs_names, config)
        try:
            if not sb.rpc("is_admin").execute().data:
                return
        except Exception:
            return
        for obs in obs_names:
            pull_observation_annotations(obs, config)
        for field in nircam_fields:
            pull_field_annotations(field, config)
    except Exception as e:
        click.echo(f"  (annotations skipped: {e})")


def _verify_options(f):
    f = click.option("--json", "as_json", is_flag=True,
                     help="Machine-readable report (for CI); exit 1 on drift")(f)
    f = click.option("--deep", is_flag=True,
                     help="Content-hash changed/discovered files instead of the "
                          "size-match quick check (reads every such file)")(f)
    return f


@cli.group(name="verify", cls=ModalityGroup, legacy_flags=("--obs", "--field"),
           invoke_without_command=True)
@click.option("--cloud", "cloud", is_flag=True,
              help="Also verify the cloud registry against the actual buckets "
                   "(admin; needs S3 LIST credentials — OSN primary, legacy R2)")
@click.option("--local", is_flag=True,
              help="--cloud against local Supabase (127.0.0.1:54321)")
@_verify_options
@click.pass_context
def verify(ctx, cloud, local, deep, as_json):
    """Verify local tree ↔ index, and optionally index ↔ cloud buckets.

    Without --cloud: reconciles the local products tree against the storage
    index via one bulk directory scan (no file reads): clears vanished files,
    and adopts present files whose size matches the cloud object (rsync-style
    quick check — the server hash is presumed, not re-computed). --deep
    additionally content-hashes every changed/discovered file. The whole tree
    by default; `verify spec --obs X` / `verify im --field Y` scope the scan.

    With --cloud (admin): additionally compares live DB pointers vs the
    registry vs actual bucket LISTs — OSN first (the data home), then the
    legacy R2 data bucket. Exits non-zero when live pointers lack registry
    rows (missing) or registry rows lack bucket objects (dangling), so a
    scheduled CI run can alarm on drift. It checks the whole catalog, so it
    takes no modality.
    """
    if ctx.invoked_subcommand:
        if cloud or local:
            raise click.UsageError("--cloud checks the whole catalog; run it without "
                                   "spec / im.", ctx)
        ctx.obj = dict(deep=deep, as_json=as_json)
        return
    _verify(None, None, None, cloud=cloud, deep=deep, as_json=as_json, local=local)


@click.command(name="spectroscopy")
@click.option("--obs", "obs_filter", multiple=True, cls=_VariadicOption, required=True,
              help="Observation name(s)")
@_verify_options
@click.pass_context
def verify_spectroscopy(ctx, obs_filter, deep, as_json):
    """The local spectra and exposures of these observations."""
    parent = ctx.obj or {}
    _verify(normalize_names(obs_filter), None, SPECTROSCOPY,
            cloud=False, deep=deep or parent.get("deep", False),
            as_json=as_json or parent.get("as_json", False), local=False)


@click.command(name="imaging")
@click.option("--field", "field_filter", multiple=True, cls=_VariadicOption, required=True,
              help="Field name(s)")
@_verify_options
@click.pass_context
def verify_imaging(ctx, field_filter, deep, as_json):
    """The local mosaics, exposures and expmaps of these fields."""
    parent = ctx.obj or {}
    _verify(None, normalize_names(field_filter), IMAGING,
            cloud=False, deep=deep or parent.get("deep", False),
            as_json=as_json or parent.get("as_json", False), local=False)


verify.add_modality(verify_spectroscopy, SPECTROSCOPY.alias)
verify.add_modality(verify_imaging, IMAGING.alias)


def _verify(observations, fields, modality, *, cloud, deep, as_json, local):
    import json as _json

    from .config import products_dir as _products_dir

    payload = {}
    if not as_json:
        click.echo("Verifying local tree against the index...")
    store = _open_store()
    product_types = None
    if modality is not None:
        product_types = modality.pull_types(
            [n for n in modality.product_names()
             if any(k.pullable for k in modality.kinds([n]))])
    local_totals = store.verify_local_objects(
        _products_dir(),
        product_types=product_types,
        observations=observations or None,
        fields=fields or None,
        deep=deep,
        show_progress=not as_json,
    )
    store.close()
    payload["local"] = local_totals
    if not as_json:
        click.echo(f"  cleared (file vanished): {local_totals['cleared']}")
        click.echo(f"  {'re-hashed' if deep else 'refreshed'} (file changed): "
                   f"{local_totals['rehashed']}")
        click.echo(f"  discovered (already present): {local_totals['discovered']}")
        if local_totals.get("mismatched"):
            click.echo(f"  mismatched (size ≠ cloud object; left pending): "
                       f"{local_totals['mismatched']}")

    drift = False
    if cloud:
        try:
            from campfire.deploy.config import load_config
            from campfire.deploy.registry import cloud_reconcile_report
            from campfire.deploy.supabase import get_supabase_client
        except ImportError:
            click.echo("✗ --cloud requires the deploy extra: pip install campfire[deploy]")
            sys.exit(2)
        config = load_config(None, local=local)
        sb = get_supabase_client(config)
        report = cloud_reconcile_report(config, sb)
        payload["cloud"] = report
        drift = not report.get("ok", False)
        if not as_json:
            click.echo(f"\nCloud registry: {report['registry_rows']} rows, "
                       f"{report['live_pointers']} live DB pointers")
            if report["missing"]:
                click.echo(f"  ✗ {len(report['missing'])} live pointer(s) with NO registry row:")
                for k in report["missing"][:10]:
                    click.echo(f"      missing: {k}")
            for label, b in report["backends"].items():
                click.echo(f"  [{label}] bucket objects: {b['bucket_objects']}, "
                           f"registry rows homed here: {b['registry_rows']}")
                if b["dangling"]:
                    click.echo(f"    ✗ {len(b['dangling'])} registry row(s) with no bucket object:")
                    for k in b["dangling"][:10]:
                        click.echo(f"        dangling: {k}")
                if b["orphans"]:
                    click.echo(f"    {len(b['orphans'])} orphan bucket object(s) "
                               f"({len(b['adoptable'])} adoptable)")
                if b.get("unregistered_archive"):
                    click.echo(f"    ✗ {len(b['unregistered_archive'])} unregistered archive "
                               f"object(s) (a retain copied bytes but never registered them; "
                               f"never adopted as live):")
                    for k in b["unregistered_archive"][:10]:
                        click.echo(f"        unregistered: {k}")
            for label, err in report.get("errors", {}).items():
                click.echo(f"  [{label}] bucket LIST unavailable: {err}")
            click.echo("\n  Cloud verification: "
                       + ("PASS" if report.get("ok") else "DRIFT DETECTED"))

    if as_json:
        click.echo(_json.dumps(payload, indent=2))
    if drift:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Data releases (admin)
# ---------------------------------------------------------------------------


@cli.group(name="dr")
def dr_group():
    """Data releases: frozen, citable snapshots of the published catalog.

    \b
    For readers: `dr list` shows the releases you may read; `campfire pull
    --dr TAG` fetches one. The other commands are the admin's release
    workflow.

    \b
    A release goes through four steps, in this order:
      1. freeze    snapshot the published catalog into frozen tables
                   (minutes; refused while any deploy is running)
      2. archive   verify every file the release names (its manifest) and
                   copy it under archive/, so redeploys cannot break it
                   (hours for the first release: it reads every mosaic once)
      3. export    write the release's export files (gzip JSONL per stream
                   and program) for readers to download
      4. publish   make it visible to readers (a deliberate step: DOI,
                   known issues; review `dr diff` first)

    \b
    `campfire dr build --tag T --nirspec ...` runs steps 1-3 and resumes
    where it stopped; each step can also be run on its own, and each ends by
    printing the next one. `dr status` shows every release; `dr diff`
    compares one with live; `dr refs --obs X` lists the releases that need
    an observation; `dr withdraw` hides a published release; `dr purge`
    deletes an unpublished one.
    """


def _dr_hint(sb, tag: str, say=click.echo) -> None:
    """Print the release's next step (best effort)."""
    from .dr import fetch_next_step
    hint = fetch_next_step(sb, tag)
    if hint:
        say(hint)


def _check_conditional_put(s3, say) -> None:
    """Every archive write is a conditional PUT (If-None-Match); a botocore that
    predates S3 conditional writes rejects the parameter outright."""
    model = getattr(getattr(s3, 'meta', None), 'service_model', None)
    if model is not None and 'IfNoneMatch' not in model.operation_model('PutObject').input_shape.members:
        say("✗ this boto3/botocore cannot send If-None-Match on PutObject; upgrade it (pip install -U boto3)")
        sys.exit(2)


@dr_group.command(name="list")
@click.option("--json", "as_json", is_flag=True, help="Print the releases as JSON")
@click.option("--base-url", default=None, help="API base URL")
def dr_list(as_json, base_url):
    """The data releases you may read (P5-2): published ones; every state
    past its freeze for admins. Pull one with `campfire pull spec --dr TAG`."""
    import json as _json

    try:
        api = APIClient(APISession(base_url=base_url or resolve_base_url()))
    except AuthenticationError as e:
        click.echo(f"✗ {e}")
        sys.exit(1)
    releases = api.list_releases()
    if as_json:
        click.echo(_json.dumps(releases, indent=2, default=str))
        return
    if not releases:
        click.echo("No data release is published yet.")
        return
    from .dr import _when
    click.echo(f"  {'TAG':<16} {'STATE':<10} {'FROZEN':<12} {'PUBLISHED':<12} {'SPECTRA':>8}  DOI")
    for r in releases:
        counts = r.get("counts") or {}
        click.echo(f"  {r['tag']:<16} {r.get('state', ''):<10} {_when(r.get('frozen_at'))[:10]:<12} "
                   f"{_when(r.get('published_at'))[:10]:<12} {str(counts.get('spectra', '')):>8}  {r.get('doi') or ''}")


@dr_group.command(name="status")
@click.option("--json", "as_json", is_flag=True,
              help="Print the raw dr_readiness() report as JSON")
@click.option("--local", is_flag=True,
              help="Against local Supabase (127.0.0.1:54321)")
def dr_status(as_json, local):
    """Is the catalog ready to freeze? The stage-1 readiness numbers.

    Calls the admin-only dr_readiness() RPC and prints: published spectra by
    cfpipe_version (by observation count), draft/revoked rows, deploys in
    progress (the deploy lease), NIRCam fields with mosaics back in draft,
    registry rows pointing at deleted spectra, provisional etag: hashes,
    line-fit staleness, needs-review / implicit-pin counts and (once deployed)
    science-hash coverage. Login mode (campfire login, RLS) is the normal path;
    CAMPFIRE_DEPLOY_MODE=service-role works too.
    """
    import json as _json

    from .dr import fetch_readiness, format_readiness

    try:
        from campfire.deploy.config import load_config
        from campfire.deploy.supabase import get_supabase_client
    except ImportError:
        click.echo("✗ `campfire dr` needs the deploy extra: pip install campfire[deploy]")
        sys.exit(2)
    try:
        config = load_config(None, local=local)
        sb = get_supabase_client(config)
        report = fetch_readiness(sb)
    except Exception as e:  # noqa: BLE001 - one line, not a traceback
        msg = getattr(e, "message", None) or str(e)
        if "Admin privileges required" in msg:
            click.echo("✗ dr status is admin-only.")
        elif "dr_readiness" in msg and ("404" in msg or "not find" in msg.lower()):
            click.echo("✗ dr_readiness() is not deployed on this database yet.")
        else:
            click.echo(f"✗ Could not fetch the readiness report: {msg}")
        sys.exit(1)

    if as_json:
        click.echo(_json.dumps(report, indent=2, sort_keys=True))
    else:
        click.echo(format_readiness(report))



def _dr_client(local: bool, *, what: str):
    """Resolve config + Supabase client for a dr verb, admin-gated in login mode."""
    try:
        from campfire.deploy.cli import _gate_admin
        from campfire.deploy.config import load_config
        from campfire.deploy.supabase import get_supabase_client
    except ImportError:
        click.echo("✗ `campfire dr` needs the deploy extra: pip install campfire[deploy]")
        sys.exit(2)
    config = load_config(None, local=local)
    _gate_admin(config, what=what)
    return config, get_supabase_client(config)


@dr_group.command(name="freeze")
@click.option("--tag", required=True, metavar="TAG",
              help="Release tag, e.g. DR2026.1 (letters, digits, . _ -)")
@click.option("--nirspec", is_flag=True, help="Include the NIRSpec catalog component")
@click.option("--field", "fields", multiple=True, metavar="FIELD",
              help="Include the published NIRCam mosaics of FIELD (repeatable)")
@click.option("--photometry", "photometry", multiple=True, metavar="FIELD/CATALOG",
              help="Include a photometry catalog component, e.g. cosmos/'COSMOS v1.2' (repeatable)")
@click.option("--notes", default=None, help="Free-text notes stored on the release row")
@click.option("--inline", is_flag=True,
              help="Run the freeze inside the RPC call instead of a pg_cron job "
                   "(local stacks; production uses the job)")
@click.option("--no-wait", is_flag=True, help="Return after enqueueing; poll with `dr status`")
@click.option("--poll", type=float, default=5.0, show_default=True, metavar="SECONDS",
              help="Polling interval while waiting")
@click.option("--timeout", type=float, default=3600.0, show_default=True, metavar="SECONDS",
              help="Give up waiting after this long (the freeze keeps running server-side)")
@click.option("--json", "as_json", is_flag=True, help="Print the final release row as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase")
def dr_freeze(tag, nirspec, fields, photometry, notes, inline, no_wait, poll, timeout, as_json, local):
    """Freeze a data release: frozen tables from the published catalog.

    Writes the frozen tables for TAG in one snapshot (P4a; full-row copies
    since P6b): dr_spectra, dr_targets, dr_objects, dr_line_fits,
    dr_lines, dr_catalog_matches, dr_photometry, dr_nircam_images,
    dr_fields, dr_filter_options and dr_manifest. Refused while a deploy
    lease is live or TAG is already published; re-freezing an unpublished
    tag rewrites it. Nothing is visible to non-admins until `dr publish`.
    On production the work runs as a one-shot pg_cron job and this command
    polls until it finishes; --local stacks run it inline.
    """
    import json as _json

    from .dr import (FREEZE_TERMINAL_STATES, components_from_options, enqueue_freeze,
                     format_release, wait_for_freeze)

    try:
        components = components_from_options(nirspec=nirspec, fields=fields, photometry=photometry)
    except ValueError as e:
        click.echo(f"✗ {e}")
        sys.exit(2)
    config, sb = _dr_client(local, what="`campfire dr freeze`")
    # --json: stdout carries the JSON document alone; progress goes to stderr
    say = (lambda m: click.echo(m, err=True)) if as_json else click.echo
    if local:
        inline = True
    try:
        summary = enqueue_freeze(sb, tag, components, notes=notes, inline=inline)
    except Exception as e:  # noqa: BLE001 - one line, not a traceback
        msg = getattr(e, "message", None) or str(e)
        say(f"✗ Could not start the freeze: {msg}")
        sys.exit(1)
    say(f"Freeze of {tag} ({', '.join(components)}): {summary.get('state')} "
        f"[{summary.get('mode')}]")
    row = summary
    if summary.get('state') == 'freezing' and not no_wait:
        try:
            row = wait_for_freeze(sb, tag, poll_seconds=poll, timeout_seconds=timeout, echo=say)
        except TimeoutError as e:
            say(f"… {e}; `campfire dr status` shows it when it lands.")
            sys.exit(3)
    if as_json:
        click.echo(_json.dumps(row, indent=2, sort_keys=True, default=str))
    else:
        click.echo(format_release(row))
    _dr_hint(sb, tag, say)
    if row.get('state') == 'failed':
        sys.exit(1)
    if row.get('state') not in FREEZE_TERMINAL_STATES:
        sys.exit(0)


def _dr_osn_s3(config, *, max_pool_connections: int = 10):
    """(s3 client, bucket) for the OSN data bucket the data releases live on,
    or (None, reason). The [r2_osn] block (CAMPFIRE_S3_OSN_*), else a data
    block that itself points at OSN."""
    from campfire.deploy.backend import make_s3_client, resolve_backend
    errors = []
    for purpose in ('osn', 'data'):
        try:
            cand = resolve_backend(config, purpose)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{purpose}: {e}")
            continue
        if cand.backend == 'osn':
            return make_s3_client(cand, max_pool_connections=max_pool_connections), cand.bucket
        errors.append(f"{purpose}: resolves to {cand.backend}, not OSN")
    return None, '; '.join(errors)


@dr_group.command(name="purge")
@click.argument("tag")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option("--keep-exports", is_flag=True,
              help="Leave the files under dr/TAG/exports/ and dr/TAG/page/")
@click.option("--keep-archive", is_flag=True,
              help="Leave the archive objects only this release references (they become collectable by `campfire archive gc`)")
@click.option("--local", is_flag=True, help="Against local Supabase")
def dr_purge(tag, yes, keep_exports, keep_archive, local):
    """Delete an UNPUBLISHED release, its files and all its frozen rows.

    The clean-up after a rehearsal freeze or a failed attempt. Published
    (or withdrawn) tags are never purged. Three things go, in this order:
    the archive objects only this release references (its byte-pass copies,
    its stamped FITS, the copies the deploy gate retained for its members;
    bytes deleted, registry rows marked collected), then the frozen rows,
    then the export and page files under dr/TAG/. The archive and the files
    live on the OSN data bucket, so those steps need OSN credentials
    (CAMPFIRE_S3_OSN_* or [r2_osn] in deploy.toml); --keep-archive and
    --keep-exports skip them. With --local nothing in the bucket is touched:
    the local database redirects Supabase only, so the bucket is still the
    shared one and the same tag may be a production release. A run
    interrupted between the deletes and the row updates is healed by running
    it again.
    """
    from .archive import CollectError, collect
    from .dr import purge_archive_plan, purge_release

    config, sb = _dr_client(local, what="`campfire dr purge`")
    s3 = bucket = None
    if local and not (keep_exports and keep_archive):
        # --local redirects the database only; the object-store config is the
        # shared one, so a local purge must never delete a (possibly
        # production) release's bytes (Codex on #600)
        click.echo(f"  --local: the archive objects and the files under dr/{tag}/ are left alone "
                   f"(the bucket is the shared one)")
        keep_exports = keep_archive = True
    if not (keep_exports and keep_archive):
        # resolved BEFORE the rows go, so a missing credential never strands
        # the files of a purged tag (nothing would name them afterwards)
        s3, bucket = _dr_osn_s3(config)
        if s3 is None:
            click.echo(f"✗ Deleting the archive objects and export files of {tag} needs OSN credentials "
                       f"({bucket}); pass --keep-archive --keep-exports to purge the rows only.")
            sys.exit(2)
    plan: list[dict] = []
    if not keep_archive:
        try:
            plan = purge_archive_plan(sb, tag)
        except Exception as e:  # noqa: BLE001 - one line, not a traceback
            msg = getattr(e, "message", None) or str(e)
            click.echo(f"✗ Could not plan the archive sweep of {tag}: {msg}")
            sys.exit(1)
    if not yes:
        what = f"Delete release {tag} and every frozen row under it"
        if plan:
            from .sync import format_size
            size = sum(int(r.get('stored_size_bytes') or r.get('size_bytes') or 0) for r in plan)
            what += f", and the {len(plan):,} archive object(s) ({format_size(size)}) only it references"
        click.confirm(what + "?", abort=True)
    if plan:
        try:
            collect(sb, s3, bucket, plan, tag=tag, log=click.echo)
        except CollectError as e:
            click.echo(f"✗ Archive sweep of {tag} stopped: {e}\n  No frozen row was touched; run the purge again.")
            sys.exit(1)
        except Exception as e:  # noqa: BLE001
            msg = getattr(e, "message", None) or str(e)
            click.echo(f"✗ Could not mark the archive objects of {tag} collected: {msg}\n"
                       f"  Their bytes may be gone (verify --cloud reports them as dangling); run the purge again.")
            sys.exit(1)
    try:
        result = purge_release(sb, tag)
    except Exception as e:  # noqa: BLE001 - one line, not a traceback
        msg = getattr(e, "message", None) or str(e)
        click.echo(f"✗ Could not purge {tag}: {msg}")
        sys.exit(1)
    deleted = result.get('deleted') or {}
    parts = [f"{int(v):,} {k}" for k, v in deleted.items() if v]
    click.echo(f"Purged {tag} (was {result.get('state_before')}): " + (' · '.join(parts) or 'no rows'))
    if s3 is not None:
        # only after the purge succeeded: a refused purge (published tag) keeps its files
        from campfire.deploy.tiles import delete_r2_prefix
        try:
            n = delete_r2_prefix(s3, bucket, f"dr/{tag}/exports/")
        except Exception as e:  # noqa: BLE001
            click.echo(f"✗ The rows are gone, but deleting dr/{tag}/exports/ failed: {e}")
            sys.exit(1)
        click.echo(f"  deleted {n:,} export file(s) under dr/{tag}/exports/")
        try:
            n = delete_r2_prefix(s3, bucket, f"dr/{tag}/page/")
        except Exception as e:  # noqa: BLE001
            click.echo(f"✗ The rows are gone, but deleting dr/{tag}/page/ failed: {e}")
            sys.exit(1)
        if n:
            click.echo(f"  deleted {n:,} page file(s) under dr/{tag}/page/")


@dr_group.command(name="export")
@click.option("--tag", required=True, metavar="TAG", help="Release tag to export")
@click.option("--json", "as_json", is_flag=True, help="Print the run report as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (refused: the bucket is the shared one)")
def dr_export(tag, as_json, local):
    """Build the export files of a frozen release (P4b).

    Writes one gzip JSONL file per (stream, program or component) under
    dr/TAG/exports/ on the OSN data bucket, from the frozen rows, here on this
    machine (OSN credentials: CAMPFIRE_S3_OSN_* or [r2_osn] in deploy.toml).
    The dr_exports ledger makes it resumable: a rerun builds only the files
    that are not ready. Needs `campfire login` as an admin.
    """
    import json as _json

    from .dr_export import format_export_report, run_export

    say = (lambda m: click.echo(m, err=True)) if as_json else click.echo
    if local:
        # a local ledger with the shared bucket: the end-of-run sweep would
        # treat a production tag's files as superseded
        say("✗ `campfire dr export --local` is refused: the bucket is the shared one.")
        sys.exit(2)
    config, sb = _dr_client(False, what="`campfire dr export`")
    s3, bucket = _dr_osn_s3(config)
    if s3 is None:
        say(f"✗ `campfire dr export` needs OSN credentials for the data bucket ({bucket}).")
        sys.exit(2)
    say(f"Exporting {tag} …")
    try:
        report = run_export(sb, s3, bucket, tag, log=say)
    except Exception as e:  # noqa: BLE001 - one line, not a traceback
        say(f"✗ {getattr(e, 'message', None) or e}")
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(report.as_dict(), indent=2, sort_keys=True, default=str))
    else:
        click.echo(format_export_report(report))
    _dr_hint(sb, tag, say)
    if report.failed or report.remaining > 0:
        sys.exit(1)


@dr_group.command(name="archive")
@click.option("--tag", required=True, metavar="TAG", help="Release tag whose member bytes to archive")
@click.option("--obs", default=None, metavar="OBS", help="Only the NIRSpec members of one observation (the staged first run)")
@click.option("--field", default=None, metavar="FIELD", help="Only the NIRCam mosaic members of one field")
@click.option("--workers", type=int, default=8, show_default=True, help="Parallel transfers")
@click.option("--limit", type=int, default=None, help="Process at most N members this run")
@click.option("--retry-failed", is_flag=True, help="Take up members a previous run marked failed")
@click.option("--dry-run", is_flag=True, help="Report what each pending member would need; touch nothing")
@click.option("--status", "status_only", is_flag=True, help="Only print the byte-pass progress")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (127.0.0.1:54321)")
def dr_archive(tag, obs, field, workers, limit, retry_failed, dry_run, status_only, as_json, local):
    """Archive the files of a frozen release (P4c): its manifest, under archive/.

    Every file is first verified against the hash recorded at the freeze.
    NIRSpec FITS finals are stamped with CMPFRDR = TAG in the primary header
    (science hash unchanged, verified) and uploaded to archive/<sha256>/<relpath>;
    sidecars and mosaics are archived unstamped. A file another release already
    archived is reused, so an unchanged FITS keeps the stamp of the first
    release that archived it. Resumable: re-run until nothing is pending; a file
    whose frozen bytes are gone is marked failed with the reason. One run per
    release at a time: a second is refused while the first holds the release
    (a run that died frees it after 10 minutes), and purge waits too. Needs OSN
    credentials (CAMPFIRE_S3_OSN_* or [r2_osn] in deploy.toml) and an admin
    login. (`campfire archive gc` is the unrelated retention tool.)
    """
    import json as _json

    from campfire_layout import Scope, key_prefix
    from campfire_layout.keys import KeyScheme

    config, sb = _dr_client(local, what="`campfire dr archive`")
    # --json: stdout carries the JSON document alone; progress goes to stderr
    say = (lambda m: click.echo(m, err=True)) if as_json else click.echo
    from campfire.deploy.dr_bytes import fetch_summary, format_report, format_summary, run_byte_pass

    if obs and field:
        say("✗ Pass --obs or --field, not both.")
        sys.exit(2)
    prefix = None
    if obs:
        prefix = key_prefix('nirspec_spec', Scope(obs=obs), scheme=KeyScheme.CANONICAL) + '/'
    elif field:
        prefix = key_prefix('nircam_mosaic', Scope(field=field, filt='x'), scheme=KeyScheme.CANONICAL).rsplit('/', 1)[0] + '/'

    if status_only:
        try:
            summary = fetch_summary(sb, tag)
        except Exception as e:  # noqa: BLE001
            say(f"✗ {getattr(e, 'message', None) or e}")
            sys.exit(1)
        click.echo(_json.dumps(summary, indent=2, sort_keys=True) if as_json else format_summary(summary))
        return

    s3 = bucket = None
    if local and not dry_run:
        # a local registry with the shared bucket: every archive object written
        # would be unregistered in production (Codex on #601)
        say("✗ `campfire dr archive --local` is refused without --dry-run: the bucket is the shared one.")
        sys.exit(2)
    if not dry_run:
        # Members live on OSN (the same bucket as the exports)
        s3, bucket = _dr_osn_s3(config, max_pool_connections=max(workers, 10))
        if s3 is None:
            say("✗ `campfire dr archive` needs OSN credentials for the data bucket "
                "(CAMPFIRE_S3_OSN_* or [r2_osn] in deploy.toml): " + bucket)
            sys.exit(2)

    _check_conditional_put(s3, say)
    scope = f" under {prefix}" if prefix else ""
    say(f"{'Planning' if dry_run else 'Archiving'} the member bytes of {tag}{scope} …")
    try:
        report = run_byte_pass(sb, s3, bucket, tag, workers=workers, limit=limit,
                               include_failed=retry_failed, prefix=prefix, dry_run=dry_run,
                               log=say)
    except Exception as e:  # noqa: BLE001 - one line, not a traceback
        say(f"✗ {getattr(e, 'message', None) or e}")
        sys.exit(1)
    # Failures stored by THIS or an EARLIER run: without --retry-failed the
    # earlier ones are not even fetched, so the report alone can look clean
    # while the release can never become ready (Codex on #601).
    stored_failed = None
    summary_unknown = False
    if not dry_run:
        try:
            summary = fetch_summary(sb, tag)
            stored_failed = int(((summary.get('by_state') or {}).get('failed') or {}).get('count') or 0)
        except Exception as e:  # noqa: BLE001
            # unknown is not clean: earlier failures may remain (Codex on #601)
            summary_unknown = True
            say(f"✗ could not read the byte-pass summary ({getattr(e, 'message', None) or e}); "
                f"run `campfire dr archive --tag {tag} --status` to check for failed members")
    if as_json:
        click.echo(_json.dumps({**report.as_dict(), 'stored_failed': stored_failed},
                               indent=2, sort_keys=True, default=str))
    else:
        click.echo(format_report(report))
        if stored_failed:
            click.echo(f"✗ {stored_failed:,} member(s) of {tag} are marked failed "
                       f"(`campfire dr archive --tag {tag} --status` lists them; rerun with --retry-failed)")
    if not dry_run and not prefix:
        _dr_hint(sb, tag, say)
    elif not dry_run:
        say(f"Archived the files under {prefix}; run `campfire dr archive --tag {tag}` for the rest.")
    if not dry_run and (report.by_state.get('failed') or stored_failed or summary_unknown):
        sys.exit(1)


@dr_group.command(name="build")
@click.option("--tag", required=True, metavar="TAG", help="Release tag, e.g. DR2026.1")
@click.option("--nirspec", is_flag=True, help="Freeze with the NIRSpec catalog component (first run only)")
@click.option("--field", "fields", multiple=True, metavar="FIELD",
              help="Freeze with the published NIRCam mosaics of FIELD (repeatable; first run only)")
@click.option("--photometry", "photometry", multiple=True, metavar="FIELD/CATALOG",
              help="Freeze with a photometry catalog component (repeatable; first run only)")
@click.option("--notes", default=None, help="Free-text notes stored on the release row (first run only)")
@click.option("--workers", type=int, default=8, show_default=True, help="Parallel transfers while archiving")
def dr_build(tag, nirspec, fields, photometry, notes, workers):
    """Freeze, archive and export a release: steps 1-3, resumable.

    Each step is skipped when it is already done, so re-running after an
    interruption picks up where it stopped: an unfrozen TAG is frozen with the
    given components (they are ignored once TAG is frozen; re-freeze with
    `dr freeze`), its manifest files are archived (`dr archive`), and its
    export files are built (`dr export`). Stops at the first step that leaves
    work undone and says what to fix. Publishing stays a separate, deliberate
    step: `campfire dr publish TAG`.
    """
    from campfire.deploy.dr_bytes import fetch_summary, format_report, run_byte_pass

    from .dr import (components_from_options, enqueue_freeze, fetch_release, format_release,
                     wait_for_freeze)
    from .dr_export import format_export_report, run_export

    config, sb = _dr_client(False, what="`campfire dr build`")
    s3, bucket = _dr_osn_s3(config, max_pool_connections=max(workers, 10))
    if s3 is None:
        click.echo(f"✗ `campfire dr build` needs OSN credentials for the data bucket ({bucket}).")
        sys.exit(2)
    _check_conditional_put(s3, click.echo)

    # 1. freeze (unless TAG is already frozen)
    row = fetch_release(sb, tag)
    if row is not None and row.get('state') in ('published', 'withdrawn'):
        click.echo(f"✗ {tag} is {row['state']}; a published release is never rebuilt (make a new tag).")
        sys.exit(1)
    if row is None or row.get('state') == 'failed':
        try:
            components = components_from_options(nirspec=nirspec, fields=fields, photometry=photometry)
        except ValueError as e:
            click.echo(f"✗ {tag} is not frozen yet; pass its components (--nirspec, --field, --photometry): {e}")
            sys.exit(2)
        click.echo(f"1/3 Freezing {tag} ({', '.join(components)}) …")
        try:
            row = enqueue_freeze(sb, tag, components, notes=notes)
        except Exception as e:  # noqa: BLE001
            click.echo(f"✗ Could not start the freeze: {getattr(e, 'message', None) or e}")
            sys.exit(1)
    else:
        if nirspec or fields or photometry or notes:
            click.echo(f"  ({tag} is already frozen; the component options are ignored. "
                       f"Re-freeze it with `campfire dr freeze`.)")
        click.echo(f"1/3 {tag} is frozen.")
    if row.get('state') == 'freezing':
        try:
            row = wait_for_freeze(sb, tag, echo=click.echo)
        except TimeoutError as e:
            click.echo(f"… {e}; re-run `campfire dr build --tag {tag}` once `dr status` shows it frozen.")
            sys.exit(3)
    if row.get('state') == 'failed':
        click.echo(format_release(row))
        _dr_hint(sb, tag)
        sys.exit(1)

    # 2. archive
    click.echo(f"2/3 Archiving the manifest files of {tag} …")
    try:
        report = run_byte_pass(sb, s3, bucket, tag, workers=workers, log=click.echo)
    except Exception as e:  # noqa: BLE001
        click.echo(f"✗ {getattr(e, 'message', None) or e}")
        sys.exit(1)
    if report.seen:
        click.echo(format_report(report))
    by = (fetch_summary(sb, tag) or {}).get('by_state') or {}
    if int((by.get('failed') or {}).get('count') or 0) or int((by.get('pending') or {}).get('count') or 0):
        _dr_hint(sb, tag)
        sys.exit(1)

    # 3. export
    click.echo(f"3/3 Exporting {tag} …")
    try:
        exp = run_export(sb, s3, bucket, tag, log=click.echo)
    except Exception as e:  # noqa: BLE001
        click.echo(f"✗ {getattr(e, 'message', None) or e}")
        sys.exit(1)
    click.echo(format_export_report(exp))
    _dr_hint(sb, tag)
    if exp.failed or exp.remaining > 0:
        sys.exit(1)


@dr_group.command(name="publish")
@click.argument("tag")
@click.option("--doi", default=None, metavar="DOI", help="DOI of the release, e.g. 10.5281/zenodo.123456")
@click.option("--known-issues", default=None, help="Known-issues text shown with the release")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option("--json", "as_json", is_flag=True, help="Print the release row as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (127.0.0.1:54321)")
def dr_publish(tag, doi, known_issues, yes, as_json, local):
    """Publish a release (P4d): ready -> published.

    The release must be `ready` (every export file built, every member
    archived; re-derived on the server), or `withdrawn` to publish it again.
    Publishing makes the frozen rows visible to every reader within their
    programs. Admin only.
    """
    import json as _json

    from .dr import fetch_diff, format_release, publish_release

    config, sb = _dr_client(local, what="`campfire dr publish`")
    # A ready release no longer pins its observations (its bytes are archived),
    # so live may have moved on since the freeze. Say so before publishing
    # (Fable review of #602); `campfire dr diff` has the details.
    try:
        counts = (fetch_diff(sb, tag, limit=0) or {}).get('counts') or {}
    except Exception:  # noqa: BLE001 - a warning only; dr_publish decides
        counts = {}
    moved = {k: int(counts.get(k) or 0) for k in
             ('spectrum_gone', 'spectrum_unpublished', 'spectrum_science_changed')}
    if any(moved.values()):
        click.echo(f"⚠  Live has moved on since {tag} was frozen: " +
                   ', '.join(f"{v:,} {k.replace('_', ' ')}" for k, v in moved.items() if v) +
                   f" (`campfire dr diff {tag}`). The release still publishes its frozen rows.",
                   err=as_json)
    if not yes and not click.confirm(f"Publish {tag} to every reader of its programs?", default=False,
                                     err=as_json):
        click.echo("Aborted.", err=as_json)
        sys.exit(1)
    try:
        row = publish_release(sb, tag, doi=doi, known_issues=known_issues)
    except Exception as e:  # noqa: BLE001
        click.echo(f"✗ Could not publish {tag}: {getattr(e, 'message', None) or e}", err=as_json)
        sys.exit(1)
    click.echo(_json.dumps(row, indent=2, sort_keys=True, default=str) if as_json else format_release(row))
    _dr_hint(sb, tag, (lambda m: click.echo(m, err=True)) if as_json else click.echo)


@dr_group.command(name="withdraw")
@click.argument("tag")
@click.option("--reason", required=True, help="Why (appended to the release's known issues)")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
@click.option("--json", "as_json", is_flag=True, help="Print the release row as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (127.0.0.1:54321)")
def dr_withdraw(tag, reason, yes, as_json, local):
    """Withdraw a published release (P4d): hidden from non-admins, kept intact.

    Rows and archive objects stay (a published release is never deleted);
    `dr publish TAG` makes it visible again. Admin only.
    """
    import json as _json

    from .dr import format_release, withdraw_release

    config, sb = _dr_client(local, what="`campfire dr withdraw`")
    if not yes and not click.confirm(f"Withdraw {tag}? Readers lose access until it is published again.",
                                     default=False, err=as_json):
        click.echo("Aborted.", err=as_json)
        sys.exit(1)
    try:
        row = withdraw_release(sb, tag, reason)
    except Exception as e:  # noqa: BLE001
        click.echo(f"✗ Could not withdraw {tag}: {getattr(e, 'message', None) or e}", err=as_json)
        sys.exit(1)
    click.echo(_json.dumps(row, indent=2, sort_keys=True, default=str) if as_json else format_release(row))


@dr_group.command(name="diff")
@click.argument("tag")
@click.argument("against", required=False, default="live", type=click.Choice(["live"]))
@click.option("--limit", type=int, default=20, show_default=True, help="Samples per kind")
@click.option("--json", "as_json", is_flag=True, help="Print the diff as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (127.0.0.1:54321)")
def dr_diff(tag, against, limit, as_json, local):
    """What changed in live since TAG was frozen (P4d).

    Spectra whose science changed, left the published set or are new;
    objects renamed, re-redshifted, re-graded or deactivated; review
    contradictions raised or cleared since the freeze. Admin only.
    """
    import json as _json

    from .dr import fetch_diff, format_diff

    config, sb = _dr_client(local, what="`campfire dr diff`")
    try:
        diff = fetch_diff(sb, tag, limit=limit)
    except Exception as e:  # noqa: BLE001
        click.echo(f"✗ Could not diff {tag}: {getattr(e, 'message', None) or e}", err=as_json)
        sys.exit(1)
    click.echo(_json.dumps(diff, indent=2, sort_keys=True, default=str) if as_json else format_diff(diff))


@dr_group.group(name="page")
def dr_page_group():
    """The release page document: markdown and figures shown on /releases/TAG.

    Authored before publication from a directory holding README.md and its
    figures (produced by the scripts under scripts/dr/); frozen with the
    release once it is published.
    """


@dr_page_group.command(name="push")
@click.argument("tag")
@click.argument("directory", type=click.Path(exists=True, file_okay=False))
@click.option("--dry-run", is_flag=True, help="List what would be uploaded and set; write nothing")
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON")
@click.option("--local", is_flag=True,
              help="Against local Supabase (127.0.0.1:54321, or CAMPFIRE_LOCAL_SUPABASE_URL); the bucket stays the shared one")
def dr_page_push(tag, directory, dry_run, as_json, local):
    """Upload DIRECTORY's files under dr/TAG/page/<sha16>/ and set TAG's page.

    DIRECTORY must hold README.md (the page markdown); every other regular
    file is a figure (no subdirectories; at most 50 files of 25 MB). Needs
    OSN credentials (CAMPFIRE_S3_OSN_* or [r2_osn] in deploy.toml) and
    `campfire login` as an admin. Refused for a published release. Keys are
    content-addressed (the first 16 hex of each file's sha256), never
    overwritten: a file already there with the same bytes is reused, a
    different one fails the push; the keys the database retires are
    deleted after the page is set. --local is refused without --dry-run:
    the local database redirects Supabase only, the bucket is the shared one.
    """
    import json as _json

    from .dr_page import PageError, check_editable, push_page

    say = (lambda m: click.echo(m, err=True)) if as_json else click.echo
    if local and not dry_run:
        say("✗ --local refused without --dry-run (the bucket is the shared one)")
        sys.exit(2)
    config, sb = _dr_client(local, what="`campfire dr page push`")
    s3 = bucket = None
    try:
        # the release first: the operator sees the real reason before the
        # credentials check (review on P6b-3)
        check_editable(sb, tag)
        if not dry_run:
            s3, bucket = _dr_osn_s3(config)
            if s3 is None:
                say(f"✗ `campfire dr page push` needs OSN credentials for the data bucket ({bucket}).")
                sys.exit(2)
        result = push_page(sb, s3, bucket, tag, directory, dry_run=dry_run, log=say)
    except PageError as e:
        say(f"✗ {e}")
        sys.exit(2)
    except Exception as e:  # noqa: BLE001 - one line, not a traceback
        say(f"✗ {getattr(e, 'message', None) or e}")
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(result, indent=2, sort_keys=True, default=str))
    elif dry_run:
        click.echo("Dry run: nothing uploaded or set.")
    else:
        ups = result.get('uploads') or {}
        summary = ', '.join(f"{n} {k}" for k in ('uploaded', 'reused')
                            if (n := sum(1 for v in ups.values() if v == k)))
        click.echo(f"Set the page of {tag}: {result.get('markdown_bytes', 0):,} bytes of markdown, "
                   f"{result.get('assets', 0)} asset(s)" + (f" ({summary})" if summary else ''))
        if result.get('deleted'):
            click.echo(f"  deleted {len(result['deleted'])} retired file(s) under dr/{tag}/page/")


@dr_page_group.command(name="show")
@click.argument("tag")
@click.option("--json", "as_json", is_flag=True, help="Print the page as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (127.0.0.1:54321, or CAMPFIRE_LOCAL_SUPABASE_URL)")
def dr_page_show(tag, as_json, local):
    """Print TAG's page markdown and its registered figures."""
    import json as _json

    from .dr_page import fetch_page, format_page

    config, sb = _dr_client(local, what="`campfire dr page show`")
    page = fetch_page(sb, tag)
    if page is None:
        click.echo(f"✗ no release {tag} (or not visible to this account)")
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps(page, indent=2, sort_keys=True, default=str))
    else:
        click.echo(format_page(page))


@dr_group.command(name="refs")
@click.option("--obs", required=True, metavar="OBS", help="Observation name")
@click.option("--json", "as_json", is_flag=True, help="Print the references as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (127.0.0.1:54321)")
def dr_refs(obs, as_json, local):
    """Which releases froze spectra of OBS, and whether they block a removal (P4d).

    `deploy remove` runs the same check and refuses while any release
    blocks; check it by hand before the manual grating-delete procedure.
    """
    import json as _json

    from .dr import format_references, observation_references

    config, sb = _dr_client(local, what="`campfire dr refs`")
    try:
        refs = observation_references(sb, obs)
    except Exception as e:  # noqa: BLE001
        click.echo(f"✗ Could not read references: {getattr(e, 'message', None) or e}", err=as_json)
        sys.exit(1)
    click.echo(_json.dumps(refs, indent=2, sort_keys=True, default=str) if as_json else format_references(obs, refs))
    if any(r.get('blocks') for r in refs):
        sys.exit(1)


# ---------------------------------------------------------------------------
# Archive (admin)
# ---------------------------------------------------------------------------


@cli.group(name="archive")
def archive_group():
    """Content-addressed archive tooling (admin): retention."""


@archive_group.command(name="gc")
@click.option("--dry-run", is_flag=True, help="List the collectable objects; change nothing")
@click.option("--yes", is_flag=True, help="Collect without the confirmation prompt")
@click.option("--limit", type=int, default=None,
              help="List at most N candidate rows (the totals, and a collection, always cover every candidate)")
@click.option("--json", "as_json", is_flag=True, help="Print the report as JSON")
@click.option("--local", is_flag=True, help="Against local Supabase (127.0.0.1:54321); lists only, the bucket is the shared one")
def archive_gc(dry_run, yes, limit, as_json, local):
    """Collect the archive objects no data release references.

    The archive holds exactly the files releases reference; what no release's
    manifest names (a purged release's leftovers, a copy retained for a member
    later purged) is collectable, whatever its type and age. Collection
    deletes the bytes and marks the registry rows collected (hash, size and
    date kept). Without --dry-run the candidates are listed and, after a
    confirmation (--yes skips it), collected; that needs OSN credentials
    (CAMPFIRE_S3_OSN_* or [r2_osn] in deploy.toml). Admin-only: a non-admin's
    RLS view of the registry holds no archive rows.
    """
    import json as _json

    from .archive import CollectError, collect, format_gc_report, gc_candidates, gc_report

    try:
        from campfire.deploy.cli import _gate_admin
        from campfire.deploy.config import load_config
        from campfire.deploy.supabase import get_supabase_client
    except ImportError:
        click.echo("✗ `campfire archive` needs the deploy extra: pip install campfire[deploy]")
        sys.exit(2)
    if local and not dry_run:
        click.echo("  --local: listing only (the bucket is the shared one)")
        dry_run = True
    config = load_config(None, local=local)
    # Admin gate through the same client the listing uses: under RLS a
    # non-admin sees only status = 'active' rows, so the archive query would
    # succeed with nothing and read as "no candidates". Skipped for
    # service-role / --local clients (no auth.uid()), like every deploy verb.
    _gate_admin(config, what="`campfire archive gc`")
    s3 = bucket = None
    if not dry_run:
        # resolved BEFORE anything is listed or confirmed
        s3, bucket = _dr_osn_s3(config)
        if s3 is None:
            click.echo(f"✗ Collecting needs OSN credentials ({bucket}); pass --dry-run to list only.")
            sys.exit(2)
    try:
        sb = get_supabase_client(config)
        rows = gc_candidates(sb)
    except Exception as e:  # noqa: BLE001 - one line, not a traceback
        msg = getattr(e, "message", None) or str(e)
        click.echo(f"✗ Could not list archive rows: {msg}")
        sys.exit(1)
    report = gc_report(rows, max_rows=limit, dry_run=dry_run)
    if as_json and dry_run:
        click.echo(_json.dumps(report, indent=2, sort_keys=True))
    else:
        click.echo(format_gc_report(report, max_rows=limit if limit is not None else 20))
    if dry_run or not rows:
        return
    if not yes:
        click.confirm(f"Delete these {len(rows):,} archive object(s) and mark their rows collected?", abort=True)
    try:
        totals = collect(sb, s3, bucket, rows, log=click.echo)
    except CollectError as e:
        click.echo(f"✗ Collection stopped: {e}\n  Rows whose bytes went are reported by `campfire verify --cloud`; run again.")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        msg = getattr(e, "message", None) or str(e)
        click.echo(f"✗ Could not mark the rows collected: {msg}\n  Run again (a missing object deletes cleanly).")
        sys.exit(1)
    if as_json:
        click.echo(_json.dumps({**report, 'dry_run': False, 'result': totals}, indent=2, sort_keys=True))


def main():
    """Entry point for the CLI."""
    _register_deploy_group()
    _register_fitsgl_group()
    cli()


if __name__ == "__main__":
    main()
