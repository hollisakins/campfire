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

import os
import sys
from functools import partial
from multiprocessing import get_context
from time import sleep

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
             label=None, **kwargs):
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
    label : str, optional
        Human-readable name for this batch, shown as a progress group when a
        reporting session is active. Defaults to ``func.__name__``.
    **kwargs
        Extra keyword arguments bound to *func* via functools.partial.

    Returns
    -------
    list
        Collected return values (one per task, in task order).
    """
    if retry:
        func = _RetryOnIOError(func)

    if kwargs:
        worker = partial(func, **kwargs)
    else:
        worker = func

    # When a reporting session is active, take the instrumented path so the
    # live renderer gets progress + per-worker status + merged logs. Otherwise
    # behaviour below is byte-identical to before.
    from campfire_pipeline.common import reporting
    session = reporting.active_session()
    if session is not None:
        group_label = label or getattr(func, '__name__', 'tasks')
        return _dispatch_reported(worker, tasks, n_processes, use_starmap,
                                  session, group_label)

    if n_processes > 1:
        log(f"Dispatching {len(tasks)} tasks across {n_processes} workers")
        with _MP_CTX.Pool(processes=n_processes) as pool:
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


def _dispatch_reported(worker, tasks, n_processes, use_starmap, session, label):
    """Instrumented dispatch: same result list/order/exceptions, plus events.

    Order matters — some callers (e.g. nirspec ``stuck_shutters``) zip results
    against the input tasks — so the parallel path uses ordered ``pool.imap``,
    never ``imap_unordered``.
    """
    from campfire_pipeline.common import reporting

    gid = session.start_group(label, len(tasks))
    results = []
    try:
        if n_processes > 1:
            log(f"Dispatching {len(tasks)} tasks across {n_processes} workers")
            shim = reporting._ReportingShim(worker, use_starmap)
            with _MP_CTX.Pool(processes=n_processes,
                              initializer=reporting.worker_init,
                              initargs=(session.queue,)) as pool:
                for result in pool.imap(shim, tasks):
                    results.append(result)
                    session.advance_group(gid)
        else:
            log(f"Processing {len(tasks)} tasks serially")
            pid = os.getpid()
            for task in tasks:
                task_label = reporting._label(task, use_starmap)
                session.emit((reporting.TASK_START, pid, task_label))
                try:
                    result = worker(*task) if use_starmap else worker(task)
                except BaseException as exc:
                    session.emit((reporting.TASK_ERROR, pid, task_label,
                                  repr(exc)))
                    raise
                session.emit((reporting.TASK_DONE, pid, task_label))
                results.append(result)
                session.advance_group(gid)
    finally:
        session.finish_group(gid)
    return results
