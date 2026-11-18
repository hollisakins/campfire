"""The threaded bkgsub kernels return the library results bit for bit.

``_bkg_kernels`` stands in for scipy / numpy / astropy / photutils calls in
SubtractBackground so the mosaic background can use the CLI's --processes as
threads without changing a pixel. Each kernel is compared with the call it
replaces by exact equality; the sample and chunk sizes are shrunk so the
parallel paths run on test-sized arrays.
"""

import warnings

import numpy as np
import pytest
from astropy import stats as astrostats
from astropy.io import fits
from photutils.background import (
    Background2D,
    BiweightLocationBackground,
    BkgZoomInterpolator,
)
from photutils.segmentation import detect_sources
from scipy import ndimage

from campfire_pipeline.nircam import _bkg_kernels as K

T = 4


@pytest.fixture(autouse=True)
def small_kernels(monkeypatch):
    monkeypatch.setattr(K, "_CHUNK", 1 << 12)
    monkeypatch.setattr(K, "_SAMPLE", 1 << 10)


@pytest.fixture
def rng():
    return np.random.default_rng(11)


def _image(rng, shape=(700, 530), dtype=np.float32):
    img = ndimage.gaussian_filter(rng.normal(size=shape), 1.5)
    img += 0.02 * np.linspace(0, 1, shape[1])
    return img.astype(dtype)


@pytest.mark.parametrize("sigma", [1.0, 2.0, 5.0, 15.0])
@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_gaussian(rng, sigma, dtype):
    img = _image(rng, dtype=dtype)
    ref = ndimage.gaussian_filter(img, sigma, mode="reflect")
    assert np.array_equal(K.gaussian(img, sigma, threads=T), ref)


@pytest.mark.parametrize("radius", [2.5, 5.0, 19.0])
def test_dilate(rng, radius):
    mask = _image(rng) > 0.55
    assert mask.any()
    ref = ndimage.distance_transform_edt(~mask) <= radius
    assert np.array_equal(K.dilate(mask, radius, threads=T), ref)


def test_median_filter(rng):
    img = _image(rng)
    yy, xx = np.mgrid[-6:7, -6:7]
    ring = (np.hypot(yy, xx) <= 6) & (np.hypot(yy, xx) >= 4)
    ref = ndimage.median_filter(img, footprint=ring)
    assert np.array_equal(K.median_filter(img, ring, threads=T), ref)


@pytest.mark.parametrize("n", [100_001, 100_000])
@pytest.mark.parametrize("dtype", [np.float32, np.float64])
def test_robust_statistics(rng, n, dtype):
    x = (rng.standard_t(3, size=n) * 0.01 + 0.003).astype(dtype)
    x[::5] = np.round(x[::5], 3)                     # plenty of ties
    m = K.median(x, threads=T)
    assert m == np.median(x) and m.dtype == np.median(x).dtype
    assert K.biweight_scale(x, threads=T) == astrostats.biweight_scale(x)
    assert K.biweight_location(x, threads=T) == \
        astrostats.biweight_location(x)


def test_median_bracket_miss_falls_back(rng, monkeypatch):
    # sorted data defeats a strided sample only if the bracket is too tight
    x = np.sort(rng.normal(size=50_001))
    monkeypatch.setattr(K, "_select", lambda *a: None)
    assert K.median(x, threads=T) == np.median(x)


@pytest.mark.parametrize("args", [
    dict(zoom_factor=10, order=3, mode="reflect", grid_mode=True),
    dict(zoom_factor=(7, 9), order=3, mode="reflect", grid_mode=True),
    dict(zoom_factor=4, order=1, mode="nearest", grid_mode=False),
])
def test_zoom(rng, args):
    mesh = rng.normal(size=(61, 47)).cumsum(0).cumsum(1)
    ref = ndimage.zoom(mesh, args["zoom_factor"], order=args["order"],
                       mode=args["mode"], grid_mode=args["grid_mode"])
    got = K.zoom(mesh, threads=T, **args)
    assert np.array_equal(got, ref)


@pytest.mark.parametrize("masked", [False, True])
def test_detect(rng, masked):
    img = _image(rng)
    mask = np.zeros(img.shape, bool)
    if masked:
        mask[100:300, 50:200] = True
    thr = np.float64(0.25)
    seg = detect_sources(img, thr, n_pixels=12, mask=mask if masked else None)
    got = K.detect(img, thr, 12, mask=mask if masked else None, threads=T)
    assert np.array_equal(got, seg.data)


def test_detect_none(rng):
    img = _image(rng)
    assert K.detect(img, 1e9, 5, threads=T) is None


@pytest.mark.parametrize("size", [3, 5, (3, 5)])
def test_mesh_nanmedian_filter(rng, size):
    mesh = rng.normal(size=(83, 71))
    mesh[rng.random(mesh.shape) < 0.3] = np.nan
    mesh[20:30, 10:25] = np.nan                       # all-NaN windows
    ref = ndimage.generic_filter(mesh, np.nanmedian, size=size,
                                 mode="constant", cval=np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        got = K.mesh_nanmedian_filter(mesh, size)
    assert np.array_equal(got, ref, equal_nan=True)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
@pytest.mark.filterwarnings("ignore::astropy.utils.exceptions.AstropyDeprecationWarning")
def test_background2d(rng):
    img = _image(rng, shape=(640, 510))
    mask = rng.random(img.shape) < 0.2
    kw = dict(box_size=10, sigma_clip=astrostats.SigmaClip(sigma=3),
              filter_size=5, bkg_estimator=BiweightLocationBackground(),
              exclude_percentile=10, mask=mask)
    ref = Background2D(img, interpolator=BkgZoomInterpolator(), **kw)
    got = K.FastBackground2D(img, interpolator=K.ExactZoomInterpolator(),
                             **kw, **K.background2d_threads(T))
    assert np.array_equal(got.background_mesh, ref.background_mesh,
                          equal_nan=True)
    assert np.array_equal(got.background, ref.background)
    assert np.array_equal(got.background_rms, ref.background_rms)


def test_threaded_compute_matches_serial(tmp_path):
    from test_nircam_bkgsub_equivalence import _covered_scene, _crop_sb

    sci, err, wht = _covered_scene()
    path = str(tmp_path / "tile.fits")
    fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(sci, name="SCI"),
        fits.ImageHDU(err, name="ERR"),
        fits.ImageHDU(wht, name="WHT"),
    ]).writeto(path)
    sub_1, mask_1, bits_1 = _crop_sb(n_threads=1).compute(path)
    sub_t, mask_t, bits_t = _crop_sb(n_threads=T).compute(path)
    assert np.array_equal(bits_t, bits_1)
    assert np.array_equal(mask_t, mask_1)
    assert np.array_equal(sub_t, sub_1, equal_nan=True)
