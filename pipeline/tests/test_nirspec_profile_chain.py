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


def test_gate_mean_square_scale_counts_the_tails():
    """Noise with heavier tails than a Gaussian (a t distribution, unit variance) and no
    source: a MAD of the residuals reads low and would inflate the detection S/N of
    nothing; the gate's clipped mean square counts the tails and reads above it."""
    rng = np.random.default_rng(11)
    for seed in range(4):
        exps = T._exposures()
        _fill(exps, dict(width=0.15), 0.0, seed=seed)
        nu = 4.0
        M0 = PF.ProfileModel(exps)
        for k, e in enumerate(exps):                # heavy-tailed differences with the stated variance
            t = rng.standard_t(nu, size=e.diff.shape) / np.sqrt(nu / (nu - 2))
            e.diff = t * np.sqrt(M0.diff_variance(k))
        M = PF.ProfileModel(exps)
        _, det = PF.detection_snr(M)
        f, _, _, A = M.solve(det['shape'], np.zeros(len(M.exps)))
        r = ((M.pix['d'] - A @ f) / np.sqrt(M.pix['v']))[M.use]
        mad = (1.4826 * np.median(np.abs(r - np.median(r)))) ** 2 / float(np.median(M.dof_weight()[M.use]))
        assert mad < 0.9 * det['c']


# ---------------------------------------------------------------------------
# bounds, mapping, fixed fits
# ---------------------------------------------------------------------------
def test_config_knobs_reach_priors_and_bounds():
    cfg = _cfg(prior_centre_sigma=0.05, prior_width_log_sigma=0.4, bound_centre=0.2, bound_width_max=0.5,
               gate_snr_prism=12.0)
    assert cfg.priors.centre_sigma == 0.05 and cfg.priors.width_log_sigma == 0.4
    assert cfg.gate_prism == 12.0
    M = PF.ProfileModel(T._exposures(src_y=0.1), priors=cfg.priors, bounds=cfg.bounds)
    lo, hi = M.bounds()
    assert lo[0] == pytest.approx(0.1 - 0.2) and hi[0] == pytest.approx(0.1 + 0.2)
    assert hi[2] == pytest.approx(np.log(0.5)) and lo.size == 4
    with pytest.raises(ValueError):
        PC.ChainConfig.from_stage_config(dict(profile_fit_shape='nope'))


def test_bound_hits_bitmask():
    M = PF.ProfileModel(T._exposures(src_y=0.0), bounds=PF.Bounds(centre=0.3, width_max=0.6))
    lo, hi = M.bounds()
    q = np.array([hi[0], 0.0, np.log(0.2), 0.0])
    assert M.bound_hits(q) == PF.BOUND_BITS['centre']
    q = np.array([0.0, 0.0, hi[2], lo[3]])
    assert M.bound_hits(q) == PF.BOUND_BITS['width'] | PF.BOUND_BITS['width_change']
    q = np.array([0.0, 0.0, lo[2], 0.0])                   # point-source floor is not a hit
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
    shutters: the light that the closed bottom shutter would have passed is missing.
    The shutters clip before the post-shutter blur, so an image is the sum of its open
    shutters' images; what lands on the closed shutter's rows is only the blur from the
    open ones."""
    exps = _grating(T._exposures())
    exps[0].open_k = (0, 1)                 # bottom shutter closed in the grating configuration
    _fill(exps, dict(width=0.15), 20.0)
    R = PF.fit_fixed(exps=exps, shape=dict(centre_offset=0.05, width=0.3, drift=0.0, width_change=0.0))
    closed = PF.frame_image(R, 0)
    e = R.model.exps[0]
    e.open_k = (-1,)
    bottom = PF.frame_image(R, 0)
    e.open_k = (-1, 0, 1)
    full = PF.frame_image(R, 0)
    assert np.allclose(closed + bottom, full, rtol=1e-10, atol=1e-12 * np.nanmax(full), equal_nan=True)
    rows = (e.y > -1.5) & (e.y < -0.5)
    assert np.nansum(bottom[rows]) > 0.5 * np.nansum(closed[rows])   # the missing light is not negligible


def test_fits_take_no_pointing_offsets():
    """Every fit holds the per-exposure pointing offsets at zero (they took neighbours'
    light in crowded data), and fit_fixed's chi2 is the data chi2 only."""
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 50.0)
    M = PF.ProfileModel(exps)
    a = PF.fit_fixed(model=M)
    assert np.all(a.eoff == 0) and a.eoff.size == len(exps)
    p, e = PF._qvec(a)
    assert M.chi2_at(p, e, a.use) == pytest.approx(a.chi2, rel=1e-6)
    b = PF.fit(exps, model=M)
    assert np.all(b.eoff == 0) and b.eoff.size == len(exps) and len(M.param_names()) == 4


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------
def test_a_faint_source_takes_its_own_fit_and_the_prior_decides_its_shape():
    """No detection gate (user, 2026-11-10): a source far below the old gates takes its own
    fit, and with little in the data its width and centre stay near the prior's (median
    width 0.15 at the planned position), so the extraction is close to the default shape's.
    The detection S/N is still measured and reported."""
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 0.3, seed=3)
    for sc in (dict(), dict(gate_snr_prism=1e6)):            # the gate keys no longer reach the chain
        C = PC.choose_shape(exps, PC.ChainConfig.from_stage_config(sc), is_prism=True)
        assert C.shape == 'own' and C.reason.split(';')[0] == 'own_fit'
        assert np.isfinite(C.snr_det) and C.snr_det < 25
        assert 'own' in C.candidates and np.isfinite(C.chi2['default'])
        p = C.result.params
        assert 0.05 < p['width'] < 0.45 and abs(p['centre'] - p['centre_prior']) < 0.2


