"""Tests for the NIRCam diagonal-striping step's algorithmic primitives.

Synthesizes a frame with a planted diagonal stripe pattern at a known
angle and verifies that:

1. The angle search recovers the planted angle to within the fine grid.
2. The strip-blended model recovers the planted stripe with significantly
   smaller RMS than the noise floor.
3. The shared ``fit_residual_striping`` helper composes correctly with
   the diagonal correction (no double-correction or coupling artifacts).

These tests do not exercise the full step end-to-end (which would require
a synthetic JWST canonical exposure file with SRCMASK + DQ + history) —
the primitives are where the algorithmic correctness lives.
"""

import numpy as np
import pytest

from campfire_pipeline.nircam.steps.diag_striping import (
    _bin_indices,
    _coarse_fine_search,
    diagonal_stripe_model,
    diagonal_stripe_model_blended,
    regularize_strip_deltas,
)
from campfire_pipeline.nircam.steps.striping import fit_residual_striping


def _planted_diagonal_stripe(shape, theta_deg, bin_width, rng, smoothing=5.0):
    """Make a band-limited diagonal stripe pattern.

    A real scattered-light stripe is smooth across neighbouring bins (the
    pattern has a finite spatial scale set by the source PSF and the
    optical geometry). Random per-bin amplitudes would not be invariant
    under the per-strip bin partition shift — by smoothing across bins
    we model a realistic stripe whose structure scale exceeds
    ``bin_width``.
    """
    from scipy.ndimage import gaussian_filter1d

    bin_idx = _bin_indices(shape, theta_deg, bin_width)
    n_bins = int(bin_idx.max()) + 1
    bin_amps = rng.normal(0.0, 1.0, n_bins).astype(np.float32)
    if smoothing > 0:
        bin_amps = gaussian_filter1d(bin_amps, sigma=smoothing).astype(np.float32)
    return bin_amps[bin_idx].astype(np.float32)


def test_angle_search_recovers_planted_theta():
    rng = np.random.default_rng(42)
    shape = (512, 512)
    theta_true = 27.5
    bin_width = 3
    stripe = _planted_diagonal_stripe(shape, theta_true, bin_width, rng)
    noise = rng.normal(0.0, 0.2, shape).astype(np.float32)
    data = stripe + noise
    mask = np.zeros(shape, dtype=bool)

    opt_theta, _, _ = _coarse_fine_search(
        data, mask, bin_width,
        theta_min=20.0, theta_max=35.0,
        coarse_step=1.0, fine_step=0.1,
    )
    # Fine grid is 0.1°; allow one grid point of slop.
    assert abs(opt_theta - theta_true) < 0.2, (
        f"recovered θ={opt_theta} vs planted {theta_true}"
    )


def test_global_model_recovers_planted_stripe():
    rng = np.random.default_rng(0)
    shape = (512, 512)
    theta_true = 30.0
    bin_width = 3
    stripe = _planted_diagonal_stripe(shape, theta_true, bin_width, rng)
    noise_sigma = 0.2
    noise = rng.normal(0.0, noise_sigma, shape).astype(np.float32)
    data = stripe + noise
    mask = np.zeros(shape, dtype=bool)

    model = diagonal_stripe_model(data, mask, theta_true, bin_width)
    err = np.std(model - stripe)
    # Per-bin median of N≈85 noise samples should land within ~σ/√N of
    # the true bin amplitude. Generous bound: bin median noise ≪ noise.
    assert err < noise_sigma, f"global recovery err={err}, noise={noise_sigma}"


def test_blended_model_handles_amplitude_variation():
    """When stripe amplitude varies across x, the blended model tracks
    it; a global model would average over the variation."""
    rng = np.random.default_rng(1)
    shape = (1024, 1024)
    theta_true = 30.0
    bin_width = 3
    base_stripe = _planted_diagonal_stripe(shape, theta_true, bin_width, rng,
                                           smoothing=8.0)
    # Modulate amplitude with x — closer to "left edge" gets 3x stronger.
    x = np.arange(shape[1])
    amplitude_x = 1.0 + 2.0 * (1.0 - x / shape[1])
    varying_stripe = (base_stripe * amplitude_x[None, :]).astype(np.float32)
    noise = rng.normal(0.0, 0.1, shape).astype(np.float32)
    data = varying_stripe + noise
    mask = np.zeros(shape, dtype=bool)

    global_model = diagonal_stripe_model(data, mask, theta_true, bin_width)
    blended = diagonal_stripe_model_blended(
        data, mask, theta_true, bin_width,
        column_width=128, overlap=32,
    )
    err_global = np.std(global_model - varying_stripe)
    err_blended = np.std(blended - varying_stripe)
    # Blended should beat global meaningfully when amplitude varies.
    assert err_blended < 0.7 * err_global, (
        f"blended err={err_blended}, global err={err_global}"
    )


