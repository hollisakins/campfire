"""Tests for the SubtractBackground negativity guard (bg_guard).

Fast synthetic scenes (small boxes, scaled-down guard params). The three
behaviors under test mirror the A2744 validation in
experiments/bkg_nonneg:

1. Near-no-op on a compliant field (the gate keeps blank fields intact).
2. A coherent oversubtraction trough (masked region, elevated model) is
   corrected back to statistical insignificance.
3. The ceiling caps a background mesh that sits above the observed flux
   floor, including where the fit mask hides the violation.
"""

import numpy as np
import pytest
from astropy import stats as astrostats
from scipy.ndimage import gaussian_filter

from campfire_pipeline.nircam.bkgsub import SubtractBackground

SIG = 0.01
RNG = np.random.default_rng(11)


def _scene(n=512, sky=0.005, nsrc=40):
    """Smooth sky gradient + correlated noise + compact sources."""
    yy, xx = np.mgrid[0:n, 0:n] / n
    skymap = sky * (1 + 0.4 * xx - 0.2 * yy)
    noise = gaussian_filter(RNG.normal(size=(n, n)), 1.2, mode="wrap")
    noise *= SIG / noise.std()
    src = np.zeros((n, n))
    for _ in range(nsrc):
        y0, x0 = RNG.uniform(15, n - 15, 2)
        amp = RNG.lognormal(np.log(8 * SIG), 0.7)
        s = RNG.uniform(1.5, 3.0)
        y1, y2 = int(y0) - 12, int(y0) + 12
        x1, x2 = int(x0) - 12, int(x0) + 12
        dy = np.arange(y1, y2)[:, None] - y0
        dx = np.arange(x1, x2)[None, :] - x0
        src[y1:y2, x1:x2] += amp * np.exp(-(dy**2 + dx**2) / (2 * s * s))
    sci = (skymap + noise + src).astype(np.float32)
    err = np.full_like(sci, SIG)
    return sci, err, skymap


def _guard_sb(**over):
    kw = dict(
        ring_radius_in=40, ring_width=4, ring_downsample=2,
        tier_kernel_size=[15, 5, 2], tier_npixels=[10, 3, 1],
        tier_nsigma=[1.5, 1.5, 1.5], tier_dilate_size=[15, 9, 7],
        bg_box_size=10, bg_filter_size=5,
        bg_guard=True,
        guard_ceiling_boxes=[16, 32, 64],
        guard_trough_sigmas=[3.0, 8.0],
    )
    kw.update(over)
    return SubtractBackground(**kw)


def _smoothed_min_signif(resid, exclude, sigma=3.0):
    sm = SubtractBackground._smooth_masked(resid, exclude, sigma)
    ok = np.isfinite(sm) & ~exclude
    rms = astrostats.biweight_scale(sm[ok])
    return float(np.nanmin(np.where(ok, sm, np.nan))) / rms


def test_guard_near_noop_on_compliant_field():
    sci, err, _ = _scene()
    sb = _guard_sb()
    mask, bitmask = sb.mask_from_arrays(sci, err)
    bkg = sb.estimate_background(sci, mask)
    off = (bitmask & 1) != 0
    guarded = sb.apply_negativity_guard(sci, bkg, mask, off)
    d = guarded - bkg.background
    # corrections only ever lower the map, by at most a small ceiling slack
    assert np.median(d) == pytest.approx(0.0, abs=1e-5)
    assert np.percentile(d, 99) <= 1e-4
    # materially a no-op: re-interpolation ripple from a handful of capped
    # mesh cells is ~1e-11..1e-7, so bound the amplitude, not exact zeros
    assert float((np.abs(d) > 0.2 * SIG).mean()) < 0.01


def test_guard_corrects_masked_oversubtraction_trough():
    sci, err, _ = _scene()
    sb = _guard_sb()
    mask, bitmask = sb.mask_from_arrays(sci, err)
    off = (bitmask & 1) != 0

    # carve a coherent trough: remove a smooth blob from the image and mask
    # its footprint for the fit, so the fitted model rides above the local
    # flux floor there (the wing-oversubtraction geometry)
    n = sci.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    blob = 4 * SIG * np.exp(-(((yy - 250) ** 2 + (xx - 250) ** 2)
                              / (2 * 25.0 ** 2)))
    sci_t = sci - blob.astype(np.float32)
    mask_t = mask | (blob > 0.4 * SIG)

    bkg = sb.estimate_background(sci_t, mask_t)
    resid_before = sci_t - bkg.background
    guarded = sb.apply_negativity_guard(sci_t, bkg, mask_t, off)
    resid_after = sci_t - guarded

    excl = np.zeros_like(mask)
    before = _smoothed_min_signif(resid_before, excl)
    after = _smoothed_min_signif(resid_after, excl)
    assert before < -5          # the injected trough is highly significant
    # guard lifts it back toward the -t floor; a residual core smaller than
    # the gate's npixels can keep the minimum slightly below it
    assert after > before + 1.5
    assert after > -4.2
    # and the correction is local: the far field sees at most the same
    # sparse small-area firing the compliant-field test allows
    far = np.hypot(yy - 250, xx - 250) > 120
    d = guarded - bkg.background
    assert float((np.abs(d[far]) > 0.2 * SIG).mean()) < 0.01


