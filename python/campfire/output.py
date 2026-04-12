"""
Rich output utilities for the CAMPFIRE CLI.

Provides styled terminal output with automatic plain-text fallback
when stdout is not a tty (piped, redirected, or CI).

Usage::

    from campfire.output import console, print_success, print_error, track, progress_bar

    print_success("Sync complete: 42 targets")
    print_error("Authentication failed")

    for item in track(items, description="Processing"):
        process(item)

    with progress_bar(total=100, description="Uploading") as pb:
        for chunk in chunks:
            upload(chunk)
            pb.update(1)
"""

import sys

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    track as _rich_track,
)
from rich.table import Table

# Shared console instances — Rich auto-detects tty and strips ANSI when piped.
console = Console(highlight=False)
error_console = Console(stderr=True, highlight=False)


def is_tty() -> bool:
    """Check if stdout is an interactive terminal."""
    return console.is_terminal


# ---------------------------------------------------------------------------
# Styled message helpers (replace click.echo with ✓/✗/⚠ glyphs)
# ---------------------------------------------------------------------------

def print_success(msg: str) -> None:
    """Print a success message with green ✓."""
    console.print(f"[green]✓[/green] {msg}")


def print_error(msg: str) -> None:
    """Print an error message with red ✗ to stderr."""
    error_console.print(f"[red]✗[/red] {msg}")


def print_warning(msg: str) -> None:
    """Print a warning with yellow ⚠."""
    console.print(f"[yellow]⚠[/yellow]  {msg}")


def print_msg(msg: str = "") -> None:
    """Print a plain message (replaces click.echo / bare print)."""
    console.print(msg)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def make_table(*columns: str, title: str | None = None, padding: tuple = (0, 2)) -> Table:
    """Create a Rich Table pre-configured with column headers."""
    table = Table(
        title=title,
        show_header=True,
        header_style="bold",
        padding=padding,
        box=None,
    )
    for col in columns:
        table.add_column(col)
    return table


# ---------------------------------------------------------------------------
# Progress bars (replace tqdm everywhere)
# ---------------------------------------------------------------------------

_PROGRESS_COLUMNS = (
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    BarColumn(),
    MofNCompleteColumn(),
    TimeRemainingColumn(),
)


def track(iterable, description: str = "", total: int | None = None):
    """Drop-in replacement for ``for x in tqdm(iterable, ...)``.

    In non-tty mode, returns the iterable unchanged (no progress display).
    """
    if not is_tty():
        return iterable

    return _rich_track(
        iterable,
        description=description,
        total=total,
        console=console,
    )


class _NoOpProgressBar:
    """Silent progress bar for non-tty contexts."""

    def __init__(self, total=None, **kwargs):
        self.total = total or 0
        self.n = 0

    def update(self, n=1):
        self.n += n

    def refresh(self):
        pass

    def close(self):
        pass

    def write(self, msg):
        console.print(msg)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class _RichProgressBar:
    """Thin tqdm-like wrapper around ``rich.progress.Progress``.

    Supports both context-manager and standalone (create → update → close)
    usage, plus dynamic total adjustment via ``.total`` / ``.n`` / ``.refresh()``.
    """

    def __init__(self, total=None, description="", transient=False):
        self._progress = Progress(
            *_PROGRESS_COLUMNS,
            console=console,
            transient=transient,
        )
        self._description = description
        self._task_id = None
        self._started = False
        self.n = 0
        self.total = total

    def _ensure_started(self):
        if not self._started:
            self._progress.start()
            self._task_id = self._progress.add_task(
                self._description, total=self.total,
            )
            self._started = True

    def update(self, n=1):
        self._ensure_started()
        self.n += n
        self._progress.advance(self._task_id, n)

    def refresh(self):
        """Sync the display with current ``.total`` and ``.n`` values."""
        self._ensure_started()
        self._progress.update(
            self._task_id, total=self.total, completed=self.n,
        )

    def close(self):
        if self._started:
            self._progress.stop()
            self._started = False

    def write(self, msg):
        """Print a message without disturbing the progress bar."""
        if self._started:
            self._progress.console.print(msg)
        else:
            console.print(msg)

    def __enter__(self):
        self._ensure_started()
        return self

    def __exit__(self, *args):
        self.close()


def progress_bar(total=None, description: str = "", transient: bool = False):
    """Create a progress bar.

    Returns an object with ``.update(n)``, ``.close()``, settable ``.total``
    and ``.n``, and ``.refresh()`` for dynamic-total patterns.  Works as a
    context manager (auto-closes on exit).

    In non-tty mode returns a silent no-op.
    """
    if not is_tty():
        return _NoOpProgressBar(total=total)
    return _RichProgressBar(
        total=total, description=description, transient=transient,
    )
