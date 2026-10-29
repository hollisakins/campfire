"""Tests for the one-pass prediction of other sources' light (nirspec/profile_contam.py)
and the product columns that report it (stage3 model-rows and contamination columns).

Synthetic three-nod slitlets from test_nirspec_profile_fit. The trace-map geometry
and the per-pixel calibration (which need real rate files and jwst WCS) are
replaced by stand-ins; the bookkeeping of frames, partners, signs and units is
what is tested here.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_contam as CT
from campfire_pipeline.nirspec import stage3 as S3

import test_nirspec_profile_fit as T

NY, NX = T.NY, T.NX


def _source(sid, centre, scale):
    exps = T._exposures(src_y=0.0)
    for e in exps:
        e.name = e.name.replace('_1.fits', f'_{sid}.fits')
        e.origin = (1, 1)
    R = T._truth_result(exps, dict(centre=centre, drift=0.0, width=0.1, width_change=0.0), [0, 0, 0], scale)
    return exps, R


def _pass1_of(R, sid):
    frames = {}
    for j, e in enumerate(R.model.exps):
        img = PF.frame_image(R, j)
        good = np.isfinite(img) & (np.abs(img) > 0)
        idx = np.flatnonzero(good)
        frames[CT.exposure_key(e.name)] = dict(origin=np.array(e.origin), shape=np.array(e.y.shape),
                                               idx=idx, img=img.ravel()[idx], err=0.05 * np.abs(img.ravel()[idx]),
                                               cal=np.ones(idx.size))
    return CT.Pass1(sid=sid, lam=R.lam, flux=R.flux, flux_err=0.05 * R.flux, frames=frames)


@pytest.fixture
def stand_ins(monkeypatch):
    """Geometry: contaminant 2 lights every pixel of every nod; calibration 1 everywhere."""
    def geometry(exps, sid, maps, failed_open='flag', beyond='mask'):
        return CT.Geometry(mask=[np.ones(e.y.shape, bool) for e in exps],
                           sources=[{2: np.ones(e.y.shape, bool)} for e in exps],
                           unmodelled=[np.zeros(e.y.shape, bool) for e in exps])
    monkeypatch.setattr(CT, 'geometry', geometry)
    monkeypatch.setattr(CT, 'calibration', lambda e, rate_dir: np.ones(e.y.shape))


def test_frame_image_vs_model_frame():
    """The nod difference model is the frame images combined with +1 and -1/n."""
    exps, R = _source(1, 0.05, 10.0)
    for k in range(3):
        expect = sum(w * PF.frame_image(R, j) for j, w in PF.term_weights(exps, k))
        got = PF.model_frame(R, k, applied_pathloss=False, common=True)
        inside = np.ones(got.shape, bool)
        for j, _ in PF.term_weights(exps, k):
            inside &= (exps[j].wav > R.lam[0]) & (exps[j].wav < R.lam[-1])
        assert np.nanmax(np.abs(got - expect)[inside]) < 1e-9 * np.nanmax(np.abs(expect))


def test_prediction_respects_target_override(stand_ins):
    """Target nod 1 subtracted only nod 3 (A - C): the contaminant's light in nod 1's
    difference is its frame 1 minus its frame 3, whatever the contaminant's own partners."""
    t_exps, _ = _source(1, 0.0, 10.0)
    t_exps[0].partners = [2]
    c_exps, Rc = _source(2, 0.3, 5.0)
    P1 = _pass1_of(Rc, 2)
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    F = [PF.frame_image(Rc, j) for j in range(3)]
    np.testing.assert_allclose(P.pred[0], np.nan_to_num(F[0] - F[2]), atol=1e-9)
    np.testing.assert_allclose(P.pred[1], np.nan_to_num(F[1] - 0.5 * (F[0] + F[2])), atol=1e-9)
    assert 2 in P.contributors and not P.missing


def test_unreliable_contaminant_is_masked_not_subtracted(stand_ins):
    """A contaminant whose pass 1 may hold the target's light is never subtracted;
    its prediction only masks pixels above the noise threshold."""
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
    c_exps, Rc = _source(2, 0.3, 5.0)
    P1 = _pass1_of(Rc, 2)
    P1.reliable = False
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert P.unreliable == {2}
    assert np.all(P.pred[0] == 0) and not P.unmodelled[0].any()
    before = t_exps[0].diff.copy()
    CT.apply(t_exps, P, mode='hybrid', kappa=1.0)
    np.testing.assert_array_equal(t_exps[0].diff, before)
    sig = CT.diff_sigma(t_exps, 0)
    big = np.abs(P.pred_unreliable[0]) > sig
    assert big.any() and not t_exps[0].ok[big].any()


