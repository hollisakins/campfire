"""Tests for the intra-visit detector-fixed artifact step's numerical core.

Synthesizes dither stacks with a known detector-fixed artifact plus bright
sources that *move* between dithers (as real sky sources do under a dither
pattern) and verifies that the pure functions:

1. Count distinct dither positions and the minimum pairwise throw correctly,
   collapsing co-located repeat exposures.
2. Recover the detector-fixed artifact while rejecting the moving sources —
   the whole premise of the method — using dither rejection alone and with a
   source mask.
3. Isolate a compact artifact from a smooth large-scale background.
4. Leave pixels undefined where too few dithers contribute.
5. Fit the per-exposure template amplitude, returning ~0 for an absent
   artifact and clamping to ``[0, c_max]``.

The full step (ImageModel I/O + tiered source mask + write-back) is exercised
by the A/B pipeline runs on rj0911, per the experiments workflow.
"""

import numpy as np
import pytest

from campfire_pipeline.nircam.steps.artifact import (
    artifact_dq_mask,
    build_artifact_template,
    build_sky_model,
    despike,
    dither_positions_and_spacing,
    fit_template_scale,
    inpaint_nan,
    stationary_pixels,
)

LW_SCALE = 0.0630  # arcsec/pix


def _make_stack(artifact, src_positions, src_flux=50.0, noise=0.1, seed=0):
    """Stack of frames sharing ``artifact``; a bright source at each position."""
    rng = np.random.default_rng(seed)
    h, w = artifact.shape
    frames, masks = [], []
    for (sy, sx) in src_positions:
        f = rng.normal(0.0, noise, (h, w)) + artifact
        f[sy - 3:sy + 3, sx - 3:sx + 3] += src_flux
        frames.append(f)
        masks.append(np.zeros((h, w), dtype=bool))
    return frames, masks


# --- dither geometry --------------------------------------------------------

def test_box_dither_geometry():
    # rj0911 INTRAMODULEBOX F444W commanded offsets (arcsec).
    xo = np.array([-92.9, -93.1, -87.1, -86.9])
    yo = np.array([-3.1, 2.9, 3.1, -2.9])
    npos, sp = dither_positions_and_spacing(xo, yo, LW_SCALE)
    assert npos == 4
    # ~6" box at 0.063"/px -> ~95 px between adjacent corners.
    assert 80.0 < sp < 160.0


def test_colocated_repeats_collapse():
    # Two physical positions, each visited twice (NEXP=2 style).
    npos, sp = dither_positions_and_spacing(
        [0.0, 0.001, 5.0, 5.001], [0.0, 0.0, 0.0, 0.0], LW_SCALE)
    assert npos == 2
    assert sp == pytest.approx(5.0 / LW_SCALE, rel=1e-3)


def test_single_position_infinite_spacing():
    npos, sp = dither_positions_and_spacing([1.0], [2.0], LW_SCALE)
    assert npos == 1
    assert np.isinf(sp)


# --- template construction --------------------------------------------------

def _blob(h=200, w=200, val=5.0):
    a = np.zeros((h, w))
    a[90:110, 90:110] = val
    return a


def test_recovers_artifact_rejects_moving_sources():
    artifact = _blob(val=5.0)
    # Source in a different corner each dither.
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)])
    tmpl, nc, peds, diag = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=0.0,
        min_contributing=2)
    # Artifact core recovered.
    assert tmpl[95:105, 95:105].mean() == pytest.approx(5.0, abs=0.5)
    # Moving sources rejected — no residual where any single source sat.
    for (sy, sx) in [(20, 20), (20, 170), (170, 20), (170, 170)]:
        assert np.abs(tmpl[sy - 2:sy + 2, sx - 2:sx + 2]).mean() < 0.5
    assert diag['undefined_frac'] == 0.0


