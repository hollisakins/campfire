"""Tests for the shape chain of the extraction v2 prototype (nirspec/profile_chain.py)
and the profile-fit primitives it uses (gate statistic, planned position, bounds,
shape transfer).

Synthetic three-nod slitlets from test_nirspec_profile_fit; data are drawn at the
frame level and differenced like stage 2b, so the nod bookkeeping is exercised.
"""

import numpy as np
import pytest
from astropy.io import fits

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_chain as PC

import test_nirspec_profile_fit as T

NY, NX = T.NY, T.NX


def _fill(exps, shape, flux_scale, noise=1.0, seed=1, eoff=None):
    """Frame-level data from a shape dict (centre_offset, drift, width, width_change)."""
    M = PF.ProfileModel(exps, chromatic=True)
    p = PF.shape_vector(shape, M.c_prior, 0.15)
    params = dict(centre=p[0], drift=p[1], width=float(np.exp(p[2])), width_change=p[3])
    R = T._truth_result(exps, params, np.zeros(len(exps)) if eoff is None else eoff, flux_scale)
    frames = T._frames_from_truth(exps, R)
    rng = np.random.default_rng(seed)
    nz = [noise * rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = frames[k] + nz[k] - np.mean([frames[j] + nz[j] for j in e.partners], axis=0)
        e.var = np.full((NY, NX), noise ** 2)
    return R


def _grating(exps):
    for e in exps:
        e.grating = 'G395M/F290LP'
    return exps


def _cfg(**kw):
    sc = dict(gate_snr_prism=10.0, gate_snr_grating=30.0)
    sc.update(kw)
    return PC.ChainConfig.from_stage_config(sc)


# ---------------------------------------------------------------------------
# planned position
# ---------------------------------------------------------------------------
def test_planned_position_ignores_zero_placeholder():
    exps = T._exposures(src_y=0.12)
    exps[2].src_y = 0.0
    c, flag = PF.planned_position(exps)
    assert c == pytest.approx(0.12) and flag == 1


def test_planned_position_flags_disagreement():
    exps = T._exposures(src_y=0.12)
    exps[2].src_y = 0.30
    c, flag = PF.planned_position(exps, tolerance=0.05)
    assert flag == 2 and c == pytest.approx(0.12)
    assert PF.planned_position(T._exposures(src_y=0.12))[1] == 0


# ---------------------------------------------------------------------------
# gate statistic
# ---------------------------------------------------------------------------
def test_detection_snr_null_is_near_zero():
    vals = []
    for seed in range(4):
        exps = T._exposures()
        _fill(exps, dict(width=0.15), 0.0, seed=seed)
        s, det = PF.detection_snr(PF.ProfileModel(exps))
        vals.append(s)
        assert 0.8 < det['c'] < 1.25                      # noise scale ~1 for correct errors
    assert max(vals) < 8.0


def test_detection_snr_scales_with_flux():
    s = []
    for scale in (2.0, 4.0):
        exps = T._exposures()
        _fill(exps, dict(width=0.15), scale, seed=7)
        s.append(PF.detection_snr(PF.ProfileModel(exps))[0])
    assert s[0] > 20
    assert s[1] / s[0] == pytest.approx(2.0, rel=0.15)


def test_detection_snr_does_not_penalise_offset_sources():
    """The best of the starting shapes is used: a compact source 0.3 off plan is not
    under-detected by the default template."""
    a, b = T._exposures(), T._exposures()
    _fill(a, dict(width=0.15), 2.0, seed=2)
    _fill(b, dict(centre_offset=0.3, width=0.03), 2.0, seed=2)
    sa, sb = PF.detection_snr(PF.ProfileModel(a))[0], PF.detection_snr(PF.ProfileModel(b))[0]
    assert sb > 0.7 * sa


def test_noise_scale_is_robust_to_shape_mismatch():
    """A bright source fitted with the wrong (default) shape: the reduced chi2 grows,
    the MAD noise scale (the old gate option) stays ~1."""
    exps = T._exposures()
    _fill(exps, dict(centre_offset=0.25, width=0.05), 200.0, seed=4)
    M = PF.ProfileModel(exps)
    R = PF.fit_fixed(model=M)
    assert R.chi2_red > 2.0
    _, det = PF.detection_snr(M, scale='mad')
    assert det['c'] < 0.5 * R.chi2_red and det['c'] < 1.6
    assert det['c'] == det['c_mad']


def test_gate_mean_square_scale_counts_the_tails():
    """Noise with heavier tails than a Gaussian (a t distribution, unit variance): the
    MAD reads low, so dchi2 / c_mad inflates the detection S/N of nothing; the
    clipped mean square stays near the true variance and the null stays low."""
    rng = np.random.default_rng(11)
    gates = {'mad': [], 'mean_square': []}
    for seed in range(4):
        exps = T._exposures()
        _fill(exps, dict(width=0.15), 0.0, seed=seed)
        nu = 4.0
        M0 = PF.ProfileModel(exps)
        for k, e in enumerate(exps):                # heavy-tailed differences with the stated variance
            t = rng.standard_t(nu, size=e.diff.shape) / np.sqrt(nu / (nu - 2))
            e.diff = t * np.sqrt(M0.diff_variance(k))
        M = PF.ProfileModel(exps)
        for sc in gates:
            s, det = PF.detection_snr(M, scale=sc)
            gates[sc].append(s)
        assert det['c_mad'] < 0.9 * det['c_ms']
    assert np.median(gates['mean_square']) < np.median(gates['mad'])


# ---------------------------------------------------------------------------
# bounds, mapping, fixed fits
# ---------------------------------------------------------------------------
def test_config_knobs_reach_priors_and_bounds():
    cfg = _cfg(prior_centre_sigma=0.05, prior_width_log_sigma=0.4, bound_centre=0.2, bound_width_max=0.5,
               bound_offset=0.05, profile_fit_prism_mode='always', gate_snr_prism=12.0)
    assert cfg.priors.centre_sigma == 0.05 and cfg.priors.width_log_sigma == 0.4
    assert cfg.prism_mode == 'always' and cfg.gate_prism == 12.0
    M = PF.ProfileModel(T._exposures(src_y=0.1), priors=cfg.priors, bounds=cfg.bounds)
    lo, hi = M.bounds()
    assert lo[0] == pytest.approx(0.1 - 0.2) and hi[0] == pytest.approx(0.1 + 0.2)
    assert hi[2] == pytest.approx(np.log(0.5)) and hi[-1] == pytest.approx(0.05)
    with pytest.raises(ValueError):
        PC.ChainConfig.from_stage_config(dict(profile_fit_shape='nope'))


def test_bound_hits_bitmask():
    M = PF.ProfileModel(T._exposures(src_y=0.0), bounds=PF.Bounds(centre=0.3, width_max=0.6))
    lo, hi = M.bounds()
    q = np.array([hi[0], 0.0, np.log(0.2), 0.0, 0.0, 0.0])
    assert M.bound_hits(q) == PF.BOUND_BITS['centre']
    q = np.array([0.0, 0.0, hi[2], 0.0, 0.0, lo[-1]])
    assert M.bound_hits(q) == PF.BOUND_BITS['width'] | PF.BOUND_BITS['offset']
    q = np.array([0.0, 0.0, lo[2], 0.0, 0.0, 0.0])         # point-source floor is not a hit
    assert M.bound_hits(q) == 0


def test_map_shape_roundtrip():
    src = dict(centre=0.25, centre_prior=0.1, drift=0.03, width=0.2, width_change=0.04, pivot=3.0)
    there = PF.map_shape(src, 4.2)
    back = PF.map_shape(dict(centre=0.1 + there['centre_offset'], centre_prior=0.1, drift=there['drift'],
                             width=there['width'], width_change=there['width_change'], pivot=4.2), 3.0)
    assert back['centre_offset'] == pytest.approx(0.15)
    assert back['width'] == pytest.approx(0.2)
    assert there['centre_offset'] == pytest.approx(0.15 + 0.03 * np.log2(4.2 / 3.0))


def test_prism_transfer_uses_grating_shutters():
    """A transferred shape is evaluated through the grating exposure's own open
    shutters: no model light where that exposure's slitlet is closed."""
    exps = _grating(T._exposures())
    exps[0].open_k = (0, 1)                 # bottom shutter closed in the grating configuration
    _fill(exps, dict(width=0.15), 20.0)
    R = PF.fit_fixed(exps=exps, shape=dict(centre_offset=0.05, width=0.3, drift=0.0, width_change=0.0))
    img = PF.frame_image(R, 0)
    y = exps[0].y
    closed = (y < -0.5 - 0.6)               # below the open area, beyond the PSF blur
    assert np.nanmax(np.abs(img[closed])) < 1e-3 * np.nanmax(img)


def test_fit_fixed_with_offsets_reports_data_chi2_only():
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 50.0, eoff=np.array([0.0, 0.02, -0.02]))
    M = PF.ProfileModel(exps)
    a = PF.fit_fixed(model=M)
    b = PF.fit_fixed(model=M, fit_eoff=True)
    assert b.chi2 < a.chi2
    assert b.eoff[1] == pytest.approx(0.02, abs=0.01) and b.eoff[2] == pytest.approx(-0.02, abs=0.01)
    p, e = PF._qvec(b)
    assert M.chi2_at(p, e, b.use) == pytest.approx(b.chi2, rel=1e-6)


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------
def test_chain_picks_default_below_gate():
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 0.3, seed=3)
    C = PC.choose_shape(exps, _cfg(), is_prism=True)
    assert C.shape == 'default' and C.reason == 'below_gate'
    assert 'own' not in C.candidates and np.isfinite(C.chi2['default'])


