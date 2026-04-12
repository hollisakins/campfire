"""
Textual TUI dashboard for CAMPFIRE.

Launched when ``campfire`` is run with no arguments in an interactive terminal.
Provides a status panel, sync controls, and an observation browser with
inline download triggering.
"""

from __future__ import annotations

import sys

from rich.text import Text
from textual.app import App, ComposeResult
from textual import work
from textual.binding import Binding
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Static,
)


# ---------------------------------------------------------------------------
# Data helpers (run blocking I/O in thread via asyncio executor)
# ---------------------------------------------------------------------------

def _get_store():
    """Open a read-only LocalStore, or None if DB doesn't exist."""
    from .config import ensure_data_dir, meta_dir
    from .db.store import LocalStore, SchemaMismatchError

    ensure_data_dir()
    db_path = meta_dir() / "campfire.db"
    if not db_path.exists():
        return None
    try:
        return LocalStore(db_path)
    except SchemaMismatchError:
        return None


def _fetch_status_data() -> dict:
    """Collect status data (credentials, catalog, disk usage)."""
    from .auth.credentials import CredentialManager
    from .sync import format_size

    result = {
        "logged_in": False,
        "email": None,
        "observations": 0,
        "last_synced": None,
        "obs_stats": [],
        "stale_count": 0,
        "disk_usage": None,
    }

    creds = CredentialManager()
    if creds.exists():
        loaded = creds.load()
        if loaded:
            result["logged_in"] = True
            if loaded.is_oauth() and loaded.user_email:
                result["email"] = loaded.user_email

    store = _get_store()
    if store:
        obs_list = store.get_synced_observations()
        result["observations"] = len(obs_list)

        last = store.get_last_synced_at()
        if last:
            result["last_synced"] = last[:16].replace("T", " ")

        for obs in obs_list:
            stats = store.get_observation_stats(obs)
            if stats["synced_count"] > 0:
                result["obs_stats"].append({
                    "observation": obs,
                    "downloaded": stats["synced_count"],
                    "size": format_size(stats["total_bytes"]),
                })

        result["stale_count"] = len(store.get_stale_files())
        store.close()

    # Skip disk usage scan — too slow for large data directories with FITS files.
    # The CLI `campfire status` can afford the wait; the TUI cannot block on mount.

    return result


def _fetch_observation_summary() -> list[dict]:
    """Get the observation summary table data."""
    store = _get_store()
    if not store:
        return []
    summary = store.get_observation_summary()
    store.close()
    return summary


def _run_sync(on_status) -> dict | None:
    """Run sync_metadata synchronously (called from a worker thread)."""
    from .api.session import APISession, resolve_base_url
    from .api.client import APIClient
    from .config import meta_dir as _meta_dir
    from .exceptions import AuthenticationError
    from .sync import sync_metadata

    base_url = resolve_base_url()
    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError:
        on_status("Not logged in. Run: campfire login")
        return None

    store = _get_store()
    if not store:
        on_status("No local database. Run: campfire sync first.")
        return None

    try:
        on_status("Syncing catalog...")
        result = sync_metadata(
            api, store, _meta_dir(),
            show_progress=False,
            full=False,
        )
        return result
    except Exception as e:
        on_status(f"Sync failed: {e}")
        return None
    finally:
        store.close()


def _run_download(obs_name: str, on_status) -> dict | None:
    """Download files for a single observation (called from a worker thread)."""
    from .api.session import APISession, resolve_base_url, create_download_session
    from .api.client import APIClient
    from .config import products_dir as _products_dir
    from .exceptions import AuthenticationError
    from .sync import download_observation

    base_url = resolve_base_url()
    try:
        api_session = APISession(base_url=base_url)
        api = APIClient(api_session)
    except AuthenticationError:
        on_status("Not logged in.")
        return None

    store = _get_store()
    if not store:
        on_status("No local database.")
        return None

    try:
        on_status(f"Downloading {obs_name}...")
        dl_session = create_download_session(max_workers=4)
        stats = download_observation(
            api, obs_name,
            _products_dir(), store,
            max_workers=4,
            download_session=dl_session,
        )
        return stats
    except Exception as e:
        on_status(f"Download failed: {e}")
        return None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class StatusPanel(Static):
    """Displays credential validity, catalog stats, and disk usage."""

    DEFAULT_CSS = """
    StatusPanel {
        height: auto;
        max-height: 12;
        padding: 0 1;
        border: solid $primary;
    }
    """

    def on_mount(self) -> None:
        self.loading = True
        self.refresh_data()

    @work(thread=True)
    def refresh_data(self) -> None:
        data = _fetch_status_data()
        self.app.call_from_thread(self._show_status, data)

    def _show_status(self, data: dict) -> None:
        lines = []
        if data["logged_in"]:
            user = data["email"] or "authenticated"
            lines.append(f"[green]✓[/green] Logged in as {user}")
        else:
            lines.append("[red]✗[/red] Not logged in")

        obs_count = data["observations"]
        last = data["last_synced"]
        if obs_count:
            ts = f" (synced {last})" if last else ""
            lines.append(f"  Catalog: {obs_count} observations{ts}")
        else:
            lines.append("  No catalog — run [bold]campfire sync[/bold]")

        if data["stale_count"]:
            lines.append(f"  [yellow]⚠[/yellow] {data['stale_count']} stale file(s)")

        if data["disk_usage"]:
            lines.append(f"  Disk usage: {data['disk_usage']}")

        self.update(Text.from_markup("\n".join(lines)))
        self.loading = False