def test_mask_threshold_and_clean_fraction():
    exps, _ = _source(1, 0.0, 10.0)
    for e in exps:
        e.var = np.ones(e.y.shape)
    sig = CT.diff_sigma(exps, 0)
    pred = [np.zeros(e.y.shape) for e in exps]
    ms = CT.main_shutter(exps[0], 0.0)
    rows = np.unique(np.nonzero(ms)[0])
    block = np.zeros(exps[0].y.shape, bool)
    block[rows[: len(rows) // 2], :] = True
    pred[0][block] = 3.0 * sig[block]                  # above kappa = 1 sigma
    pred[0][~block] = 0.5 * sig[~block]                # below
    P = CT.Prediction(pred=pred, dpred=[0.1 * np.abs(p) for p in pred],
                      unmodelled=[np.zeros(e.y.shape, bool) for e in exps])
    before = exps[0].diff.copy()
    clean = CT.apply(exps, P, mode='mask', kappa=1.0)
    assert not exps[0].ok[block].any() and exps[0].ok[~block & ms].all()
    expect0 = (ms & ~block).sum() / ms.sum()
    assert clean['per_exposure'][0] == pytest.approx(expect0)
    assert clean['overall'] == pytest.approx((ms.sum() * 2 + (ms & ~block).sum()) / (3 * ms.sum()), rel=1e-6)
    np.testing.assert_array_equal(exps[0].diff, before)          # mask does not touch the data
    # hybrid: subtracts everywhere, masks only where the prediction itself is uncertain
    exps, _ = _source(1, 0.0, 10.0)
    for e in exps:
        e.var = np.ones(e.y.shape)
    before = exps[0].diff.copy()
    CT.apply(exps, P, mode='hybrid', kappa=1.0)
    np.testing.assert_allclose(exps[0].diff, before - pred[0])
    assert exps[0].ok[ms].all()                                   # dpred = 0.1 pred < 1 sigma everywhere


def test_smooth_spectrum_clips_outliers_and_averages():
    """Pass-1 spectra: isolated wild points (8649: 31082 uJy where the rest is ~0.1) and
    a short group of them are clipped; the running mean recovers the continuum and
    its error drops by ~sqrt(n)."""
    rng = np.random.default_rng(5)
    n = 600
    truth = 1.0 + 0.3 * np.sin(np.linspace(0, 3, n))
    err = np.full(n, 0.5)
    f = truth + err * rng.normal(size=n)
    f[100] = 3e4
    f[300:304] = -500.0
    f[450] = np.nan
    fs, es, clipped = CT.smooth_spectrum(f, err, n=31)
    assert clipped[100] and clipped[300:304].all() and clipped.sum() < 12
    ok = np.isfinite(fs)
    assert not ok[450] and ok.sum() == n - 1
    assert np.max(np.abs(fs - truth)[ok][20:-20]) < 0.5
    assert np.median(es[ok]) == pytest.approx(0.5 / np.sqrt(31), rel=0.1)
    f0, e0, c0 = CT.smooth_spectrum(f, err, n=0)                 # 0: full resolution, as before
    assert np.array_equal(f0, f, equal_nan=True) and not c0.any()


def test_missing_frame_masks_instead_of_skipping(stand_ins):
    """A contaminant whose pass 1 has no frame for an exposure that holds its light:
    its pixels in the differences that use that exposure are unmodelled. A frame
    absent because the contaminant is not in that exposure is no light, not missing."""
    t_exps, _ = _source(1, 0.0, 10.0)
    c_exps, Rc = _source(2, 0.3, 5.0)
    P1 = _pass1_of(Rc, 2)
    key = CT.exposure_key(t_exps[2].name)
    del P1.frames[key]
    P1.nosrc = set()
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert all(P.unmodelled[k].all() for k in range(3)) and P.missing_frame == {2}
    P1.nosrc = {key}                                       # the contaminant is not in that exposure
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert not any(u.any() for u in P.unmodelled) and not P.missing_frame


def test_beyond_range_masked_only_for_bright_contaminants(monkeypatch):
    """Pixels where a contaminant's trace runs past its nominal range are unmodelled
    when that contaminant's predicted light reaches the noise on the target's cutout,
    and left alone for a faint one (9334: faint contaminants' beyond-range blocks cost
    it its own fit)."""
    monkeypatch.setattr(CT, 'calibration', lambda e, rate_dir: np.ones(e.y.shape))
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
    half = np.zeros(t_exps[0].y.shape, bool)
    half[:, : NX // 2] = True

    def geometry(exps, sid, maps, failed_open='flag', beyond='mask'):
        return CT.Geometry(mask=[np.ones(e.y.shape, bool) for e in exps], sources=[{2: half.copy()} for e in exps],
                           unmodelled=[np.zeros(e.y.shape, bool) for e in exps],
                           beyond_sources=[{2: ~half} for e in exps])
    monkeypatch.setattr(CT, 'geometry', geometry)
    for scale, expect in ((50.0, True), (1e-4, False)):
        c_exps, Rc = _source(2, 0.3, scale)
        P1 = _pass1_of(Rc, 2)
        P1.nosrc = set()
        P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
        assert bool(P.unmodelled[0][:, NX // 2:].all()) == expect, scale


def test_failed_open_is_flag_only_by_default():
    """Failed-open shutters: their sky cancels in the nod difference, so they are
    recorded (flag) and not masked unless asked (``failed_open = 'mask'``)."""
    from campfire_pipeline.nirspec import trace_overlap as TO

    class E:
        def __init__(self, k):
            self.name = f'jw00000001001_03101_0000{k + 1}_nrs1_1.fits'
            self.group, self.y, self.origin = 0, np.zeros((NY, NX)), (1, 1)
            self.partners = [j for j in range(3) if j != k]
    exps = [E(k) for k in range(3)]
    shared = TO.Shared(exposure=0, from_exposure=0, sign=1, name='fo', source_id=0, kind='failed_open',
                       iy=np.array([10, 11]), ix=np.array([5, 5]), target_src=np.array([True, True]))
    orig = TO.nod_contamination
    try:
        TO.nod_contamination = lambda *a, **k: [shared]
        mp = {CT.exposure_key(e.name): None for e in exps}
        g = CT.geometry(exps, 1, mp)
        assert g.failed_open[0][10, 5] and not g.unmodelled[0].any() and not g.mask[0].any()
        g = CT.geometry(exps, 1, mp, failed_open='mask')
        assert g.unmodelled[0][10, 5] and g.mask[0][10, 5]
    finally:
        TO.nod_contamination = orig


def test_pair_flag_needs_light_in_the_extraction(stand_ins):
    """Similar pass-1 brightness alone does not make a pair: the contaminant's light in
    the target's extraction must reach ``min_frac`` of the target's own."""
    t_exps, Rt = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
    own = _pass1_of(Rt, 1)
    for centre, expect in ((0.0, 1), (2.3, 0)):           # on the target's rows / two shutters away
        c_exps, Rc = _source(2, centre, 10.0)
        P1 = _pass1_of(Rc, 2)
        P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.', own_pass1=own)
        assert P.pair_candidates and P.pair_candidates[0][0] == 2
        CT.pair_flag(P, Rt, t_exps, min_frac=0.3)
        assert P.pair == expect, (centre, P.pair_frac)


def test_contam_light_ratio_scales_with_contaminant():
    t_exps, Rt = _source(1, 0.0, 10.0)
    pred = [PF.model_frame(Rt, k, applied_pathloss=False, common=True) * 2.0 for k in range(3)]
    P = CT.Prediction(pred=[np.nan_to_num(p) for p in pred], dpred=[np.zeros(e.y.shape) for e in t_exps],
                      unmodelled=[np.zeros(e.y.shape, bool) for e in t_exps])
    assert CT.contam_light_ratio(P, Rt, t_exps) == pytest.approx(2.0, rel=1e-6)


def test_model_rows_extraction_uses_negative_copies():
    rng = np.random.default_rng(3)
    ny, nx = 30, 200
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2)
    P = prof - 0.5 * np.exp(-0.5 * ((rows - 10) / 1.2) ** 2) - 0.5 * np.exp(-0.5 * ((rows - 20) / 1.2) ** 2)
    P = P * np.ones((1, nx)) / prof.sum()
    flux = 5.0
    err = np.ones((ny, nx))
    data = flux * P + rng.normal(size=(ny, nx))
    f, e, sel = S3.model_rows_extraction(P, data, err, None, frac=0.02)
    assert np.mean(f) == pytest.approx(flux, abs=4 * np.mean(e) / np.sqrt(nx))
    np.testing.assert_allclose(e, 1.0 / np.sqrt(np.sum(np.where(sel, P, 0) ** 2, axis=0)))
    assert np.std((f - flux) / e) == pytest.approx(1.0, abs=0.15)
    # the copies carry information: the error is smaller than from the positive rows alone
    fa, ea, _ = S3.model_rows_extraction(np.where(P > 0, P, 0.0), data, err, None, frac=0.02)
    assert np.median(e) < np.median(ea)


def test_contam2d_fraction_recovers_injected_contaminant(tmp_path):
    ny, nx = 30, 100
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    P = prof / 1e12                                       # PROF2D: MJy per uJy
    sci_uJy = 10.0 * prof                                 # a 10 uJy source
    contam_MJy = 0.1 * sci_uJy / 1e12                     # predicted other-source light: 10 % of it
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(P, name='PROF2D'), fits.ImageHDU(contam_MJy, name='CONTAM2D')]
                 ).writeto(path)
    err = np.full((ny, nx), 0.01)
    out = S3._profile_fit_columns(str(path), sci_uJy.copy(), err, np.zeros((ny, nx), bool), 12, 18,
                                  np.linspace(1, 5, nx), rows='aperture', contam_1d='report')
    c = out['columns']
    np.testing.assert_allclose(c['fnu_prof'][0], 10.0, rtol=1e-6)
    np.testing.assert_allclose(c['fnu_contam'][0], 1.0, rtol=1e-6)
    assert out['header']['PFCONTAM'] == pytest.approx(0.1, rel=1e-6)
    assert out['header']['PFROWS'] == 'aperture'
    assert (out['choice'], out['reason']) == ('profile', 'contam_report')
    assert out['header']['EXTRACT'] == 'profile' and out['header']['CONTSTAT'] == 'predicted'
    assert [h.name for h in out['hdus']] == ['PROF2D', 'CONTAM2D']
    m = S3._profile_fit_columns(str(path), sci_uJy.copy(), err, np.zeros((ny, nx), bool), 12, 18,
                                np.linspace(1, 5, nx), rows='model', contam_1d='report')
    np.testing.assert_allclose(m['columns']['fnu_prof'][0], 10.0, rtol=1e-6)
    # subtract (default): the best spectrum is the decontaminated flux
    s = S3._profile_fit_columns(str(path), sci_uJy.copy(), err, np.zeros((ny, nx), bool), 12, 18,
                                np.linspace(1, 5, nx), rows='aperture')
    assert (s['choice'], s['reason']) == ('profile_decontam', 'contam_significant') and s['header']['PFCONSIG'] > 3
    cs = s['columns']
    fnu, fnu_err = S3.best_spectrum(s['choice'], cs['fnu_prof'][0], cs['fnu_prof_err'][0], cs['fnu_contam'][0],
                                    None, None)
    np.testing.assert_allclose(fnu, 9.0, rtol=1e-6)
    np.testing.assert_allclose(cs['fnu_prof'][0], 10.0, rtol=1e-6)
    np.testing.assert_array_equal(fnu_err, cs['fnu_prof_err'][0])     # statistical error only


def test_unmodelled_columns_flagged(tmp_path):
    """Columns where other light the prediction does not hold (UNMOD2D) reaches the
    extraction rows: listed as wavelength ranges (UNPRED), the target's light there as
    UNPREDF, PFCONTAM leaves them out; fnu_contam keeps what is predicted there."""
    ny, nx = 30, 100
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    sci_uJy = 10.0 * prof
    contam = 0.1 * sci_uJy / 1e12
    unmod = np.zeros((ny, nx))
    unmod[14:17, 80:] = 1.0                               # past 80: unpredicted light on the trace
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(prof / 1e12, name='PROF2D'), fits.ImageHDU(contam, name='CONTAM2D'),
                  fits.ImageHDU(0.1 * contam, name='CONTERR2D'), fits.ImageHDU(unmod, name='UNMOD2D')]).writeto(path)
    sci = sci_uJy.copy()
    sci[:, 80:] += 5.0 * prof[:, 80:]                     # the unpredicted light is in the data
    wave = np.linspace(1, 5, nx)
    out = S3._profile_fit_columns(str(path), sci, np.full((ny, nx), 0.01), np.zeros((ny, nx), bool), 12, 18,
                                  wave, rows='aperture', contam_1d='report', unmod_max=0.05)
    c = out['columns']
    assert np.isfinite(c['fnu_contam'][0]).all()
    assert out['header']['PFCONTAM'] == pytest.approx(0.1, rel=1e-6)
    assert out['header']['PFUNMODF'] == pytest.approx(0.2)
    np.testing.assert_allclose(c['fnu_contam_err'][0][:80], 0.1, rtol=1e-6)
    assert out['header']['CONTSTAT'] == 'partial'
    # light-weighted share of the columns past 80 (flux 15 there, 10 before), times the weight
    # fraction (P^2 / V on the aperture rows 13-17) on the unpredicted pixels
    u = np.sum(prof[14:17, 0] ** 2) / np.sum(prof[13:18, 0] ** 2)
    assert out['header']['UNPREDF'] == pytest.approx(20 * 15.0 * u / (80 * 10.0 + 20 * 15.0), rel=1e-6)
    names = [h.name for h in out['hdus']]
    assert names == ['PROF2D', 'CONTAM2D', 'UNPRED']        # CONTERR2D / UNMOD2D stay in the _pf.fits
    t = out['hdus'][2].data
    assert len(t) == 1 and t['wave_min'][0] == pytest.approx(wave[80]) and t['wave_max'][0] == pytest.approx(wave[-1])