def test_chain_picks_own_fit():
    exps = T._exposures()
    _fill(exps, dict(centre_offset=0.1, width=0.25), 30.0, seed=3)
    C = PC.choose_shape(exps, _cfg(), is_prism=True)
    assert C.shape == 'own' and C.reason == 'own_fit'
    assert C.result.params['width'] == pytest.approx(0.25, abs=0.04)
    assert C.chi2['own'] < C.chi2['default']
    assert 'MBKGSCL' not in C.header_cards() or not np.isfinite(C.header_cards()['MBKGSCL'])   # nodded: no term


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


def test_sibling_grating_shape_is_held_over_the_wavelengths_and_labelled_grating():
    """A grating takes the shape of its source's other grating that took its own fit, as it
    takes a PRISM shape (kept unless its own fit is better and different at detection S/N
    ``own_vs_prism_min_gate``): its centre offset and width at that grating's pivot, without
    the chromatic terms (they would be extrapolated an octave), labelled 'grating'."""
    from campfire_pipeline.nirspec import profile_contam as CT
    truth = dict(centre_offset=0.12, width=0.08, drift=0.0, width_change=0.0)
    exps = _grating(T._exposures(src_y=0.05))
    _fill(exps, truth, 8.0, seed=11)
    h = fits.Header()
    h.update(SHAPE='own', SRCCEN=0.05 + 0.12, SRCCENPL=0.05, SRCDRIFT=0.25, SRCWIDTH=0.08, SRCDWID=0.2,
             SRCPIVOT=2.3)
    ref = PC.PrismShape.from_header('obs_g235m_f170lp_7', h, chromatic=CT.is_prism_product('obs_g235m_f170lp_7'))
    assert not ref.chromatic and ref.params['drift'] == 0.0 and ref.params['width_change'] == 0.0
    assert PC.PrismShape.from_header('obs_prism_clear_7', h, chromatic=True).params['drift'] == 0.25
    M = PF.ProfileModel(exps)
    x = PF.map_shape(ref.params, 2 * M.pivot)                # held, wherever the target's pivot is
    assert x['centre_offset'] == pytest.approx(0.12) and x['width'] == pytest.approx(0.08)
    C = PC.choose_shape(exps, _cfg(own_vs_prism_min_gate=1e6), is_prism=False, prism=ref)
    assert C.transfer == 'used' and C.shape == 'grating'
    assert C.reason in ('shapes_agree', 'prism_kept', 'prism_kept:gate') and 'own' in C.candidates
    assert C.result.params['width'] == pytest.approx(0.08)
    assert C.header_cards()['SHAPESRC'] == 'obs_g235m_f170lp_7'


