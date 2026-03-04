"""
Parallel dispatch helper: replaces repeated Pool/serial patterns across stages.
"""

from time import sleep
from functools import partial
from multiprocessing import Pool

from campfire_pipeline.common.io import log


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
             **kwargs):
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
        sleep(1)  # brief pause for log readability before pool forks
        with Pool(processes=n_processes) as pool:
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
