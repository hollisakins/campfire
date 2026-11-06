"""Tests for the calibration-star fits of the diffraction model (``nirspec.optics_calibration``):
a synthetic star through the closed-form stand-in (``optics_standin``) is recovered by the
per-range fit, its stacking samples match the model on noiseless data, and the stack metric
reads a known residual."""

import json
import os

import numpy as np
import pytest

from campfire_pipeline.nirspec import optics_calibration as OC
from campfire_pipeline.nirspec import profile_fit as PF

from test_nirspec_profile_fit import NX, NY, _exposures, _spectrum

STAR = dict(centre=0.1, drift=0.0, width=0.05, width_change=0.0)
OFFSETS = [0.0, 0.02, -0.015]


def _star(noise=1.0, flux=300.0, seed=4):
    """Three nods of a star with its own offset per exposure, no centre law (the centre is
    where the light is, as the calibration fits it)."""
    exps = _exposures(src_y=0.1)
    M = PF.ProfileModel(exps, chromatic=True, centre_law_tables=PF.NO_CENTRE_LAW)
    lam = M.lam_grid
    R = PF.FitResult(params=dict(STAR, pivot=M.pivot, centre_prior=M.c_prior), eoff=np.array(OFFSETS), lam=lam,
                     flux=flux * _spectrum(lam), flux_err=np.zeros_like(lam), chi2=0.0, npix=0, nfree=0,
                     chromatic=True, model=M)
    rng = np.random.default_rng(seed)
    fn = [noise * rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = PF.model_frame(R, k, applied_pathloss=False) + fn[k] - np.mean([fn[j] for j in e.partners], axis=0)
        e.var = np.full((NY, NX), max(noise, 1e-3) ** 2)
    return exps


def test_range_fit_recovers_a_star():
    RF = OC.RangeFit(_star(), (1.0, 4.0))
    s = RF.start()
    g = RF.fit(x0=s['_x'], use=s['_use'])
    assert g['c0'] == pytest.approx(STAR['centre'], abs=0.01)
    assert g['w'] == pytest.approx(STAR['width'], abs=0.01)
    assert np.allclose(np.array(g['offsets']) - g['offsets'][0], OFFSETS, atol=0.01)
    assert 0.8 < g['chi2_raw'] / g['ndof'] < 1.2          # chi2 itself is on the floored variances
    assert g['peak_snr'] > 20


def test_samples_match_the_model_without_noise():
    RF = OC.RangeFit(_star(noise=1e-6), (1.0, 4.0))
    g = RF.fit(x0=[STAR['centre'], 0.0], clip_rounds=0)
    s = RF.samples(g)
    core = np.abs(s['u']) < 1.5
    assert s['u'].size > 100
    assert np.allclose(s['d'][core], s['m'][core], rtol=1e-3, atol=1e-5 * s['m'].max())


@pytest.fixture
def small_optics(monkeypatch, tmp_path):
    """A real diffraction model on small grids (a table builds in a second), with a kernel
    linear between nodes 0.05 apart, zero at +-0.15 (so the calibration's nodes, which
    include these, represent it exactly)."""
    from campfire_pipeline.nirspec import optics as O
    monkeypatch.setattr(O, 'LAM', np.array([1.0, 3.0]))
    monkeypatch.setattr(O, 'C', np.round(np.arange(-0.375, 0.3751, 0.0125), 5))
    monkeypatch.setattr(O, 'Y', np.round(np.arange(-1.0, 1.0001, 0.025), 5))
    delta = np.linspace(-0.15, 0.15, 7)
    k = np.exp(-0.5 * (delta / 0.04) ** 2) * (np.abs(delta) < 0.12)
    opt = O.Optics(0.996, 0.0344, -0.0026, 0.25, [dict(lam=1.0, delta=delta, k=k)], cache_dir=str(tmp_path / 'optics'))
    # the same kernel on the calibration's nodes, scaled as kernel_response reads it (light per unit offset)
    offsets = (np.arange(O.C.size) - O.C.size // 2) * (O.C[1] - O.C[0])
    kvec = np.interp(OC.DELTA, delta, k, left=0.0, right=0.0)
    kvec /= (OC.FINE[1] - OC.FINE[0]) * np.interp(offsets, delta, k, left=0.0, right=0.0).sum()
    return opt, kvec


@pytest.mark.parametrize('width', [PF.W_FLOOR, 0.03])
def test_kernel_response_is_the_model_linear_in_the_kernel(small_optics, width):
    opt, kvec = small_optics
    exps = _exposures()
    M = PF.ProfileModel(exps, centre_law_tables=PF.NO_CENTRE_LAW, optics=[opt] * len(exps))
    p = np.array([0.0, 0.0, np.log(width), 0.0])
    f = 300.0 * _spectrum(M.lam_grid)
    eoff = np.zeros(len(exps))
    want = M.design(p, eoff) @ f
    got = OC.kernel_response(M, p, f, eoff, width) @ kvec
    # exact for a resolved width; a point is the floor width in the model, a delta in the response
    tol = 1e-6 if width > 1.5 * PF.W_FLOOR else 2e-3
    assert np.max(np.abs(got - want)) < tol * np.max(np.abs(want))


def test_kernel_solve_recovers_a_kernel(small_optics, tmp_path):
    from campfire_pipeline.nirspec import optics as O
    opt, kvec = small_optics
    exps = _exposures()
    M = PF.ProfileModel(exps, centre_law_tables=PF.NO_CENTRE_LAW, optics=[opt] * len(exps))
    p = np.array([0.0, 0.0, np.log(0.03), 0.0])
    R = OC.kernel_response(M, p, 300.0 * _spectrum(M.lam_grid), np.zeros(len(exps)), 0.03)
    d = R @ kvec
    AtA, Atd = R.T @ R, R.T @ d
    k = OC.solve_kernel(AtA, Atd)
    # neighbouring core nodes are nearly interchangeable once the PSF blurs them, so the test is
    # the data the kernel makes and the light it carries, not each node
    assert np.max(np.abs(R @ k - d)) < 1e-3 * d.max()
    light = OC.HAT.sum(axis=0)
    assert light @ k == pytest.approx(light @ kvec, rel=1e-3)
    assert np.all(k[np.abs(OC.DELTA) > 0.15] < 1e-6 * k.max())          # nothing where the kernel has nothing
    # two stars' files summed per detector and range, then a model file the pipeline reads
    os.makedirs(tmp_path / 'kernel')
    for key, nexp in (('a_1_NRS1', 3), ('b_2_NRS1', 60)):
        np.savez(tmp_path / 'kernel' / f'{key}.npz', detector='NRS1', widths='{}', nexp=nexp, r2_AtA=AtA / 2,
                 r2_Atd=Atd / 2, r2_n=10, r2_nod_AtA=AtA / 4, r2_nod_Atd=Atd / 4, r2_nod_n=4)
    tot = OC.sum_normals(str(tmp_path / 'kernel'), per_star=False)    # every pixel alike
    assert set(tot) == {('NRS1', 2)} and tot[('NRS1', 2)][2] == 20 and np.allclose(tot[('NRS1', 2)][0], AtA)
    nod = OC.sum_normals(str(tmp_path / 'kernel'), part='nod_', per_star=False)   # the nod differences alone
    assert nod[('NRS1', 2)][2] == 8 and np.allclose(nod[('NRS1', 2)][0], AtA / 2)
    # the default, each star once: the three-nod star whole, the 60-exposure star at 3/60
    one = OC.sum_normals(str(tmp_path / 'kernel'))
    assert np.allclose(one[('NRS1', 2)][0], AtA / 2 * (1 + 3 / 60)) and one[('NRS1', 2)][2] == 20
    base = tmp_path / 'base.json'
    base.write_text(json.dumps({'meta': {'version': 'base'}, 'diffraction': dict(rho=0.996, s0=0.0344, s1=-0.0026,
                                                                                 alpha=0.25),
                                'x_loss': dict(b=0.5, lam0=2.0), 'NRS1': [], 'NRS2': []}))
    np.savez(tmp_path / 'kernel' / 'c_3_NRS2.npz', detector='NRS2', widths='{}', nexp=3, r2_AtA=AtA, r2_Atd=Atd, r2_n=10)
    OC.step_solve(str(tmp_path / 'kernel'), str(base), str(tmp_path / 'new.json'), 'test-1', log=lambda s: None)
    new = O.load_optics(str(tmp_path / 'new.json'))
    assert new['version'] == 'test-1'
    node = new['NRS1'].kernel[0]
    assert node['lam'] == pytest.approx(np.sqrt(OC.RANGES[2][0] * OC.RANGES[2][1]))
    assert np.allclose(node['k'], k)                 # the summed normals are the one star's
    assert new['NRS1'].x_loss == dict(b=0.5, lam0=2.0)   # the rest of the base model is kept


def test_laws_recover_a_known_centre_law_and_nod_scale():
    """Stars whose centre moves with wavelength by D_quadrant + alpha x position (relative to
    1.0-2.6 um), and whose second exposure sits 1.1 x its planned shift away: the law fit gets
    D and alpha back, and the nod rows the scale."""
    rng = np.random.default_rng(1)
    lam = np.array([0.5 * (a + b) for a, b in OC.RANGES])
    D = {1: 0.02 * (lam - 2.0), 2: -0.01 * (lam - 2.0)}
    alpha = -0.03 * (lam - 2.0)
    recs = []
    for i in range(60):
        q, c = 1 + i % 2, rng.uniform(-0.3, 0.3)
        true = D[q] + alpha * c
        true = true - np.mean(true[list(OC.REF_RANGES)])
        plan = rng.uniform(-0.02, 0.02)
        recs.append(dict(key=f's{i}_NRS1', obs=f'o{i % 3}', detector='NRS1', quadrant=q, has_source=[True, True],
                         ranges=[dict(range=r, c_node=c + true[r] + 1e-4 * rng.normal(), e_c=1e-3,
                                      offsets=[0.0, 0.1 * plan], shifts=[0.0, plan]) for r in range(len(OC.RANGES))]))
    keys = {('NRS1', r): [x['key'] for x in recs] for r in range(len(OC.RANGES))}
    fit = OC.centre_law_fit(OC.centre_rows(recs, keys))
    # D is defined against the reference ranges' mean, so compare changes from it
    for q in (1, 2):
        got = np.array(fit[f'Q{q}_NRS1']['D'])
        want = D[q] - np.mean(D[q][list(OC.REF_RANGES)])
        assert np.allclose(got - np.mean(got[list(OC.REF_RANGES)]), want, atol=2e-3)
    assert np.allclose(fit['NRS1']['alpha'], alpha - np.mean(alpha[list(OC.REF_RANGES)]), atol=2e-3)
    res = OC.centre_law_residuals(OC.centre_rows(recs, keys), OC.law_table(fit))
    assert np.nanmax(np.abs(res)) < 2e-3
    nods = OC.nod_rows(recs, keys)
    plan, off = np.array([x['plan'] for x in nods]), np.array([x['off'] for x in nods])
    assert np.sum(plan * off) / np.sum(plan ** 2) == pytest.approx(0.1, abs=1e-6)


def test_x_loss_fit_recovers_b_and_the_correction_undoes_it():
    """Visits whose fluxes carry an x loss jwst's table missed, exp(-0.6 (lambda - 2)+ x^2):
    fit_x_loss finds 0.6, and x_correction with it is that loss."""
    from campfire_pipeline.nirspec import optics as O
    rng = np.random.default_rng(2)
    pairs = []
    for i in range(200):
        lam = rng.choice([0.85, 1.25, 2.0, 3.0, 4.25])
        xv, xr = rng.uniform(-0.45, 0.45), rng.uniform(-0.1, 0.1)
        lnr = -0.6 * max(lam - 2.0, 0.0) * (xv ** 2 - xr ** 2) + 0.01 * rng.normal()
        pairs.append(dict(lnr=lnr, lam=lam, xv=xv, xr=xr, band=lam))
    b = OC.fit_x_loss(pairs)
    assert b == pytest.approx(0.6, abs=0.02)
    opt = O.Optics(0.996, 0.0344, -0.0026, 0.25, [], x_loss=dict(b=b, lam0=2.0))
    lam = np.array([1.0, 2.0, 4.0])
    assert np.allclose(opt.x_correction(lam, 0.0), 1.0)
    assert np.allclose(opt.x_correction(lam, np.nan), 1.0)              # no SRCXPOS: no correction
    assert np.allclose(opt.x_correction(lam, 0.3), [1.0, 1.0, np.exp(-b * 2.0 * 0.09)])
    assert np.allclose(O.Optics(0.996, 0.0344, -0.0026, 0.25, []).x_correction(lam, 0.3), 1.0)


def test_clean_keys_and_stack_metric(tmp_path):
    def rec(key, snr=50, c=0.0, sat=0.0, w=0.01, chi2r=1.0):
        return dict(key=key, detector='NRS1', ranges=[dict(range=2, lam=[1.5, 2.0], peak_snr=snr, c_node=c, sat=sat,
                                                           w=w, chi2=chi2r * 100.0, ndof=100.0)])
    recs = [rec('a', c=0.1), rec('b', snr=40, c=-0.2, chi2r=1.3), rec('c', snr=30, chi2r=0.8),
            rec('faint', snr=5), rec('edge', c=0.4), rec('saturated', sat=0.02),
            rec('galaxy', w=0.15), rec('odd', chi2r=8.0)]
    keys = OC.clean_keys(recs)
    assert keys == {('NRS1', 2): ['a', 'b', 'c']}
    # many bright stars far below 1 (the variance floor) do not push out a faint one that fits at 1.5
    bright = [rec(f'bright{i}', snr=150, chi2r=0.1) for i in range(20)]
    assert 'faint_fits' in OC.clean_keys(bright + [rec('faint_fits', snr=25, chi2r=1.5)])[('NRS1', 2)]
    os.makedirs(tmp_path / 'samples')
    u = np.linspace(-4, 4, 4000)
    m = np.exp(-0.5 * (u / 0.6) ** 2)
    for k in ('a', 'b', 'c'):                          # data 1 % of the peak above the model, everywhere
        np.savez(tmp_path / 'samples' / f'{k}.npz', r2_u=u, r2_d=m + 0.01, r2_m=m)
    met = OC.stack_metric(str(tmp_path / 'samples'), keys)
    assert met[('NRS1', 2)]['n'] == 3
    peak = max(met[('NRS1', 2)]['model'])                # the binned model's peak, just under 1
    assert met[('NRS1', 2)]['max'] == pytest.approx(1.0 / peak, rel=1e-3)
    # per dataset: only 'a' starts with the prefix, too few stars for a median
    def write(run, f=1.0):
        os.makedirs(tmp_path / run / 'records', exist_ok=True)
        for r in recs:
            r2 = json.loads(json.dumps(r))
            r2['ranges'][0]['chi2'] *= f
            with open(tmp_path / run / 'records' / f"{r['key']}.json", 'w') as fh:
                json.dump(r2, fh)
    write('')
    assert OC.parse_groups('one=a,rest=') == {'one': 'a', 'rest': ''}
    per = OC.step_score_groups(str(tmp_path), OC.parse_groups('one=a,all='), log=lambda s: None)
    assert per['one'] == {} and per['all'][('NRS1', 2)]['n'] == 3
    # two runs of the same stars: chi2 per range, the median ratio b / a
    write('A')
    write('B', 1.1)
    cmp = OC.step_compare(str(tmp_path / 'A'), str(tmp_path / 'B'), log=lambda s: None)
    n, ca, cb, ratio = cmp[('all', 'NRS1', 2)]
    assert n == 6 and ratio == pytest.approx(1.1) and cb == pytest.approx(1.1 * ca)   # 'edge', 'saturated' left out


def _visit(sid, det, pos, flux, along='y', snr=100.0, sat=0.0, bands=((1.0, 2.0), (3.0, 4.0))):
    return dict(sid=sid, det=det, srcx=pos if along == 'x' else 0.0, srcy=pos if along == 'y' else 0.0,
                bands=[list(b) for b in bands], flux=[(f, f / snr, snr) for f in flux], sat=[sat] * len(bands))


def test_repeat_pairs_against_the_most_central_visit():
    rows = [_visit(1, 'NRS1', 0.02, [10.0, 20.0]), _visit(1, 'NRS1', 0.3, [11.0, 18.0]),
            _visit(1, 'NRS1', -0.2, [10.5, 20.0], snr=5.0),                      # too faint: not used
            _visit(1, 'NRS2', 0.3, [1.0, 1.0]),                                  # the other detector: its own star
            _visit(2, 'NRS1', 0.1, [5.0, 5.0]), _visit(2, 'NRS1', -0.4, [5.0, 4.0], sat=0.5),   # saturated
            dict(sid=3, det='NRS1', error='ValueError: x')]
    p = OC.repeat_pairs(rows)
    assert [len(b) for b in p] == [1, 1]
    assert p[0][0]['lnr'] == pytest.approx(np.log(1.1)) and p[1][0]['lnr'] == pytest.approx(np.log(0.9))
    assert p[0][0]['pos'] == 0.3 and p[0][0]['ref_pos'] == 0.02
    assert OC.repeat_pairs(rows, exclude=[1]) == [[], []]


def test_repeat_along_x_refits_the_correction(tmp_path):
    """A scan whose fluxes carry the along-dispersion loss exp(-0.6 (lambda - 2)+ x^2) after a
    correction with b = 0.2 was applied: the refit undoes that one and finds 0.6."""
    rng = np.random.default_rng(3)
    bands = ((1.0, 1.5), (2.5, 3.5), (3.5, 5.0))
    rows = []
    for sid in range(40):
        for x in (0.0, -0.4, -0.2, 0.2, 0.4):
            f = [np.exp(-(0.6 - 0.2) * max(np.sqrt(a * b) - 2.0, 0.0) * x ** 2 + 0.005 * rng.normal())
                 for a, b in bands]
            rows.append(_visit(sid, 'NRS1', x, f, along='x', bands=bands))
    path = tmp_path / 'visits.json'
    path.write_text(json.dumps(dict(optics='test', x_loss=dict(b=0.2, lam0=2.0), rows=rows)))
    res = OC.step_repeat(str(path), along='x', log=lambda s: None)
    assert res[0]['slope'] == pytest.approx(0.0, abs=0.02)              # no loss below 2 um
    assert res[2]['slope'] == pytest.approx(-0.4 * (np.sqrt(3.5 * 5.0) - 2.0), rel=0.05)
    assert res[-1]['b'] == pytest.approx(0.6, abs=0.03) and res[-1]['b_used'] == 0.2
