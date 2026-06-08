"""
Cross-process reporting plumbing for the ``cfpipe`` live renderer.

The pipeline parallelises everything through :func:`campfire_pipeline.common.parallel.dispatch`,
and both ``cfpipe``'s own :func:`campfire_pipeline.common.io.log` and JWST/stpipe's
stdlib ``logging`` write straight to the shared terminal — so N workers interleave
into an unparseable wall of text. This module demultiplexes that:

* A parent-side :class:`ReportingSession` (a context manager opened by the ``run``
  CLI commands) owns a ``multiprocessing.Manager().Queue()``, a background **drain
  thread**, and a renderer (rich live display on a TTY, plain merged output
  otherwise — see :mod:`campfire_pipeline.common.render`).
* Each worker, via the pool *initializer* (:func:`worker_init`), routes its
  ``log()`` output, its root-logger records (stpipe), and its ``stdout``/``stderr``
  onto that queue instead of the terminal.
* The drain thread is the **single writer** to the renderer; the main process also
  pushes group-progress events through the same queue, so the renderer never sees
  concurrent mutation.

Nothing here imports ``rich`` at module load: the renderer is imported lazily by
the session (parent only), so workers — which import this module via the pool
initializer — stay lightweight, and ``rich`` never lands in the forkserver
preload list.
"""

import io as _io
import logging
import os
import sys
import threading
from datetime import datetime
from multiprocessing import Manager
from queue import Empty

from campfire_pipeline.common import io as cfio


# ---------------------------------------------------------------------------
# Event protocol — plain tuples on the queue, tagged by these constants
# ---------------------------------------------------------------------------

TASK_START = 'task_start'      # (TASK_START, pid, label)
TASK_DONE = 'task_done'        # (TASK_DONE, pid, label)
TASK_ERROR = 'task_error'      # (TASK_ERROR, pid, label, repr)
LOG = 'log'                    # (LOG, pid, timestamp, message)
GROUP_START = 'group_start'    # (GROUP_START, gid, label, total)
GROUP_ADVANCE = 'group_advance'  # (GROUP_ADVANCE, gid)
GROUP_DONE = 'group_done'      # (GROUP_DONE, gid)

_STOP = ('__stop__',)          # sentinel pushed by __exit__ to end the drain loop


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt(args):
    return ' '.join(str(a) for a in args)


# ---------------------------------------------------------------------------
# Parent-process session singleton
# ---------------------------------------------------------------------------

_SESSION = None


def active_session():
    """Return the active :class:`ReportingSession`, or ``None``.

    Read by :func:`campfire_pipeline.common.parallel.dispatch` to decide between
    the instrumented and the default (byte-identical-to-before) paths. Only ever
    set in the *parent* process; workers never consult it.
    """
    return _SESSION


# ---------------------------------------------------------------------------
# Worker-side machinery (runs in pool workers under spawn/forkserver)
# ---------------------------------------------------------------------------

# Set per worker by worker_init(). Workers share no globals with the parent, so
# the queue arrives via the pool initializer rather than by inheritance.
_WORKER_QUEUE = None
_WORKER_PID = None


def _emit(event):
    """Push an event onto the worker's queue, swallowing any failure.

    Reporting must never break or slow the science path: a full/closed queue
    silently drops the event rather than raising into pipeline code.
    """
    q = _WORKER_QUEUE
    if q is None:
        return
    try:
        q.put(event)
    except Exception:
        pass


def _label(task, use_starmap):
    """Best-effort human label for a task. Total — never raises.

    Tasks are heterogeneous: bare filename strings, source-id ints, astropy
    row-groups, or (for ``use_starmap``) tuples whose first element is usually a
    filename. Falls back to the type name, then ``'?'``.
    """
    try:
        head = task[0] if (use_starmap and isinstance(task, (tuple, list))) else task
        if isinstance(head, str):
            return os.path.basename(head)
        if isinstance(head, (int, float)):
            return str(head)
        for attr in ('name', 'filename', 'path'):
            val = getattr(head, attr, None)
            if isinstance(val, str):
                return os.path.basename(val)
        return type(head).__name__
    except Exception:
        return '?'