def test_chain_picks_own_above_gate():
    exps = T._exposures()
    _fill(exps, dict(centre_offset=0.1, width=0.25), 30.0, seed=3)
    C = PC.choose_shape(exps, _cfg(), is_prism=True)
    assert C.shape == 'own' and C.reason == 'gate_passed'
    assert C.result.params['width'] == pytest.approx(0.25, abs=0.04)
    assert C.chi2['own'] < C.chi2['default']


def _prism_of(shape, pivot, c_prior, pa=np.nan):
    p = dict(centre=c_prior + shape['centre_offset'], centre_prior=c_prior, drift=shape.get('drift', 0.0),
             width=shape['width'], width_change=shape.get('width_change', 0.0), pivot=pivot)
    return PC.PrismShape(product='p_prism_clear_1', shape='own', params=p, pa_v3=pa)


def test_chain_picks_prism_when_own_not_better():
    truth = dict(centre_offset=0.08, width=0.2, drift=0.0, width_change=0.0)
    exps = _grating(T._exposures(src_y=0.05))
    _fill(exps, truth, 40.0, seed=5)
    M = PF.ProfileModel(exps)
    prism = _prism_of(truth, M.pivot, 0.05)
    C = PC.choose_shape(exps, _cfg(), is_prism=False, prism=prism)
    assert C.transfer == 'used' and C.shape == 'prism', C.reason
    assert C.reason in ('shapes_agree', 'prism_kept')
    assert C.shdis == 0


