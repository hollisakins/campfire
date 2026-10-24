"""Tests for the two-Gaussian galaxy of the profile fit (profile_fit.fit_two, the
adoption rule, and every place that carries a shape: PRISM -> grating transfer,
headers, the joint solve's shape).

Synthetic three-nod slitlets from test_nirspec_profile_fit; data drawn per frame and
differenced like stage 2b.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_chain as PC
from campfire_pipeline.nirspec import profile_joint as PJ

import test_nirspec_profile_fit as T

NY, NX = T.NY, T.NX
CORE_HALO = dict(centre_offset=0.05, width=0.10, halo=0.45, halo_ratio=4.0)     # second width 0.40
SINGLE = dict(centre_offset=0.05, width=0.20)


def _fill(exps, shape, flux_scale, noise=1.0, seed=1):
    """Frame-level data from a shape dict (``shape_vector`` style, optionally with a halo)."""
    M = PF.ProfileModel(exps, chromatic=True)
    p = PF.shape_vector(shape, M.c_prior, 0.15)
    R = T._truth_result(exps, PF.shape_params(p, M), np.zeros(len(exps)), flux_scale)
    frames = []
    for j, e in enumerate(exps):
        spec = np.interp(e.wav, R.lam, R.flux)
        frames.append(spec * M.throughput(p, R.eoff, j, e.wav, e.dy) * M.image(p, R.eoff, j, e.y, e.wav, e.dy))
    rng = np.random.default_rng(seed)
    nz = [noise * rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = frames[k] + nz[k] - np.mean([frames[j] + nz[j] for j in e.partners], axis=0)
        e.var = np.full((NY, NX), noise ** 2)
    return R


def _cfg(**kw):
    sc = dict(gate_snr_prism=10.0, gate_snr_grating=30.0)
    sc.update(kw)
    return PC.ChainConfig.from_stage_config(sc)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------
def test_no_halo_is_exactly_the_single_gaussian():
    exps = T._exposures()
    M = PF.ProfileModel(exps)
    e = exps[0]
    p4 = np.array([0.05, 0.01, np.log(0.2), -0.02])
    p6 = np.concatenate([p4, [0.0, 3.0]])
    args = (np.zeros(3), 0, e.y, e.wav, e.dy)
    assert PF.galaxy_components(p6) is None
    assert np.array_equal(M.image(p4, *args), M.image(p6, *args))
    lam, dy = e.wav[NY // 2], e.dy[NY // 2]
    assert np.array_equal(M.transmission(p4, np.zeros(3), 0, lam, dy), M.transmission(p6, np.zeros(3), 0, lam, dy))


def test_two_equal_gaussians_are_one():
    exps = T._exposures()
    M = PF.ProfileModel(exps)
    e = exps[1]
    p4 = np.array([0.05, 0.0, np.log(0.2), 0.0])
    p6 = np.concatenate([p4, [0.4, 1.0]])
    args = (np.zeros(3), 1, e.y, e.wav, e.dy)
    assert np.allclose(M.image(p4, *args), M.image(p6, *args), rtol=1e-12, atol=1e-15)


def test_halo_spreads_light_out_of_the_shutter():
    """The second Gaussian moves light out of the main shutter, and the transmission is
    the light-weighted sum of the two Gaussians' (a point source's is unchanged)."""
    exps = T._exposures()
    M = PF.ProfileModel(exps)
    lam, dy, e0 = np.array([2.0]), np.array([T.DY]), np.zeros(3)
    p4 = np.array([0.0, 0.0, np.log(0.1), 0.0])
    p6 = np.concatenate([p4, [0.45, 4.0]])
    pb = np.array([0.0, 0.0, np.log(0.4), 0.0])
    one = (0,)
    t6 = M.transmission(p6, e0, 0, lam, dy, aperture=one)
    t4 = M.transmission(p4, e0, 0, lam, dy, aperture=one)
    assert t6 < t4 - 0.05
    assert t6 == pytest.approx(0.55 * t4 + 0.45 * M.transmission(pb, e0, 0, lam, dy, aperture=one), rel=1e-12)
    assert M.transmission(p6, e0, 0, lam, dy, width=0.0, aperture=one) == M.transmission(p4, e0, 0, lam, dy, width=0.0,
                                                                                         aperture=one)


# ---------------------------------------------------------------------------
# carrying the shape
# ---------------------------------------------------------------------------
def test_shape_carriers_keep_halo():
    exps = T._exposures()
    M = PF.ProfileModel(exps)
    p = PF.shape_vector(CORE_HALO, M.c_prior, 0.15)
    assert p.size == 6 and p[4] == 0.45 and p[5] == 4.0
    d = PF.shape_dict(p, M.c_prior)
    assert d['halo'] == 0.45 and d['halo_ratio'] == 4.0 and d['width'] == pytest.approx(0.10)
    params = PF.shape_params(p, M)
    assert params['halo'] == 0.45
    m = PF.map_shape(dict(params, width_change=0.02), 3.5)
    assert m['halo'] == 0.45 and m['halo_ratio'] == 4.0
    R = PF.FitResult(params=params, eoff=np.zeros(3), lam=M.lam_grid, flux=np.ones(M.lam_grid.size),
                     flux_err=np.zeros(M.lam_grid.size), chi2=0.0, npix=0, nfree=0, chromatic=True, model=M)
    assert np.allclose(PF._qvec(R)[0], p)
    # single-Gaussian shapes stay four-parameter everywhere
    p1 = PF.shape_vector(SINGLE, M.c_prior, 0.15)
    assert p1.size == 4 and 'halo' not in PF.shape_params(p1, M) and 'halo' not in PF.map_shape(
        PF.shape_params(p1, M), 3.0)
    # joint solve: shape at another model's pivot
    pj, _ = PJ.shape_at(params, None, M)
    assert pj.size == 6 and pj[4] == 0.45 and pj[5] == 4.0
    assert PJ.shape_at(PF.shape_params(p1, M), None, M)[0].size == 4


def test_header_roundtrip(tmp_path):
    exps = T._exposures()
    M = PF.ProfileModel(exps)
    params = PF.shape_params(PF.shape_vector(CORE_HALO, M.c_prior, 0.15), M)
    h = fits.Header()
    h['PFSHAPE'] = 'own'
    for k, v in dict(PFCEN=params['centre'], PFCENPR=params['centre_prior'], PFDRIFT=0.0, PFWIDTH=params['width'],
                     PFDWIDTH=0.0, PFPIVOT=params['pivot']).items():
        h[k] = v
    for k, v in PC.halo_cards(params).items():
        h[k] = v
    ps = PC.PrismShape.from_header('x_prism_clear_1', h)
    assert ps.params['halo'] == 0.45 and ps.params['halo_ratio'] == 4.0
    path = tmp_path / 'x_prism_clear_1_pf.fits'
    fits.PrimaryHDU(header=h).writeto(path)
    got = PJ.read_shape(str(tmp_path), 'x_prism_clear_1')
    assert got[0]['halo'] == 0.45 and got[0]['halo_ratio'] == 4.0
    del h['PFHALO'], h['PFHRATIO']
    assert 'halo' not in PC.PrismShape.from_header('x', h).params
    assert PC.halo_cards(dict(width=0.2)) == {}


# ---------------------------------------------------------------------------
# fitting and the rule
# ---------------------------------------------------------------------------
def test_fit_two_adopted_on_core_plus_halo():
    exps = T._exposures(src_y=0.05)
    _fill(exps, CORE_HALO, 60.0, seed=2)
    R0 = PF.fit(exps, priors=PF.Priors(centre_sigma=0.08), bounds=PF.Bounds(width_max=0.6))
    use0 = R0.use.copy()
    R2 = PF.fit_two(R0)
    assert np.array_equal(R0.model.use, use0)             # the model's mask is left as it was
    adopt, st = PF.two_gauss_test(R0, R2)
    assert adopt and st['dchi2'] > 3 * st['thr']
    assert R2.params['halo'] == pytest.approx(0.45, abs=0.1)
    assert R2.params['width'] * R2.params['halo_ratio'] == pytest.approx(0.40, rel=0.25)
    assert R2.chi2 / st['nu'] < 1.1
    good = np.isfinite(R2.flux) & (R2.lam > 1.0) & (R2.lam < 4.8)
    assert abs(np.median(R2.flux[good] / (60.0 * T._spectrum(R2.lam[good]))) - 1) < 0.02


def test_fit_two_not_adopted_on_a_single_gaussian():
    exps = T._exposures(src_y=0.05)
    _fill(exps, SINGLE, 60.0, seed=4)
    R0 = PF.fit(exps, priors=PF.Priors(centre_sigma=0.08), bounds=PF.Bounds(width_max=0.6))
    R2 = PF.fit_two(R0)
    adopt, st = PF.two_gauss_test(R0, R2)
    assert not adopt and st['dchi2'] < st['thr']
    assert R2.halo_fit['sigma2'] <= 0.6 + 1e-9


def test_rule_scales_with_excess_scatter():
    """c = chi2 / N_eff of the single Gaussian when above 1: the same chi2 gain counts less."""
    exps = T._exposures(src_y=0.05)
    _fill(exps, CORE_HALO, 60.0, seed=2)
    R0 = PF.fit(exps)
    R2 = PF.fit_two(R0)
    _, a = PF.two_gauss_test(R0, R2)
    R0.chi2 *= 3.0
    R2.chi2 += 2.0 * (R0.chi2 / 3.0)             # same gain in chi2
    _, b = PF.two_gauss_test(R0, R2)
    assert b['c'] == pytest.approx(3 * a['c'], rel=1e-6) and b['dchi2'] == pytest.approx(a['dchi2'] / 3, rel=1e-6)


def test_chain_two_gauss_switch():
    exps = T._exposures(src_y=0.05)
    _fill(exps, CORE_HALO, 60.0, seed=2)
    off = PC.choose_shape(exps, _cfg(), is_prism=True)
    assert off.two == {} and 'halo' not in off.result.params
    cards = off.header_cards()
    assert not any(k.startswith('PF2G') for k in cards) and 'PFHALO' not in cards
    on = PC.choose_shape(exps, _cfg(profile_fit_two_gauss=True), is_prism=True)
    assert on.shape == 'own' and on.two['adopt'] and on.result is on.candidates['own2']
    cards = on.header_cards()
    assert cards['PF2G'][0] is True and cards['PFHALO'][0] == pytest.approx(on.result.params['halo'])
    assert cards['PF2GDF'][0] == pytest.approx(on.two['dflux'])
    # same Gaussian own fit either way
    assert on.candidates['own'].params == off.candidates['own'].params


def test_chain_two_gauss_kept_single_is_unchanged():
    exps = T._exposures(src_y=0.05)
    _fill(exps, SINGLE, 60.0, seed=4)
    off = PC.choose_shape(exps, _cfg(), is_prism=True)
    on = PC.choose_shape(exps, _cfg(profile_fit_two_gauss=True), is_prism=True)
    assert on.two and not on.two['adopt'] and on.result is on.candidates['own']
    assert on.result.params == off.result.params
    assert np.array_equal(on.result.flux, off.result.flux, equal_nan=True)
    assert on.header_cards()['PF2G'][0] is False and 'PFHALO' not in on.header_cards()


def test_two_gauss_not_tried_below_gate():
    exps = T._exposures(src_y=0.05)
    _fill(exps, CORE_HALO, 0.3, seed=5)
    C = PC.choose_shape(exps, _cfg(profile_fit_two_gauss=True), is_prism=True)
    assert C.shape == 'default' and C.two == {} and 'own2' not in C.candidates


def test_transferred_halo_fits_a_grating_better_than_dropping_it():
    """A grating taking a two-Gaussian PRISM shape: with h and r carried the fixed-shape
    fit describes the data; with only the core width (h dropped) it does not."""
    exps = T._exposures(src_y=0.05)
    for e in exps:
        e.grating = 'G395M/F290LP'
    _fill(exps, CORE_HALO, 60.0, seed=6)
    M = PF.ProfileModel(exps)
    carry = PF.fit_fixed(model=M, shape=CORE_HALO, fit_eoff=True)
    drop = PF.fit_fixed(model=M, shape={k: v for k, v in CORE_HALO.items() if not k.startswith('halo')},
                        fit_eoff=True)
    assert carry.params['halo'] == 0.45 and 'halo' not in drop.params
    assert carry.chi2 < 0.7 * drop.chi2
    # the reported flux follows the shape: main-shutter factor below the single Gaussian's
    lam = np.array([2.0, 3.0])
    assert np.all(PF.main_shutter_factor(carry, lam) < PF.main_shutter_factor(drop, lam))


def test_pass1_settings_signature_only_when_on():
    from campfire_pipeline.nirspec import profile_contam as CT
    assert 'two_gauss' not in CT.pass1_settings({})
    s = CT.pass1_settings(dict(profile_fit_two_gauss=True))
    assert s['two_gauss'] == '25.0,2.0,0.95,0.6'
