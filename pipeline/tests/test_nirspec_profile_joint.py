"""Tests for the joint solve of overlap groups (nirspec/profile_joint.py) and the
one-pass pieces it relies on (``profile_contam.predict`` exclusions and joint light,
``apply``, the continuity rule).

Synthetic three-nod slitlets from test_nirspec_profile_fit; two sources share the
same detector frame (their traces fully overlap) at different slit positions. The
trace-map geometry and the per-pixel calibration are stand-ins, as in
test_nirspec_profile_contam.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_contam as CT
from campfire_pipeline.nirspec import profile_joint as PJ
from campfire_pipeline.nirspec import stage3 as S3

import test_nirspec_profile_fit as T
from test_nirspec_profile_contam import _source, _pass1_of, stand_ins  # noqa: F401 - fixture

NY, NX = T.NY, T.NX


def _member(sid, exps, centre, workspace='.'):
    M = PF.ProfileModel(exps, chromatic=True)
    p = PF.shape_vector(dict(centre_offset=centre - M.c_prior, width=0.1), M.c_prior, 0.15)
    m = PJ.Member(sid=sid, product=f'p_{sid}', exps=exps, model=M, p=p, eoff=np.zeros(len(exps)),
                  params=dict(centre=centre, drift=0.0, width=0.1, width_change=0.0, pivot=M.pivot,
                              centre_prior=M.c_prior), workspace=workspace)
    PJ.basis_frames(m)
    return m


def _pair(scale1=10.0, scale2=5.0, c1=-0.15, c2=0.2, noise=0.0, seed=3):
    """Two sources on one slitlet frame; every exposure's difference holds both."""
    e1, R1 = _source(1, c1, scale1)
    e2, R2 = _source(2, c2, scale2)
    rng = np.random.default_rng(seed)
    fnoise = [noise * rng.normal(size=(NY, NX)) for _ in range(3)]
    for k in range(3):
        d = (PF.model_frame(R1, k, applied_pathloss=False, common=True)
             + PF.model_frame(R2, k, applied_pathloss=False, common=True))
        if noise:
            d = d + fnoise[k] - np.mean([fnoise[j] for j in e1[k].partners], axis=0)
        for e in (e1[k], e2[k]):
            e.diff = d.copy()
            e.var = np.full(d.shape, max(noise, 1e-3) ** 2)
    return e1, R1, e2, R2


@pytest.fixture
def unit_cal(monkeypatch):
    monkeypatch.setattr(CT, 'calibration', lambda e, rate_dir: np.ones(e.y.shape))


def test_joint_solve_recovers_two_overlapping_sources(unit_cal):
    """Noise-free data of two sources sharing every pixel: both spectra come back
    exactly; every datum is used once (the second member owns none of its pixels,
    they are the first member's)."""
    e1, R1, e2, R2 = _pair()
    m1, m2 = _member(1, e1, -0.15), _member(2, e2, 0.2)
    n = PJ.owned_pixels([m1, m2])
    assert n == m1.model.npix and m1.owned.all() and not m2.owned.any()
    S = PJ.solve([m1, m2])
    for m, R in ((m1, R1), (m2, R2)):
        f = S.flux[m.sid]
        ok = np.isfinite(f)
        assert ok.mean() > 0.9
        np.testing.assert_allclose(f[ok], np.interp(S.lam[m.sid][ok], R.lam, R.flux), rtol=1e-5, atol=1e-6)
        infl = S.inflation(m.sid)
        assert np.nanmin(infl) >= 1.0 - 1e-6                 # sharing pixels costs, never gains
        c = S.corr[m.sid]
        assert np.nanmin(c) >= -1.0 - 1e-9 and np.nanmax(c) <= 1.0 + 1e-9
        assert set(np.unique(S.corr_sid[m.sid][np.isfinite(c)])) == {3 - m.sid}


def test_joint_solve_errors_match_scatter(unit_cal):
    """With noise: the joint errors describe the scatter of the solution about the truth
    (reduced chi2 of the residual near one)."""
    e1, R1, e2, R2 = _pair(noise=0.5)
    m1, m2 = _member(1, e1, -0.15), _member(2, e2, 0.2)
    PJ.owned_pixels([m1, m2])
    S = PJ.solve([m1, m2])
    for m, R in ((m1, R1), (m2, R2)):
        f, e = S.flux[m.sid], S.err[m.sid]
        ok = np.isfinite(f) & np.isfinite(e) & (e > 0)
        z = (f[ok] - np.interp(S.lam[m.sid][ok], R.lam, R.flux)) / e[ok]
        assert 0.6 < np.std(z) < 1.4 and abs(np.mean(z)) < 0.3
        assert (S.err[m.sid][ok] >= S.err_iso[m.sid][ok] * (1 - 1e-6)).all()


