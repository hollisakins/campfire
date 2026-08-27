"""
Parallel dispatch helper: replaces repeated Pool/serial patterns across stages.

The start method is platform-aware:

* **Linux → forkserver.** The Linux default ``fork`` blows past per-process
  commit accounting when forking from a multi-GB parent (e.g. after the
  persistence step's snowblind deep-copies fragment the heap): the kernel must
  reserve ``parent_RSS × n_workers`` bytes at fork time even though
  copy-on-write means actual usage will be far smaller, and ``os.fork()``
  returns ``ENOMEM`` before any real allocation happens. The forkserver helper
  is launched once early, stays small (~tens of MB plus the preloaded modules
  below), and is what does the per-task forks.

* **macOS → spawn.** ``fork()`` (and therefore ``forkserver``, which forks its
  workers from the helper) is unsafe on macOS: Apple's threaded frameworks
  (GCD / Accelerate) and cv2's ``parallel_for_`` pool deadlock in the child.
  This is why Python defaults macOS to ``spawn``. stcal's snowball flagging
  (``stcal.jump.jump.flag_large_events``) calls into cv2, so a forked worker
  hangs (``S`` / 0% CPU) the moment it reaches "Flagging Snowballs". The
  macOS ENOMEM concern that motivates forkserver on candide does not apply
  here, so ``spawn`` is both safe and sufficient.

Preloading the heavy scientific imports into the forkserver lets Linux workers
inherit them via copy-on-write rather than re-importing per pool. ``jhat`` and
``tweakreg`` are intentionally absent — importing them touches CRDS singleton
state in a way that locks the context (see feedback_lazy_jwst_imports), so
those stay as lazy imports inside the worker functions that need them. ``stcal``
and ``snowblind`` are likewise absent: pre-importing cv2 into the forkserver
helper risks the same half-built-pool deadlock there. (``spawn`` ignores the
preload list entirely — each worker re-imports from scratch.)
"""

import sys
from contextlib import contextmanager
from functools import partial
from multiprocessing import get_context
from time import monotonic, sleep

from campfire_pipeline.common.io import log


if sys.platform == 'darwin':
    _MP_CTX = get_context('spawn')
else:
    _MP_CTX = get_context('forkserver')
    _MP_CTX.set_forkserver_preload([
        'numpy',
        'scipy',
        'astropy.io.fits',
        'astropy.wcs',
        'astropy.table',
        'jwst',
        'jwst.datamodels',
        'stdatamodels',
        'stdatamodels.jwst',
        'crds',
        'campfire_pipeline.common.io',
        'campfire_pipeline.common.cfp',
        'campfire_pipeline.common.parallel',
    ])


def mem_available_bytes():
    """Best-effort *currently available* system memory, in bytes.

    Linux: ``MemAvailable`` from ``/proc/meminfo`` — the kernel's estimate of
    what new allocations can claim without swapping, which correctly discounts
    whatever this process (and everyone else on a shared node) already holds.
    Elsewhere: ``psutil`` when importable. Returns ``None`` when neither
    source exists; callers must treat ``None`` as "unknown", never as zero.
    """
    try:
        with open('/proc/meminfo') as fp:
            for line in fp:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        import psutil
        return int(psutil.virtual_memory().available)
    except Exception:
        return None


