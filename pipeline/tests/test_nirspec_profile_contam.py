"""Tests for the one-pass prediction of other sources' light (nirspec/profile_contam.py)
and the product columns that report it (stage3 model-rows and contamination columns).

Synthetic three-nod slitlets from test_nirspec_profile_fit. The trace-map geometry
and the per-pixel calibration (which need real rate files and jwst WCS) are
replaced by stand-ins; the bookkeeping of frames, partners, signs and units is
what is tested here.
"""

import os

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
    """Geometry: contaminant 2 lights every pixel of every nod, from every frame;
    calibration 1 everywhere."""
    def geometry(exps, sid, maps):
        return CT.Geometry(mask=[np.ones(e.y.shape, bool) for e in exps],
                           sources=[{2: np.ones(e.y.shape, bool)} for e in exps],
                           lit_from=[{2: set(range(len(exps)))} for e in exps])
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
    assert 2 in P.contributors


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
    assert np.all(P.pred[0] == 0)
    before = t_exps[0].diff.copy()
    CT.apply(t_exps, P, kappa=1.0)
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
    pred[0][block] = 3.0 * sig[block]                  # above 1 sigma: "dirty"
    pred[0][~block] = 0.5 * sig[~block]                # below
    dpred = [0.1 * np.abs(p) for p in pred]
    dpred[0][block] = 2.0 * sig[block]                 # the prediction's own uncertainty above kappa = 1 sigma
    P = CT.Prediction(pred=pred, dpred=dpred)
    before = exps[0].diff.copy()
    clean = CT.apply(exps, P, kappa=1.0)
    # subtracted everywhere, dropped only where the prediction itself is uncertain
    np.testing.assert_allclose(exps[0].diff, before - pred[0])
    assert not exps[0].ok[block].any() and exps[0].ok[~block & ms].all()
    expect0 = (ms & ~block).sum() / ms.sum()
    assert clean['per_exposure'][0] == pytest.approx(expect0)
    assert clean['overall'] == pytest.approx((ms.sum() * 2 + (ms & ~block).sum()) / (3 * ms.sum()), rel=1e-6)
    assert clean['clean_pred_exposure'][0] == pytest.approx(expect0)


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


def test_missing_pass1_or_frame_is_an_error(stand_ins):
    """Every source the maps name has a pass 1: a contaminant without one, or without
    the frame of an exposure whose light the maps put on the target, is an error, not
    a pixel to mask. A frame absent because the contaminant is not in that exposure
    (``nosrc``) is no light; one the maps put no light from is not needed."""
    t_exps, _ = _source(1, 0.0, 10.0)
    c_exps, Rc = _source(2, 0.3, 5.0)
    with pytest.raises(CT.MissingContaminant, match='no pass 1'):
        CT.predict(t_exps, 1, {'x': None}, lambda s: None, '.')
    P1 = _pass1_of(Rc, 2)
    key = CT.exposure_key(t_exps[2].name)
    del P1.frames[key]
    P1.nosrc = set()
    with pytest.raises(CT.MissingContaminant, match='no frame'):
        CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    P1.nosrc = {key}                                       # the contaminant is not in that exposure
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert 2 in P.contributors
    # a contaminant modelled elsewhere (``exclude``: pass 1b, the joint solve) needs none
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: None, '.', exclude={2})
    assert all(not p.any() for p in P.pred)


def test_missing_frame_from_an_unlit_exposure_is_not_needed(monkeypatch):
    """The maps put the contaminant's light on the target only from exposure 0 (its
    source shutter in the other nods misses the target): a missing frame of exposure 2
    is not an error, and the prediction comes from the frames there are."""
    monkeypatch.setattr(CT, 'calibration', lambda e, rate_dir: np.ones(e.y.shape))
    monkeypatch.setattr(CT, 'geometry', lambda exps, sid, maps: CT.Geometry(
        mask=[np.ones(e.y.shape, bool) for e in exps], sources=[{2: np.ones(e.y.shape, bool)} for e in exps],
        lit_from=[{2: {0}} for e in exps]))
    t_exps, _ = _source(1, 0.0, 10.0)
    c_exps, Rc = _source(2, 0.3, 5.0)
    P1 = _pass1_of(Rc, 2)
    del P1.frames[CT.exposure_key(t_exps[2].name)]
    P1.nosrc = set()
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    F = [PF.frame_image(Rc, j) for j in range(3)]
    np.testing.assert_allclose(P.pred[0], np.nan_to_num(F[0] - 0.5 * F[1]), atol=1e-9)