def test_shape_sensitivity_of_a_faint_member_inside_a_bright_one(unit_cal):
    """5215 inside 2976: a faint member on a bright member's profile is formally
    determined (moderate correlation) but a small error of the bright one's shape moves
    it by a large fraction of its own flux; the bright one hardly moves."""
    e1, R1, e2, R2 = _pair(scale1=100.0, scale2=2.0, c1=0.0, c2=0.12, noise=0.05)
    m1, m2 = _member(1, e1, 0.0), _member(2, e2, 0.12)
    PJ.owned_pixels([m1, m2])
    S = PJ.solve([m1, m2], sens=(0.02, 0.1))
    rel = {s: PJ._integrated(S.sens[s], S.err[s], S.flux[s])[1] for s in (1, 2)}
    assert rel[2] > 0.1 and rel[1] < 0.01 and rel[2] > 20 * rel[1]
    assert (PJ.joint_flags(S.corr[2], S.err[2], S.sens[2], nwin=31) & PJ.FLAG_SHAPE).mean() > 0.5


def test_joint_pass1_predicts_the_member_light(unit_cal, stand_ins):
    """A member's joint spectrum as a Pass1 (``joint = True``): placed on the other
    member it is that member's light from the solve; ``predict`` subtracts it and never
    masks it, and keeps its statistical error apart (``dpred_joint``)."""
    e1, R1, e2, R2 = _pair()
    m1, m2 = _member(1, e1, -0.15), _member(2, e2, 0.2)
    sols = PJ.solve_group([m1, m2])
    P1 = sols[2].pass1(2)
    assert P1.joint and P1.reliable
    t_exps, _ = _source(1, 0.0, 10.0)
    for e in t_exps:
        e.var = np.ones(e.y.shape)
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.')
    F = [PF.frame_image(R2, j) for j in range(3)]
    expect = np.nan_to_num(F[0] - 0.5 * (F[1] + F[2]))
    inside = np.isfinite(F[0]) & (e2[0].wav > R2.lam[1]) & (e2[0].wav < R2.lam[-2])
    np.testing.assert_allclose(P.pred[0][inside], expect[inside], rtol=1e-4, atol=1e-6 * np.abs(expect).max())
    assert P.joint == {2} and 2 in P.contributors
    np.testing.assert_allclose(P.pred_joint[0], P.pred[0])
    assert np.all(P.dpred[0] == 0)                           # no floor, nothing to mask by
    ok_before = t_exps[0].ok.copy()
    d_before = t_exps[0].diff.copy()
    CT.apply(t_exps, P, mode='mask', kappa=1.0)               # joint light: subtracted even in mask mode
    np.testing.assert_array_equal(t_exps[0].ok, ok_before & ~P.unmodelled[0])   # only the unpredictable dropped
    assert P.unmodelled[0].sum() < 10
    np.testing.assert_allclose(t_exps[0].diff, d_before - P.pred[0])


def test_excluded_member_light_is_left_out(unit_cal, stand_ins):
    """While a group is being solved, a member's light is not predicted on another
    member (``exclude``); its pixels no spectrum can predict stay unmodelled."""
    t_exps, _ = _source(1, 0.0, 10.0)
    c_exps, Rc = _source(2, 0.3, 5.0)
    P1 = _pass1_of(Rc, 2)
    P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.', exclude={2})
    assert all(np.all(p == 0) for p in P.pred) and 2 not in P.contributors and P.excluded == {2}


def test_select_groups_strongest_first_and_capped():
    pairs = {
        1: {2: dict(sig=50.0, frac=2.0), 3: dict(sig=5.0, frac=5.0)},        # 3: below the significance
        2: {1: dict(sig=12.0, frac=0.05)},                                   # light in 2 small vs its own
        4: {5: dict(sig=30.0, frac=0.5), 6: dict(sig=20.0, frac=0.4)},
        5: {6: dict(sig=11.0, frac=0.9)},
        7: {8: dict(sig=40.0, frac=1.0)},                                    # 8 not a product of this run
    }
    g = PJ.select_groups(pairs, {1, 2, 3, 4, 5, 6, 7}, min_sigma=10, min_frac=0.3, max_members=6)
    assert g == [[1, 2], [4, 5, 6]]
    g = PJ.select_groups(pairs, {1, 2, 3, 4, 5, 6, 7}, min_sigma=10, min_frac=0.3, max_members=2)
    assert g == [[1, 2], [4, 5]]                                             # strongest pair of 4 kept
    # only the members that receive strongly coupled light get a joint product
    assert PJ.receivers(pairs, [1, 2]) == {1} and PJ.receivers(pairs, [4, 5, 6]) == {4, 5}
    # negative light (a contaminant's copies in the partner nods) counts as much
    assert PJ.select_groups({1: {2: dict(sig=50.0, frac=-2.0)}}, {1, 2}) == [[1, 2]]


