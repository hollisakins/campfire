"""Tests for the memory-aware parallel tile scheduler (resample + bkgsub).

The scheduler contract (see the "Parallelism" section of
``nircam/steps/resample.py``):

* ``-p``/``--processes`` is a CEILING — the actual pool width comes from the
  memory budget, and the per-stage MemoryGate throttles below that.
* Scheduler config keys must never enter the manifest config hash: adding or
  tuning them cannot mark existing tiles stale (protecting tens of hours of
  already-built mosaics from a spurious full rebuild).
* ``--processes 1`` stays strictly serial, in-process, ordering-stable, and
  fail-fast.
* In parallel mode a failing tile does not kill its siblings; failures are
  collected and raised together at the end.
* The MemoryGate never over-reserves its budget, clamps oversized requests
  instead of deadlocking, and vetoes admissions on live memory pressure —
  except for the first holder, so one tile always makes progress.
"""

import tempfile
import threading
import time
import types

import pytest
from astropy.io import fits

import campfire_pipeline.common.parallel as parallel_mod
import campfire_pipeline.nircam.steps.resample as resample_mod
from campfire_pipeline.common.parallel import MemoryGate, mem_available_bytes
from campfire_pipeline.nircam.manifest import (
    MOSAIC_BKGSUB_KEY, _resample_config_hash, bkgsub_stamp_value,
)


# Every scheduler key, at non-default values, as a user config would set them.
SCHEDULER_KEYS = {
    'parallel_tiles': False,
    'mem_fraction': 0.5,
    'mem_margin': 2.0,
    'mem_bkgsub_bytes_per_pixel': 99.0,
    'mem_drizzle_base_bytes_per_pixel': 17.0,
}


def _field(n_tiles=3, shape=(100, 100)):
    tiles = {f'A{i}': None for i in range(1, n_tiles + 1)}
    return types.SimpleNamespace(
        name='cosmos',
        tiles=tiles,
        filter_dir=lambda f: tempfile.gettempdir(),
        get_tile_corners=lambda t: [(0, 0), (0, 1), (1, 1), (1, 0)],
        get_tile_wcs=lambda t, pixel_scale: (
            (50.0, 50.0), (150.0, 2.0), shape, 0.0,
        ),
    )


def _worker_kwargs(field):
    return dict(
        filtname='f444w', exposure_files=['x.fits'], field=field,
        step_config={'pixel_scale': '30mas'}, reduction_version='v1',
        pixel_scale=0.03, pixel_scale_str='30mas',
        overwrite=False, epoch=None,
    )


# ---------------------------------------------------------------------------
# MemoryGate
# ---------------------------------------------------------------------------
# The gate's primitives are process-safe *and* thread-safe, so the admission
# logic is exercised with threads in one process.

def test_gate_never_exceeds_budget(monkeypatch):
    monkeypatch.setattr(parallel_mod, 'mem_available_bytes', lambda: None)
    gate = MemoryGate(100, poll_seconds=0.01)
    lock = threading.Lock()
    active, peak = [0], [0]

    def worker():
        with gate.hold(60):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 60 + 60 > 100: strictly one holder at a time, but all three complete.
    assert peak[0] == 1
    assert gate._used.value == 0


def test_gate_oversized_request_clamped(monkeypatch):
    monkeypatch.setattr(parallel_mod, 'mem_available_bytes', lambda: None)
    gate = MemoryGate(100, poll_seconds=0.01)
    granted = gate.acquire(10**9)   # would deadlock if not clamped
    assert granted == 100
    gate.release(granted)
    assert gate._used.value == 0


def test_gate_live_pressure_vetoes_all_but_first_holder(monkeypatch):
    avail = {'v': 0}
    monkeypatch.setattr(parallel_mod, 'mem_available_bytes',
                        lambda: avail['v'])
    gate = MemoryGate(100, reserve_bytes=0, poll_seconds=0.01)
    g1 = gate.acquire(40)   # gate holds nothing yet → live veto bypassed
    entered = threading.Event()

    def second():
        g = gate.acquire(40)
        entered.set()
        gate.release(g)

    t = threading.Thread(target=second)
    t.start()
    try:
        # Token budget has room (40+40 <= 100) but MemAvailable=0 vetoes.
        assert not entered.wait(0.1)
        avail['v'] = 1000
        assert entered.wait(2.0)
    finally:
        gate.release(g1)
        t.join()