def test_pixels_without_calibration_get_no_light(monkeypatch, stand_ins):
    """A frame pixel with no calibration on either side (the contaminant's flats stop, or
    the detector pixel is unusable) gets no predicted light and is not masked: the
    prediction there is zero and the fit keeps the pixel."""
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
    c_exps, Rc = _source(2, 0.3, 5.0)
    P1 = _pass1_of(Rc, 2)
    blk = np.zeros(t_exps[0].y.shape, bool)
    blk[:, :20] = True
    for fr in P1.frames.values():
        fr['cal'] = np.where(blk.ravel()[fr['idx']], np.nan, 1.0)
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert not P.pred[0][blk].any() and P.pred[0][~blk].any()
    ok = t_exps[0].ok.copy()
    CT.apply(t_exps, P, kappa=1.0)
    assert np.array_equal(t_exps[0].ok[blk], ok[blk])


def test_failed_open_is_recorded_only():
    """Failed-open shutters: their sky cancels in the nod difference, so they are
    recorded and never masked."""
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
        assert g.failed_open[0][10, 5] and not g.mask[0].any() and not g.sources[0]
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
    P = CT.Prediction(pred=[np.nan_to_num(p) for p in pred], dpred=[np.zeros(e.y.shape) for e in t_exps])
    assert CT.contam_light_ratio(P, Rt, t_exps) == pytest.approx(2.0, rel=1e-6)


def test_contam2d_fraction_recovers_injected_contaminant(tmp_path):
    """Other light at 10 % of the target: fnu_contam measures it and the best spectrum
    stays fnu_prof, nothing subtracted."""
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
                                  np.linspace(1, 5, nx))
    c = out['columns']
    np.testing.assert_allclose(c['fnu_prof'][0], 10.0, rtol=1e-6)
    np.testing.assert_allclose(c['fnu_contam'][0], 1.0, rtol=1e-6)
    assert out['header']['PFCONTAM'] == pytest.approx(0.1, rel=1e-6) and out['header']['PFCONSIG'] > 3
    assert (out['choice'], out['reason']) == ('profile', 'contam_predicted')
    assert out['header']['EXTRACT'] == 'profile' and out['header']['CONTSTAT'] == 'predicted'
    assert [h.name for h in out['hdus']] == ['PROF2D', 'CONTAM2D']
    fnu, fnu_err = S3.best_spectrum(out['choice'], c['fnu_prof'][0], c['fnu_prof_err'][0], None, None)
    np.testing.assert_allclose(fnu, 10.0, rtol=1e-6)
    np.testing.assert_array_equal(fnu_err, c['fnu_prof_err'][0])


def test_nothing_predicted_is_nan(tmp_path):
    """Columns where no other light is predicted: fnu_contam is NaN there."""
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
    assert out['header']['CONTSTAT'] == 'none'
    assert [h.name for h in out['hdus']] == ['PROF2D', 'CONTAM2D']


def test_not_separable_wavelengths_are_nan(tmp_path):
    """The source's joint pass 1 says its light cannot be told apart from a neighbour's
    (NOTSEP): fnu_contam is NaN there, never a number."""
    ny, nx = 30, 100
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    wave = np.linspace(1, 5, nx)
    lam = np.linspace(0.9, 5.1, 400)
    ns = (lam > 2.0) & (lam < 3.0)
    tab = fits.BinTableHDU.from_columns([fits.Column(name='lam', format='D', array=lam),
                                         fits.Column(name='notsep', format='L', array=ns)], name='NOTSEP')
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(prof / 1e12, name='PROF2D'),
                  fits.ImageHDU(0.1 * 10.0 * prof / 1e12, name='CONTAM2D'), tab]).writeto(path)
    out = S3._profile_fit_columns(str(path), 10.0 * prof, np.full((ny, nx), 0.01), np.zeros((ny, nx), bool), 12, 18,
                                  wave)
    c = out['columns']['fnu_contam'][0]
    inside = (wave > 2.02) & (wave < 2.98)
    outside = (wave < 1.98) | (wave > 3.02)
    assert np.isnan(c[inside]).all()
    np.testing.assert_allclose(c[outside], 1.0, rtol=1e-6)
    assert out['header']['PFNOTSEP'] == pytest.approx(inside.mean(), abs=0.02)