def test_nothing_predicted_is_nan_and_no_unpredf(tmp_path):
    """Columns where no other light is predicted: fnu_contam is NaN there; without
    another source on the trace UNPREDF is not written."""
    ny, nx = 30, 100
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    contam = np.zeros((ny, nx))
    path = tmp_path / 'x_pf.fits'
    hdr = fits.Header()
    hdr['PFNCONT'] = 0
    fits.HDUList([fits.PrimaryHDU(header=hdr), fits.ImageHDU(prof / 1e12, name='PROF2D'),
                  fits.ImageHDU(contam, name='CONTAM2D')]).writeto(path)
    out = S3._profile_fit_columns(str(path), 10.0 * prof, np.full((ny, nx), 0.01), np.zeros((ny, nx), bool), 12, 18,
                                  np.linspace(1, 5, nx))
    assert np.isnan(out['columns']['fnu_contam'][0]).all()
    assert (out['choice'], out['reason']) == ('profile', 'contam_none')
    assert out['header']['CONTSTAT'] == 'none' and 'UNPREDF' not in out['header']
    assert [h.name for h in out['hdus']] == ['PROF2D', 'CONTAM2D']


def test_insignificant_prediction_is_not_subtracted(tmp_path):
    ny, nx = 30, 100
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    contam = np.zeros((ny, nx))
    contam[:, 50] = 1e-4 * prof[:, 50] / 1e12               # far below the integrated noise
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(prof / 1e12, name='PROF2D'),
                  fits.ImageHDU(contam, name='CONTAM2D')]).writeto(path)
    out = S3._profile_fit_columns(str(path), 10.0 * prof, np.full((ny, nx), 0.01), np.zeros((ny, nx), bool), 12, 18,
                                  np.linspace(1, 5, nx))
    assert (out['choice'], out['reason']) == ('profile', 'contam_insignificant')
    assert out['header']['PFCONSIG'] < 3
    c = out['columns']['fnu_contam'][0]
    assert np.isfinite(c[50]) and np.isnan(np.delete(c, 50)).all()