def test_joint_flags():
    corr = np.array([-0.5, -0.95, -0.2, -0.99])
    err = np.ones(4)
    sens = np.array([0.0, 0.0, 0.5, 0.01])
    f = PJ.joint_flags(corr, err, sens, degenerate=0.9, nwin=16)        # shape bias above err / 4
    np.testing.assert_array_equal(f, [0, PJ.FLAG_DEGENERATE, PJ.FLAG_SHAPE, PJ.FLAG_DEGENERATE])


def test_pair_metrics_scale_with_the_contaminant(stand_ins):
    """The contaminant's light in the target's spectrum: significance and fraction both
    scale with its brightness; a reliable contaminant is marked so."""
    out = []
    for scale in (2.0, 4.0):
        t_exps, Rt = _source(1, 0.0, 10.0)
        for e in t_exps:
            e.var = np.ones(e.y.shape)
        c_exps, Rc = _source(2, 0.3, scale)
        P1 = _pass1_of(Rc, 2)
        P = CT.predict(t_exps, 1, {'x': None}, lambda s: P1 if s == 2 else None, '.', keep_sources=True)
        R = PF.fit_fixed(t_exps, shape=dict(centre_offset=0.0, width=0.1))
        R.flux = np.interp(R.lam, Rt.lam, Rt.flux)
        out.append(PJ.pair_metrics(t_exps, R, P)[2])
    assert out[0]['reliable'] and out[1]['sig'] == pytest.approx(2 * out[0]['sig'], rel=1e-6)
    assert out[1]['frac'] == pytest.approx(2 * out[0]['frac'], rel=1e-6) and out[0]['frac'] > 0


def test_profile_fit_columns_joint_error_and_flag(tmp_path):
    """A product of a joint-solve group: the statistical cost of solving the shared light
    (error inflation) enters fnu_pf_contam_err, and wavelengths where the split is not
    determined (correlation below -PFJDEGT) are flagged."""
    ny, nx = 30, 100
    rows = np.arange(ny)[:, None]
    prof = np.exp(-0.5 * ((rows - 15) / 1.2) ** 2) * np.ones((1, nx))
    prof = prof / prof[:, :1].sum()
    sci = 10.0 * prof
    wave = np.linspace(3, 5, nx)
    lam = np.linspace(2.9, 5.1, 300)
    corr = np.where(lam > 4.0, -0.95, -0.2)
    jt = fits.BinTableHDU.from_columns([
        fits.Column(name='lam', format='D', array=lam), fits.Column(name='flux', format='D', array=np.ones(300)),
        fits.Column(name='err', format='D', array=np.full(300, 2.0)),
        fits.Column(name='err_iso', format='D', array=np.ones(300)),
        fits.Column(name='corr', format='D', array=corr), fits.Column(name='corr_sid', format='K', array=np.ones(300, int)),
        fits.Column(name='flag', format='I', array=np.where(corr < -0.9, PJ.FLAG_DEGENERATE, 0)
                    | np.where(lam < 3.2, PJ.FLAG_SHAPE, 0))],
        name='JOINT')
    h0 = fits.PrimaryHDU()
    h0.header['PFJDEGT'] = 0.9
    path = tmp_path / 'x_pf.fits'
    fits.HDUList([h0, fits.ImageHDU(prof / 1e12, name='PROF2D'), fits.ImageHDU(0.0 * prof, name='CONTAM2D'),
                  fits.ImageHDU(0.0 * prof, name='CONTERR2D'), jt]).writeto(path)
    err = np.full((ny, nx), 0.01)
    out = S3._profile_fit_columns(str(path), sci, err, np.zeros((ny, nx), bool), 12, 18, wave,
                                  rows='aperture', contam_1d='report')
    c = out['columns']
    np.testing.assert_allclose(c['pf_joint_infl'][0], 2.0)
    np.testing.assert_allclose(c['fnu_pf_contam_err'][0], c['fnu_pf_err'][0] * np.sqrt(3.0), rtol=1e-6)
    np.testing.assert_allclose(c['fnu_pf_dec_err'][0], 2.0 * c['fnu_pf_err'][0], rtol=1e-6)
    flag = c['pf_joint_flag'][0]
    assert (flag[wave > 4.05] == PJ.FLAG_DEGENERATE).all()
    assert (flag[wave < 3.15] == PJ.FLAG_SHAPE).all() and not flag[(wave > 3.25) & (wave < 3.95)].any()
    assert 'JOINT' in [h.name for h in out['hdus']]