def test_grating_transfer_plan():
    """A source's grating products that fell to the default take the shape of its grating that
    took its own fit (the highest detection S/N when several did); not where a PRISM shape was
    used, a product fell back, or no grating took its own fit."""
    from campfire_pipeline.nirspec.stage3 import grating_transfer_plan

    def r(prod, sid, shape, snr=10.0, transfer='no_prism', prism=False, fallback=None):
        return dict(product=prod, source_id=sid, shape=shape, snr_det=snr, transfer=transfer, is_prism=prism,
                    fallback=fallback)
    results = [r('o_g140m_1', 1, 'default'), r('o_g235m_1', 1, 'own', 80), r('o_g395m_1', 1, 'own', 120),
               r('o_g140m_2', 2, 'default'), r('o_g235m_2', 2, 'default'),                      # no reference
               r('o_g140m_3', 3, 'default', transfer='prism_not_own'), r('o_g235m_3', 3, 'own', 90),
               r('o_prism_3', 3, 'default', prism=True),
               r('o_g140m_4', 4, 'prism', transfer='used'), r('o_g235m_4', 4, 'own', 90),       # PRISM shape used
               r('o_g140m_5', 5, None, fallback='no_crf'), r('o_g235m_5', 5, 'own', 90)]
    assert grating_transfer_plan(results) == {'o_g140m_1': 'o_g395m_1', 'o_g140m_3': 'o_g235m_3'}


def test_refresh_science_after_the_revision(tmp_path):
    """After the revision reran stage 2b: a nod it excluded leaves the product and its
    Spec3 inputs, a nod it now subtracts is only reported, and a product left with no
    nod is not made."""
    import types
    from campfire_pipeline.nirspec import stage3 as S3

    def canonical(name, bkg):
        h = fits.Header()
        h['CFP_BKG'] = bkg
        fits.PrimaryHDU(header=h).writeto(tmp_path / name)
        return str(tmp_path / name)
    a = canonical('a_1.fits', '2026-11-09T10:00:00')
    b = canonical('b_1.fits', 'excluded:override')
    c = canonical('c_1.fits', '2026-11-09T10:00:00')        # was skipped, now subtracted
    d = canonical('d_2.fits', 'skipped:nods=1')
    obs = types.SimpleNamespace(workspace_dir=str(tmp_path), name='o')
    t1 = dict(product_name='o_g395m_1', groups=[[a, b, c]], science=['a_1.fits', 'b_1.fits'], source_id=1)
    t2 = dict(product_name='o_g395m_2', groups=[[d]], science=['d_2.fits'], source_id=2)
    s3_of = {'o_g395m_1': (['a_1.fits', 'b_1.fits'], str(tmp_path), 'o_g395m_1', 1),
             'o_g395m_2': (['d_2.fits'], str(tmp_path), 'o_g395m_2', 2)}
    keep = S3._refresh_science(obs, [t1, t2], s3_of)
    assert keep == [t1] and t1['science'] == ['a_1.fits']
    assert s3_of == {'o_g395m_1': (['a_1.fits'], str(tmp_path), 'o_g395m_1', 1)}


def test_chain_picks_own_when_significantly_better():
    truth = dict(centre_offset=0.2, width=0.12, drift=0.0, width_change=0.0)
    exps = _grating(T._exposures(src_y=0.05))
    _fill(exps, truth, 60.0, seed=6)
    M = PF.ProfileModel(exps)
    prism = _prism_of(dict(centre_offset=0.0, width=0.3), M.pivot, 0.05)
    C = PC.choose_shape(exps, _cfg(), is_prism=False, prism=prism)
    assert C.shape == 'own' and C.reason == 'own_better'
    assert C.shdis == 1
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


def test_a_centre_on_its_bound_is_kept_and_flagged():
    """A bright source 0.45 off plan (bound 0.3): the own fit ends on the centre bound; it is
    kept, and the bits and SHAPEWHY say so."""
    exps = T._exposures(src_y=0.0)
    _fill(exps, dict(centre_offset=0.45, width=0.05), 60.0, seed=8)
    C = PC.choose_shape(exps, _cfg(), is_prism=True)
    assert C.bound_bits & PF.BOUND_BITS['centre']
    assert C.shape == 'own' and C.reason == 'own_fit;bound:centre'


def test_a_width_on_its_bound_is_kept_and_the_reported_flux_hardly_depends_on_the_width():
    """A bright source wider than the width bound: the own fit ends on the width bound and
    is kept, flagged (user, 2026-11-10; until then a width on its bound fell back to the
    default shape). The reported flux is the light that entered the shutter, which the data
    measure: the fit on its bound and the default shape both report it to about 10 %, within
    2 % of each other, though neither has the true width."""
    exps = T._exposures(src_y=0.0)
    R_true = _fill(exps, dict(centre_offset=0.0, width=0.35), 60.0, seed=8)
    C = PC.choose_shape(exps, _cfg(bound_width_max=0.2), is_prism=True)
    assert C.bound_bits & PF.BOUND_BITS['width']
    assert C.shape == 'own' and C.reason.startswith('own_fit;bound:') and 'width' in C.reason
    assert C.result.params['width'] == pytest.approx(0.2, rel=1e-2)
    assert C.chi2['default'] - C.chi2['own'] > 100

    def reported(R):
        g = np.isfinite(R.flux) & (R.lam > 1.0) & (R.lam < 4.8)
        return float(np.sum((R.flux * PF.reported_factor(R, R.lam))[g]))
    truth = reported(R_true)
    own, default = reported(C.result) / truth, reported(C.candidates['default']) / truth
    assert abs(own - 1) < 0.15 and abs(default - 1) < 0.15 and abs(own - default) < 0.02