def test_no_prediction_is_contam_off(tmp_path):
    ny, nx = 30, 100
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(prof / 1e12, name='PROF2D')]).writeto(path)
    out = S3._profile_fit_columns(str(path), 10.0 * prof, np.full((ny, nx), 0.01), np.zeros((ny, nx), bool), 12, 18,
                                  np.linspace(1, 5, nx))
    assert (out['choice'], out['reason']) == ('profile', 'contam_off') and out['header']['CONTSTAT'] == 'off'
    assert np.isnan(out['columns']['fnu_contam'][0]).all()


def test_fallback_pf_file_gives_the_boxcar(tmp_path):
    """A ``_pf.fits`` holding only a fallback reason: the best spectrum is the 3-pixel
    boxcar, the reason is carried, fnu_prof / fnu_contam are NaN."""
    from campfire_pipeline.nirspec import profile_product as PP
    nx = 50
    path = tmp_path / 'x_pf.fits'
    PP.write_fallback(str(path), 'no_usable_pixels')
    out = S3._profile_fit_columns(str(path), np.zeros((30, nx)), np.ones((30, nx)), np.zeros((30, nx), bool),
                                  12, 18, np.linspace(1, 5, nx))
    assert (out['choice'], out['reason']) == ('boxcar3', 'no_usable_pixels')
    assert out['header']['EXTRACT'] == 'boxcar3' and out['header']['EXTRWHY'] == 'no_usable_pixels'
    assert out['hdus'] == []
    assert np.isnan(out['columns']['fnu_prof'][0]).all() and np.isnan(out['columns']['fnu_contam'][0]).all()
    box, box_err = np.arange(nx, dtype=float), np.ones(nx)
    f, e = S3.best_spectrum(out['choice'], out['columns']['fnu_prof'][0], out['columns']['fnu_prof_err'][0],
                            out['columns']['fnu_contam'][0], box, box_err)
    assert f is box and e is box_err


