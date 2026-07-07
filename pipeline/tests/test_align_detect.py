"""Tests for the NIRCam centroid-only source detector (nircam/align/detect.py).

Fixtures inject 2-D Gaussian point sources onto Gaussian noise (mirrors the
RNG-injection pattern in test_nircam_diag_striping.py) — no real FITS needed for
the pure core; the wrapper test writes a minimal SCI/ERR/DQ HDUList.
"""

import numpy as np
import pytest
from astropy.io import fits
from photutils.utils.exceptions import NoDetectionsWarning

from campfire_pipeline.nircam.align import (
    detect_in_exposure,
    detect_star_centroids,
)


def _inject(shape, sources, rng, noise=1.0, fwhm=2.5):
    """Gaussian sources (cx, cy, amp) added to Gaussian noise."""
    sigma = fwhm / 2.3548
    img = rng.normal(0.0, noise, shape).astype(float)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for cx, cy, amp in sources:
        img += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
    return img


def _nearest(cat, cx, cy):
    if len(cat) == 0:
        return np.inf
    return float(np.min(np.hypot(np.asarray(cat['x']) - cx,
                                 np.asarray(cat['y']) - cy)))


# --- pure core --------------------------------------------------------------

def test_recovers_injected_sources():
    rng = np.random.default_rng(1)
    truth = [(30.4, 25.2, 300.0), (60.1, 70.8, 150.0), (45.0, 45.0, 500.0)]
    img = _inject((100, 100), truth, rng)
    cat = detect_star_centroids(img, fwhm=2.5, nsigma=5.0)

    assert len(cat) >= 3
    for cx, cy, _ in truth:
        assert _nearest(cat, cx, cy) < 0.3
    assert np.all(np.diff(np.asarray(cat['flux'])) <= 0)   # flux descending


def test_mag_is_flux_proxy():
    rng = np.random.default_rng(1)
    img = _inject((100, 100), [(50.0, 50.0, 400.0), (30.0, 70.0, 200.0)], rng)
    cat = detect_star_centroids(img)
    assert len(cat) >= 2
    assert np.allclose(np.asarray(cat['mag']),
                       -2.5 * np.log10(np.asarray(cat['flux'])), atol=1e-6)


def test_mask_suppresses_source():
    rng = np.random.default_rng(1)
    img = _inject((100, 100), [(30.0, 30.0, 400.0), (70.0, 70.0, 400.0)], rng)
    mask = np.zeros((100, 100), dtype=bool)
    mask[24:37, 24:37] = True                    # cover the (30, 30) source
    cat = detect_star_centroids(img, mask=mask)
    assert _nearest(cat, 70, 70) < 0.3
    assert _nearest(cat, 30, 30) > 5.0


def test_edge_rejection():
    rng = np.random.default_rng(1)
    img = _inject((100, 100), [(3.0, 50.0, 400.0), (50.0, 50.0, 400.0)], rng)
    cat = detect_star_centroids(img, edge=8)
    assert _nearest(cat, 50, 50) < 0.3
    assert not np.any(np.asarray(cat['x']) < 8)  # within-edge source dropped


def test_pure_noise_returns_empty_typed(recwarn):
    rng = np.random.default_rng(2)
    img = rng.normal(0.0, 1.0, (80, 80)).astype(float)
    cat = detect_star_centroids(img, nsigma=50.0)
    assert len(cat) == 0
    assert {'x', 'y', 'flux', 'mag', 'npix'}.issubset(cat.colnames)
    # the NoDetectionsWarning is suppressed inside the function
    assert not any(issubclass(w.category, NoDetectionsWarning) for w in recwarn)


