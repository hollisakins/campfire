"""The bkgsub performance paths reproduce the straightforward computation.

Two shortcuts in SubtractBackground must not change pixels:

1. The incremental trough pass (``_gated_trough_pass``) recomputes the
   smoothed residual only around each iteration's gated pixels. It must equal,
   bit for bit, the from-scratch loop it replaced (kept below as the
   reference).
2. ``compute()`` works on a padded, grid-aligned crop of the coverage. It must
   give the full-image result: the same mask, and the same pixels up to the
   last-bit ripple of the cubic mesh zoom's prefilter tail.
"""

import numpy as np
import pytest
from astropy import stats as astrostats
from astropy.io import fits
from photutils.segmentation import detect_sources
from scipy.ndimage import gaussian_filter

from campfire_pipeline.nircam.bkgsub import SubtractBackground

SIG = 0.01


def _reference_trough_pass(sb, sci, bkgmap, exclude, sigma_map):
    """The from-scratch trough loop (pre-incremental implementation)."""
    m = bkgmap.copy()
    with np.errstate(invalid="ignore"):
        inv_sigma = np.where(
            np.isfinite(sigma_map) & (sigma_map > 0), 1.0 / sigma_map, np.nan)
    for s in sb.guard_trough_sigmas:
        npixels = max(int(sb.guard_trough_npix * s * s), 30)
        for _ in range(sb.guard_trough_max_iter):
            resid_eq = (sci - m) * inv_sigma
            sm = SubtractBackground._smooth_masked(resid_eq, exclude, s)
            finite = np.isfinite(sm)
            rms = float(astrostats.biweight_scale(sm[finite & ~exclude]))
            if not np.isfinite(rms) or rms <= 0:
                break
            signif = np.where(finite, sm / rms, 0.0)
            seg = detect_sources(-signif, threshold=sb.guard_trough_t,
                                 n_pixels=npixels)
            if seg is None:
                break
            sky_area = np.bincount(seg.data[~exclude].ravel(),
                                   minlength=int(seg.data.max()) + 1)
            keep = sky_area >= npixels
            keep[0] = False
            if not keep.any():
                break
            gate = keep[seg.data]
            corr = np.where(gate, np.minimum(sm + sb.guard_trough_t * rms, 0.0),
                            0.0)
            corr[~np.isfinite(corr)] = 0.0
            corr = corr * np.where(np.isfinite(sigma_map), sigma_map, 0.0)
            m = m + corr
    return m


def _trough_scene(n=384, seed=3):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:n, 0:n]
    noise = gaussian_filter(rng.normal(size=(n, n)), 1.2, mode="wrap")
    noise *= SIG / noise.std()
    troughs = np.zeros((n, n))
    # one trough hugging the image edge, so the windows meet the real border
    for (y0, x0, r, a) in [(120, 140, 30, 6), (300, 60, 18, 4),
                           (40, 360, 22, 5), (250, 280, 45, 3)]:
        troughs += a * SIG * np.exp(-((yy - y0) ** 2 + (xx - x0) ** 2)
                                    / (2 * r * r))
    sci = (noise - troughs).astype(np.float32)
    exclude = np.zeros((n, n), bool)
    for (y0, x0) in [(120, 140), (200, 200), (300, 60), (10, 10)]:
        exclude |= np.hypot(yy - y0, xx - x0) < 7
    sigma_map = np.full((n, n), SIG, dtype=np.float32)
    sigma_map[:, -20:] = np.nan          # no coverage on one side
    sigma_map[150:170, :] *= 1.5         # a depth step
    return sci, exclude, sigma_map


@pytest.mark.parametrize("block", [32, 512])
def test_incremental_trough_matches_recompute(monkeypatch, block):
    sci, exclude, sigma_map = _trough_scene()
    bkg = np.zeros(sci.shape)            # float64, as the guard's maps are
    sb = SubtractBackground(bg_guard=True, guard_trough_sigmas=[3.0, 8.0])
    monkeypatch.setattr(SubtractBackground, "_TROUGH_BLOCK", block)

    ref = _reference_trough_pass(sb, sci, bkg, exclude, sigma_map)
    got = sb._gated_trough_pass(sci, bkg, exclude, sigma_map)

    assert ref.min() < -2 * SIG          # the troughs fired
    assert np.array_equal(got, ref, equal_nan=True)