def test_profile_shape_mismatch_raises(tmp_path):
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.ones((20, 40)), name='PROF2D')]).writeto(path)
    with pytest.raises(ValueError, match='does not match'):
        S3._profile_fit_columns(str(path), np.zeros((30, 50)), np.ones((30, 50)), np.zeros((30, 50), bool),
                                12, 18, np.linspace(1, 5, 50))


@pytest.mark.parametrize('have,pred,csig,mode,expect', [
    (False, False, 0.0, 'subtract', ('profile', 'contam_off')),
    (True, False, 0.0, 'subtract', ('profile', 'contam_none')),
    (True, True, 10.0, 'report', ('profile', 'contam_report')),
    (True, True, 3.5, 'subtract', ('profile_decontam', 'contam_significant')),
    (True, True, 3.0, 'subtract', ('profile', 'contam_insignificant')),
])
def test_choose_extraction(have, pred, csig, mode, expect):
    assert S3.choose_extraction(have, pred, csig, mode, 3.0) == expect
    assert expect[0] in S3.EXTRACTIONS and expect[1] in S3.EXTRACT_REASONS


def test_best_spectrum_error_is_statistical_only():
    prof, err = np.array([10.0, 10.0, np.nan]), np.array([1.0, 1.0, 1.0])
    contam = np.array([2.0, np.nan, 2.0])
    f, e = S3.best_spectrum('profile_decontam', prof, err, contam, None, None)
    np.testing.assert_array_equal(f, [8.0, 10.0, np.nan])       # nothing predicted: nothing subtracted
    np.testing.assert_array_equal(e, err)
    f, e = S3.best_spectrum('profile', prof, err, contam, None, None)
    np.testing.assert_array_equal(f, prof)
    with pytest.raises(ValueError):
        S3.best_spectrum('optimal', prof, err, contam, None, None)


