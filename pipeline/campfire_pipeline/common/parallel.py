"""
Parallel dispatch helper: replaces repeated Pool/serial patterns across stages.
"""

from time import sleep
from functools import partial
from multiprocessing import Pool

from campfire_pipeline.common.io import log


def dispatch(func, tasks, n_processes=1, use_starmap=False, **kwargs):
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
    **kwargs
        Extra keyword arguments bound to *func* via functools.partial.

    Returns
    -------
    list
        Collected return values (one per task).
    """
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
