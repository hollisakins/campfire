"""
Wall-clock and CPU time of the stage-3 profile-fit steps (for the timing baseline).

Two levels, both written to the log as one line each so a run's log can be summed:

* ``phase(name)`` in the driver process: wall time, and CPU time of the driver plus
  every worker process that finished during the phase (``getrusage`` of the
  process and its reaped children; ``dispatch`` joins its pool, so a phase's
  workers are counted in that phase).
  Line: ``TIMING-PHASE <name> wall=<s> cpu=<s>``.
* ``step(name)`` inside one task (a product, a joint group): wall and CPU time of
  the calling process, summed per step name until ``report`` writes them and
  starts over. Steps may nest (a sub-step's time is also inside its parent).
  Line: ``TIMING <label> <step>=<wall>/<cpu> ...``.
"""

import contextlib
import resource
import time
from collections import defaultdict

_wall = defaultdict(float)
_cpu = defaultdict(float)
_count = defaultdict(int)


def _rusage_cpu():
    s = resource.getrusage(resource.RUSAGE_SELF)
    c = resource.getrusage(resource.RUSAGE_CHILDREN)
    return s.ru_utime + s.ru_stime + c.ru_utime + c.ru_stime


@contextlib.contextmanager
def phase(name, log=None):
    """Time a driver phase (wall, and CPU of the driver and its finished workers)."""
    w0, c0 = time.perf_counter(), _rusage_cpu()
    try:
        yield
    finally:
        if log is None:
            from campfire_pipeline.common.io import log as _log
            log = _log
        log(f'TIMING-PHASE {name} wall={time.perf_counter() - w0:.2f} cpu={_rusage_cpu() - c0:.2f}')


@contextlib.contextmanager
def step(name):
    """Accumulate the wall and CPU time of a step of the current task."""
    w0, c0 = time.perf_counter(), time.process_time()
    try:
        yield
    finally:
        _wall[name] += time.perf_counter() - w0
        _cpu[name] += time.process_time() - c0
        _count[name] += 1


def start():
    """Start mark for ``stop`` (a step that does not fit a ``with`` block)."""
    return time.perf_counter(), time.process_time()


def stop(name, mark):
    _wall[name] += time.perf_counter() - mark[0]
    _cpu[name] += time.process_time() - mark[1]
    _count[name] += 1


def reset():
    _wall.clear()
    _cpu.clear()
    _count.clear()


def snapshot():
    """{step: (wall, cpu, calls)} accumulated since the last ``reset``."""
    return {k: (_wall[k], _cpu[k], _count[k]) for k in _wall}


def report(label, log=None):
    """Log the accumulated steps of a task and start over."""
    if log is None:
        from campfire_pipeline.common.io import log as _log
        log = _log
    s = snapshot()
    if s:
        log(f'TIMING {label} ' + ' '.join(f'{k}={w:.3f}/{c:.3f}' for k, (w, c, _) in sorted(s.items())))
    reset()