def test_unpredicted_ranges_merge():
    wave = np.linspace(1.0, 2.0, 11)
    flag = np.zeros(11, bool)
    flag[[2, 3, 7]] = True
    t = S3.unpredicted_ranges(wave, flag, np.linspace(0, 1, 11)).data
    np.testing.assert_allclose(t['wave_min'], [1.2, 1.7])
    np.testing.assert_allclose(t['wave_max'], [1.3, 1.7])
    np.testing.assert_allclose(t['weight_max'], [0.3, 0.7])
    m = S3._merge_ranges([t, S3.unpredicted_ranges(wave, np.r_[np.zeros(3, bool), np.ones(2, bool),
                                                               np.zeros(6, bool)]).data]).data
    np.testing.assert_allclose(m['wave_min'], [1.2, 1.7])
    np.testing.assert_allclose(m['wave_max'], [1.4, 1.7])


def test_smooth_spectrum_keeps_coherent_lines():
    """A contaminant's emission line resolved over a few samples survives the clipping
    (it is light to predict); a single wild point does not."""
    rng = np.random.default_rng(7)
    n = 400
    err = np.full(n, 0.1)
    f = 1.0 + err * rng.normal(size=n)
    f[200:203] += np.array([3.0, 6.0, 3.0])                  # a line over three samples
    f[100] = 50.0                                            # one wild point
    fs, es, clipped = CT.smooth_spectrum(f, err, n=31, keep_lines=True)
    np.testing.assert_allclose(fs[200:203], f[200:203])
    assert clipped[100] and not clipped[200:203].any() and abs(fs[100] - 1.0) < 0.1
    fs0, _, c0 = CT.smooth_spectrum(f, err, n=31)
    assert c0[200:203].all() and fs0[201] < 1.5             # without it the line is smeared out


def test_smooth_points_from_resolution_elements():
    assert CT.smooth_points(15) == 31 and CT.smooth_points(7.5) == 15 and CT.smooth_points(0) == 0
    s = CT.pass1_settings({'contam_pass1_smooth_prism': 5, 'contam_pass1_smooth_grating': 15})
    assert s['smooth_prism'] == 11 and s['smooth_grating'] == 31
    s = CT.pass1_settings({'contam_pass1_smooth': 21})              # the round-2 knob overrides both
    assert s['smooth_prism'] == s['smooth_grating'] == 21