def test_brightest_caps_to_n():
    rng = np.random.default_rng(3)
    sources = [(15 + 20 * (i % 4), 15 + 20 * (i // 4), 100.0 + 40 * i)
               for i in range(12)]
    img = _inject((100, 100), sources, rng)
    cat_all = detect_star_centroids(img)
    assert len(cat_all) >= 5
    cat = detect_star_centroids(img, brightest=5)
    assert len(cat) == 5
    assert np.array_equal(np.asarray(cat['flux']),
                          np.asarray(cat_all['flux'])[:5])   # the 5 brightest


def test_nan_pixels_handled():
    rng = np.random.default_rng(4)
    img = _inject((80, 80), [(40.0, 40.0, 400.0)], rng)
    img[0:10, 0:10] = np.nan
    cat = detect_star_centroids(img)                 # must not crash
    assert _nearest(cat, 40, 40) < 0.3


# --- quality cuts (S2) ------------------------------------------------------

def test_snr_min_drops_low_peak_sources():
    # peak SNR ≈ amp / background_rms (rms ~ 1 here). snr_min drops the faint
    # source (~15σ) but keeps the bright one (~400σ), even though nsigma found both.
    rng = np.random.default_rng(6)
    img = _inject((100, 100), [(30.0, 30.0, 400.0), (70.0, 70.0, 15.0)], rng)
    base = detect_star_centroids(img, nsigma=4.0)
    assert _nearest(base, 30, 30) < 0.3 and _nearest(base, 70, 70) < 0.6
    cut = detect_star_centroids(img, nsigma=4.0, snr_min=40.0)
    assert _nearest(cut, 30, 30) < 0.3               # bright kept
    assert _nearest(cut, 70, 70) > 5.0               # faint dropped


def test_objmag_lim_keeps_only_range():
    # objmag_lim trims a magnitude window (uncalibrated DAO mag). Derive the
    # limits from the actual catalog so the test doesn't hard-code kernel flux.
    rng = np.random.default_rng(7)
    img = _inject((120, 120),
                  [(30.0, 30.0, 600.0), (60.0, 60.0, 120.0), (90.0, 90.0, 25.0)],
                  rng)
    full = detect_star_centroids(img, nsigma=4.0)
    assert len(full) >= 3
    # DAOStarFinder can emit a negative-flux detection (mag = nan); rank on the
    # finite mags. objmag_lim itself drops the nan-mag rows (isfinite gate).
    mags = np.sort(np.asarray(full['mag']))
    mags = mags[np.isfinite(mags)]
    lo, hi = mags[0] + 0.01, mags[-1] - 0.01         # exclude the extremes
    cut = detect_star_centroids(img, nsigma=4.0, objmag_lim=(lo, hi))
    assert 0 < len(cut) < len(full)
    cm = np.asarray(cut['mag'])
    assert np.all(np.isfinite(cm) & (cm >= lo) & (cm <= hi))


def test_detect_in_exposure_masks_saturated_dq(tmp_path):
    # A SATURATED pixel (DQ bit 1) is masked even without DO_NOT_USE — a
    # saturated core corrupts the centroid/flux the mag cut can't catch.
    rng = np.random.default_rng(8)
    shape = (100, 100)
    sci = _inject(shape, [(30.0, 30.0, 400.0), (70.0, 70.0, 400.0)],
                  rng).astype('float32')
    err = np.ones(shape, dtype='float32')
    dq = np.zeros(shape, dtype='uint32')
    dq[24:37, 24:37] = 2                             # SATURATED, no DO_NOT_USE
    path = tmp_path / 'jw01727028001_04101_00003_nrca1.fits'
    fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(sci, name='SCI'),
        fits.ImageHDU(err, name='ERR'),
        fits.ImageHDU(dq, name='DQ'),
    ]).writeto(path, overwrite=True)

    cat = detect_in_exposure(str(path))
    assert _nearest(cat, 70, 70) < 0.3               # clean source detected
    assert _nearest(cat, 30, 30) > 5.0               # saturated source masked


# --- FITS wrapper -----------------------------------------------------------

def test_detect_in_exposure_masks_dq_and_err(tmp_path):
    rng = np.random.default_rng(5)
    shape = (100, 100)
    # A: DQ-flagged, B: clean, C: under an off-detector (NaN ERR) patch.
    sci = _inject(shape, [(30.0, 30.0, 400.0),
                          (70.0, 70.0, 400.0),
                          (50.0, 75.0, 400.0)], rng).astype('float32')
    err = np.ones(shape, dtype='float32')
    err[70:81, 45:56] = np.nan                       # covers C (x=50, y=75)
    dq = np.zeros(shape, dtype='uint32')
    dq[24:37, 24:37] = 1                             # DO_NOT_USE over A (30, 30)

    path = tmp_path / 'jw01727028001_04101_00003_nrca1.fits'
    fits.HDUList([
        fits.PrimaryHDU(),
        fits.ImageHDU(sci, name='SCI'),
        fits.ImageHDU(err, name='ERR'),
        fits.ImageHDU(dq, name='DQ'),
    ]).writeto(path, overwrite=True)

    cat = detect_in_exposure(str(path))
    assert _nearest(cat, 70, 70) < 0.3               # clean source detected
    assert _nearest(cat, 30, 30) > 5.0               # DQ-masked
    assert _nearest(cat, 50, 75) > 5.0               # ERR-masked
