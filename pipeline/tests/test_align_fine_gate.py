"""Fine-fit acceptance gate: significance of the shift, not residual improvement.

The decision is a bias/variance trade-off — keep the pooled coarse attitude
(which averages ~M times more pairs) unless this detector's own offset exceeds
the noise of estimating it. These tests pin the statistic and the guard rails,
not the tuning constant.
"""
import numpy as np
import pytest

from campfire_pipeline.nircam.align.solve import (  # noqa: E402
    _FINE_MAX_DEGRADE, _RAYLEIGH_MEDIAN, _averaged_scatter,
    _fine_significance)  # noqa: F401


def se_for(resid, n):
    """The standard error the gate divides by."""
    return (resid / _RAYLEIGH_MEDIAN) / np.sqrt(n)


def test_rayleigh_median_constant():
    # median radial deviation of a 2-D Gaussian = sqrt(2 ln 2) * sigma_axis
    rng = np.random.default_rng(1)
    e = rng.normal(0, 3.0, size=(200000, 2))
    assert np.median(np.hypot(*e.T)) == pytest.approx(3.0 * _RAYLEIGH_MEDIAN,
                                                      rel=0.01)


def test_significance_is_shift_over_standard_error():
    resid, n = 0.019, 100
    se = se_for(resid, n)
    assert _fine_significance(3.0 * se, resid, n) == pytest.approx(3.0, rel=1e-9)
    assert _fine_significance(0.5 * se, resid, n) == pytest.approx(0.5, rel=1e-9)


def test_significance_scales_with_match_count():
    """Four times the pairs halves the noise, so the same shift is twice as
    significant — the property that makes a fixed threshold scale-free."""
    resid = 0.019
    shift = 0.002
    t_small = _fine_significance(shift, resid, 25)
    t_large = _fine_significance(shift, resid, 100)
    assert t_large == pytest.approx(2.0 * t_small, rel=1e-9)


def test_significance_scales_with_scatter():
    """A noisier detector needs a bigger shift to clear the same bar."""
    assert (_fine_significance(0.002, 0.040, 64)
            < _fine_significance(0.002, 0.020, 64))


def test_significance_guards_degenerate_inputs():
    assert not np.isfinite(_fine_significance(np.nan, 0.02, 100))
    assert not np.isfinite(_fine_significance(0.002, np.nan, 100))
    assert not np.isfinite(_fine_significance(0.002, 0.02, 1))    # n < 2
    assert not np.isfinite(_fine_significance(0.002, 0.0, 100))   # resid <= 0


def test_default_threshold_accepts_real_offsets_rejects_noise():
    """At the shipped threshold a real detector offset is accepted and a
    noise-level one is not. Numbers from a pg004 F200W visit fitted to a dense
    catalog: 328 distinct sources, 7.8 mas of scatter per axis among the pairs
    averaged, an `rshift` fit; the detector's offset was 4.6 mas, and the fit
    repeated after it was applied moved things by 0.7 mas."""
    import inspect

    from campfire_pipeline.nircam.align.solve import solve_exposure_group
    k = inspect.signature(solve_exposure_group).parameters[
        'fine_min_significance'].default
    assert k == 3.0
    n, scatter = 328, 0.0078
    t_real = _fine_significance(0.0046, 0.006, n, 'rshift', sigma_axis=scatter)
    assert t_real == pytest.approx(8.7, abs=0.2)
    assert t_real > k
    assert _fine_significance(0.0007, 0.006, n, 'rshift', sigma_axis=scatter) < k


def test_significance_is_normalized_for_fit_geometry():
    """A richer geometry moves sources further on noise alone (mean squared
    noise displacement ~ (p/2)*sigma^2/n), so the same shift must score LOWER
    for it. Without this a noise-only `general` fit clears the gate on variance.
    """
    resid, n = 0.019, 100
    shift = 3.0 * se_for(resid, n)
    t = {g: _fine_significance(shift, resid, n, g)
         for g in ('shift', 'rshift', 'rscale', 'general')}
    assert t['shift'] == pytest.approx(3.0, rel=1e-9)
    assert t['rshift'] == pytest.approx(3.0 / np.sqrt(3 / 2), rel=1e-9)
    assert t['rscale'] == pytest.approx(3.0 / np.sqrt(2.0), rel=1e-9)
    assert t['general'] == pytest.approx(3.0 / np.sqrt(3.0), rel=1e-9)
    assert t['shift'] > t['rshift'] > t['rscale'] > t['general']