def test_faint_prediction_is_reported_where_it_is(tmp_path):
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
    assert (out['choice'], out['reason']) == ('profile', 'contam_predicted')
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
                            box, box_err)
    assert f is box and e is box_err


def test_profile_shape_mismatch_raises(tmp_path):
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(np.ones((20, 40)), name='PROF2D')]).writeto(path)
    with pytest.raises(ValueError, match='does not match'):
        S3._profile_fit_columns(str(path), np.zeros((30, 50)), np.ones((30, 50)), np.zeros((30, 50), bool),
                                12, 18, np.linspace(1, 5, 50))


@pytest.mark.parametrize('have,pred,expect', [
    (False, False, ('profile', 'contam_off')),
    (True, False, ('profile', 'contam_none')),
    (True, True, ('profile', 'contam_predicted')),
])
def test_choose_extraction(have, pred, expect):
    assert S3.choose_extraction(have, pred) == expect
    assert expect[0] in S3.EXTRACTIONS and expect[1] in S3.EXTRACT_REASONS


def test_best_spectrum():
    prof, err = np.array([10.0, 10.0, np.nan]), np.array([1.0, 1.0, 1.0])
    f, e = S3.best_spectrum('profile', prof, err, None, None)
    np.testing.assert_array_equal(f, prof)
    np.testing.assert_array_equal(e, err)
    with pytest.raises(ValueError):
        S3.best_spectrum('optimal', prof, err, None, None)


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


def test_excluded_member_light_is_left_out(stand_ins):
    """A contaminant whose light is modelled elsewhere (a member of the target's joint
    first pass) is not predicted on the target (``exclude``)."""
    t_exps, _ = _source(1, 0.0, 10.0)
    c_exps, Rc = _source(2, 0.3, 5.0)
    P1 = _pass1_of(Rc, 2)
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.', exclude={2})
    assert all(np.all(p == 0) for p in P.pred) and 2 not in P.contributors and P.excluded == {2}


# ---------------------------------------------------------------------------
# light past a contaminant's cutout (profile_extend)
# ---------------------------------------------------------------------------
def test_merge_sparse_extension_replaces_uncalibrated_pixels():
    from campfire_pipeline.nirspec import profile_extend as PE
    a = dict(origin=np.array([1, 1]), shape=np.array([2, 4]), idx=np.array([0, 1, 2, 3]),
             img=np.array([1., 2., 3., 4.]), err=np.ones(4), cal=np.array([1., 1., np.nan, np.nan]))
    b = dict(origin=np.array([1, 3]), shape=np.array([1, 4]), idx=np.array([0, 1, 2, 3]),
             img=np.array([30., 40., 50., 60.]), err=np.ones(4), cal=np.ones(4), ext=np.ones(4, bool))
    m = PE.merge_sparse(a, b)
    assert tuple(m['origin']) == (1, 1) and tuple(m['shape']) == (1, 6)
    det = {int(i): (float(v), bool(x)) for i, v, x in zip(m['idx'], m['img'], m['ext'])}
    # columns 1-2 of a (calibrated) kept; 3-4 (no calibration) replaced by b; b's 5-6 added
    assert det[0] == (1.0, False) and det[1] == (2.0, False)
    assert det[2] == (30.0, True) and det[3] == (40.0, True) and det[5] == (60.0, True)