def test_gate_first_wait_notice_is_prompt(monkeypatch):
    # A stall shorter than the 5-minute warn cadence must still be visible:
    # the first "waiting" notice fires after the first poll timeout, and the
    # grant line reports the total wait when there was a visible one.
    monkeypatch.setattr(parallel_mod, 'mem_available_bytes', lambda: None)
    lines = []
    monkeypatch.setattr(parallel_mod, 'log', lambda *a, **k: lines.append(a[0]))
    gate = MemoryGate(100, poll_seconds=0.01, warn_seconds=60)
    g1 = gate.acquire(80, label='first')

    def second():
        gate.release(gate.acquire(60, label='second'))

    t = threading.Thread(target=second)
    t.start()
    time.sleep(0.1)   # several poll timeouts, far below warn_seconds
    assert any('second waiting' in ln for ln in lines)
    gate.release(g1)
    t.join()
    granted = [ln for ln in lines if 'second: reserved' in ln]
    assert len(granted) == 1 and 'wait' in granted[0]
    # An uncontended acquire logs no wait suffix.
    assert 'wait' not in next(ln for ln in lines if 'first: reserved' in ln)


def test_mem_available_bytes_reads_something_sane():
    avail = mem_available_bytes()
    # Linux (CI, candide) reads /proc/meminfo; a platform where every source
    # is missing legitimately returns None.
    assert avail is None or avail > 0


# ---------------------------------------------------------------------------
# Footprint estimators
# ---------------------------------------------------------------------------

def test_drizzle_estimate_counts_context_planes():
    cfg = {'mem_drizzle_base_bytes_per_pixel': 10.0, 'mem_margin': 1.0}
    # 1..32 inputs → one int32 context plane; 33 → two.
    assert resample_mod._estimate_drizzle_bytes(1000, 1, cfg) == 1000 * 14
    assert resample_mod._estimate_drizzle_bytes(1000, 32, cfg) == 1000 * 14
    assert resample_mod._estimate_drizzle_bytes(1000, 33, cfg) == 1000 * 18


def test_bkgsub_estimate_scales_with_area_and_config():
    cfg = {'mem_bkgsub_bytes_per_pixel': 50.0, 'mem_margin': 2.0}
    assert resample_mod._estimate_bkgsub_bytes(1000, cfg) == 100_000


# ---------------------------------------------------------------------------
# Manifest-hash invariance
# ---------------------------------------------------------------------------

def test_scheduler_keys_do_not_touch_manifest_hash():
    base = {'pixfrac': 1, 'kernel': 'square'}
    tuned = dict(base, **SCHEDULER_KEYS)
    assert (_resample_config_hash(base, '60mas')
            == _resample_config_hash(tuned, '60mas'))
    assert bkgsub_stamp_value(base) == bkgsub_stamp_value(tuned)


# ---------------------------------------------------------------------------
# Pool planning
# ---------------------------------------------------------------------------

def test_pool_plan_budget_and_ceilings(monkeypatch):
    field = _field(n_tiles=5)
    cfg = {'mem_fraction': 1.0, 'mem_margin': 1.0,
           'mem_drizzle_base_bytes_per_pixel': 6.0}
    # Lightest stage estimate: 100*100 px * (6 + 4 ctx) B/px = 100,000 B.
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 250_000)
    tiles = list(field.tiles)
    assert resample_mod._plan_tile_pool(
        tiles, field, '30mas', cfg, 16) == (2, 250_000)

    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 10**12)
    # -p is the ceiling ...
    assert resample_mod._plan_tile_pool(tiles, field, '30mas', cfg, 3)[0] == 3
    # ... and so is the tile count.
    assert resample_mod._plan_tile_pool(tiles, field, '30mas', cfg, 64)[0] == 5

    # A budget too small for even one estimate still admits one worker (the
    # gate clamps its request rather than deadlocking).
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 10)
    assert resample_mod._plan_tile_pool(tiles, field, '30mas', cfg, 16)[0] == 1


