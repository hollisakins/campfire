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
                                  np.linspace(1, 5, nx), rows='aperture')
    c = out['columns']
    np.testing.assert_allclose(c['fnu_pf'][0], 10.0, rtol=1e-6)
    np.testing.assert_allclose(c['fnu_pf_contam'][0], 1.0, rtol=1e-6)
    assert out['header']['PFCONTAM'] == pytest.approx(0.1, rel=1e-6)
    assert out['header']['PFROWS'] == 'aperture'
    assert [h.name for h in out['hdus']] == ['PROF2D', 'CONTAM2D']
    m = S3._profile_fit_columns(str(path), sci_uJy.copy(), err, np.zeros((ny, nx), bool), 12, 18,
                                np.linspace(1, 5, nx), rows='model')
    np.testing.assert_allclose(m['columns']['fnu_pf'][0], 10.0, rtol=1e-6)
    np.testing.assert_allclose(m['columns']['fnu_pfa'][0], 10.0, rtol=1e-6)