def _worker_log_sink(timestamp, args, kwargs):
    """Sink installed into ``io._SINK`` inside each worker."""
    _emit((LOG, _WORKER_PID, timestamp, _fmt(args)))


class _QueueLoggingHandler(logging.Handler):
    """Root-logger handler that forwards (stpipe) records onto the queue."""

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            return
        _emit((LOG, _WORKER_PID, _now(), msg))


class _StreamToQueue:
    """File-like shim that forwards a worker's stdout/stderr onto the queue.

    Splits on newlines *and* carriage returns (so ``tqdm``-style progress bars
    don't accumulate forever), drops blank lines, and delegates ``fileno`` to the
    real stream so libraries that probe for a descriptor still work.
    """

    def __init__(self, original):
        self._original = original
        self._buf = ''

    def write(self, s):
        if not s:
            return
        self._buf += s
        while True:
            nl = self._buf.find('\n')
            cr = self._buf.find('\r')
            idx = min(x for x in (nl, cr) if x >= 0) if (nl >= 0 or cr >= 0) else -1
            if idx < 0:
                break
            line = self._buf[:idx].strip()
            self._buf = self._buf[idx + 1:]
            if line:
                _emit((LOG, _WORKER_PID, _now(), line))

    def flush(self):
        line = self._buf.strip()
        if line:
            _emit((LOG, _WORKER_PID, _now(), line))
        self._buf = ''

    def isatty(self):
        return False

    def fileno(self):
        if self._original is not None and hasattr(self._original, 'fileno'):
            return self._original.fileno()
        raise _io.UnsupportedOperation('fileno')


def worker_init(queue):
    """Pool initializer — runs once per worker before any task.

    Routes everything the worker would otherwise scatter across the terminal
    onto *queue*: ``cfpipe`` ``log()`` calls, root-logger records (stpipe), and
    raw ``stdout``/``stderr``. The root logger's existing handlers are stripped
    so stpipe's default stderr ``StreamHandler`` can't double-print; redirecting
    the streams as well catches anything a handler re-adds mid-run.
    """
    global _WORKER_QUEUE, _WORKER_PID
    _WORKER_QUEUE = queue
    _WORKER_PID = os.getpid()

    # cfpipe's own log() -> queue
    cfio.set_sink(_worker_log_sink)

    # stpipe / stdlib logging -> queue (replace, don't add, to kill stderr dupes)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    qh = _QueueLoggingHandler()
    qh.setFormatter(logging.Formatter('%(name)s - %(levelname)s - %(message)s'))
    root.addHandler(qh)
    root.setLevel(logging.INFO)

    # raw stdout/stderr -> queue (belt-and-suspenders for direct prints)
    sys.stdout = _StreamToQueue(sys.__stdout__)
    sys.stderr = _StreamToQueue(sys.__stderr__)


class _ReportingShim:
    """Picklable worker wrapper that brackets each task with start/done events.

    A top-level class (not a closure) so it pickles under ``spawn`` — same
    constraint that motivates ``_RetryOnIOError`` in ``parallel.py``. Holds only
    the (already partial-/retry-wrapped) worker and the starmap flag; it reads
    the queue from the worker global so the queue isn't pickled per task. Return
    values and exceptions pass through unchanged so dispatch() semantics are
    preserved exactly.
    """

    def __init__(self, func, use_starmap):
        self.func = func
        self.use_starmap = use_starmap

    def __call__(self, task):
        pid = os.getpid()
        label = _label(task, self.use_starmap)
        _emit((TASK_START, pid, label))
        try:
            result = self.func(*task) if self.use_starmap else self.func(task)
        except BaseException as exc:
            _emit((TASK_ERROR, pid, label, repr(exc)))
            raise
        _emit((TASK_DONE, pid, label))
        return result


# ---------------------------------------------------------------------------
# Parent-process session
# ---------------------------------------------------------------------------