def test_extension_pixels_are_predicted(monkeypatch):
    """Pixels where a contaminant's trace runs past its nominal range are predicted from
    its pass 1 extended there (``ext`` pixels, count-rate units), converted with the
    target's calibration. Without an extension nothing is predicted there and nothing
    is masked: the maps follow every trace and every pass 1 is extended."""
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
    c_exps, Rc = _source(2, 0.3, 50.0)
    past = np.zeros(t_exps[0].y.shape, bool)
    past[:, 120:] = True
    monkeypatch.setattr(CT, 'geometry', lambda exps, sid, maps: CT.Geometry(
        mask=[np.ones(e.y.shape, bool) for e in exps], sources=[{2: np.ones(e.y.shape, bool)} for e in exps],
        lit_from=[{2: {0, 1, 2}} for e in exps]))
    monkeypatch.setattr(CT, 'calibration', lambda e, rate_dir: np.full(e.y.shape, 2.0))   # target: 2 per rate unit
    P1 = _pass1_of(Rc, 2)
    P1.nosrc = set()
    for fr in P1.frames.values():                        # in-range frames stop at column 120 ...
        col = fr['idx'] % NX
        x = col >= 120
        fr['ext'] = x                                    # ... past it the light is in count-rate units
        fr['cal'] = np.where(x, 1.0, 2.0)
        fr['img'] = np.where(x, fr['img'] / 2.0, fr['img'])
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    lit = past & (np.abs(np.nan_to_num(PF.frame_image(Rc, 0))) > 0)
    F = [PF.frame_image(Rc, j) for j in range(3)]
    expect = np.nan_to_num(F[0] - 0.5 * (F[1] + F[2]))
    np.testing.assert_allclose(P.pred[0][lit], expect[lit], rtol=1e-5, atol=1e-9)
    for fr in P1.frames.values():                        # without the extension
        keep = ~fr['ext']
        for n in ('idx', 'img', 'err', 'cal', 'ext'):
            fr[n] = fr[n][keep]
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert not P.pred[0][past].any()
    ok = t_exps[0].ok.copy()
    CT.apply(t_exps, P, kappa=1.0)
    assert np.array_equal(t_exps[0].ok[past], ok[past])


def test_step_timing_accumulates_and_reports():
    from campfire_pipeline.nirspec import profile_timing as TM
    TM.reset()
    for _ in range(2):
        with TM.step('a'):
            sum(range(10000))
    m = TM.start()
    TM.stop('b', m)
    s = TM.snapshot()
    assert s['a'][2] == 2 and s['b'][2] == 1 and s['a'][0] >= 0
    lines = []
    TM.report('product x', log=lines.append)
    assert lines[0].startswith('TIMING product x a=') and ' b=' in lines[0] and TM.snapshot() == {}
    with TM.phase('p', log=lines.append):
        pass
    assert lines[1].startswith('TIMING-PHASE p wall=') and 'cpu=' in lines[1]


# ---------------------------------------------------------------------------
# A contaminant whose own pass 1 falls back: predicted from its 3-pixel boxcar
# ---------------------------------------------------------------------------
NO_SMOOTH = {'contam_pass1_smooth': 0}


def _default_shape_source(sid, scale):
    """A source of the default shape (prior width, at its planned position) and the
    3-pixel boxcar stage 3 would measure of it: the calibrated frame (the frame over
    the point-source path loss) summed over the three rows around SRCYPOS, in uJy."""
    from campfire_pipeline.nirspec import profile_chain as PC
    w = PC.ChainConfig.from_stage_config({}).priors.width_median
    exps, R = _source(sid, 0.0, scale)
    R.params.update(centre=R.params['centre_prior'], width=w)
    r = NY // 2                                     # nod 1: SRCYPOS (0) at this row's centre
    cal = PF.frame_image(R, 0) / exps[0].pl_point
    box = np.nansum(cal[r - 1:r + 2], axis=0) * 1e12
    return R, (exps[0].wav[r].copy(), box, 0.01 * np.abs(box))


def _no_usable_pixels(sid):
    exps, _ = _source(sid, 0.0, 1.0)
    for e in exps:
        e.ok = np.zeros(e.y.shape, bool)
    return exps


def test_boxcar_pass1_is_the_boxcar_placed_with_the_default_shape(stand_ins):
    """A source with no usable pixel has no pass 1 of its own; its 3-pixel boxcar,
    divided by the part of the default shape's light the three rows hold, is its true
    spectrum, and placed with that shape it gives the frames a fit would."""
    Rc, box = _default_shape_source(2, 5.0)
    exps = _no_usable_pixels(2)
    with pytest.raises(PF.NoUsablePixels):
        PF.ProfileModel(exps)
    P1 = CT.boxcar_pass1(exps, 2, {}, '.', True, NO_SMOOTH, box)
    assert P1.why == 'boxcar' and P1.reliable and P1.spec_src == 'boxcar'
    mid = (Rc.lam > 1.2) & (Rc.lam < 4.6)
    np.testing.assert_allclose(np.interp(Rc.lam[mid], P1.lam, P1.flux), Rc.flux[mid], rtol=0.02)
    for j, e in enumerate(exps):
        fr = P1.frames[CT.exposure_key(e.name)]
        truth = PF.frame_image(Rc, j).ravel()[fr['idx']]
        inner = (e.wav.ravel()[fr['idx']] > 1.2) & (e.wav.ravel()[fr['idx']] < 4.6)   # grid ends: tilted rows
        big = inner & (np.abs(truth) > 0.05 * np.nanmax(np.abs(truth)))
        np.testing.assert_allclose(fr['img'][big], truth[big], rtol=0.02)