def _covered_pass1(t_exps, Rt, c_offset, c_scale=0.0):
    """The pass 1 of a contaminant (sid 2, centre ``c_offset`` in the same slitlet) whose
    trace the target covers: solved unmasked over data holding the target's light (and
    its own, ``c_scale``), with the solve's design stored as ``compute_pass1`` does."""
    import copy
    exps = copy.deepcopy(t_exps)
    shape = dict(centre_offset=c_offset, drift=0.0, width=0.1, width_change=0.0)
    Rc_true = T._truth_result(exps, dict(centre=c_offset, drift=0.0, width=0.1, width_change=0.0), [0, 0, 0], c_scale)
    for k, e in enumerate(exps):
        e.diff = (PF.model_frame(Rt, k, applied_pathloss=False, common=True)
                  + PF.model_frame(Rc_true, k, applied_pathloss=False, common=True))
    M = PF.ProfileModel(exps, chromatic=True)
    R = PF.fit_fixed(model=M, shape=shape, use=np.ones(M.npix, bool))
    frames = {}
    for j, e in enumerate(exps):
        img = PF.frame_image(R, j)
        prof = PF.frame_image(R, j, spectrum=np.ones(R.lam.size))
        idx = np.flatnonzero(np.isfinite(img) & (np.abs(img) > 0))
        frames[CT.exposure_key(e.name)] = dict(origin=np.array(e.origin), shape=np.array(e.y.shape), idx=idx,
                                               img=img.ravel()[idx], err=0.05 * np.abs(img.ravel()[idx]),
                                               cal=np.ones(idx.size), prof=prof.ravel()[idx],
                                               plam=e.wav.ravel()[idx].astype(np.float32))
    P1 = CT.Pass1(sid=2, lam=R.lam, flux=R.flux, flux_err=R.flux_err, frames=frames, reliable=False, nosrc=set())
    P1.flux_raw, P1.err_raw = R.flux.copy(), R.flux_err.copy()
    P1.nband, P1.n_smooth, P1.keep_lines = CT.normal_band(R), 0, True
    return P1, Rc_true


def test_echo_of_the_target_is_taken_out_of_an_unreliable_contaminant(stand_ins):
    """5301 / 8427: a contaminant whose trace the target covers is solved over the
    target's pixels; its pass 1 then holds the target's light, and predicted back it
    lands on the target's own light (its lines masked as contamination). With the
    echo taken out, a dark contaminant predicts nothing, and a real one its own light."""
    t_exps, Rt = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.full(e.y.shape, 1e-4)
    own = _pass1_of(Rt, 1)
    P1c, _ = _covered_pass1(t_exps, Rt, 0.3)
    assert P1c.has_design()
    assert np.nanmedian(np.abs(P1c.flux_raw)) > 0.05 * np.nanmedian(Rt.flux)     # the echo is there
    kw = dict(own_pass1=own)
    Pn = CT.predict(t_exps, 1, {'x': None}, lambda s: P1c if s == 2 else None, '.', echo=False, **kw)
    Pe = CT.predict(t_exps, 1, {'x': None}, lambda s: P1c if s == 2 else None, '.', **kw)
    assert Pe.echoed == {2} and not Pn.echoed and Pe.unreliable == {2}
    big = max(np.nanmax(np.abs(p)) for p in Pn.pred_unreliable)
    left = max(np.nanmax(np.abs(p)) for p in Pe.pred_unreliable)
    assert left < 1e-3 * big
    # a real contaminant keeps its light (the target's own pass 1 is clean here)
    P1r, Rc = _covered_pass1(t_exps, Rt, 0.3, c_scale=2.0)
    Pr = CT.predict(t_exps, 1, {'x': None}, lambda s: P1r if s == 2 else None, '.', **kw)
    truth = [np.nan_to_num(sum(w * PF.frame_image(Rc, j) for j, w in PF.term_weights(t_exps, k))) for k in range(3)]
    for k in range(3):
        np.testing.assert_allclose(Pr.pred_unreliable[k], truth[k], atol=1e-3 * np.max(np.abs(truth[k])))
    # without the target's own pass 1 nothing is corrected
    P0 = CT.predict(t_exps, 1, {'x': None}, lambda s: P1c if s == 2 else None, '.')
    assert not P0.echoed
    # nor when the target's own pass 1 failed the sanity guard (its level is not its light)
    own.why = 'guard'
    P0 = CT.predict(t_exps, 1, {'x': None}, lambda s: P1c if s == 2 else None, '.', own_pass1=own)
    assert not P0.echoed


def test_second_round_reads_first_round_results_only(tmp_path):
    """Pass 1b subtracts only first-round reliable results: a file another pass 1b has
    already rewritten (reliable or not) is not used, whatever the finishing order."""
    d = tmp_path / CT.PASS1_SUBDIR
    d.mkdir()
    lam = np.linspace(1, 2, 5)
    for name, rnd, rel in (('a', '1', True), ('b', '2', True), ('c', '1', False)):
        np.savez(d / f'{name}_pass1.npz', sid=1, lam=lam, flux=lam, flux_err=lam, keys=np.array([], dtype=str),
                 nosrc=np.array([], dtype=str), reliable=rel, round=rnd)
    assert CT.first_round_reliable(str(tmp_path), 'a') is not None
    assert CT.first_round_reliable(str(tmp_path), 'b') is None
    assert CT.first_round_reliable(str(tmp_path), 'c') is None
    assert CT.first_round_reliable(str(tmp_path), 'zz') is None and CT.first_round_reliable(str(tmp_path), None) is None