class ReportingSession:
    """Context manager owning the queue, drain thread and renderer.

    Open one around a whole run (per-observation / per-field loop) in the CLI.
    While active, :func:`active_session` returns it and :func:`dispatch`
    instruments its pools. On ``off`` mode — or when neither a rich nor plain
    renderer is wanted — ``__enter__`` is a no-op and behaviour is identical to
    today.
    """

    def __init__(self, renderer_mode='auto', log_lines=15):
        self.renderer_mode = (renderer_mode or 'auto').lower()
        self.log_lines = int(log_lines)
        self.active = False
        self.queue = None
        self._manager = None
        self._renderer = None
        self._drain_thread = None
        self._group_counter = 0
        self._group_lock = threading.Lock()
        self._prev_session = None

    @classmethod
    def from_config(cls, config, force_plain=False):
        """Build a session from the merged ``[logging]`` config block.

        ``force_plain`` (wired to ``--no-tui``) downgrades a rich display to the
        plain merged renderer without disabling reporting entirely.
        """
        log_cfg = (config or {}).get('logging', {})
        mode = log_cfg.get('renderer', 'auto')
        if force_plain and str(mode).lower() not in ('off',):
            mode = 'plain'
        return cls(renderer_mode=mode, log_lines=log_cfg.get('log_lines', 15))

    # -- renderer selection -------------------------------------------------

    def _make_renderer(self):
        if self.renderer_mode == 'off':
            return None
        from campfire_pipeline.common import render
        if self.renderer_mode == 'plain':
            return render.PlainRenderer()
        if self.renderer_mode == 'rich':
            return render.RichRenderer(log_lines=self.log_lines)
        # auto: rich on an interactive terminal, plain otherwise
        if sys.stdout.isatty():
            try:
                return render.RichRenderer(log_lines=self.log_lines)
            except Exception:
                return render.PlainRenderer()
        return render.PlainRenderer()

    # -- group progress (called from the main process) ----------------------

    def start_group(self, label, total):
        with self._group_lock:
            gid = self._group_counter
            self._group_counter += 1
        self.emit((GROUP_START, gid, label, total))
        return gid

    def advance_group(self, gid):
        self.emit((GROUP_ADVANCE, gid))

    def finish_group(self, gid):
        self.emit((GROUP_DONE, gid))

    def emit(self, event):
        if self.queue is not None:
            try:
                self.queue.put(event)
            except Exception:
                pass

    # -- parent-side log sink ----------------------------------------------

    def _parent_sink(self, timestamp, args, kwargs):
        self.emit((LOG, os.getpid(), timestamp, _fmt(args)))

    # -- drain thread -------------------------------------------------------

    def _drain(self):
        while True:
            try:
                event = self.queue.get(timeout=0.2)
            except Empty:
                self._safe(self._renderer.tick)
                continue
            except (EOFError, OSError, BrokenPipeError):
                break
            if event == _STOP:
                break
            self._safe(self._renderer.handle, event)

    @staticmethod
    def _safe(fn, *a):
        try:
            fn(*a)
        except Exception:
            # A renderer hiccup must never take down the run.
            pass

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self):
        renderer = self._make_renderer()
        if renderer is None:
            self.active = False
            return self

        global _SESSION
        self._prev_session = _SESSION
        self._renderer = renderer
        self._manager = Manager()
        self.queue = self._manager.Queue()

        self._renderer.start()
        cfio.set_sink(self._parent_sink)
        self._drain_thread = threading.Thread(target=self._drain, daemon=True)
        self._drain_thread.start()

        self.active = True
        _SESSION = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.active:
            return False

        global _SESSION
        # Restore logging first so teardown chatter prints normally.
        cfio.set_sink(None)
        _SESSION = self._prev_session

        try:
            self.emit(_STOP)
            if self._drain_thread is not None:
                self._drain_thread.join(timeout=5.0)
        finally:
            self._safe(self._renderer.stop)
            if self._manager is not None:
                self._safe(self._manager.shutdown)
        self.active = False
        return False  # never suppress the original exception