def test_pool_plan_ungated_without_meminfo(monkeypatch):
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: None)
    field = _field()
    n, budget = resample_mod._plan_tile_pool(
        list(field.tiles), field, '30mas', {}, 2)
    assert (n, budget) == (2, None)


# ---------------------------------------------------------------------------
# resample_step routing
# ---------------------------------------------------------------------------

def test_serial_path_never_dispatches(monkeypatch):
    field = _field()
    captured = []
    monkeypatch.setattr(resample_mod, 'select_overlapping_files',
                        lambda files, poly: [])
    monkeypatch.setattr(resample_mod, 'log',
                        lambda *a, **k: captured.append(a[0] if a else ''))

    def no_dispatch(*a, **k):
        raise AssertionError('dispatch must not run at --processes 1')

    monkeypatch.setattr(resample_mod, 'dispatch', no_dispatch)
    resample_mod.resample_step(
        'f444w', ['x.fits'], field, {'pixel_scale': '30mas'}, 'v1',
        n_processes=1,
    )
    visited = [m.split(',')[0].removeprefix('resample: tile ')
               for m in captured
               if isinstance(m, str) and m.startswith('resample: tile ')]
    assert visited == list(field.tiles)


def _patch_parent_selection(monkeypatch, selected_by_tile):
    """Stub the parent-side selection pre-pass of the parallel path.

    ``selected_by_tile`` maps tile name → list the selection should return
    (missing tiles select nothing). The footprint pass is stubbed to a
    sentinel so the test also proves it is computed once and threaded
    through to every per-tile selection call.
    """
    sentinel = object()
    monkeypatch.setattr(resample_mod, 'exposure_footprints',
                        lambda files: sentinel)

    tile_seq = iter([])

    def fake_select(files, poly, footprints=None):
        assert footprints is sentinel
        tile = next(tile_seq)
        return selected_by_tile.get(tile, [])

    def arm(tiles):
        nonlocal tile_seq
        tile_seq = iter(tiles)

    monkeypatch.setattr(resample_mod, 'select_overlapping_files', fake_select)
    return arm


def test_parallel_path_dispatches_with_gate(monkeypatch):
    field = _field()
    arm = _patch_parent_selection(
        monkeypatch, {t: ['x.fits'] for t in field.tiles})
    arm(list(field.tiles))
    calls = {}

    def fake_dispatch(func, tasks, n_processes=1, initializer=None,
                      initargs=(), **kwargs):
        calls.update(func=func, tasks=list(tasks), n_processes=n_processes,
                     initializer=initializer, initargs=initargs,
                     kwargs=kwargs)
        return [{'tile': t, 'error': None} for t, _ in tasks]

    monkeypatch.setattr(resample_mod, 'dispatch', fake_dispatch)
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 1 << 40)
    resample_mod.resample_step(
        'f444w', ['x.fits'], field, {'pixel_scale': '30mas'}, 'v1',
        n_processes=4,
    )
    assert calls['func'] is resample_mod._process_tile
    assert calls['tasks'] == [(t, ['x.fits']) for t in field.tiles]
    assert calls['kwargs']['use_starmap'] is True
    assert 1 < calls['n_processes'] <= 4
    assert calls['initializer'] is resample_mod._init_tile_worker
    assert isinstance(calls['initargs'][0], MemoryGate)
    assert calls['kwargs']['capture_errors'] is True
    assert calls['kwargs']['tag_logs'] is True


def test_parallel_pool_sized_on_tiles_with_work(monkeypatch):
    # 3 tiles, one with no overlapping exposures: the empty tile is skipped
    # in the parent (no worker, no gate traffic) and the pool is sized on
    # the 2 tiles that actually have work, not the raw tile list.
    field = _field(n_tiles=3)
    arm = _patch_parent_selection(
        monkeypatch, {'A1': ['x.fits'], 'A3': ['x.fits']})
    arm(list(field.tiles))
    calls = {}

    def fake_dispatch(func, tasks, n_processes=1, **kwargs):
        calls.update(tasks=list(tasks), n_processes=n_processes)
        return [{'tile': t, 'error': None} for t, _ in tasks]

    monkeypatch.setattr(resample_mod, 'dispatch', fake_dispatch)
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 1 << 40)
    resample_mod.resample_step(
        'f444w', ['x.fits'], field, {'pixel_scale': '30mas'}, 'v1',
        n_processes=8,
    )
    assert calls['tasks'] == [('A1', ['x.fits']), ('A3', ['x.fits'])]
    assert calls['n_processes'] == 2