def test_chain_picks_own_when_significantly_better():
    truth = dict(centre_offset=0.2, width=0.12, drift=0.0, width_change=0.0)
    exps = _grating(T._exposures(src_y=0.05))
    _fill(exps, truth, 60.0, seed=6)
    M = PF.ProfileModel(exps)
    prism = _prism_of(dict(centre_offset=0.0, width=0.3), M.pivot, 0.05)
    C = PC.choose_shape(exps, _cfg(), is_prism=False, prism=prism)
    assert C.shape == 'own' and C.reason == 'own_better'
    assert C.shdis == 1
    C2 = PC.choose_shape(exps, _cfg(profile_fit_prism_mode='always'), is_prism=False, prism=prism)
    assert C2.shape == 'prism' and C2.reason == 'prism_always'
    # below the own-vs-PRISM gate floor the PRISM shape is kept even when the own fit is better
    C3 = PC.choose_shape(exps, _cfg(own_vs_prism_min_gate=10 * C.snr_det), is_prism=False, prism=prism)
    assert C3.shape == 'prism' and C3.reason == 'prism_kept:gate'


def test_chain_no_transfer_at_a_different_roll():
    truth = dict(centre_offset=0.0, width=0.15)
    exps = _grating(T._exposures())
    for e in exps:
        e.pa_v3 = 120.0
    _fill(exps, truth, 40.0)
    prism = _prism_of(dict(truth, drift=0.0, width_change=0.0), 3.0, 0.1, pa=130.0)
    C = PC.choose_shape(exps, _cfg(), is_prism=False, prism=prism)
    assert C.transfer == 'pa_mismatch' and 'prism' not in C.candidates


def test_bound_hit_detected_and_chain_keeps_own_when_fallback_is_worse():
    """A bright source 0.45 off plan (bound 0.3): the own fit ends on the centre
    bound, the default shape describes the data far worse, so the own fit is kept
    and flagged (review: never fall back to a clearly worse shape)."""
    exps = T._exposures(src_y=0.0)
    _fill(exps, dict(centre_offset=0.45, width=0.05), 60.0, seed=8)
    C = PC.choose_shape(exps, _cfg(), is_prism=True)
    assert C.bound_bits & PF.BOUND_BITS['centre']
    assert C.shape == 'own' and C.reason.startswith('bound_kept')


