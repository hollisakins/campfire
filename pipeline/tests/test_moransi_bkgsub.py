"""Tests for Moran's-I source masking (nircam/moransi_bkgsub.py).

Covers three things:
  1. the vectorized ``morans_i_local`` matches a naive per-patch reference on
     fully-valid data (guards the vectorization),
  2. the statistic is NaN/valid-aware (guards the donated zero-fill bug), and
  3. ``morans_source_mask`` + the ``SubtractBackground(mask_method="moransi")``
     integration produce a sensible source mask and a flattened background.
"""

import numpy as np
import pytest
from astropy.io import fits
from scipy import ndimage

from campfire_pipeline.nircam.moransi_bkgsub import (
    _QUEEN_KERNEL,
    morans_i_local,
    morans_source_mask,
)


def _ref_morans_i_local(image, p, kernel):
    """Naive per-patch reference (the donated algorithm) for fully-valid data."""
    H, W = image.shape
    out = np.zeros((H // p, W // p))
    n = p * p
    edges = 2 * p * 4 - 4
    w_norm = 8 * n - edges
    for i in range(0, H - p + 1, p):
        for j in range(0, W - p + 1, p):
            block = image[i:i + p, j:j + p]
            s = np.std(block)
            if s == 0:
                out[i // p, j // p] = 0.0
                continue
            bstd = (block - np.mean(block)) / s
            ns = ndimage.convolve(bstd, kernel, mode="constant", cval=0.0)
            out[i // p, j // p] = (n / w_norm) * np.sum(bstd * ns)
    return out


def _gaussian(shape, y0, x0, sigma, amp):
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    return amp * np.exp(-((yy - y0) ** 2 + (xx - x0) ** 2) / (2 * sigma ** 2))


# ---------------------------------------------------------------------------
# 1. vectorization equivalence
# ---------------------------------------------------------------------------

def test_vectorized_matches_reference_fully_valid():
    rng = np.random.default_rng(42)
    p = 20
    img = rng.standard_normal((100, 80)).astype(np.float64)
    # add some structure so patches differ
    img += _gaussian(img.shape, 30, 25, 6, 8.0)

    vec = morans_i_local(img, p)
    ref = _ref_morans_i_local(img, p, _QUEEN_KERNEL)

    assert vec.shape == ref.shape == (5, 4)
    assert np.allclose(vec, ref, rtol=2e-3, atol=1e-4)


def test_source_patch_scores_higher_than_blank():
    rng = np.random.default_rng(1)
    p = 20
    img = 0.05 * rng.standard_normal((60, 60))
    img += _gaussian(img.shape, 10, 10, 4, 20.0)   # bright source in patch (0,0)

    i_grid = morans_i_local(img, p)
    # the source patch should be the most autocorrelated of the nine.
    assert i_grid[0, 0] == pytest.approx(i_grid.max())
    assert i_grid[0, 0] > np.median(i_grid) + 0.1


# ---------------------------------------------------------------------------
# 2. NaN / valid awareness (the donated zero-fill bug)
# ---------------------------------------------------------------------------

def test_masked_pixels_do_not_inject_structure():
    """Masking part of a noise patch must NOT make it look source-like.

    Zero-filling the masked pixels (the donated behaviour) injects a constant,
    perfectly-autocorrelated block and inflates I; the NaN-aware statistic must
    stay centred on the blank-noise distribution (E[I] ~ 0). A single noise
    patch has a large I spread, so this is asserted over many patches.
    """
    rng = np.random.default_rng(7)
    p, ntile = 20, 10
    # Blank sky with a realistic PEDESTAL: masked->0 pixels are then far from
    # the patch mean, which is exactly when zero-filling injects structure.
    img = 100.0 + rng.standard_normal((p * ntile, p * ntile))

    i_valid = morans_i_local(img, p).ravel()             # baseline distribution

    mask = np.zeros(img.shape, bool)
    for by in range(ntile):
        mask[by * p: by * p + 10, :] = True              # top half of each patch

    i_nan = morans_i_local(img.copy(), p, valid=~mask).ravel()
    i_zero = morans_i_local(np.where(mask, 0.0, img), p).ravel()   # zeros as data

    # Zero-fill systematically inflates I; NaN-aware tracks the blank baseline.
    assert i_zero.mean() > i_nan.mean() + 1.0
    assert abs(i_nan.mean() - i_valid.mean()) < abs(i_zero.mean() - i_valid.mean())


def test_too_empty_patch_is_nan():
    p = 20
    img = np.random.default_rng(0).standard_normal((40, 40))
    img[:, :20] = np.nan                # left column of patches fully invalid
    i_grid = morans_i_local(img, p, valid=~np.isnan(img), min_valid=100)
    assert np.isnan(i_grid[:, 0]).all()      # empty patches -> NaN
    assert np.isfinite(i_grid[:, 1]).all()   # valid patches -> finite


def test_non_multiple_shape_pads_cleanly():
    p = 20
    img = np.random.default_rng(3).standard_normal((55, 47))  # not multiples of 20
    i_grid = morans_i_local(img, p)
    assert i_grid.shape == (3, 3)            # ceil(55/20)=3, ceil(47/20)=3
    assert np.isfinite(i_grid).all()


# ---------------------------------------------------------------------------
# 3. morans_source_mask
# ---------------------------------------------------------------------------

def test_source_mask_polarity_and_source_flagged():
    rng = np.random.default_rng(11)
    p = 20
    img = 100.0 + 0.05 * rng.standard_normal((200, 200))   # sky pedestal + noise
    img += _gaussian(img.shape, 100, 100, 8, 30.0)         # central bright source

    mask = morans_source_mask(img, p, percentile=40.0)
    assert mask.dtype == bool
    assert mask[100, 100]                       # source centre is masked (True)
    # keep ~percentile% as background -> ~60% masked; some kept, not all.
    # (Moran's I on pure noise is near-random, so no *single* blank pixel is
    # reliably background — assert the fraction, not a specific pixel.)
    assert 0.45 < mask.mean() < 0.75
    assert not mask.all()


def test_source_mask_fraction_tracks_percentile():
    """Higher percentile keeps more pixels as background -> fewer masked."""
    rng = np.random.default_rng(5)
    p = 20
    img = 0.05 * rng.standard_normal((200, 200))
    for (y, x) in [(40, 60), (150, 120), (90, 30)]:
        img += _gaussian(img.shape, y, x, 6, 25.0)

    frac30 = morans_source_mask(img, p, percentile=30.0).mean()
    frac60 = morans_source_mask(img, p, percentile=60.0).mean()
    assert frac60 < frac30                       # monotonic
    # keep ~percentile% as background -> mask ~ (1 - percentile/100)
    assert frac30 == pytest.approx(0.70, abs=0.12)
    assert frac60 == pytest.approx(0.40, abs=0.12)


def test_source_mask_excludes_off_detector():
    p = 20
    img = 0.05 * np.random.default_rng(2).standard_normal((80, 80))
    input_mask = np.zeros(img.shape, bool)
    input_mask[:20, :] = True                    # top row off-detector
    err = np.ones_like(img)
    err[:20, :] = np.nan
    mask = morans_source_mask(img, p, input_mask=input_mask, error=err,
                              percentile=50.0)
    assert mask[:20, :].all()                    # off-detector -> excluded (True)


# ---------------------------------------------------------------------------
# 4. SubtractBackground integration
# ---------------------------------------------------------------------------

def _write_synthetic_mosaic(path, shape=(240, 240), seed=13):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    sky = 5.0 + 0.02 * xx + 0.015 * yy          # smooth large-scale gradient
    noise = 0.05 * rng.standard_normal(shape)
    sci = (sky + noise).astype(np.float32)
    for (y, x, a) in [(60, 70, 40), (170, 150, 35), (120, 40, 30)]:
        sci += _gaussian(shape, y, x, 7, a).astype(np.float32)
    err = (0.05 * np.ones(shape)).astype(np.float32)
    # off-detector border
    sci[:8, :] = np.nan
    err[:8, :] = np.nan
    hdul = fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(sci, name="SCI"),
        fits.ImageHDU(err, name="ERR"),
    ])
    hdul.writeto(path, overwrite=True)
    return sci


def test_subtractbackground_moransi_flattens_gradient(tmp_path):
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    fpath = str(tmp_path / "mosaic_i2d.fits")
    sci = _write_synthetic_mosaic(fpath)

    bkg = SubtractBackground(mask_method="moransi", moransi_patch_size=20,
                             moransi_percentile=40.0)
    sub, mask_final, bitmask = bkg.compute(fpath)

    # The fitted background is sci - sub; it must recover the true smooth sky in
    # a blank region (sources remain in `sub`, so we avoid them). Median of the
    # residual there must be ~0 (pedestal removed).
    yy, xx = np.mgrid[0:sci.shape[0], 0:sci.shape[1]]
    true_sky = 5.0 + 0.02 * xx + 0.015 * yy
    model = sci - sub
    blank = (slice(195, 235), slice(195, 235))          # far from all sources
    assert abs(np.nanmedian(sub[blank])) < 0.5
    assert np.nanmedian(np.abs(model - true_sky)[blank]) < 0.3
    # the bright source is captured in the source mask
    assert mask_final[55:65, 65:75].mean() > 0.5


def test_subtractbackground_methods_agree_on_gradient(tmp_path):
    """Both methods should remove the smooth gradient (fitter is identical)."""
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    fpath = str(tmp_path / "mosaic_i2d.fits")
    sci = _write_synthetic_mosaic(fpath)

    sub_t, _, _ = SubtractBackground(mask_method="tiered").compute(fpath)
    sub_m, _, _ = SubtractBackground(mask_method="moransi").compute(fpath)

    # Both recover the smooth sky in a blank region (the fitter is identical).
    yy, xx = np.mgrid[0:sci.shape[0], 0:sci.shape[1]]
    true_sky = 5.0 + 0.02 * xx + 0.015 * yy
    blank = (slice(195, 235), slice(195, 235))
    for sub in (sub_t, sub_m):
        model = sci - sub
        assert abs(np.nanmedian(sub[blank])) < 0.5
        assert np.nanmedian(np.abs(model - true_sky)[blank]) < 0.3


def test_bad_mask_method_raises(tmp_path):
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    fpath = str(tmp_path / "mosaic_i2d.fits")
    _write_synthetic_mosaic(fpath)
    with pytest.raises(ValueError, match="Unknown mask_method"):
        SubtractBackground(mask_method="bogus").compute(fpath)


def test_from_config_picks_up_mask_method():
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    cfg = {"mask_method": "moransi", "moransi_patch_size": 32,
           "moransi_percentile": 55.0, "unrelated_key": 999}
    bkg = SubtractBackground.from_config(cfg)
    assert bkg.mask_method == "moransi"
    assert bkg.moransi_patch_size == 32
    assert bkg.moransi_percentile == 55.0


def test_call_stamps_background_method(tmp_path):
    from campfire_pipeline.nircam.bkgsub import SubtractBackground

    fp = str(tmp_path / "mosaic_i2d.fits")
    _write_synthetic_mosaic(fp)
    out = SubtractBackground(mask_method="moransi").call(fp)
    with fits.open(out) as h:
        assert h[0].header["CMPFRBKG"] == "moransi"
        assert h[0].header["BKGPATCH"] == 20
        assert "BKGPCTL" in h[0].header

    fp2 = str(tmp_path / "mosaic2_i2d.fits")
    _write_synthetic_mosaic(fp2)
    out2 = SubtractBackground(mask_method="tiered").call(fp2)
    with fits.open(out2) as h:
        assert h[0].header["CMPFRBKG"] == "tiered"
        assert "BKGPATCH" not in h[0].header


# ---------------------------------------------------------------------------
# 5. manifest config_hash (the load-bearing A/B rebuild trigger)
# ---------------------------------------------------------------------------

def test_config_hash_backward_compatible_for_tiered():
    """Default (tiered) must reproduce the pre-feature hash -> no mass rebuild."""
    import hashlib
    import json as _json
    from campfire_pipeline.nircam.manifest import _resample_config_hash

    legacy = _json.dumps({
        "pixfrac": 1, "kernel": "square",
        "pixel_scale": "30mas", "background_subtract": True,
    }, sort_keys=True)
    legacy_hash = f"sha256:{hashlib.sha256(legacy.encode()).hexdigest()}"

    base = {"pixfrac": 1, "kernel": "square", "background_subtract": True}
    assert _resample_config_hash(base, "30mas") == legacy_hash
    assert _resample_config_hash({**base, "mask_method": "tiered"},
                                 "30mas") == legacy_hash


def test_config_hash_distinguishes_methods_and_params():
    from campfire_pipeline.nircam.manifest import _resample_config_hash

    base = {"pixfrac": 1, "kernel": "square", "background_subtract": True}
    h_tiered = _resample_config_hash(base, "30mas")
    h_moransi = _resample_config_hash({**base, "mask_method": "moransi"}, "30mas")
    h_p50 = _resample_config_hash(
        {**base, "mask_method": "moransi", "moransi_percentile": 50.0}, "30mas")

    assert h_tiered != h_moransi                 # tiered <-> moransi differ
    assert h_moransi != h_p50                     # moransi params matter


def test_check_config_changed_detects_method_switch(tmp_path):
    from campfire_pipeline.nircam.manifest import (
        check_config_changed, write_manifest,
    )

    # A manifest written for tiered...
    tiered_cfg = {"resample": {"pixfrac": 1, "kernel": "square",
                               "background_subtract": True,
                               "mask_method": "tiered"}}
    from campfire_pipeline.nircam.manifest import _resample_config_hash
    mpath = str(tmp_path / "mosaic_manifest.json")
    write_manifest({"config_hash": _resample_config_hash(
        tiered_cfg["resample"], "30mas")}, mpath)

    # ...is unchanged for tiered, but changed once we switch to moransi.
    assert not check_config_changed(mpath, tiered_cfg, "30mas")
    moransi_cfg = {"resample": {**tiered_cfg["resample"],
                               "mask_method": "moransi"}}
    assert check_config_changed(mpath, moransi_cfg, "30mas")