def test_isolation_removes_smooth_background_keeps_blob():
    h = w = 200
    yy, xx = np.mgrid[0:h, 0:w]
    smooth = 0.01 * xx + 0.008 * yy          # large-scale gradient (detector-fixed)
    artifact = _blob(val=5.0) + smooth
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)])
    tmpl_iso, _, _, _ = build_artifact_template(
        frames, masks, isolate_background=True, isolate_box=64,
        isolate_filter=3, smooth_sigma=0.0, min_contributing=2)
    # Compact blob survives; the smooth ramp is largely removed away from it.
    assert tmpl_iso[95:105, 95:105].mean() > 3.0
    corner = tmpl_iso[160:180, 160:180].mean()
    assert abs(corner) < 1.0


def test_undefined_accounting_and_zero_fill_path():
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)])
    # Mask a clean patch in 3 of 4 frames -> only 1 contributes there.
    for m in masks[:3]:
        m[50:60, 50:60] = True
    # interpolate_gaps=False exercises the legacy zero-fill path.
    tmpl, nc, peds, diag = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=0.0,
        min_contributing=2, interpolate_gaps=False)
    assert nc[55, 55] == 1                       # coverage tracked correctly
    assert tmpl[55, 55] == 0.0                   # zero-filled (no correction)
    assert diag['undefined_frac'] > 0.0


def test_template_smoothing_reduces_noise():
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)], noise=0.3)
    raw, _, _, _ = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=0.0)
    sm, _, _, _ = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=2.0)
    # Off-artifact noise is suppressed by smoothing.
    assert np.std(sm[150:190, 10:50]) < np.std(raw[150:190, 10:50])


# --- per-exposure scale fit -------------------------------------------------

def test_scale_fit_recovers_amplitude():
    rng = np.random.default_rng(1)
    tmpl = _blob(val=1.0)
    data = 1.7 * tmpl + rng.normal(0, 0.05, tmpl.shape)
    c = fit_template_scale(data, tmpl, np.zeros_like(tmpl, bool), c_max=3.0)
    assert c == pytest.approx(1.7, abs=0.1)


def test_scale_fit_zero_when_no_artifact():
    rng = np.random.default_rng(2)
    tmpl = _blob(val=1.0)
    data = rng.normal(0, 0.05, tmpl.shape)    # pure noise, no artifact
    c = fit_template_scale(data, tmpl, np.zeros_like(tmpl, bool), c_max=3.0)
    assert c == pytest.approx(0.0, abs=0.05)


def test_scale_fit_clamps_and_never_negative():
    tmpl = _blob(val=1.0)
    # Anti-correlated data would want c<0; clamp keeps it at 0 (never adds).
    c_neg = fit_template_scale(-2.0 * tmpl, tmpl,
                               np.zeros_like(tmpl, bool), c_max=3.0)
    assert c_neg == 0.0
    # Beyond the grid ceiling clamps to c_max.
    c_hi = fit_template_scale(10.0 * tmpl, tmpl,
                              np.zeros_like(tmpl, bool), c_max=3.0)
    assert c_hi == pytest.approx(3.0, abs=1e-6)


def test_scale_fit_empty_support_returns_zero():
    tmpl = np.zeros((200, 200))               # no support at all
    data = np.ones((200, 200))
    c = fit_template_scale(data, tmpl, np.zeros_like(tmpl, bool))
    assert c == 0.0


# --- stationarity-aware masking ---------------------------------------------

def test_artifact_dq_mask_saturated_yes_jump_persistence_no():
    from jwst.datamodels import dqflags
    dq = np.zeros((2, 4), dtype=np.uint32)
    dq[0, 0] = dqflags.pixel['DO_NOT_USE']
    dq[0, 1] = dqflags.pixel['SATURATED']
    dq[0, 2] = dqflags.pixel['JUMP_DET']
    dq[0, 3] = dqflags.pixel['PERSISTENCE']
    m = artifact_dq_mask(dq)
    assert m[0, 0] and m[0, 1]                  # DO_NOT_USE + SATURATED masked
    assert not m[0, 2] and not m[0, 3]          # JUMP_DET, PERSISTENCE kept
    # A pixel carrying SATURATED among other (kept) bits is still masked.
    combo = dqflags.pixel['SATURATED'] | dqflags.pixel['JUMP_DET']
    assert artifact_dq_mask(np.array([[combo]], dtype=np.uint32))[0, 0]