def test_bound_bits_never_change_the_choice():
    """A noisy fit pushed onto a tight width bound is kept as well; only the flag differs."""
    exps = T._exposures(src_y=0.0)
    _fill(exps, dict(centre_offset=0.0, width=0.15), 4.0, seed=9)
    cfg = _cfg(bound_width_max=0.16, prior_width_log_sigma=5.0)
    C = PC.choose_shape(exps, cfg, is_prism=True)
    assert C.shape == 'own'
    assert ('bound:' in C.reason) == bool(C.bound_bits)


def test_chain_masked_fraction_skips_own_fit():
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 40.0)
    C = PC.choose_shape(exps, _cfg(), is_prism=True, clean=dict(overall=0.3, per_exposure=[0.3, 0.3, 0.3]))
    assert C.shape == 'default' and C.reason.startswith('masked:0.30')


def test_chain_dominance_is_recorded_only():
    """Other sources dominate the trace although the subtraction kept most of it (a
    well-predicted contaminant is subtracted, not masked): the ratio is kept on the
    result, measured at the fallback shape, and the own fit is still used."""
    exps = T._exposures()
    _fill(exps, dict(width=0.15), 40.0)
    clean = dict(overall=0.9, per_exposure=[0.9] * 3, clean_pred=0.3)
    seen = []

    def ratio(R):
        seen.append(R)
        return 3.0
    C = PC.choose_shape(exps, _cfg(), is_prism=True, clean=clean, contam_ratio=ratio)
    assert C.shape == 'own' and C.dominance == 3.0
    assert seen and seen[0] is C.candidates['default']


def test_chain_fixed_mode():
    exps = T._exposures()
    _fill(exps, dict(width=0.25), 30.0)
    X = PC.choose_shape(exps, _cfg(profile_fit_shape='fixed'), is_prism=True)
    assert X.chain == 'fixed' and X.shape == 'default'
    assert X.result.params['width'] == pytest.approx(0.15)


def test_header_flags_written():
    exps = _grating(T._exposures())
    _fill(exps, dict(width=0.15), 40.0)
    prism = _prism_of(dict(centre_offset=0.0, width=0.15), 3.0, 0.1)
    C = PC.choose_shape(exps, _cfg(), is_prism=False, prism=prism)
    from campfire_pipeline.nirspec import extraction_cards as XC
    from campfire_pipeline.nirspec.profile_product import shape_header
    h = shape_header(C)
    for key, typ in [('EXTRVER', str), ('OPTICVER', str), ('FLUXWID', float), ('SHAPE', str), ('SHAPEWHY', str),
                     ('SHAPESRC', str), ('DETSNR', float), ('SRCCEN', float), ('SRCCENPL', float),
                     ('SRCDRIFT', float), ('SRCWIDTH', float), ('SRCDWID', float), ('SRCPIVOT', float),
                     ('SRCCHROM', bool), ('SRCCHI2', float)]:
        assert key in h and isinstance(h[key], typ), key
    # every card carries its description from the one list
    assert all(h.comments[k] == XC.CARDS[k] for k in h)
    back = PC.PrismShape.from_header('x', h)
    assert back.params['width'] == pytest.approx(C.result.params['width'])


def test_extraction_cards_omit_what_does_not_apply():
    from campfire_pipeline.nirspec import extraction_cards as XC
    h = fits.Header()
    XC.put(h, 'CONTFRAC', 0.2)
    XC.put(h, 'CONTFRAC', np.nan)                 # no longer applies: removed, not a placeholder
    XC.put(h, 'SHAPESRC', '')
    XC.put(h, 'NBRDET', np.bool_(True))
    assert 'CONTFRAC' not in h and 'SHAPESRC' not in h and h['NBRDET'] is True
    with pytest.raises(KeyError):
        XC.put(h, 'PFSHAPE', 'own')
    # descriptions fit beside a numeric value on one 80-character card
    assert all(len(d) <= 47 for d in XC.CARDS.values())
    assert all(len(k) <= 8 for k in XC.CARDS)