class MemoryGate:
    """Weighted byte-budget semaphore for memory-heavy pipeline stages.

    A pool worker reserves an *estimated* peak footprint before entering a
    heavy stage and releases it after, so total reserved bytes never exceed
    ``budget_bytes`` no matter how many workers the pool has — the pool size
    caps CPU concurrency, the gate caps memory concurrency. Distinct stages
    reserve their own estimates (e.g. mosaic bkgsub is ~2.5-3x heavier than
    the drizzle), which both throttles the heavy stage harder and staggers
    its peaks instead of aligning them.

    Admission is doubly guarded: the byte ledger against ``budget_bytes``
    (fixed at construction), *and* a live re-read of ``MemAvailable`` at each
    admission minus ``reserve_bytes`` of slack — the nodes are shared, so
    loop-start state can be stale by the time a tile is admitted. The live
    veto is skipped while the gate holds nothing, so one tile always makes
    progress and external pressure can delay but never permanently starve
    the run. A request larger than the whole budget is clamped to it (the
    oversized stage then runs alone rather than deadlocking).

    Create it in the parent from this module's multiprocessing context and
    hand it to workers via the pool ``initializer`` — multiprocessing sync
    primitives cannot ride in task arguments, only through process setup.
    The primitives also work across plain threads in one process, which is
    how the unit tests exercise the admission logic.
    """

    def __init__(self, budget_bytes, *, reserve_bytes=8 << 30,
                 poll_seconds=30, warn_seconds=300):
        self.budget = max(int(budget_bytes), 1)
        self.reserve = int(reserve_bytes)
        self.poll = poll_seconds
        self.warn = warn_seconds
        # Raw shared value; every access is under self._cond's lock.
        self._used = _MP_CTX.Value('q', 0, lock=False)
        self._cond = _MP_CTX.Condition()

    def acquire(self, nbytes, label=''):
        """Block until *nbytes* (clamped to the budget) can be reserved.

        Returns the number of bytes actually reserved, which must be passed
        back to :meth:`release`.
        """
        req = min(max(int(nbytes), 0), self.budget)
        start = monotonic()
        last_warn = 0.0
        with self._cond:
            while True:
                if self._used.value + req <= self.budget:
                    avail = mem_available_bytes()
                    if (self._used.value == 0 or avail is None
                            or avail - self.reserve >= req):
                        self._used.value += req
                        if label:
                            log(f"memory gate: {label}: reserved "
                                f"{req / 2**30:.1f} GiB (gate "
                                f"{self._used.value / 2**30:.1f}/"
                                f"{self.budget / 2**30:.1f} GiB)")
                        return req
                self._cond.wait(timeout=self.poll)
                waited = monotonic() - start
                if waited - last_warn >= self.warn:
                    last_warn = waited
                    log(f"memory gate: {label or 'task'} waiting "
                        f"{waited:.0f}s for {req / 2**30:.1f} GiB "
                        f"(gate {self._used.value / 2**30:.1f}/"
                        f"{self.budget / 2**30:.1f} GiB reserved)")

    def release(self, granted):
        with self._cond:
            self._used.value -= int(granted)
            self._cond.notify_all()

    @contextmanager
    def hold(self, nbytes, label=''):
        granted = self.acquire(nbytes, label=label)
        try:
            yield
        finally:
            self.release(granted)


class _RetryOnIOError:
    """Picklable retry wrapper for CRDS file race conditions.

    Uses a class instead of a closure so multiprocessing can pickle it
    (closures from @wraps break pickle because the wrapper's qualname
    still points to the original function).
    """

    _CRDS_ERROR_PHRASES = (
        'empty or corrupt fits',
        'no simple card found',
        'cannot reshape array',
        'not a fits file',
    )

    def __init__(self, func, max_retries=2, delays=(3, 10)):
        self.func = func
        self.max_retries = max_retries
        self.delays = delays

    def __call__(self, *args, **kwargs):
        for attempt in range(self.max_retries + 1):
            try:
                return self.func(*args, **kwargs)
            except (OSError, IOError, ValueError) as e:
                err_msg = str(e).lower()
                is_crds_error = any(p in err_msg for p in self._CRDS_ERROR_PHRASES)
                if is_crds_error and attempt < self.max_retries:
                    delay = self.delays[min(attempt, len(self.delays) - 1)]
                    log(f"CRDS cache error (attempt {attempt + 1}/"
                        f"{self.max_retries + 1}): {e}. Retrying in {delay}s...")
                    sleep(delay)
                else:
                    raise


def dispatch(func, tasks, n_processes=1, use_starmap=False, retry=False,
             initializer=None, initargs=(), **kwargs):
    """Run *func* over *tasks* serially or in parallel.

    Parameters
    ----------
    func : callable
        Worker function.
    tasks : list
        Items to process.  Each item is either a single positional arg
        (use_starmap=False → Pool.map) or a tuple of positional args
        (use_starmap=True → Pool.starmap).
    n_processes : int
        1 for serial execution, >1 for multiprocessing.
    use_starmap : bool
        If True, each task is unpacked as positional args.
    retry : bool
        If True, wrap worker with retry logic for CRDS file errors.
    initializer, initargs : callable, tuple
        Forwarded to ``Pool`` so workers can receive state that cannot ride
        in task arguments (e.g. a :class:`MemoryGate`'s sync primitives).
        Ignored on the serial path — the caller already holds any such state
        in-process.
    **kwargs
        Extra keyword arguments bound to *func* via functools.partial.

    Returns
    -------
    list
        Collected return values (one per task).
    """
    if retry:
        func = _RetryOnIOError(func)

    if kwargs:
        worker = partial(func, **kwargs)
    else:
        worker = func

    if n_processes > 1:
        log(f"Dispatching {len(tasks)} tasks across {n_processes} workers")
        with _MP_CTX.Pool(processes=n_processes, initializer=initializer,
                          initargs=initargs) as pool:
            if use_starmap:
                return pool.starmap(worker, tasks)
            else:
                return pool.map(worker, tasks)
    else:
        log(f"Processing {len(tasks)} tasks serially")
        results = []
        for task in tasks:
            if use_starmap:
                results.append(worker(*task))
            else:
                results.append(worker(task))
        return results