class SyncPanel(Static):
    """Sync controls: trigger catalog sync and show status."""

    DEFAULT_CSS = """
    SyncPanel {
        height: auto;
        max-height: 6;
        padding: 0 1;
        border: solid $accent;
        layout: horizontal;
    }
    SyncPanel Button {
        margin: 0 2 0 0;
        min-width: 16;
    }
    SyncPanel Label {
        padding: 1 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Button("Sync Catalog", id="sync-btn", variant="primary")
        yield Label("Press S or click to sync", id="sync-status")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sync-btn":
            self._start_sync()

    def _start_sync(self) -> None:
        btn = self.query_one("#sync-btn", Button)
        btn.disabled = True
        self._update_status("Syncing...")
        self._do_sync()

    @work(thread=True)
    def _do_sync(self) -> None:
        def on_status(msg):
            self.app.call_from_thread(self._update_status, msg)

        result = _run_sync(on_status)

        if result:
            msg = (f"✓ Synced: {result['targets']} targets, "
                   f"{result['spectra']} spectra")
            self.app.call_from_thread(self._sync_done, msg)
        else:
            self.app.call_from_thread(self._sync_done, None)

    def _update_status(self, msg: str) -> None:
        self.query_one("#sync-status", Label).update(msg)

    def _sync_done(self, msg: str | None) -> None:
        btn = self.query_one("#sync-btn", Button)
        btn.disabled = False
        if msg:
            self._update_status(msg)
        # Refresh other panels
        try:
            self.app.query_one(StatusPanel).refresh_data()
            self.app.query_one(ObservationBrowser).refresh_data()
        except Exception:
            pass


class ObservationBrowser(Static):
    """Scrollable observation table with inline download."""

    DEFAULT_CSS = """
    ObservationBrowser {
        height: 1fr;
        border: solid $surface;
    }
    ObservationBrowser DataTable {
        height: 1fr;
    }
    ObservationBrowser #dl-status {
        height: 1;
        padding: 0 1;
        dock: bottom;
    }
    """

    def compose(self) -> ComposeResult:
        yield DataTable(id="obs-table")
        yield Label("", id="dl-status")

    def on_mount(self) -> None:
        table = self.query_one("#obs-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("OBSERVATION", "PROGRAM", "FIELD", "SPECTRA", "LOCAL")
        self.refresh_data()

    @work(thread=True)
    def refresh_data(self) -> None:
        summary = _fetch_observation_summary()
        self.app.call_from_thread(self._populate, summary)

    def _populate(self, summary: list[dict]) -> None:
        table = self.query_one("#obs-table", DataTable)
        table.clear()
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
                key=row["observation"],
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Download files for the selected observation."""
        obs_name = str(event.row_key.value)
        status = self.query_one("#dl-status", Label)
        status.update(f"Downloading {obs_name}...")
        self._download(obs_name)

    @work(thread=True)
    def _download(self, obs_name: str) -> None:
        def on_status(msg):
            self.app.call_from_thread(
                self.query_one("#dl-status", Label).update, msg
            )

        result = _run_download(obs_name, on_status)

        if result:
            dl = result.get("downloaded", 0)
            failed = result.get("failed", 0)
            msg = f"✓ {obs_name}: {dl} downloaded"
            if failed:
                msg += f", {failed} failed"
            self.app.call_from_thread(
                self.query_one("#dl-status", Label).update, msg
            )
            # Refresh to show updated local counts
            self.refresh_data()
            try:
                self.app.query_one(StatusPanel).refresh_data()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class CampfireApp(App):
    """CAMPFIRE interactive dashboard."""

    TITLE = "CAMPFIRE"
    SUB_TITLE = "NIRSpec Data Portal"

    CSS = """
    Screen {
        layout: vertical;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "sync", "Sync"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        yield StatusPanel()
        yield SyncPanel()
        yield ObservationBrowser()
        yield Footer()

    def action_refresh(self) -> None:
        """Refresh all panels."""
        self.query_one(StatusPanel).refresh_data()
        self.query_one(ObservationBrowser).refresh_data()

    def action_sync(self) -> None:
        """Trigger catalog sync."""
        self.query_one(SyncPanel)._start_sync()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def launch_tui() -> None:
    """Launch the TUI, or print a message if not in a terminal."""
    if not sys.stdout.isatty():
        print("TUI requires an interactive terminal. Run 'campfire --help' for CLI usage.")
        return
    app = CampfireApp()
    app.run()
