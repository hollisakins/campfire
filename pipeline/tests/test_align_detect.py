"""Tests for the NIRCam align source detector (nircam/align/detect.py).

Detection is SEP segmentation of the SNR map (sci/err), mirroring the refcat
build (the shared ``refcat/extract.py::sep_extract_sources`` core), with
calibrated AB Kron magnitudes whenever a zeropoint is available. Fixtures
inject 2-D Gaussian sources onto Gaussian noise with a unit error map (so the
SNR map equals the image) — no real FITS needed for the pure core; the wrapper
tests write a minimal SCI/ERR/DQ HDUList.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nircam.align import (
    detect_in_exposure,
    detect_sources,
)


def _inject(shape, sources, rng, noise=1.0, fwhm=2.5):
    """Gaussian sources (cx, cy, amp) added to Gaussian noise."""
    sigma = fwhm / 2.3548
    img = rng.normal(0.0, noise, shape).astype(float)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    for cx, cy, amp in sources:
        img += amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * sigma ** 2))
    return img


def _err(shape, noise=1.0):
    return np.full(shape, float(noise))


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
    cat = detect_sources(img, _err(img.shape))

    assert len(cat) >= 3
    for cx, cy, _ in truth:
        assert _nearest(cat, cx, cy) < 0.5
    assert np.all(np.diff(np.asarray(cat['flux'])) <= 0)   # flux descending


def test_uncalibrated_mag_is_flux_proxy():
    # Without a zeropoint, mag degrades to -2.5 log10(flux) and is flagged so.
    rng = np.random.default_rng(1)
    img = _inject((100, 100), [(50.0, 50.0, 400.0), (30.0, 70.0, 200.0)], rng)
    cat = detect_sources(img, _err(img.shape))
    assert len(cat) >= 2
    assert not cat.meta['mag_calibrated']
    assert np.allclose(np.asarray(cat['mag']),
                       -2.5 * np.log10(np.asarray(cat['flux'])), atol=1e-6)


def test_mask_suppresses_source():
    rng = np.random.default_rng(1)
    img = _inject((100, 100), [(30.0, 30.0, 400.0), (70.0, 70.0, 400.0)], rng)
    mask = np.zeros((100, 100), dtype=bool)
    mask[24:37, 24:37] = True                    # cover the (30, 30) source
    cat = detect_sources(img, _err(img.shape), mask=mask)
    assert _nearest(cat, 70, 70) < 0.5
    assert _nearest(cat, 30, 30) > 5.0


def test_edge_rejection():
    rng = np.random.default_rng(1)
    img = _inject((100, 100), [(3.0, 50.0, 400.0), (50.0, 50.0, 400.0)], rng)
    cat = detect_sources(img, _err(img.shape), edge=8)
    assert _nearest(cat, 50, 50) < 0.5
    assert not np.any(np.asarray(cat['x']) < 8)  # within-edge source dropped


def test_pure_noise_returns_empty_typed():
    rng = np.random.default_rng(2)
    img = rng.normal(0.0, 1.0, (80, 80)).astype(float)
    cat = detect_sources(img, _err(img.shape))
    assert len(cat) == 0
    assert {'x', 'y', 'flux', 'fluxerr', 'mag', 'snr', 'npix'}.issubset(
        cat.colnames)
    assert 'mag_calibrated' in cat.meta


def test_nan_pixels_handled():
    rng = np.random.default_rng(4)
    img = _inject((80, 80), [(40.0, 40.0, 400.0)], rng)
    img[0:10, 0:10] = np.nan
    cat = detect_sources(img, _err(img.shape))        # must not crash
    assert _nearest(cat, 40, 40) < 0.5


def test_missing_err_falls_back_to_global_rms():
    # No error map: a sigma-clipped constant RMS stands in — degraded but the
    # bright source must still come out.
    rng = np.random.default_rng(5)
    img = _inject((100, 100), [(50.0, 50.0, 400.0)], rng)
    cat = detect_sources(img, None)
    assert _nearest(cat, 50, 50) < 0.5


# --- quality cuts -----------------------------------------------------------

def test_snr_min_drops_low_snr_sources():
    # snr_min cuts on *integrated* flux SNR (flux/fluxerr), the refcat's own
    # selection. The faint source detects fine at the per-pixel threshold but
    # its integrated SNR (~tens) falls below a raised snr_min; the bright one
    # (~hundreds) survives.
    rng = np.random.default_rng(6)
    img = _inject((100, 100), [(30.0, 30.0, 400.0), (70.0, 70.0, 30.0)], rng)
    base = detect_sources(img, _err(img.shape), snr_min=None)
    assert _nearest(base, 30, 30) < 0.5 and _nearest(base, 70, 70) < 1.0
    cut = detect_sources(img, _err(img.shape), snr_min=150.0)
    assert _nearest(cut, 30, 30) < 0.5               # bright kept
    assert _nearest(cut, 70, 70) > 5.0               # faint dropped


def test_objmag_lim_cuts_calibrated_mags():
    # With a zeropoint, mags are calibrated AB (Kron flux + ZP) and objmag_lim
    # trims a real AB window. Derive the limits from the actual catalog so the
    # test doesn't hard-code Kron fluxes.
    rng = np.random.default_rng(7)
    img = _inject((120, 120),
                  [(30.0, 30.0, 600.0), (60.0, 60.0, 120.0), (90.0, 90.0, 40.0)],
                  rng)
    full = detect_sources(img, _err(img.shape), snr_min=None, zeropoint=25.0)
    assert full.meta['mag_calibrated']
    assert len(full) >= 3
    mags = np.sort(np.asarray(full['mag']))
    mags = mags[np.isfinite(mags)]
    lo, hi = mags[0] + 0.01, mags[-1] - 0.01         # exclude the extremes
    cut = detect_sources(img, _err(img.shape), snr_min=None, zeropoint=25.0,
                         objmag_lim=(lo, hi))
    assert 0 < len(cut) < len(full)
    cm = np.asarray(cut['mag'])
    assert np.all(np.isfinite(cm) & (cm >= lo) & (cm <= hi))


def test_objmag_lim_skipped_without_zeropoint():
    # An AB window applied to instrumental mags would cut everything, so with
    # no zeropoint the cut must be skipped (loudly), not applied.
    rng = np.random.default_rng(7)
    img = _inject((120, 120), [(30.0, 30.0, 600.0), (60.0, 60.0, 120.0)], rng)
    full = detect_sources(img, _err(img.shape), snr_min=None)
    assert not full.meta['mag_calibrated']
    cut = detect_sources(img, _err(img.shape), snr_min=None,
                         objmag_lim=(19.0, 28.0))
    assert len(cut) == len(full)


def test_calibrated_mag_recovers_known_flux():
    # A bright isolated Gaussian of known total flux F: the Kron aperture
    # (2.5 x Kron radius ~ 3 sigma for a Gaussian, >99% enclosed) mag must
    # land near ZP - 2.5 log10(F).
    rng = np.random.default_rng(11)
    fwhm, amp = 2.5, 2000.0
    sigma = fwhm / 2.3548
    total = amp * 2 * np.pi * sigma ** 2
    img = _inject((100, 100), [(50.0, 50.0, amp)], rng, noise=0.5, fwhm=fwhm)
    zp = 25.0
    cat = detect_sources(img, _err(img.shape, 0.5), fwhm=fwhm, zeropoint=zp)
    assert len(cat) >= 1
    row = np.argmin(np.hypot(np.asarray(cat['x']) - 50,
                             np.asarray(cat['y']) - 50))
    expected = zp - 2.5 * np.log10(total)
    assert abs(float(cat['mag'][row]) - expected) < 0.1


def test_ab_zeropoint_from_sci_header():
    from campfire_pipeline.nircam.align.detect import (
        ab_zeropoint_from_sci_header,
    )
    # LW pixel (0.063"): PIXAR_SR ~ 9.31e-14 sr -> ZP ~ 26.5 AB, and exactly
    # -2.5 log10(PIXAR_SR * 1e6 / 3631)
    h = fits.Header({'BUNIT': 'MJy/sr', 'PIXAR_SR': 9.31e-14})
    zp = ab_zeropoint_from_sci_header(h)
    assert abs(zp - (-2.5 * np.log10(9.31e-14 * 1e6 / 3631.0))) < 1e-10
    assert 26.0 < zp < 27.0
    # missing / wrong units -> None
    assert ab_zeropoint_from_sci_header(fits.Header({'BUNIT': 'MJy/sr'})) is None
    assert ab_zeropoint_from_sci_header(
        fits.Header({'BUNIT': 'DN/s', 'PIXAR_SR': 9.31e-14})) is None


# --- FITS wrapper -----------------------------------------------------------

def _write_exposure(path, sci, err, dq, header_cards=None):
    sci_hdu = fits.ImageHDU(sci.astype('float32'), name='SCI')
    for k, v in (header_cards or {}).items():
        sci_hdu.header[k] = v
    fits.HDUList([
        fits.PrimaryHDU(),
        sci_hdu,
        fits.ImageHDU(err.astype('float32'), name='ERR'),
        fits.ImageHDU(dq.astype('uint32'), name='DQ'),
    ]).writeto(path, overwrite=True)


def test_detect_in_exposure_masks_saturated_dq(tmp_path):
    # A SATURATED pixel (DQ bit 1) is masked even without DO_NOT_USE — a
    # saturated core corrupts the position/flux the mag cut can't catch.
    rng = np.random.default_rng(8)
    shape = (100, 100)
    sci = _inject(shape, [(30.0, 30.0, 400.0), (70.0, 70.0, 400.0)], rng)
    dq = np.zeros(shape, dtype='uint32')
    dq[24:37, 24:37] = 2                             # SATURATED, no DO_NOT_USE
    path = tmp_path / 'jw01727028001_04101_00003_nrca1.fits'
    _write_exposure(path, sci, np.ones(shape), dq)

    cat = detect_in_exposure(str(path))
    assert _nearest(cat, 70, 70) < 0.5               # clean source detected
    assert _nearest(cat, 30, 30) > 5.0               # saturated source masked


def test_detect_in_exposure_masks_dq_and_err(tmp_path):
    rng = np.random.default_rng(5)
    shape = (100, 100)
    # A: DQ-flagged, B: clean, C: under an off-detector (NaN ERR) patch.
    sci = _inject(shape, [(30.0, 30.0, 400.0),
                          (70.0, 70.0, 400.0),
                          (50.0, 75.0, 400.0)], rng)
    err = np.ones(shape)
    err[70:81, 45:56] = np.nan                       # covers C (x=50, y=75)
    dq = np.zeros(shape, dtype='uint32')
    dq[24:37, 24:37] = 1                             # DO_NOT_USE over A (30, 30)
    path = tmp_path / 'jw01727028001_04101_00003_nrca1.fits'
    _write_exposure(path, sci, err, dq)

    cat = detect_in_exposure(str(path))
    assert _nearest(cat, 70, 70) < 0.5               # clean source detected
    assert _nearest(cat, 30, 30) > 5.0               # DQ-masked
    assert _nearest(cat, 50, 75) > 5.0               # ERR-masked


def test_detect_in_exposure_resolves_zeropoint_from_header(tmp_path):
    # BUNIT=MJy/sr + PIXAR_SR on the SCI header -> calibrated AB mags, exactly
    # ZP - 2.5 log10(flux) with ZP from ab_zeropoint_from_sci_header.
    from campfire_pipeline.nircam.align.detect import (
        ab_zeropoint_from_sci_header,
    )
    rng = np.random.default_rng(9)
    shape = (100, 100)
    sci = _inject(shape, [(50.0, 50.0, 400.0)], rng)
    cards = {'BUNIT': 'MJy/sr', 'PIXAR_SR': 9.31e-14}
    path = tmp_path / 'jw01727028001_04101_00003_nrcalong.fits'
    _write_exposure(path, sci, np.ones(shape), np.zeros(shape, 'uint32'),
                    header_cards=cards)

    cat = detect_in_exposure(str(path))
    assert len(cat) >= 1
    assert cat.meta['mag_calibrated']
    zp = ab_zeropoint_from_sci_header(fits.Header(cards))
    expected = zp - 2.5 * np.log10(np.asarray(cat['flux']))
    assert np.allclose(np.asarray(cat['mag']), expected, atol=1e-6)
