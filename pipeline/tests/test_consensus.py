"""Tests for the PRISM-priority redshift consensus hierarchy."""

import numpy as np
import pytest

from campfire_pipeline.metadata.summary import (
    determine_best_redshift,
    _filter_informative,
    _scalar_fallback,
)


def _make_chi2_curve(zbest, n_pix, chi2_range, chi2_min=100.0, n_z=500):
    """
    Build a synthetic chi2(z) curve with a clear minimum at zbest.

    The curve is a parabola centered at zbest, scaled so that
    max(chi2) - min(chi2) = chi2_range.
    """
    z = np.linspace(0.0, 10.0, n_z)
    # Parabola: chi2 = chi2_min + chi2_range * ((z - zbest) / half_span)^2
    half_span = (z[-1] - z[0]) / 2
    chi2 = chi2_min + chi2_range * ((z - zbest) / half_span) ** 2
    # Clip so the range is exactly chi2_range at the endpoints
    chi2 = np.clip(chi2, chi2_min, chi2_min + chi2_range)
    return {
        'z': z,
        'chi2': chi2,
        'chi2_min': chi2_min,
        'zbest': zbest,
        'n_pix': n_pix,
    }


def _make_flat_curve(zbest, n_pix, chi2_min=100.0, n_z=500):
    """Build a nearly flat chi2 curve (uninformative, range/pix < 0.01)."""
    z = np.linspace(0.0, 10.0, n_z)
    noise = np.random.default_rng(42).uniform(0, 0.005 * n_pix, size=n_z)
    chi2 = chi2_min + noise
    min_idx = np.argmin(np.abs(z - zbest))
    chi2[min_idx] = chi2_min
    return {
        'z': z,
        'chi2': chi2,
        'chi2_min': chi2_min,
        'zbest': zbest,
        'n_pix': n_pix,
    }


# -----------------------------------------------------------------------
# Case 1: PRISM only
# -----------------------------------------------------------------------
def test_prism_only():
    prism = _make_chi2_curve(zbest=3.5, n_pix=400, chi2_range=50.0)
    result = determine_best_redshift(chi2_by_grating={'PRISM': prism})
    assert result == pytest.approx(3.5, abs=0.05)


# -----------------------------------------------------------------------
# Case 2: PRISM + 1 grating
# -----------------------------------------------------------------------
def test_prism_plus_one_grating_agreement():
    """Grating within dz=0.1 of PRISM → adopt grating zbest for precision."""
    prism = _make_chi2_curve(zbest=3.50, n_pix=400, chi2_range=50.0)
    g395m = _make_chi2_curve(zbest=3.52, n_pix=1200, chi2_range=200.0)
    result = determine_best_redshift(
        chi2_by_grating={'PRISM': prism, 'G395M': g395m},
    )
    assert result == pytest.approx(3.52, abs=0.05)


def test_prism_plus_one_grating_disagreement():
    """Grating far from PRISM → trust PRISM."""
    prism = _make_chi2_curve(zbest=3.50, n_pix=400, chi2_range=50.0)
    g395m = _make_chi2_curve(zbest=6.80, n_pix=1200, chi2_range=200.0)
    result = determine_best_redshift(
        chi2_by_grating={'PRISM': prism, 'G395M': g395m},
    )
    assert result == pytest.approx(3.50, abs=0.05)


# -----------------------------------------------------------------------
# Case 3: PRISM + N gratings
# -----------------------------------------------------------------------
def test_prism_plus_multi_grating_consensus_agrees():
    """Grating consensus within dz=0.1 of PRISM → adopt consensus z."""
    prism = _make_chi2_curve(zbest=3.50, n_pix=400, chi2_range=50.0)
    g235m = _make_chi2_curve(zbest=3.53, n_pix=800, chi2_range=150.0)
    g395m = _make_chi2_curve(zbest=3.51, n_pix=1200, chi2_range=200.0)
    result = determine_best_redshift(
        chi2_by_grating={'PRISM': prism, 'G235M': g235m, 'G395M': g395m},
    )
    # Should be near the grating consensus, not PRISM
    assert abs(result - 3.50) <= 0.1
    # And should NOT be exactly PRISM zbest (gratings refine it)
    assert result != pytest.approx(3.50, abs=0.001)


