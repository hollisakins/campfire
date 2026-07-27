"""Fine-fit acceptance gate: significance of the shift, not residual improvement.

The decision is a bias/variance trade-off — keep the pooled coarse attitude
(which averages ~M times more pairs) unless this detector's own offset exceeds
the noise of estimating it. These tests pin the statistic and the guard rails,
not the tuning constant.
"""
import numpy as np
import pytest

from campfire_pipeline.nircam.align.solve import (  # noqa: E402
    _FINE_MAX_DEGRADE, _RAYLEIGH_MEDIAN, _fine_significance)


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
    """At the shipped k=1.4, a COSMOS-typical real offset is accepted and a
    noise-level one is not. Numbers from the f410m calibration: sigma ~19 mas,
    n ~ 285 -> se ~ 0.95 mas; observed real offsets ~3 mas."""
    k = 1.4
    resid, n = 0.019, 285
    se = se_for(resid, n)
    assert se * 1e3 == pytest.approx(0.95, abs=0.15)
    assert _fine_significance(0.003, resid, n) > k       # real 3 mas offset
    assert _fine_significance(0.0005, resid, n) < k      # 0.5 mas: noise


def test_degrade_guard_is_above_median_noise():
    """The degradation guard must not fire on ordinary noise in the median
    residual (~1.06/sqrt(n), i.e. ~14% at n=60) but must catch real damage."""
    assert _FINE_MAX_DEGRADE > 1.0 + 1.06 / np.sqrt(60)
    assert _FINE_MAX_DEGRADE < 2.0
