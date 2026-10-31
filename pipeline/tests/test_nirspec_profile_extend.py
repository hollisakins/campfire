"""The light past a source's cutout (profile_extend): the per-end solve."""
import numpy as np


def _block(lam, spec, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    gimg = np.full(lam.shape, 0.5)
    D = gimg * spec(lam) + noise * rng.standard_normal(lam.shape)
    V = np.full(lam.shape, max(noise, 1e-3) ** 2)
    ok = np.ones(lam.shape, bool)
    return (0, D, V, ok, [(1.0, gimg, lam)])


def test_solve_end_recovers_a_spectrum_in_count_rate_units():
    from campfire_pipeline.nirspec import profile_extend as PE
    lam = np.tile(np.linspace(5.3, 5.6, 200), (6, 1))
    spec = lambda x: 2.0 - 3.0 * (x - 5.3)                                   # noqa: E731
    out = PE._solve_end([_block(lam, spec)], np.sort(lam[0]), 0, True, 5.0, 2)
    assert out is not None
    good = np.isfinite(out['flux'])
    assert np.allclose(out['flux'][good], spec(out['lam'][good]), rtol=1e-3)


def test_solve_end_uses_only_pixels_on_its_own_grid():
    from campfire_pipeline.nirspec import profile_extend as PE
    # one block holds both ends; the blue end's grid must not take the red end's pixels
    lam = np.tile(np.r_[np.linspace(2.6, 2.85, 100), np.linspace(5.3, 5.6, 100)], (6, 1))
    spec = lambda x: np.where(x < 4, 1.0, 7.0)                             # noqa: E731
    blk = _block(lam, spec)
    blue = PE._solve_end([blk], np.sort(lam[0][lam[0] < 4]), 0, True, 5.0, 2)
    red = PE._solve_end([blk], np.sort(lam[0][lam[0] > 4]), 0, True, 5.0, 2)
    assert np.allclose(blue['flux'][np.isfinite(blue['flux'])], 1.0, rtol=1e-3)
    assert np.allclose(red['flux'][np.isfinite(red['flux'])], 7.0, rtol=1e-3)
    assert blue['lam'].max() < 2.9 and red['lam'].min() > 5.2


def test_solve_end_shorter_than_the_smoothing_window():
    from campfire_pipeline.nirspec import profile_extend as PE
    lam = np.tile(np.linspace(5.3, 5.34, 17), (8, 1))
    out = PE._solve_end([_block(lam, lambda x: np.full_like(x, 3.0))], np.sort(lam[0]), 31, True, 5.0, 2)
    assert out is not None and out['flux'].size == 17
    assert np.allclose(out['flux'][np.isfinite(out['flux'])], 3.0, rtol=1e-3)


def test_solve_end_too_few_pixels():
    from campfire_pipeline.nirspec import profile_extend as PE
    lam = np.tile(np.linspace(5.3, 5.31, 3), (2, 1))
    assert PE._solve_end([_block(lam, lambda x: np.ones_like(x))], np.sort(lam[0]), 0, True, 5.0, 2) is None