def test_unpredictable_gap_by_continuity():
    """Mapped contaminant pixels its pass 1 does not reach (8427's blue end, no flat
    field below ~2.88 um): bounded by its light on the nearest reached pixels of the
    row within ncol columns; unpredictable when farther, or when that light is bright."""
    ny, nx = 4, 100
    sig = np.ones((ny, nx))
    covered = np.zeros((ny, nx), bool)
    covered[:, :40] = covered[:, 50:60] = True
    add = np.zeros((ny, nx))
    add[:, :40], add[:, 50:60] = 0.3, 0.5
    gap = ~covered
    out = CT.unpredictable_gap(gap, covered, add, sig, ncol=31)
    assert not out[:, 40:50].any()                         # interpolated 0.3-0.5 sigma: faint
    assert not out[:, 60:91].any() and out[:, 91:].all()   # extended 31 columns past the last reached one
    out = CT.unpredictable_gap(gap, covered, 5 * add, sig, ncol=31)
    assert out[:, 40:50].all()                             # bright: stays masked
    assert CT.unpredictable_gap(gap, np.zeros((ny, nx), bool), add, sig, ncol=31)[gap].all()


def test_gap_masked_only_where_unpredictable(stand_ins):
    """In predict: a stretch of mapped pixels a bright contaminant's frames do not
    reach is left alone when its light around it is below the noise
    (``gap_columns`` > 0), and masked whole with the rule before (0)."""
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
        e.var[:, 30:70] = 1e8                                # the light around the gap is far below the noise
    c_exps, Rc = _source(2, 0.3, 50.0)                       # bright elsewhere
    P1 = _pass1_of(Rc, 2)
    P1.nosrc = set()
    for fr in P1.frames.values():
        col = fr['idx'] % NX
        keep = (col < 45) | (col >= 55)                      # its pass 1 has no value there
        for n in ('idx', 'img', 'err', 'cal'):
            fr[n] = fr[n][keep]
    gap = np.zeros(t_exps[0].y.shape, bool)
    gap[:, 45:55] = True
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert not P.unmodelled[0][gap].any()
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.', gap_columns=0)
    assert P.unmodelled[0][gap].all()


def test_contaminant_pixels_without_calibration_bounded_by_continuity(stand_ins):
    """8427 / 6723 on 5301: the contaminant's first pass-1 samples above its filter's
    cut-on sit on pixels with no flat (its calibration NaN): judged by continuity like
    the unreached pixels; a target pixel without calibration stays unmodelled."""
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
        e.var[:, 30:70] = 1e8
    c_exps, Rc = _source(2, 0.3, 50.0)
    P1 = _pass1_of(Rc, 2)
    for fr in P1.frames.values():
        col = fr['idx'] % NX
        fr['cal'] = np.where((col >= 48) & (col < 51), np.nan, fr['cal'])
    blk = np.zeros(t_exps[0].y.shape, bool)
    blk[:, 48:51] = True
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert not P.unmodelled[0][blk].any()
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.', gap_columns=0)
    assert P.unmodelled[0][blk & (np.abs(PF.frame_image(Rc, 0)) > 0)].all()


def test_continuity_ignores_covered_pixels_without_calibration(stand_ins):
    """2976 on 5215: the contaminant's last columns carry no calibration (no light in the
    prediction), and its mapped trace continues past them. Those columns must not
    bound the gap beyond with zero light: the gap next to bright light stays masked."""
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.full(e.y.shape, 1e-4)
    c_exps, Rc = _source(2, 0.3, 50.0)
    P1 = _pass1_of(Rc, 2)
    P1.nosrc = set()
    for fr in P1.frames.values():
        col = fr['idx'] % NX
        keep = col < 100                                       # its pass 1 stops at column 100 ...
        for n in ('idx', 'img', 'err', 'cal'):
            fr[n] = fr[n][keep]
        col = fr['idx'] % NX
        fr['cal'] = np.where(col >= 97, np.nan, fr['cal'])     # ... and its last 3 columns have no flat
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    bright = np.abs(np.nan_to_num(PF.frame_image(Rc, 0)))[:, 95] > 0.05 * np.nanmax(np.abs(PF.frame_image(Rc, 0)))
    assert bright.any()
    assert P.unmodelled[0][bright, 100:110].all()