def test_budget_collapsed_pool_runs_serially_on_parent_selection(monkeypatch):
    # Parallelism was asked for, but the budget affords only one worker. The
    # run falls back to the in-process loop — and must reuse the selection the
    # parent already computed rather than making each tile re-read every
    # exposure header, and must not re-visit the empty tile it already
    # reported skipping.
    field = _field(n_tiles=3)
    arm = _patch_parent_selection(
        monkeypatch, {'A1': ['x.fits'], 'A3': ['y.fits']})
    arm(list(field.tiles))
    seen = []

    monkeypatch.setattr(
        resample_mod, '_resample_tile',
        lambda tile, **kw: seen.append((tile, kw['selected'])))

    def no_dispatch(*a, **k):
        raise AssertionError('a one-worker plan must not open a pool')

    monkeypatch.setattr(resample_mod, 'dispatch', no_dispatch)
    # 100x100 tile at the default estimator constants needs ~572 kB; a 100 kB
    # MemAvailable leaves a budget under one reservation, so the plan is 1.
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 100_000)
    resample_mod.resample_step(
        'f444w', ['x.fits', 'y.fits'], field, {'pixel_scale': '30mas'}, 'v1',
        n_processes=4,
    )
    assert seen == [('A1', ['x.fits']), ('A3', ['y.fits'])]


def test_parallel_all_tiles_empty_skips_pool(monkeypatch):
    field = _field()
    arm = _patch_parent_selection(monkeypatch, {})
    arm(list(field.tiles))

    def no_dispatch(*a, **k):
        raise AssertionError('nothing to do — the pool must not spin up')

    monkeypatch.setattr(resample_mod, 'dispatch', no_dispatch)
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 1 << 40)
    resample_mod.resample_step(
        'f444w', ['x.fits'], field, {'pixel_scale': '30mas'}, 'v1',
        n_processes=4,
    )


def test_worker_uses_parent_selection(monkeypatch):
    # A worker handed `selected` must not re-run the selection. The bogus
    # implementation name stops execution right after the point where the
    # worker would have needed the selection result.
    def boom(files, poly, footprints=None):
        raise AssertionError('worker must not re-select')

    monkeypatch.setattr(resample_mod, 'select_overlapping_files', boom)
    kwargs = _worker_kwargs(_field())
    kwargs['step_config'] = {'pixel_scale': '30mas',
                             'implementation': '__test_stop__'}
    with pytest.raises(ValueError, match='__test_stop__'):
        resample_mod._process_tile('A1', selected=['exp1.fits'], **kwargs)


def test_parallel_tiles_false_stays_serial(monkeypatch):
    field = _field()
    monkeypatch.setattr(resample_mod, 'select_overlapping_files',
                        lambda files, poly: [])

    def no_dispatch(*a, **k):
        raise AssertionError('parallel_tiles = false must stay serial')

    monkeypatch.setattr(resample_mod, 'dispatch', no_dispatch)
    resample_mod.resample_step(
        'f444w', ['x.fits'], field,
        {'pixel_scale': '30mas', 'parallel_tiles': False}, 'v1',
        n_processes=8,
    )


def test_parallel_failures_collected_and_raised(monkeypatch):
    field = _field()
    arm = _patch_parent_selection(
        monkeypatch, {t: ['x.fits'] for t in field.tiles})
    arm(list(field.tiles))

    def fake_dispatch(func, tasks, **kwargs):
        return [{'tile': t, 'error': 'Traceback: boom' if t == 'A2' else None}
                for t, _ in tasks]

    monkeypatch.setattr(resample_mod, 'dispatch', fake_dispatch)
    monkeypatch.setattr(resample_mod, 'mem_available_bytes', lambda: 1 << 40)
    with pytest.raises(RuntimeError, match=r'1/3 tile\(s\) failed.*A2'):
        resample_mod.resample_step(
            'f444w', ['x.fits'], field, {'pixel_scale': '30mas'}, 'v1',
            n_processes=4,
        )


