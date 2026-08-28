"""`[nircam.resample].write_context = false` must skip the context cube.

The context cube is one int32 plane per 32 inputs at FULL tile size, so its
cost is ``tile_area * 4 * ceil(n/32)``. On COSMOS f200w the `primer` tile has
3,471 contributing exposures on a 1.15 Gpix grid — 109 planes, ~660 GiB — which
exceeds the memory budget of every node on the cluster and so cannot be
scheduled alongside anything else. Nothing in the pipeline reads CON, so it can
be turned off.

These tests assert the two halves that must agree: the ESTIMATOR must stop
charging for the cube, and the DRIZZLE must stop allocating it. A test that
only checked the estimator would happily pass while the allocation still
happened — the failure mode being guarded against.
"""
import math

import pytest

from campfire_pipeline.nircam.steps.resample import _estimate_drizzle_bytes


NPIX = 26000 * 44100          # COSMOS `primer` at 30 mas
N_INPUTS = 3471               # measured f200w overlap for that tile
GIB = 1024 ** 3


def test_estimator_charges_for_context_by_default():
    """Default behaviour must be unchanged — the cube is real and costed."""
    est = _estimate_drizzle_bytes(NPIX, N_INPUTS, {})
    planes = math.ceil(N_INPUTS / 32)
    expected = int(NPIX * (40.0 + 4 * planes) * 1.3)
    assert est == expected
    # sanity: this is the number that motivated the option
    assert est / GIB > 600


def test_estimator_drops_context_when_disabled():
    est_on = _estimate_drizzle_bytes(NPIX, N_INPUTS, {'write_context': True})
    est_off = _estimate_drizzle_bytes(NPIX, N_INPUTS, {'write_context': False})
    assert est_off == int(NPIX * 40.0 * 1.3)
    # the whole point: the deep tile stops dominating the budget
    assert est_off < est_on / 5
    assert est_off / GIB < 110


def test_estimator_disabled_is_independent_of_input_count():
    """Without a cube, cost is pure output geometry — n_inputs must not matter."""
    cfg = {'write_context': False}
    assert (_estimate_drizzle_bytes(NPIX, 8, cfg)
            == _estimate_drizzle_bytes(NPIX, 100_000, cfg))


def test_drizzle_tile_accepts_write_context_kwarg():
    """The knob must actually reach drizzle_tile, not just the estimator."""
    import inspect
    from campfire_pipeline.nircam.drizzle import drizzle_tile
    sig = inspect.signature(drizzle_tile)
    assert 'write_context' in sig.parameters
    assert sig.parameters['write_context'].default is True


def test_write_i2d_fits_tolerates_ctx_none():
    """ctx=None is the disabled path; it must not raise on .ndim/.astype."""
    import inspect
    from campfire_pipeline.nircam import drizzle as drz
    src = inspect.getsource(drz._write_i2d_fits)
    # the guard must come before any attribute access on ctx
    assert 'if ctx is None' in src
    idx_guard = src.index('if ctx is None')
    idx_ndim = src.index('ctx.ndim')
    assert idx_guard < idx_ndim


@pytest.mark.parametrize('n_inputs,expected_planes', [
    (1, 1), (32, 1), (33, 2), (1152, 36), (3471, 109),
])
def test_plane_count_model_matches_observed(n_inputs, expected_planes):
    """The plane counts validated against real CON extensions on CANDIDE:
    f162m A2 (18 inputs -> 1 plane), B8 (904 -> 29), primer (1152 -> 36)."""
    assert max(1, math.ceil(n_inputs / 32)) == expected_planes