def test_stationary_pixels_keeps_artifact_drops_moving_sources():
    h = w = 100
    # Artifact flagged at the same pixels in all 4 frames; a source flagged at
    # a *different* corner each frame (it moves).
    segs = []
    corners = [(10, 10), (10, 80), (80, 10), (80, 80)]
    for (sy, sx) in corners:
        s = np.zeros((h, w), dtype=bool)
        s[45:55, 45:55] = True            # detector-fixed artifact (all frames)
        s[sy:sy + 5, sx:sx + 5] = True    # moving source (this frame only)
        segs.append(s)
    stat = stationary_pixels(segs, k_unmask=len(segs))
    # Artifact unmasked (stationary); moving sources are not.
    assert stat[45:55, 45:55].all()
    for (sy, sx) in corners:
        assert not stat[sy:sy + 5, sx:sx + 5].any()


def test_stationary_pixels_threshold():
    h = w = 20
    s1 = np.zeros((h, w), bool); s1[5:8, 5:8] = True
    s2 = np.zeros((h, w), bool); s2[5:8, 5:8] = True
    s3 = np.zeros((h, w), bool)           # absent in the third frame
    # k=3 -> the patch (present in 2/3) is NOT stationary; k=2 -> it is.
    assert not stationary_pixels([s1, s2, s3], k_unmask=3)[6, 6]
    assert stationary_pixels([s1, s2, s3], k_unmask=2)[6, 6]


# --- gap interpolation ------------------------------------------------------

# --- sky-model source rejection ---------------------------------------------

def test_build_sky_model_keeps_artifact_removes_sky_source():
    h = w = 120
    artifact = np.zeros((h, w))
    artifact[55:65, 55:65] = 4.0                 # detector-fixed (all frames)
    # shifts[i] aligns frame i onto frame 0; a sky-fixed source sits at
    # detector (20-dy, 20-dx) in frame i so the shifts co-locate it.
    shifts = [(0, 0), (0, 30), (30, 0), (30, 30)]
    frames = []
    for dy, dx in shifts:
        f = artifact.copy()
        sy, sx = 20 - dy, 20 - dx
        f[sy - 3:sy + 3, sx - 3:sx + 3] += 30.0  # bright sky-fixed source
        frames.append(f)

    sky = build_sky_model(frames, shifts)
    resid0 = frames[0] - sky[0]
    # artifact (detector-fixed) survives in the residual...
    assert resid0[57:63, 57:63].mean() == pytest.approx(4.0, abs=0.6)
    # ...the sky-fixed source is removed at its frame-0 detector position.
    assert abs(resid0[17:23, 17:23].mean()) < 1.0


def test_despike_removes_hot_pixel_on_top_of_artifact():
    # The case the segment filter failed: a hot pixel sitting ON the smooth
    # artifact. Despike must remove it and leave the artifact intact.
    rng = np.random.default_rng(3)
    h = w = 120
    img = np.zeros((h, w))
    img[40:80, 40:80] = 0.5                    # smooth extended artifact
    img += rng.normal(0, 0.01, (h, w))
    img[60, 60] += 5.0                         # hot pixel on the artifact
    img[20, 100] += 5.0                        # hot pixel on blank sky
    out, n = despike(img, size=5, nsigma=5.0)
    assert n >= 2
    assert out[60, 60] == pytest.approx(0.5, abs=0.1)   # spike -> artifact level
    assert out[20, 100] == pytest.approx(0.0, abs=0.1)  # spike -> blank level
    # the smooth artifact away from the spike is untouched
    assert out[50, 50] == pytest.approx(img[50, 50], abs=1e-6)


def test_despike_preserves_clean_image():
    rng = np.random.default_rng(4)
    img = 0.3 + rng.normal(0, 0.01, (100, 100))     # smooth + noise, no spikes
    out, n = despike(img, size=5, nsigma=5.0)
    assert n == 0
    assert np.allclose(out, img)