def test_worker_captures_errors_only_when_asked(monkeypatch):
    def boom(files, poly):
        raise ValueError('kaboom')

    monkeypatch.setattr(resample_mod, 'select_overlapping_files', boom)
    kwargs = _worker_kwargs(_field())
    result = resample_mod._process_tile('A1', capture_errors=True, **kwargs)
    assert result['tile'] == 'A1'
    assert 'kaboom' in result['error']
    with pytest.raises(ValueError):
        resample_mod._process_tile('A1', capture_errors=False, **kwargs)


# ---------------------------------------------------------------------------
# Heavy-tail precheck
# ---------------------------------------------------------------------------

def test_heavy_precheck(tmp_path):
    i2d = str(tmp_path / 'mosaic_x_i2d.fits')
    cfg = {}

    assert resample_mod._heavy_work_expected(i2d, True, cfg)    # fresh drizzle
    assert resample_mod._heavy_work_expected(i2d, False, cfg)   # i2d missing

    hdul = fits.HDUList([fits.PrimaryHDU()])
    hdul[0].header[MOSAIC_BKGSUB_KEY] = 'v1 cfg=abc'
    hdul.writeto(i2d)
    # Stamped but split extensions missing → still heavy (re-split).
    assert resample_mod._heavy_work_expected(i2d, False, cfg)

    for suffix in ('_sci.fits', '_err.fits', '_wht.fits'):
        fits.PrimaryHDU().writeto(str(tmp_path / f'mosaic_x{suffix}'))
    # Stamped, splits present, i2d carries no SRCMASK → nothing heavy left.
    assert not resample_mod._heavy_work_expected(i2d, False, cfg)

    # i2d gains a SRCMASK extension without its split file → heavy again ...
    with fits.open(i2d, mode='update') as hdul:
        hdul.append(fits.ImageHDU(name='SRCMASK'))
    assert resample_mod._heavy_work_expected(i2d, False, cfg)
    # ... until the split exists.
    fits.PrimaryHDU().writeto(str(tmp_path / 'mosaic_x_srcmask.fits'))
    assert not resample_mod._heavy_work_expected(i2d, False, cfg)

    # A missing bkgsub stamp is heavy regardless of split state.
    unstamped = str(tmp_path / 'mosaic_y_i2d.fits')
    fits.PrimaryHDU().writeto(unstamped)
    assert resample_mod._heavy_work_expected(unstamped, False, cfg)


# ---------------------------------------------------------------------------
# Real-pool integration: the gate must survive the Pool initializer
# ---------------------------------------------------------------------------

def _gate_probe(task):
    # Runs inside a real pool worker: the initializer must have armed the
    # module-global gate, and holding it must work across processes.
    import os

    import campfire_pipeline.nircam.steps.resample as rm

    assert rm._TILE_GATE is not None
    with rm._gate_hold(10, f'probe {task}'):
        return os.getpid()


def test_gate_rides_the_pool_initializer():
    # multiprocessing sync primitives cannot ride in task arguments — the
    # initializer/initargs channel is the load-bearing plumbing here, so
    # exercise it through a real (forkserver on Linux, spawn on macOS) pool.
    gate = MemoryGate(100, poll_seconds=0.01)
    results = parallel_mod.dispatch(
        _gate_probe, [1, 2, 3], n_processes=2,
        initializer=resample_mod._init_tile_worker, initargs=(gate,),
    )
    assert len(results) == 3
    assert all(isinstance(pid, int) for pid in results)
    assert gate._used.value == 0


# ---------------------------------------------------------------------------
# Log prefixing
# ---------------------------------------------------------------------------

def test_log_prefix_tags_and_clears(capsys):
    from campfire_pipeline.common import io
    io.set_log_prefix('[A3]')
    try:
        io.log('hello')
    finally:
        io.set_log_prefix('')
    io.log('world')
    tagged, untagged = capsys.readouterr().out.strip().split('\n')
    assert tagged.endswith('[A3] hello')
    assert untagged.endswith('world')
    assert '[A3]' not in untagged
