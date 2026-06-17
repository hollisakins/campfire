"""Tests for the GP-based per-amp-row 1/f striping estimator.

Synthesizes NIRCam-shaped frames with a known, smooth per-amp-row 1/f
offset (constant per column added on top) and verifies that the GP
estimator:

1. Recovers the smooth per-amp-row offset on a clean frame.
2. Preserves the *distinct* per-amp DC level (no amp-to-amp step).
3. Interpolates the offset across a masked bright-source band using
   anchor rows of the same amp — beating the median + full-row fallback,
   which substitutes the (wrong) cross-amp median there.
4. Flags wide, anchorless gaps via inflated posterior variance rather
   than silently filling them.
5. Leaves the ``estimator="median"`` path byte-identical and validates
   the ``gp_params`` contract.

The pure ``gp_amprow_offsets`` / ``fit_residual_striping`` primitives are
where the algorithmic correctness lives; the full step is exercised by
the A/B pipeline runs.
"""

import numpy as np
import pytest

from campfire_pipeline.nircam.constants import NIR_AMPS
from campfire_pipeline.nircam.gp_striping import (
    _amprow_statistics,
    gp_amprow_offsets,
)
from campfire_pipeline.nircam.steps.striping import (
    _median_amprow_offsets,
    fit_residual_striping,
)

celerite2 = pytest.importorskip('celerite2')

SHAPE = (2048, 2048)
AMP_DC = {'A': 0.05, 'B': 0.02, 'C': -0.03, 'D': 0.01}


def _planted_frame(rng, noise=0.02, amp_dc=AMP_DC):
    """Frame = per-amp DC + smooth per-row 1/f + per-column + white noise.

    Returns ``(data, truth_horizontal)`` where ``truth_horizontal`` is the
    per-amp per-row offset broadcast across each amp's columns (what the
    horizontal estimate should recover).
    """
    rows = np.arange(SHAPE[0])
    truth = np.zeros(SHAPE)
    for amp, dc in amp_dc.items():
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        f = dc + 0.01 * np.sin(rows / 130.0) + 0.004 * np.cos(rows / 41.0)
        truth[:, c0:c1] = f[:, None]
    col = 0.003 * rng.standard_normal(SHAPE[1])[None, :]
    data = truth + col + noise * rng.standard_normal(SHAPE)
    return data, truth


def _amp_rms(estimate, truth, amp, rows):
    _, _, c0, _ = NIR_AMPS[amp]['data']
    return float(np.sqrt(np.mean((estimate[rows, c0] - truth[rows, c0]) ** 2)))


def test_amprow_statistics_basic():
    rng = np.random.default_rng(0)
    data, _ = _planted_frame(rng)
    _, _, c0, c1 = NIR_AMPS['A']['data']
    mask = np.zeros((SHAPE[0], c1 - c0), bool)
    y_r, s_hat, n_r = _amprow_statistics(data[:, c0:c1], mask, 2.0, 3)
    assert y_r.shape == (SHAPE[0],)
    # All rows have pixels; scatter ~ injected noise; counts ~ amp width.
    assert np.all(n_r > 0)
    assert np.isfinite(y_r).all()
    assert 0.01 < np.median(s_hat) < 0.04
    # Fully masking a row drives its count to zero and its median to NaN.
    mask[100, :] = True
    y_r2, _, n_r2 = _amprow_statistics(data[:, c0:c1], mask, 2.0, 3)
    assert n_r2[100] == 0
    assert np.isnan(y_r2[100])


def test_gp_recovers_clean_offset_and_preserves_dc():
    rng = np.random.default_rng(1)
    data, truth = _planted_frame(rng)
    mask = np.zeros(SHAPE, bool)
    horizontal, diag, ks_eff = gp_amprow_offsets(
        data, mask, rho=80.0, kernel_sigma=0.02)
    clean = np.arange(200, 400)
    for amp, dc in AMP_DC.items():
        # Smoothing beats the per-row noise floor (0.02) substantially.
        assert _amp_rms(horizontal, truth, amp, clean) < 0.01
        # Distinct per-amp DC preserved → no amp-to-amp step reintroduced.
        _, _, c0, _ = NIR_AMPS[amp]['data']
        assert abs(np.median(horizontal[clean, c0]) - np.median(truth[clean, c0])) < 0.01
    assert len(diag) == 4
    assert ks_eff == 0.02  # explicit override echoed back


def test_gp_self_adapting_amplitude():
    """With kernel_sigma=None the amplitude self-adapts to the exposure.

    It should land near the marginal scatter of the per-amp-row offset
    (~0.008 for the planted frame), sit well above the per-row sampling
    error, and still recover the smooth offset on clean rows.
    """
    rng = np.random.default_rng(11)
    data, truth = _planted_frame(rng)
    mask = np.zeros(SHAPE, bool)
    horizontal, _, ks_eff = gp_amprow_offsets(data, mask, rho=80.0)
    # Self-adapted to the true 1/f marginal amplitude (not the 0.02 noise,
    # not a frozen absolute) — within a factor of ~2 of the planted ~0.008.
    assert 0.004 < ks_eff < 0.02
    # Still recovers the offset on clean rows.
    clean = np.arange(200, 400)
    for amp in AMP_DC:
        assert _amp_rms(horizontal, truth, amp, clean) < 0.012
    # Frozen O(1) factor scales it linearly.
    _, _, ks_2x = gp_amprow_offsets(data, mask, rho=80.0,
                                    kernel_sigma_factor=2.0)
    assert abs(ks_2x - 2.0 * ks_eff) < 1e-6 * max(ks_2x, 1.0)