def _covered_scene(n=1100, box=(520, 450, 400), seed=5):
    """A sky + sources scene covered only inside ``box`` (y0, x0, size);
    NaN SCI / ERR and zero WHT elsewhere, as on an outer mosaic tile."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:n, 0:n] / n
    sky = 0.005 * (1 + 0.4 * xx - 0.2 * yy)
    noise = gaussian_filter(rng.normal(size=(n, n)), 1.2, mode="wrap")
    noise *= SIG / noise.std()
    src = np.zeros((n, n))
    y0, x0, size = box
    for _ in range(60):
        cy, cx = rng.uniform(15, size - 15, 2) + (y0, x0)
        amp = rng.lognormal(np.log(8 * SIG), 0.7)
        s = rng.uniform(1.5, 3.0)
        sl = (slice(int(cy) - 12, int(cy) + 12), slice(int(cx) - 12, int(cx) + 12))
        dy = np.arange(sl[0].start, sl[0].stop)[:, None] - cy
        dx = np.arange(sl[1].start, sl[1].stop)[None, :] - cx
        src[sl] += amp * np.exp(-(dy ** 2 + dx ** 2) / (2 * s * s))
    sci = (sky + noise + src).astype(np.float32)
    wht = np.zeros((n, n), np.float32)
    wht[y0:y0 + size, x0:x0 + size] = 1.0 / SIG ** 2
    wht[y0:y0 + size // 3, x0:x0 + size] *= 0.5        # a shallower strip
    covered = wht > 0
    sci[~covered] = np.nan
    err = np.where(covered, SIG, np.nan).astype(np.float32)
    return sci, err, wht


def _crop_sb(**over):
    kw = dict(
        ring_radius_in=20, ring_width=4, ring_downsample=2,
        ring_clip_box_size=20,
        tier_kernel_size=[8, 4, 2], tier_npixels=[10, 3, 1],
        tier_nsigma=[1.5, 1.5, 1.5], tier_dilate_size=[9, 7, 5],
        bg_box_size=10, bg_filter_size=5,
        bg_guard=True, guard_ceiling_boxes=[8, 16],
        guard_trough_sigmas=[3.0, 6.0],
    )
    kw.update(over)
    return SubtractBackground(**kw)


def test_coverage_crop_matches_full_image(tmp_path):
    sci, err, wht = _covered_scene()
    path = str(tmp_path / "tile.fits")
    fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(sci, name="SCI"),
        fits.ImageHDU(err, name="ERR"),
        fits.ImageHDU(wht, name="WHT"),
    ]).writeto(path)

    sb = _crop_sb()
    crop = sb.coverage_crop(sci, err, wht)
    assert crop is not None              # the scene exercises the crop
    ys, xs = crop
    assert ys.start % 80 == 0 and xs.start % 80 == 0   # lcm of 20, 10, 2, 8, 16

    sub_c, mask_c, bits_c = sb.compute(path)
    sub_f, mask_f, bits_f = _crop_sb(crop_to_coverage=False).compute(path)

    assert sub_c.shape == sub_f.shape and sub_c.dtype == sub_f.dtype
    assert np.array_equal(bits_c, bits_f)
    assert np.array_equal(mask_c, mask_f)
    assert np.array_equal(np.isnan(sub_c), np.isnan(sub_f))
    ok = np.isfinite(sub_f)
    assert np.allclose(sub_c[ok], sub_f[ok], rtol=0, atol=1e-6 * SIG)


def test_crop_skipped_when_coverage_fills_the_image():
    sci, err, wht = _covered_scene(n=600, box=(10, 10, 580))
    assert _crop_sb().coverage_crop(sci, err, wht) is None