def test_despike_dilation_removes_subthreshold_ipc_wing():
    # A bright hot pixel with a sub-threshold IPC wing one px over. The wing is
    # below the per-pixel cut on its own, so only the dilation reaches it — which
    # matters because smoothing would otherwise spread the leftover wing.
    rng = np.random.default_rng(8)
    img = rng.normal(0, 0.05, (120, 120))
    img[60, 60] += 5.0            # bright hot pixel
    img[60, 61] += 0.15          # IPC wing, below ~4*0.05 = 0.2 threshold
    out0, _ = despike(img, size=5, nsigma=4.0, maxiters=1, dilate=0)
    out1, _ = despike(img, size=5, nsigma=4.0, maxiters=1, dilate=1)
    assert abs(out1[60, 60]) < 0.1                       # peak removed
    assert abs(out1[60, 61]) < 0.1                       # wing removed (dilation)
    assert out0[60, 61] == pytest.approx(0.15, abs=0.08)  # wing survives w/o it


def test_despike_lower_nsigma_catches_more():
    # A population of spikes spanning 4.2-6 sigma: the lower threshold catches
    # strictly more of the marginal ones (single pass isolates the threshold).
    rng = np.random.default_rng(9)
    img = rng.normal(0, 0.02, (200, 200))
    ys = rng.integers(10, 190, 50)
    xs = rng.integers(10, 190, 50)
    amps = np.linspace(4.2, 6.0, 50) * 0.02
    for y, x, a in zip(ys, xs, amps):
        img[y, x] += a
    _, n4 = despike(img, size=5, nsigma=4.0, maxiters=1, dilate=0)
    _, n6 = despike(img, size=5, nsigma=6.0, maxiters=1, dilate=0)
    assert n4 > n6


def test_build_template_despikes_hot_pixels():
    # End-to-end: a detector-fixed hot pixel (same in all dithers) must not
    # survive into the template as a blob.
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)], noise=0.05)
    for f in frames:                            # detector-fixed hot pixel
        f[150, 50] += 30.0
    tmpl, _, _, diag = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=2.0,
        despike_nsigma=5.0)
    assert diag['n_despiked'] >= 1
    assert abs(tmpl[150, 50]) < 0.5             # hot pixel removed from template
    assert tmpl[95:105, 95:105].mean() == pytest.approx(5.0, abs=0.6)  # artifact kept


def test_source_rejection_default_and_domain():
    from campfire_pipeline.config import load_config
    from campfire_pipeline.nircam.steps.artifact import _VALID_SOURCE_REJECTION
    cfg = load_config()['nircam']['artifact']
    assert cfg['source_rejection'] == 'stationary'      # default = current path
    assert set(_VALID_SOURCE_REJECTION) == {'stationary', 'skymodel'}


def test_inpaint_fills_gaps_continuously():
    arr = np.ones((50, 50)) * 3.0
    arr[20:30, 20:30] = np.nan           # interior gap surrounded by 3.0
    filled = inpaint_nan(arr)
    assert np.isfinite(filled).all()
    # Uniform surroundings -> gap fills with that level (no zero hole).
    assert filled[25, 25] == pytest.approx(3.0, abs=1e-3)


def test_inpaint_smooth_not_blocky():
    # A gap inside a smooth ramp should fill smoothly (monotone across the gap),
    # not with the blocky nearest-neighbour plateaus the EDT fill produced.
    yy, xx = np.mgrid[0:60, 0:60]
    arr = 0.1 * xx.astype(float)
    arr[25:35, 25:35] = np.nan
    filled = inpaint_nan(arr)
    assert np.isfinite(filled).all()
    row = filled[30, 25:35]
    # values increase left->right across the filled gap (no flat plateau / step)
    assert np.all(np.diff(row) > 0)
    assert abs(filled[30, 30] - 0.1 * 30) < 0.3   # tracks the underlying ramp


def test_inpaint_fills_near_local_level():
    # Gap inside the artifact (value 5) fills close to 5, not 0 — smooth fill
    # blends slightly toward a nearby boundary but stays artifact-side.
    arr = np.zeros((40, 40))
    arr[:, :20] = 5.0
    arr[5:15, 2:12] = np.nan
    filled = inpaint_nan(arr)
    assert np.isfinite(filled).all()
    assert filled[10, 7] > 4.0           # clearly the artifact level, not 0