def test_boxcar_contaminant_is_subtracted_and_counted(stand_ins):
    """A boxcar pass 1 subtracts like a reliable one (its light was measured on its own
    trace), and the target's prediction lists it (``PFCBOX``)."""
    t_exps, _ = _source(1, 0.0, 10.0)
    Rc, box = _default_shape_source(2, 5.0)
    P1 = CT.boxcar_pass1(_no_usable_pixels(2), 2, {}, '.', True, NO_SMOOTH, box)
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    assert P.boxcar == {2} and not P.unreliable and 2 in P.contributors
    truth = np.nan_to_num(sum(w * PF.frame_image(Rc, j) for j, w in PF.term_weights(t_exps, 0)))
    big = (np.abs(truth) > 0.05 * np.max(np.abs(truth))) & (t_exps[0].wav > 1.2) & (t_exps[0].wav < 4.6)
    np.testing.assert_allclose(P.pred[0][big], truth[big], rtol=0.02)
    # a contaminant modelled elsewhere is not counted
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.', exclude={2})
    assert not P.boxcar


def test_missing_pass1_error_quotes_the_recorded_reason(stand_ins, tmp_path):
    """A source with no pass 1 (its own fell back and it has no boxcar either) leaves its
    reason beside the pass-1 files; a target its trace crosses fails with that reason."""
    ws = str(tmp_path)
    path = CT.missing_path(ws, 'obs_prism_3')
    os.makedirs(os.path.dirname(path))
    with open(path, 'w') as f:
        f.write('no_usable_pixels: no stage-3 s2d or spectrum to take its 3-pixel boxcar from\n')
    assert CT.missing_reason(ws, 'obs_prism_2') is None
    t_exps, _ = _source(1, 0.0, 10.0)
    with pytest.raises(CT.MissingContaminant, match='no stage-3 s2d'):          # the maps name source 2 here
        CT.predict(t_exps, 1, {'x': None}, lambda s: None, ws,
                   why_missing=lambda s: CT.missing_reason(ws, 'obs_prism_3'))


def test_spec3_boxcar3_is_stage3_fnu_3px(tmp_path):
    """The boxcar a pass 1 is taken from is stage 3's own: three s2d rows around the
    x1d aperture centre, summed, MJy -> uJy."""
    rng = np.random.default_rng(3)
    sci = rng.normal(size=(12, 40))
    wht = np.ones_like(sci)
    wht[5, 7] = 0                                         # a masked pixel is left out
    fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(sci, name='SCI'), fits.ImageHDU(np.ones_like(sci), name='ERR'),
                  fits.ImageHDU(wht, name='WHT')]).writeto(tmp_path / 'p_s2d.fits')
    wave = np.linspace(1, 2, 40)
    t = fits.BinTableHDU.from_columns([fits.Column(name='WAVELENGTH', format='D', array=wave)], name='EXTRACT1D')
    t.header['EXTRYSTR'], t.header['EXTRYSTP'] = 4, 8        # rows 3..7 (0-based), centre 5.5
    fits.HDUList([fits.PrimaryHDU(), t]).writeto(tmp_path / 'p_x1d.fits')
    w, f, e, src = S3.spec3_boxcar3(str(tmp_path), 'p')
    expect = np.where(wht[4:7] > 0, sci[4:7], 0.0).sum(axis=0) * 1e12
    assert src == 's2d'
    np.testing.assert_allclose(w, wave)
    np.testing.assert_allclose(f, expect)
    assert S3.spec3_boxcar3(str(tmp_path), 'none') is None


def test_exposure_keys_with_a_base36_sequence():
    # the activity of the exposure sequence counts 1..9, a..z (0410f after 04109)
    p = '/x/jw12497004001_0410f_00002_nrs1_10059.fits'
    assert CT.exposure_key(p) == 'jw12497004001_0410f_00002_nrs1'
    assert CT.nod_key(p) == ('00002', 'nrs1')
    assert CT.exposure_key('jw07076014001_03101_00001_nrs2_rate.fits') == 'jw07076014001_03101_00001_nrs2'