def test_bound_hit_falls_back_when_fallback_fits_as_well():
    exps = T._exposures(src_y=0.0)
    _fill(exps, dict(centre_offset=0.0, width=0.15), 4.0, seed=9)
    cfg = _cfg(bound_width_max=0.16, prior_width_log_sigma=5.0, gate_snr_prism=1.0)
    C = PC.choose_shape(exps, cfg, is_prism=True)
    if C.bound_bits:                       # the noise pushed the width onto its (tight) bound
        assert C.shape == 'default' and C.reason.startswith('bound:')
    else:
        assert C.shape == 'own'


def test_chain_masked_fraction_skips_own_fit():
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 40.0)
    C = PC.choose_shape(exps, _cfg(), is_prism=True, clean=dict(overall=0.3, per_exposure=[0.3, 0.3, 0.3]))
    assert C.shape == 'default' and C.reason.startswith('masked:0.30')


def test_chain_dominance_rules_skip_own_fit():
    """Other sources dominate the trace although hybrid masking kept most of it
    (a well-predicted contaminant is subtracted, not masked): no own fit."""
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 40.0)
    clean = dict(overall=0.9, per_exposure=[0.9] * 3, clean_pred=0.3)
    seen = []

    def ratio(R):
        seen.append(R)
        return 3.0
    always = dict(contam_dominance_apply='always')
    C = PC.choose_shape(exps, _cfg(**always), is_prism=True, clean=clean, contam_ratio=ratio)
    assert C.shape == 'default' and C.reason == 'dominated:3.00' and C.dominance == 3.0
    assert seen and seen[0] is C.candidates['default']             # measured at the fallback shape
    C = PC.choose_shape(exps, _cfg(**always), is_prism=True, clean=clean, contam_ratio=lambda R: 0.5)
    assert C.shape == 'own'
    C = PC.choose_shape(exps, _cfg(contam_dominance='pixels', **always), is_prism=True, clean=clean)
    assert C.reason == 'dominated:0.30'
    C = PC.choose_shape(exps, _cfg(contam_dominance='off'), is_prism=True, clean=clean, contam_ratio=ratio)
    assert C.shape == 'own'
    # default (round 3): the rule applies only in mask mode; in hybrid mode it is a flag (PFDOMIN)
    C = PC.choose_shape(exps, _cfg(), is_prism=True, clean=clean, contam_ratio=ratio)
    assert C.shape == 'own' and C.dominance == 3.0
    C = PC.choose_shape(exps, _cfg(contam_mode='mask'), is_prism=True, clean=clean, contam_ratio=ratio)
    assert C.reason == 'dominated:3.00'
    C = PC.choose_shape(exps, _cfg(contam_mode='mask', contam_dominance_apply='flag'), is_prism=True, clean=clean,
                        contam_ratio=ratio)
    assert C.shape == 'own'


def test_chain_free_and_fixed_modes():
    exps = T._exposures()
    _fill(exps, dict(width=0.25), 30.0)
    F = PC.choose_shape(exps, _cfg(profile_fit_shape='free'), is_prism=True)
    assert F.chain == 'free' and F.shape == 'own'
    assert F.result.model.priors.centre_sigma == PF.Priors().centre_sigma      # V0 priors
    X = PC.choose_shape(exps, _cfg(profile_fit_shape='fixed'), is_prism=True)
    assert X.chain == 'fixed' and X.shape == 'default'
    assert X.result.params['width'] == pytest.approx(0.15)


def test_header_flags_written():
    exps = _grating(T._exposures())
    _fill(exps, dict(width=0.15), 40.0)
    prism = _prism_of(dict(centre_offset=0.0, width=0.15), 3.0, 0.1)
    C = PC.choose_shape(exps, _cfg(), is_prism=False, prism=prism)
    cards = C.header_cards()
    h = fits.Header()
    for k, v in cards.items():
        h[k] = v
    for key, typ in [('PFVER', str), ('PFSHAPE', str), ('PFCHAIN', str), ('PFREASON', str), ('PFGATE', float),
                     ('PFGATETH', float), ('PFCHI2D', float), ('PFCHI2P', float), ('PFCHI2O', float),
                     ('PFBOUND', int), ('PFSRCY', int), ('PFSHDIS', int), ('PFPRISM', str), ('PFXFER', str),
                     ('PFCEN', float), ('PFCENPR', float), ('PFDRIFT', float), ('PFWIDTH', float),
                     ('PFDWIDTH', float), ('PFPIVOT', float)]:
        assert key in h and isinstance(h[key], typ), key
    back = PC.PrismShape.from_header('x', h)
    assert back.params['width'] == pytest.approx(C.result.params['width'])