def test_zero_fill_vs_interpolate_differ_at_gap():
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)])
    # Force a gap inside the artifact: mask its core in 3 of 4 frames.
    for m in masks[:3]:
        m[95:105, 95:105] = True
    tmpl_zero, _, _, _ = build_artifact_template(
        frames, masks, interpolate_gaps=False, isolate_background=False,
        smooth_sigma=0.0, min_contributing=2)
    tmpl_interp, _, _, _ = build_artifact_template(
        frames, masks, interpolate_gaps=True, isolate_background=False,
        smooth_sigma=0.0, min_contributing=2)
    # Zero-fill leaves a hole in the blob core; interpolation fills it.
    assert tmpl_zero[100, 100] == pytest.approx(0.0, abs=0.3)
    assert tmpl_interp[100, 100] == pytest.approx(5.0, abs=0.7)


# --- coverage-confidence gate -----------------------------------------------
# The template is returned UNGATED; build exposes the weight map in
# diag['coverage_confidence'] and the step applies it only to the subtraction
# (so the per-exposure amplitude fit stays keyed on the full template).

def test_coverage_confidence_downweights_low_coverage():
    # A blob pixel constrained by only 2 dithers (no cross-dither rejection)
    # gets weight ~0.5; a full-coverage pixel gets 1.0. Applying the weight
    # down-weights only the low-coverage core.
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)], noise=0.01)
    for m in masks[:2]:                       # mask part of the core in 2/4
        m[95:105, 95:105] = True
    ungated, _, _, d_off = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=0.0,
        min_contributing=2, coverage_weight=False)
    tmpl, nc, _, d_on = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=0.0,
        min_contributing=2, coverage_weight=True, coverage_smooth_sigma=0.0)
    assert nc[100, 100] == 2 and nc[92, 92] == 4
    assert d_off['coverage_confidence'] is None
    w = d_on['coverage_confidence']
    assert w[100, 100] == pytest.approx(0.5, abs=0.05)   # n=2 -> half
    assert w[92, 92] == pytest.approx(1.0, abs=1e-6)      # n=4 -> full
    assert np.allclose(tmpl, ungated)          # template returned ungated
    applied = tmpl * w
    assert applied[100, 100] == pytest.approx(0.5 * tmpl[100, 100], rel=0.1)
    assert applied[92, 92] == pytest.approx(tmpl[92, 92], rel=1e-3)


def test_coverage_confidence_zeros_inpainted_gap():
    # Pixels below min_contributing are inpainted for continuity, but the weight
    # there is 0, so applying it subtracts nothing (the over-subtraction fix).
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)], noise=0.01)
    for m in masks[:3]:                       # mask the core in 3/4 -> n=1
        m[95:105, 95:105] = True
    tmpl, nc, _, diag = build_artifact_template(
        frames, masks, isolate_background=False, smooth_sigma=0.0,
        min_contributing=2, interpolate_gaps=True, coverage_weight=True,
        coverage_smooth_sigma=0.0)
    assert nc[100, 100] == 1
    assert tmpl[100, 100] > 3.0               # inpaint fabricates ~5
    w = diag['coverage_confidence']
    assert w[100, 100] == 0.0                  # but weight is 0 there
    assert abs((tmpl * w)[100, 100]) < 0.2     # so nothing is subtracted


def test_coverage_confidence_unit_at_full_coverage():
    # Full coverage everywhere -> weight all ones; template identical to the
    # no-gate build. Noiseless so sigma-clip rejects nothing (n >= 3 everywhere).
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)], noise=0.0)
    a, _, _, da = build_artifact_template(frames, masks, smooth_sigma=2.0,
                                          coverage_weight=False)
    b, _, _, db = build_artifact_template(frames, masks, smooth_sigma=2.0,
                                          coverage_weight=True)
    assert da['coverage_confidence'] is None
    assert np.all(db['coverage_confidence'] == 1.0)
    assert np.allclose(a, b)                    # template never gated in build


def test_coverage_gate_rejects_bad_ramp():
    artifact = _blob(val=5.0)
    frames, masks = _make_stack(artifact, [(20, 20), (20, 170),
                                           (170, 20), (170, 170)])
    with pytest.raises(ValueError):
        build_artifact_template(frames, masks, coverage_weight=True,
                                coverage_n_lo=3, coverage_n_hi=3)