def test_unknown_geometry_falls_back_to_translation():
    resid, n = 0.019, 100
    assert (_fine_significance(0.002, resid, n, 'nonsense')
            == pytest.approx(_fine_significance(0.002, resid, n, 'shift')))


def test_averaged_scatter_equals_the_median_estimate_for_gaussian_residuals():
    """With Gaussian residuals the scatter of the pairs a fit averages is the
    sigma the median separation implies, so the two noise estimates agree."""
    rng = np.random.default_rng(7)
    sep = np.hypot(*rng.normal(0, 0.020, size=(2, 5000)))
    scatter, n = _averaged_scatter(sep, nclip=3, sigma=3.0)
    assert scatter == pytest.approx(0.020, rel=0.03)
    assert scatter == pytest.approx(np.median(sep) / _RAYLEIGH_MEDIAN, rel=0.05)
    assert n > 0.98 * sep.size                 # clipping at 3 rms drops ~1%


def test_averaged_scatter_sees_the_tail_the_median_does_not():
    """Three pairs in ten are stars that moved since the catalog's epoch and
    scatter three times as much as the rest (60 against 20 mas per axis). The
    clipping at 3 times the rms keeps nearly all of them, so the fit averages
    them; the median separation barely notices them. The significance of a
    given shift falls by the ratio of the two noise estimates."""
    rng = np.random.default_rng(8)
    core = np.hypot(*rng.normal(0, 0.020, size=(2, 700)))
    moved = np.hypot(*rng.normal(0, 0.060, size=(2, 300)))
    sep = np.concatenate([core, moved])
    from_median = np.median(sep) / _RAYLEIGH_MEDIAN
    scatter, n = _averaged_scatter(sep, nclip=3, sigma=3.0)
    assert from_median < 0.027
    assert scatter > 1.3 * from_median
    assert n > 0.95 * sep.size                 # the tail is inside the clipping
    t_median = _fine_significance(0.004, float(np.median(sep)), sep.size)
    t_scatter = _fine_significance(0.004, float(np.median(sep)), n,
                                   sigma_axis=scatter)
    assert t_scatter < 0.8 * t_median


def test_averaged_scatter_drops_what_the_fit_clips():
    """Wrong pairs spread over the whole 0.5 arcsec match radius are removed by
    the fit's clipping, so they do not count toward the noise either."""
    rng = np.random.default_rng(9)
    core = np.hypot(*rng.normal(0, 0.020, size=(2, 900)))
    wrong = 0.5 * np.sqrt(rng.uniform(0, 1, 100))      # uniform over the disk
    scatter, n = _averaged_scatter(np.concatenate([core, wrong]), 3, 3.0)
    assert scatter == pytest.approx(0.020, rel=0.15)
    assert n < 950


def test_averaged_scatter_guards_degenerate_inputs():
    scatter, n = _averaged_scatter(np.array([np.nan, np.nan]), 3, 3.0)
    assert n == 0 and not np.isfinite(scatter)
    assert not np.isfinite(_fine_significance(0.002, 0.02, 100, sigma_axis=np.nan))
    assert not np.isfinite(_fine_significance(0.002, 0.02, 100, sigma_axis=0.0))


def test_fine_shift_wraps_ra_at_the_meridian():
    """A detector straddling RA=0 must not read a ~360 deg displacement for a
    milliarcsecond move."""
    from astropy.table import Table

    from campfire_pipeline.nircam.align.solve import _fine_shift_arcsec

    class _Stub:
        def __init__(self, dra_deg):
            self._d = dra_deg

        def det_to_world(self, x, y):
            # sources straddling the meridian: 359.9999 deg and 0.0001 deg
            ra = np.array([359.9999, 0.0001, 359.99995])
            return (ra + self._d) % 360.0, np.array([2.2, 2.2, 2.2])

    cat = Table({'x': [1.0, 2.0, 3.0], 'y': [1.0, 2.0, 3.0]})
    idx = np.array([0, 1, 2])
    moved = 1.0 / 3.6e6                      # 1 mas in degrees
    got = _fine_shift_arcsec(_Stub(0.0), _Stub(moved), cat, idx)
    assert got == pytest.approx(0.001, rel=0.02)     # 1 mas, not ~1.3e6 arcsec


def test_degrade_guard_is_above_median_noise():
    """The degradation guard must not fire on ordinary noise in the median
    residual (~1.06/sqrt(n), i.e. ~14% at n=60) but must catch real damage."""
    assert _FINE_MAX_DEGRADE > 1.0 + 1.06 / np.sqrt(60)
    assert _FINE_MAX_DEGRADE < 2.0