def test_ceiling_caps_elevated_background():
    sci, err, _ = _scene()
    sb = _guard_sb()
    mask, bitmask = sb.mask_from_arrays(sci, err)
    bkg = sb.estimate_background(sci, mask)

    # elevate the model over a region and verify the multiscale ceiling
    # (built only from sci) vetoes it
    n = sci.shape[0]
    yy, xx = np.mgrid[0:n, 0:n]
    bump_region = np.hypot(yy - 150, xx - 350) < 60
    elevated = bkg.background + np.where(bump_region, 5 * SIG, 0.0)

    sigma_map = sb._guard_sigma_map(sci - bkg.background, ~mask)
    ceiling = sb._multiscale_ceiling(sci, np.zeros_like(mask), elevated,
                                     mask, sigma_map)
    viol = elevated > ceiling
    assert viol[bump_region].mean() > 0.9    # elevation caught
    # outside the bump the only violations are genuine caps of the raw
    # fit near sources (the guard working as intended) — keep them bounded
    assert viol[~bump_region].mean() < 0.10


def test_guard_sigma_map_follows_wht():
    sci, err, _ = _scene(n=256)
    sb = _guard_sb()
    resid = RNG.normal(scale=SIG, size=sci.shape)
    wht = np.full(sci.shape, 4000.0)
    wht[:, 128:] = 1000.0                    # 2x noisier half
    resid[:, 128:] *= 2.0
    sb.wht = wht.astype(np.float32)
    sig = sb._guard_sigma_map(resid, np.ones_like(sci, dtype=bool))
    ratio = np.median(sig[:, 128:]) / np.median(sig[:, :128])
    assert ratio == pytest.approx(2.0, rel=1e-3)


@pytest.mark.parametrize("interp_name", ["zoom", "IDW"])
def test_cap_mesh_preserves_configured_interpolator(interp_name):
    """An uncapping ceiling must reproduce the fitted map for BOTH
    interpolators — _cap_mesh re-runs the fit's own interpolation, so
    enabling the guard with bg_interpolator='IDW' cannot silently swap the
    whole background for a zoom-interpolated one."""
    sci, err, _ = _scene(n=256)
    sb = _guard_sb(bg_interpolator=interp_name)
    mask, _ = sb.mask_from_arrays(sci, err)
    bkg = sb.estimate_background(sci, mask)
    ref = bkg.background
    uncapping = np.full(sci.shape, np.inf)
    remade = sb._cap_mesh(bkg, uncapping, sci.shape)
    # identical interpolation of an identical mesh; tolerance only for the
    # float32-mesh vs float64-capped-mesh arithmetic
    assert np.allclose(remade, ref, rtol=1e-5, atol=1e-6 * SIG)


def test_trough_correction_continuous_across_excluded_footprint():
    """The trough correction must not hard-zero over excluded source
    footprints: a source inside a fired trough gets the same ambient
    lowering as its surroundings (no per-source pedestal/seam)."""
    n = 256
    yy, xx = np.mgrid[0:n, 0:n]
    noise = gaussian_filter(RNG.normal(size=(n, n)), 1.2, mode="wrap")
    noise *= SIG / noise.std()
    trough = 6 * SIG * np.exp(-(((yy - 128) ** 2 + (xx - 128) ** 2)
                                / (2 * 30.0 ** 2)))
    sci = (noise - trough).astype(np.float32)
    sigma_map = np.full((n, n), SIG, dtype=np.float32)
    exclude = np.hypot(yy - 128, xx - 128) < 8      # compact source footprint
    sb = _guard_sb()

    m = sb._gated_trough_pass(sci, np.zeros_like(sci), exclude, sigma_map)

    r_in = np.hypot(yy - 128, xx - 128)
    annulus = (r_in >= 9) & (r_in < 14)
    corr_inside = float(np.mean(m[exclude]))
    corr_annulus = float(np.mean(m[annulus]))
    assert corr_annulus < -2 * SIG                  # the trough fired
    # continuity: interior lowered like its immediate surroundings, not
    # left at ~0 on a pedestal
    assert corr_inside < 0.5 * corr_annulus
    assert abs(corr_inside - corr_annulus) < 1.5 * SIG


def test_compute_applies_guard(tmp_path):
    """End-to-end through compute(): guard on vs off differ only where the
    guard fired, and the guard never raises the background."""
    from astropy.io import fits

    sci, err, _ = _scene()
    path = str(tmp_path / "scene.fits")
    fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(sci, name="SCI"),
        fits.ImageHDU(err, name="ERR"),
    ]).writeto(path)

    sub_on, _, _ = _guard_sb().compute(path)
    sub_off, _, _ = _guard_sb(bg_guard=False).compute(path)
    d = sub_on - sub_off
    # guard only ever adds flux back, up to the (non-monotone) zoom spline's
    # re-interpolation ripple around capped mesh cells — bound it well below
    # the noise rather than at exact zero
    assert np.all(d > -0.05 * SIG)
    assert np.median(d) == pytest.approx(0.0, abs=1e-5)
