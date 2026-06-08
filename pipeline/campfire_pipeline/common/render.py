"""
Renderers for the ``cfpipe`` live reporting session.

Two implementations share one interface — ``start()``, ``handle(event)``,
``tick()``, ``stop()`` — all driven exclusively by the session's drain thread, so
neither needs internal locking:

* :class:`RichRenderer` — a ``rich.Live`` dashboard with three panels (group
  progress, per-worker status, merged scrolling log). Used on an interactive TTY.
* :class:`PlainRenderer` — timestamped, line-ordered, demultiplexed output to the
  real stdout. Used when piped/redirected (HPC batch logs) or when ``--no-tui`` is
  passed. Fixes the interleaving problem without any ANSI escapes.

``rich`` is imported lazily inside :class:`RichRenderer` so importing this module
(and constructing a :class:`PlainRenderer`) never requires it.

Event tuples are documented in :mod:`campfire_pipeline.common.reporting`.
"""

import sys
import time
from collections import deque
from datetime import datetime

from campfire_pipeline.common.reporting import (
    GROUP_ADVANCE, GROUP_DONE, GROUP_START, LOG,
    TASK_DONE, TASK_ERROR, TASK_START,
)


def _now():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# Plain renderer (non-TTY / --no-tui)
# ---------------------------------------------------------------------------

class PlainRenderer:
    """Serialised, demultiplexed plain-text output to the real stdout."""

    def __init__(self):
        self._out = None

    def start(self):
        # Capture the parent's real stdout once; parent log() now routes through
        # the session sink, so nothing else competes for this stream.
        self._out = sys.stdout
        self._groups = {}

    def _print(self, msg):
        try:
            print(msg, file=self._out, flush=True)
        except Exception:
            pass

    def handle(self, event):
        tag = event[0]
        if tag == LOG:
            _, _pid, ts, msg = event
            self._print(f"[{ts}] {msg}")
        elif tag == GROUP_START:
            _, gid, label, total = event
            self._groups[gid] = (label, total)
            self._print(f"[{_now()}] === {label}: {total} task(s) ===")
        elif tag == GROUP_DONE:
            _, gid = event
            label, total = self._groups.pop(gid, ('?', '?'))
            self._print(f"[{_now()}] === {label}: done ({total} task(s)) ===")
        elif tag == TASK_ERROR:
            _, pid, label, err = event
            self._print(f"[{_now()}] ERROR [{label}] (pid {pid}): {err}")
        # TASK_START / TASK_DONE / GROUP_ADVANCE: omitted — too granular for a log

    def tick(self):
        pass

    def stop(self):
        try:
            if self._out is not None:
                self._out.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Rich renderer (interactive TTY)
# ---------------------------------------------------------------------------

class RichRenderer:
    """Three-panel ``rich.Live`` dashboard. Constructed only on a TTY."""

    def __init__(self, log_lines=15):
        # Import here so a missing/!TTY rich install never blocks plain mode.
        from rich.console import Console
        self._Console = Console
        self.console = Console()
        self.log_lines = max(3, int(log_lines))

        self._groups = {}     # gid -> {label, total, completed, done}
        self._group_order = []
        self._workers = {}    # pid -> {label, started, status}
        self._logs = deque(maxlen=self.log_lines)
        self._live = None
        self._last_refresh = 0.0
        self._min_interval = 1.0 / 8  # cap redraws at ~8 fps

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        from rich.live import Live
        self._live = Live(
            self._render(), console=self.console,
            refresh_per_second=8, transient=False,
            screen=False, auto_refresh=False,
        )
        self._live.start()

    def stop(self):
        if self._live is None:
            return
        try:
            self._live.update(self._render(), refresh=True)
        except Exception:
            pass
        try:
            self._live.stop()
        except Exception:
            pass
        # Live.stop() restores the cursor; make sure it's visible regardless.
        try:
            self.console.show_cursor(True)
        except Exception:
            pass

    # -- event handling -----------------------------------------------------

    def handle(self, event):
        tag = event[0]
        if tag == LOG:
            _, pid, ts, msg = event
            self._logs.append((ts, msg))
        elif tag == TASK_START:
            _, pid, label = event
            self._workers[pid] = {'label': label, 'started': time.monotonic(),
                                  'status': 'run'}
        elif tag == TASK_DONE:
            _, pid, label = event
            w = self._workers.get(pid)
            if w is not None:
                w['status'] = 'idle'
        elif tag == TASK_ERROR:
            _, pid, label, err = event
            w = self._workers.get(pid)
            if w is not None:
                w['status'] = 'error'
            self._logs.append((_now(), f"ERROR [{label}]: {err}"))
        elif tag == GROUP_START:
            _, gid, label, total = event
            self._groups[gid] = {'label': label, 'total': total,
                                 'completed': 0, 'done': False}
            self._group_order.append(gid)
        elif tag == GROUP_ADVANCE:
            _, gid = event
            g = self._groups.get(gid)
            if g is not None:
                g['completed'] += 1
        elif tag == GROUP_DONE:
            _, gid = event
            g = self._groups.get(gid)
            if g is not None:
                g['done'] = True
        self._maybe_refresh()

    def tick(self):
        # Periodic redraw so elapsed timers advance without new events.
        self._maybe_refresh(force=True)

    def _maybe_refresh(self, force=False):
        now = time.monotonic()
        if not force and (now - self._last_refresh) < self._min_interval:
            return
        self._last_refresh = now
        if self._live is not None:
            try:
                self._live.update(self._render(), refresh=True)
            except Exception:
                pass

    # -- rendering ----------------------------------------------------------

    def _bar(self, completed, total, width=24):
        if total <= 0:
            return ' ' * width
        filled = int(width * min(completed, total) / total)
        return '█' * filled + '░' * (width - filled)

    def _render(self):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        from rich.text import Text

        # --- progress panel: keep the last few groups, summarise finished ---
        prog = Text()
        visible = self._group_order[-6:]
        for gid in visible:
            g = self._groups[gid]
            bar = self._bar(g['completed'], g['total'])
            state = '✓' if g['done'] else ' '
            prog.append(
                f"{state} {g['label'][:28]:28} {bar} "
                f"{g['completed']:>4}/{g['total']:<4}\n"
            )
        if not visible:
            prog.append("(waiting for work…)\n")

        # --- worker panel ---------------------------------------------------
        wtable = Table.grid(padding=(0, 2))
        wtable.add_column(justify="right")   # pid
        wtable.add_column()                  # status
        wtable.add_column()                  # current task
        wtable.add_column(justify="right")   # elapsed
        now = time.monotonic()
        for pid in sorted(self._workers):
            w = self._workers[pid]
            if w['status'] == 'run':
                elapsed = f"{now - w['started']:5.0f}s"
                status = "[green]●[/green]"
            elif w['status'] == 'error':
                elapsed = ""
                status = "[red]✗[/red]"
            else:
                elapsed = ""
                status = "[dim]·[/dim]"
            wtable.add_row(str(pid), status, w['label'][:40], elapsed)
        if not self._workers:
            wtable.add_row("", "", "[dim](no active workers)[/dim]", "")

        # --- log panel ------------------------------------------------------
        logtext = Text()
        for ts, msg in self._logs:
            logtext.append(f"[{ts}] ", style="dim")
            logtext.append(f"{msg}\n")
        if not self._logs:
            logtext.append("(no output yet)\n", style="dim")

        return Group(
            Panel(prog, title="progress", title_align="left"),
            Panel(wtable, title="workers", title_align="left"),
            Panel(logtext, title="log", title_align="left"),
        )