def test_prism_plus_multi_grating_consensus_disagrees():
    """Grating consensus far from PRISM → trust PRISM."""
    prism = _make_chi2_curve(zbest=3.50, n_pix=400, chi2_range=50.0)
    g235m = _make_chi2_curve(zbest=6.80, n_pix=800, chi2_range=150.0)
    g395m = _make_chi2_curve(zbest=6.85, n_pix=1200, chi2_range=200.0)
    result = determine_best_redshift(
        chi2_by_grating={'PRISM': prism, 'G235M': g235m, 'G395M': g395m},
    )
    assert result == pytest.approx(3.50, abs=0.05)


# -----------------------------------------------------------------------
# Case 4: no PRISM, multiple gratings
# -----------------------------------------------------------------------
def test_no_prism_multi_grating_consensus():
    """Three informative gratings, no PRISM → grating consensus."""
    g140m = _make_chi2_curve(zbest=2.50, n_pix=600, chi2_range=100.0)
    g235m = _make_chi2_curve(zbest=2.52, n_pix=800, chi2_range=150.0)
    g395m = _make_chi2_curve(zbest=2.48, n_pix=1200, chi2_range=200.0)
    result = determine_best_redshift(
        chi2_by_grating={'G140M': g140m, 'G235M': g235m, 'G395M': g395m},
    )
    assert abs(result - 2.50) < 0.1


def test_no_prism_flat_curve_gated():
    """One flat grating excluded → consensus from remaining two."""
    g140m = _make_flat_curve(zbest=0.50, n_pix=600)  # flat, should be excluded
    g235m = _make_chi2_curve(zbest=2.50, n_pix=800, chi2_range=150.0)
    g395m = _make_chi2_curve(zbest=2.52, n_pix=1200, chi2_range=200.0)
    result = determine_best_redshift(
        chi2_by_grating={'G140M': g140m, 'G235M': g235m, 'G395M': g395m},
    )
    assert abs(result - 2.50) < 0.1


def test_no_prism_all_flat_fallback():
    """All gratings flat → fall back to best single grating by priority."""
    g235m = _make_flat_curve(zbest=1.50, n_pix=800)
    g395m = _make_flat_curve(zbest=4.00, n_pix=1200)
    result = determine_best_redshift(
        chi2_by_grating={'G235M': g235m, 'G395M': g395m},
    )
    # G395M has higher priority (lower GRATING_PRIORITY value)
    assert result == pytest.approx(4.00, abs=0.05)


# -----------------------------------------------------------------------
# Case 5: single grating, no PRISM
# -----------------------------------------------------------------------
def test_single_grating_no_prism():
    g395m = _make_chi2_curve(zbest=5.20, n_pix=1200, chi2_range=200.0)
    result = determine_best_redshift(chi2_by_grating={'G395M': g395m})
    assert result == pytest.approx(5.20, abs=0.05)


# -----------------------------------------------------------------------
# Case 6: scalar fallback
# -----------------------------------------------------------------------
def test_scalar_fallback():
    scalar = {
        'G395M': {'redshift': 3.5, 'exposure_time': 5000, 'signal_to_noise': 10.0},
        'G235M': {'redshift': 2.0, 'exposure_time': 3000, 'signal_to_noise': 5.0},
    }
    result = determine_best_redshift(scalar_by_grating=scalar)
    # G395M has higher SNR, should be selected
    assert result == pytest.approx(3.5)


def test_empty_input():
    assert determine_best_redshift() is None
    assert determine_best_redshift(chi2_by_grating={}) is None
    assert determine_best_redshift(chi2_by_grating={}, scalar_by_grating={}) is None


# -----------------------------------------------------------------------
# Helper tests
# -----------------------------------------------------------------------
def test_filter_informative():
    informative = _make_chi2_curve(zbest=3.0, n_pix=500, chi2_range=100.0)
    flat = _make_flat_curve(zbest=1.0, n_pix=500)

    result = _filter_informative(
        {'G395M': informative, 'G235M': flat},
        min_chi2_range_per_pix=0.05,
    )
    assert 'G395M' in result
    assert 'G235M' not in result


def test_scalar_fallback_empty():
    assert _scalar_fallback(None) is None
    assert _scalar_fallback({}) is None