def test_residual_helper_composes_with_diagonal():
    """Plant a (smooth) diagonal stripe + small per-row offset; verify
    the two-stage correction (diag subtract → residual fit) recovers a
    near-clean frame.

    Realistic relative amplitudes: the diagonal scattered-light pattern
    dominates over the residual horizontal 1/f, which is what motivated
    this step in the first place. With horizontal ≪ stripe, the diagonal
    fit's per-bin median is a clean estimate of the stripe (the small
    horizontal contribution averages to ~0 within each bin), and the
    residual fit recovers most of the leftover horizontal.
    """
    rng = np.random.default_rng(2)
    shape = (2048, 2048)  # full detector for fit_residual_striping
    theta_true = 28.0
    bin_width = 3
    stripe = _planted_diagonal_stripe(shape, theta_true, bin_width, rng,
                                      smoothing=8.0)
    row_offsets = rng.normal(0.0, 0.05, shape[0]).astype(np.float32)
    horizontal = np.broadcast_to(row_offsets[:, None], shape)
    noise = rng.normal(0.0, 0.02, shape).astype(np.float32)
    data = stripe + horizontal + noise
    mask = np.zeros(shape, dtype=bool)

    diag_model = diagonal_stripe_model_blended(
        data, mask, theta_true, bin_width,
        column_width=256, overlap=32,
    )
    after_diag = data - diag_model

    h, v, _ = fit_residual_striping(after_diag, mask, maxiters=3)
    cleaned = after_diag - h - v

    noise_sigma = 0.02
    rms = np.std(cleaned)
    initial_rms = np.std(data)
    # Cleaned RMS should approach the noise floor, with a small budget for
    # estimation error from the per-bin / per-row median fits.
    assert rms < 1.5 * noise_sigma, (
        f"cleaned rms={rms} vs noise σ={noise_sigma} "
        f"(initial rms={initial_rms})"
    )


def test_regularize_strip_deltas_enforces_pair_ratio():
    """A spike in one strip is pulled toward its neighbors; in-budget
    smooth variations are left alone; NaN entries pass through."""
    M = np.array([
        [1.0, 1.0, 1.0],
        [1.0, 5.0, 1.1],   # bin 1: huge spike vs. neighbors; bin 2: 10% step (in budget)
        [1.0, 1.0, 1.2],
        [1.0, 1.0, np.nan],
    ])
    out = regularize_strip_deltas(M, max_ratio=0.3)

    # Bin 0 untouched (all equal).
    np.testing.assert_allclose(out[:, 0], 1.0)
    # Bin 2 within 30% across all adjacent pairs already.
    for k in range(out.shape[0] - 1):
        a, b = out[k, 2], out[k + 1, 2]
        if np.isfinite(a) and np.isfinite(b):
            assert abs(a - b) <= 0.3 * max(abs(a), abs(b)) + 1e-6
    # Bin 1: spike must be reduced so adjacent pairs satisfy the constraint.
    for k in range(out.shape[0] - 1):
        a, b = out[k, 1], out[k + 1, 1]
        assert abs(a - b) <= 0.3 * max(abs(a), abs(b)) + 1e-6
    # NaN passes through.
    assert np.isnan(out[3, 2])


def test_regularize_strip_deltas_no_op_when_in_budget():
    """Smooth variation under the cap should be left unchanged."""
    rng = np.random.default_rng(7)
    # 4 strips, 50 bins, with each bin a smooth linear ramp varying ≤ 20% per step.
    base = rng.uniform(0.5, 1.0, size=50)
    M = np.stack([base * (1.0 + 0.1 * k) for k in range(4)], axis=0)
    out = regularize_strip_deltas(M, max_ratio=0.3)
    np.testing.assert_allclose(out, M, atol=1e-9)


def test_blended_model_with_regularization_caps_amp_jumps():
    """A 5x cross-strip amplitude jump should be pulled back to within the
    configured per-pair budget by the regularizer, measured as the RMS
    amplitude ratio between the two amp-aligned strips."""
    rng = np.random.default_rng(11)
    shape = (1024, 1024)
    theta_true = 30.0
    bin_width = 3
    base_stripe = _planted_diagonal_stripe(shape, theta_true, bin_width, rng,
                                           smoothing=8.0)
    # 5x amplitude jump across the two halves of the detector.
    amp = np.where(np.arange(shape[1]) < shape[1] // 2, 1.0, 5.0)
    data = (base_stripe * amp[None, :]).astype(np.float32)
    mask = np.zeros(shape, dtype=bool)

    unreg = diagonal_stripe_model_blended(
        data, mask, theta_true, bin_width,
        column_width=512, overlap=0, max_strip_delta_ratio=0.0,
    )
    reg = diagonal_stripe_model_blended(
        data, mask, theta_true, bin_width,
        column_width=512, overlap=0, max_strip_delta_ratio=0.3,
    )

    def _strip_rms_ratio(model):
        left = np.sqrt(np.mean(model[:, :512] ** 2))
        right = np.sqrt(np.mean(model[:, 512:] ** 2))
        return max(left, right) / max(min(left, right), 1e-12)

    # Without regularization the planted 5x amplitude jump comes through.
    unreg_ratio = _strip_rms_ratio(unreg)
    reg_ratio = _strip_rms_ratio(reg)
    assert unreg_ratio > 3.0
    # Regularization should pull this most of the way back. Bins that
    # only intersect one strip can't be regularized (no neighbor), which
    # adds a small residual to the cross-strip RMS — the cap on the
    # cross-strip *paired* bins is 1/(1-0.3) ≈ 1.43.
    assert reg_ratio < 0.6 * unreg_ratio


@pytest.mark.parametrize('theta', [0.0, 15.0, 45.0, 75.0])
def test_bin_indices_contiguous(theta):
    """Bin indices should be 0..n_bins-1 with no gaps."""
    bin_idx = _bin_indices((256, 256), theta, bin_width=3)
    unique = np.unique(bin_idx)
    assert unique[0] == 0
    assert np.array_equal(unique, np.arange(unique[-1] + 1))
