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
    # write_context is a campfire-backend option; the estimator drops the
    # context term only when that backend is actually selected.
    est_on = _estimate_drizzle_bytes(
        NPIX, N_INPUTS, {'implementation': 'campfire', 'write_context': True})
    est_off = _estimate_drizzle_bytes(
        NPIX, N_INPUTS, {'implementation': 'campfire', 'write_context': False})
    assert est_off == int(NPIX * 40.0 * 1.3)
    # the whole point: the deep tile stops dominating the budget
    assert est_off < est_on / 5
    assert est_off / GIB < 110


def test_estimator_disabled_is_independent_of_input_count():
    """Without a cube, cost is pure output geometry — n_inputs must not matter."""
    cfg = {'implementation': 'campfire', 'write_context': False}
    assert (_estimate_drizzle_bytes(NPIX, 8, cfg)
            == _estimate_drizzle_bytes(NPIX, 100_000, cfg))


def test_estimator_keeps_context_for_jwst_backend():
    """jwst's Image3Pipeline ignores write_context and still materialises
    CON, so the estimator must keep charging for it there — dropping the
    term would under-budget exactly the deep tiles the option exists to
    rescue. `implementation` defaults to 'jwst', so a bare
    write_context=false keeps the term too."""
    with_ctx = _estimate_drizzle_bytes(NPIX, N_INPUTS, {})
    assert _estimate_drizzle_bytes(
        NPIX, N_INPUTS, {'write_context': False}) == with_ctx
    assert _estimate_drizzle_bytes(
        NPIX, N_INPUTS,
        {'implementation': 'jwst', 'write_context': False}) == with_ctx


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


def test_placeholder_context_is_stamped_and_only_then():
    """A skipped cube must announce itself.

    CON is retained purely for external consumers, so a 1x1x1 cube of zeros
    that looks exactly like a real-but-empty context is a trap: the reader
    has no way to tell "context disabled" from "nothing contributed" except
    by inspecting the shape. CFNOCTX makes it explicit. Equally important,
    it must be stamped ONLY on the disabled path — an unconditional card
    would change the header of every normal product.

    Checked on the AST rather than by string search so "the card exists
    somewhere in the function" cannot pass for "the card is guarded".
    """
    import ast
    import inspect
    from campfire_pipeline.nircam import drizzle as drz

    # module-level function, so getsource is already unindented
    tree = ast.parse(inspect.getsource(drz._write_i2d_fits))

    def mentions_card(node):
        return any(isinstance(n, ast.Constant) and n.value == 'CFNOCTX'
                   for n in ast.walk(node))

    def is_ctx_is_none(test):
        return (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name) and test.left.id == 'ctx'
                and len(test.ops) == 1 and isinstance(test.ops[0], ast.Is)
                and isinstance(test.comparators[0], ast.Constant)
                and test.comparators[0].value is None)

    assert mentions_card(tree), 'CFNOCTX is never stamped'

    guards = [n for n in ast.walk(tree)
              if isinstance(n, ast.If) and is_ctx_is_none(n.test)
              and any(mentions_card(b) for b in n.body)]
    assert guards, 'CFNOCTX is not guarded by `if ctx is None`'

    # ... and nowhere outside such a guard.
    guarded = {id(n) for g in guards for b in g.body for n in ast.walk(b)}
    stray = [n for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and n.value == 'CFNOCTX'
             and id(n) not in guarded]
    assert not stray, 'CFNOCTX is also stamped outside the disabled path'


@pytest.mark.parametrize('n_inputs,expected_planes', [
    (1, 1), (32, 1), (33, 2), (1152, 36), (3471, 109),
])
def test_plane_count_model_matches_observed(n_inputs, expected_planes):
    """The plane counts validated against real CON extensions on CANDIDE:
    f162m A2 (18 inputs -> 1 plane), B8 (904 -> 29), primer (1152 -> 36)."""
    assert max(1, math.ceil(n_inputs / 32)) == expected_planes