def test_gp_interpolates_across_source_gap_better_than_median():
    rng = np.random.default_rng(2)
    data, truth = _planted_frame(rng)
    mask = np.zeros(SHAPE, bool)
    # Bright extended source filling amp B over a ~100-row band, masked
    # with a margin (as the real source mask would be).
    _, _, c0, c1 = NIR_AMPS['B']['data']
    data[900:1000, c0 + 80:c0 + 380] += 5.0
    mask[880:1020, c0 + 60:c0 + 400] = True

    h_gp, _, _ = gp_amprow_offsets(data, mask, rho=80.0, kernel_sigma=0.02)
    h_med, _ = _median_amprow_offsets(data, mask, 3, 0.1, 0.20)

    gap = np.arange(900, 1000)
    rms_gp = _amp_rms(h_gp, truth, 'B', gap)
    rms_med = _amp_rms(h_med, truth, 'B', gap)
    # GP interpolates from the same amp's clean rows above/below; the median
    # path falls back to the cross-amp full-row median (wrong quantity).
    assert rms_gp < rms_med
    assert rms_gp < 0.01
    # Neighbouring clean amps unaffected.
    clean = np.arange(200, 400)
    assert _amp_rms(h_gp, truth, 'A', clean) < 0.01


def test_gp_flags_wide_anchorless_gap():
    rng = np.random.default_rng(3)
    data, truth = _planted_frame(rng)
    mask = np.zeros(SHAPE, bool)
    _, _, c0, c1 = NIR_AMPS['C']['data']
    # Mask a very wide band — far wider than rho — so the interior rows have
    # no anchor within a length scale: the GP must revert toward DC with
    # inflated posterior variance and flag those rows weak.
    mask[400:1600, c0:c1] = True
    _, diag, _ = gp_amprow_offsets(data, mask, rho=60.0, kernel_sigma=0.02,
                                   weak_frac=0.5)
    # diag format: 'C:anchors/weak/maxσ'
    c_entry = [d for d in diag if d.startswith('C:')][0]
    n_weak = int(c_entry.split(':')[1].split('/')[1])
    assert n_weak > 0


def test_gp_full_frame_output_including_reference_rows():
    rng = np.random.default_rng(4)
    data, _ = _planted_frame(rng)
    mask = np.zeros(SHAPE, bool)
    horizontal, _, _ = gp_amprow_offsets(data, mask, rho=80.0, kernel_sigma=0.02)
    assert horizontal.shape == SHAPE
    # Reference-border rows (0-3, 2044-2047) get an extrapolated, finite
    # offset even though they're excluded from the fit.
    _, _, c0, _ = NIR_AMPS['A']['data']
    assert np.isfinite(horizontal[0, c0])
    assert np.isfinite(horizontal[-1, c0])


def test_median_estimator_path_unchanged():
    rng = np.random.default_rng(5)
    data, _ = _planted_frame(rng)
    mask = np.zeros(SHAPE, bool)
    h1, v1, amp1, diag1 = fit_residual_striping(data, mask, 3)
    h2, amp2 = _median_amprow_offsets(data, mask, 3, 0.1, 0.20)
    np.testing.assert_array_equal(h1, h2)
    assert amp1 == amp2
    assert diag1 == {}  # median path carries no extra diagnostics
    # Default estimator is 'median', ampcounts in legacy 'A-N' format.
    assert all('-' in a for a in amp1)


def test_gp_params_contract():
    rng = np.random.default_rng(6)
    data, _ = _planted_frame(rng)
    mask = np.zeros(SHAPE, bool)
    # rho is the only required gp_param now (kernel_sigma self-adapts).
    with pytest.raises(ValueError, match='rho'):
        fit_residual_striping(data, mask, 3, estimator='gp')
    with pytest.raises(ValueError, match='rho'):
        fit_residual_striping(data, mask, 3, estimator='gp',
                              gp_params={'kernel_sigma': 0.02})
    with pytest.raises(ValueError, match='unknown estimator'):
        fit_residual_striping(data, mask, 3, estimator='nope')
    # rho alone is sufficient — amplitude self-adapts; returns a 4-tuple with
    # the effective amplitude in the diag dict.
    h, v, amp, diag = fit_residual_striping(
        data, mask, 3, estimator='gp', gp_params={'rho': 80.0})
    assert h.shape == SHAPE and v.shape == SHAPE and len(amp) == 4
    assert diag['kernel_sigma_eff'] > 0
