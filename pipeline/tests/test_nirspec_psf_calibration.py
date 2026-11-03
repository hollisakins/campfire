"""Tests for the cross-dispersion PSF calibration (``nirspec.psf_calibration``).

Synthetic three-nod stars on a common detector frame (the slitlet fixed, the star
moving one shutter between nods), generated with ``profile_fit``'s own image model
from a known PSF (a general components table): a star is a point source (galaxy width
at the floor). The calibration must give that PSF back through the same model.
"""

import numpy as np
import pytest

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import psf_calibration as PC
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

DY = 0.227
NY, NX = 30, 200


def _exposures(src_y=0.05, det='NRS1', quad=1):
    rows = np.arange(NY)[:, None] * np.ones((1, NX))
    lam = np.linspace(0.7, 5.3, NX)[None, :] * np.ones((NY, 1)) + 0.002 * (rows - NY / 2)
    r_mid = NY / 2
    shift = MSA_SLIT_PITCH / DY
    setups = [((-1, 0, 1), r_mid), ((0, 1, 2), r_mid - shift), ((-2, -1, 0), r_mid + shift)]
    exps = []
    for i, (open_k, r_src) in enumerate(setups):
        y = (rows - r_src) * DY
        exps.append(PF.Exposure(
            name=f'jw00000001001_03101_0000{i + 1}_{det.lower()}_1.fits', group=0, detector=det,
            grating='PRISM/CLEAR', quadrant=quad, diff=np.zeros((NY, NX)), var=np.ones((NY, NX)),
            ok=np.ones((NY, NX), bool), wav=lam.copy(), y=y, pl_applied=np.full((NY, NX), 0.8),
            pl_point=np.full((NY, NX), 0.8), src_y=src_y, open_k=open_k,
            partners=[j for j in range(3) if j != i], use_data=True, cutout=(0, 0, NY, NX)))
    return exps


def _psf(core, wing_frac=0.25, wing_sigma=0.9):
    """A general PSF table, the same at every wavelength and on both detectors: a core of
    width ``core`` rows and a wing."""
    t = dict(lam=[0.0, 100.0], components=[dict(sigma=core), dict(sigma=wing_sigma, frac=wing_frac)])
    return {'NRS1': t, 'NRS2': t}


def _inject(exps, tabs, centre, drift=0.0, flux=3000.0, noise=1.0, seed=1):
    """Nod differences of a point source with a known PSF (``tabs``, general layout), through
    the model; noise per frame, differenced like the data. Centre law off: ``centre`` is where
    the light is."""
    M = PF.ProfileModel(exps, psf_tables=tabs, centre_law_tables=PF.NO_CENTRE_LAW)
    lam = M.lam_grid
    spec = flux * (1.0 + 0.3 * np.sin(2 * lam))
    R = PF.FitResult(params=dict(centre=centre, drift=drift, width=PF.W_FLOOR, width_change=0.0, pivot=M.pivot,
                                 centre_prior=M.c_prior),
                     eoff=np.zeros(len(exps)), lam=lam, flux=spec, flux_err=np.zeros_like(lam), chi2=0.0, npix=0,
                     nfree=0, chromatic=True, model=M)
    rng = np.random.default_rng(seed)
    fn = [noise * rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        m = PF.model_frame(R, k, applied_pathloss=False, common=True)
        e.diff = m + fn[k] - np.mean([fn[j] for j in e.partners], axis=0)
        e.var = np.full(m.shape, noise ** 2)
    return M


def _task(key='syn_1_NRS1', det='NRS1', quad=1, srcy=0.05):
    return PC.StarTask(obs='syn', source_id=int(key.split('_')[1]), detector=det, groups=[], overrides=[None],
                       science=[], quadrant=quad, x_start=1000, y_start=100, srcy=srcy)


# ---------------------------------------------------------------------------
# the hook in profile_fit
# ---------------------------------------------------------------------------
def test_model_tables_default_to_the_module_tables():
    exps = _exposures()
    p = np.array([0.05, 0.0, np.log(0.1), 0.0])
    A0 = PF.ProfileModel(exps).design(p, np.zeros(3))
    A1 = PF.ProfileModel(exps, psf_tables=PF.PSF_TABLES, centre_law_tables=PF.CENTRE_LAW).design(p, np.zeros(3))
    assert np.allclose(A0.toarray(), A1.toarray())
    A2 = PF.ProfileModel(exps, psf_tables=_psf(0.7, 0.1)).design(p, np.zeros(3))
    assert not np.allclose(A0.toarray(), A2.toarray())


def test_no_centre_law_puts_the_light_at_the_centre():
    M = PF.ProfileModel(_exposures(), centre_law_tables=PF.NO_CENTRE_LAW)
    lam = np.array([1.0, 4.5])
    c = M.centre(np.array([0.1, 0.0, np.log(0.1), 0.0]), np.zeros(3), 0, lam)
    assert np.allclose(c, 0.1)


def test_width_tables_scale_the_shipped_core_only():
    lam = np.array([0.8, 2.3, 4.5])
    ship = PF.psf_components(lam, 'NRS1')
    tab = PC.width_tables(0.5, 2.3)
    comps = PF.psf_components(lam, 'NRS1', tab)
    k = 0.5 / ship[0]['sigma'][1]
    assert comps[0]['sigma'][1] == pytest.approx(0.5)
    assert np.allclose(comps[0]['sigma'], k * ship[0]['sigma'])     # its wavelength dependence kept
    for c, s in zip(comps[1:], ship[1:]):
        for key in ('sigma', 'frac', 'offset'):
            assert np.allclose(c[key], s[key])


# ---------------------------------------------------------------------------
# one star
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('S_true,centre', [(0.40, 0.05), (0.55, -0.2), (0.33, 0.25)])
def test_core_width_and_centre_recovered(S_true, centre):
    """A star made with the shipped PSF at a known core width: the width fit gives it back."""
    exps = _exposures(src_y=centre)
    _inject(exps, PC.width_tables(S_true, 2.3), centre)     # 2.3 um: the range centre
    RF = PC.RangeFit(exps, (2.0, 2.6))
    fr = PC._range_start(RF)
    assert abs(fr['S'] - S_true) < 0.01
    assert abs(fr['c_node'] - centre) < 0.005
    assert fr['peak_snr'] > 30


def test_fit_star_records_width_and_centre_per_range():
    exps = _exposures()
    _inject(exps, PF.PSF_TABLES, 0.05, flux=6000.0)
    rec = PC.fit_star(_task(), exps=exps)
    assert len(rec['ranges']) == len(PC.RANGES)
    for row in rec['ranges'][1:6]:
        RF = PC.RangeFit(exps, tuple(row['lam']))
        assert abs(row['free']['S'] - RF.core0) < 0.01             # the shipped width, as made
        assert abs(row['free']['c_node'] - 0.05) < 0.005
        assert PC.range_ok(row['free'])


# ---------------------------------------------------------------------------
# the population
# ---------------------------------------------------------------------------
def _population(n=6, scatter=0.03, seed=3):
    """Stars made with the shipped PSF, each core scaled by its own factor."""
    rng = np.random.default_rng(seed)
    recs = []
    for i in range(n):
        k = 1.0 + scatter * rng.normal()
        c = rng.uniform(-0.2, 0.2)
        exps = _exposures(src_y=c)
        core = PF.psf_components(np.array([1.75]), 'NRS1')[0]['sigma'][0]
        _inject(exps, PC.width_tables(k * core, 1.75), c, flux=4000.0, seed=10 + i)
        recs.append(PC.fit_star(_task(key=f'syn_{i + 1}_NRS1', srcy=c), exps=exps))
    return recs


def test_selection_rejects_faint_edge_and_odd_stars():
    recs = _population()
    faint = _exposures()
    _inject(faint, PF.PSF_TABLES, 0.05, flux=30.0, seed=99)
    rec_f = PC.fit_star(_task(key='syn_90_NRS1'), exps=faint)
    edge = _exposures(src_y=0.38)
    _inject(edge, PF.PSF_TABLES, 0.38, flux=4000.0, seed=98)
    rec_e = PC.fit_star(_task(key='syn_91_NRS1', srcy=0.38), exps=edge)
    broad = _exposures()
    _inject(broad, PC.width_tables(0.75, 1.75), 0.05, flux=4000.0, seed=97)
    rec_b = PC.fit_star(_task(key='syn_92_NRS1'), exps=broad)
    sel = PC.select(recs + [rec_f, rec_e, rec_b])
    clean = set(sel['clean'][('NRS1', 2)])
    assert 'syn_90_NRS1' not in clean and 'syn_90_NRS1' not in sel['usable'].get(('NRS1', 2), [])
    assert 'syn_91_NRS1' in sel['edge'] and 'syn_91_NRS1' not in clean
    assert 'syn_92_NRS1' in sel['outliers']
    assert len(clean) == 6


def test_free_offsets_recover_the_nod_shifts():
    """Stars made with the nods at their planned positions (the law on): fitted with one
    shared position plus a free offset per exposure, the offsets are the planned shifts."""
    exps = _exposures(src_y=0.05)
    for e, s in zip(exps, (0.05, 0.066, 0.034)):
        e.src_y = s
    _inject(exps, PF.PSF_TABLES, 0.05)               # law on: each nod at its planned position
    RF = PC.RangeFit(exps, (1.0, 2.6), nod_law=False)
    fr = PC._range_start(RF)
    off, err, chi2 = PC._fit_offsets(RF, fr['_x'], fr['_use'])
    assert np.allclose(off, [0.0, 0.016, -0.016], atol=0.002)
    assert chi2 < fr['chi2']
    rows = PC.offset_rows([dict(key='syn', obs='syn', detector='NRS1', grating='PRISM/CLEAR',
                                exposures=[dict(nod=i + 1, src_y=e.src_y, has_source=True, yoffset=0.5 * (i % 2),
                                                y_start=100, x_start=100, quadrant=1, shutter_row=50,
                                                shutter_col=50) for i, e in enumerate(exps)],
                                ranges=[dict(range=0, lam=[1.0, 2.6], offsets=off, errors=err, peak_snr=50.0,
                                             dy=DY)])])
    assert np.allclose([r['off'] for r in rows], [r['planned'] for r in rows], atol=0.002)


def test_three_component_psf_recovered():
    """A star made with a core, a wing of its own width and offset and a one-sided shoulder
    after the shutter: ``fit_form`` gives the shape back."""
    truth = dict(core_sigma=0.38, wing_frac=0.25, wing_sigma=0.95, wing_offset=0.10, third_frac=0.05,
                 third_sigma=0.8, third_offset=1.4)
    form = PC.FORMS['wing3_after']
    exps = _exposures(src_y=0.05)
    _inject(exps, form.table(truth, [0.0, 100.0], None), 0.05, flux=20000.0)
    RF = PC.RangeFit(exps, (1.5, 2.6))
    ref = PC._range_start(RF)                            # floor, then the mask (as the star fits do)
    start = dict(core_sigma=0.4, wing_frac=0.3, wing_sigma=0.9, wing_offset=0.0, third_frac=0.03, third_sigma=0.6,
                 third_offset=1.0)
    g = PC.fit_form(RF, form, np.array([1.5, 2.6]), start=start, use=ref['_use'],
                    centre_start=np.array([0.05, 0.05]), det='NRS1')
    # the components trade against each other (shoulder share against wing share); what the data
    # fix is the profile: compared at the range centre, to 1 % of its peak
    lam = np.array([2.05])
    u = np.linspace(-5, 5, 401)

    def profile(table):
        comps = PF.psf_components(lam, 'NRS1', table)
        return sum(c['frac'][0] * np.exp(-0.5 * ((u - c['offset'][0]) / c['sigma'][0]) ** 2) / c['sigma'][0]
                   for c in comps) / np.sqrt(2 * np.pi)
    p_true, p_fit = profile(form.table(truth, [0.0, 100.0], None)), profile(g['_tables'])
    assert np.max(np.abs(p_fit - p_true)) < 0.01 * p_true.max()
    assert abs(np.mean(g['values']['core_sigma']) - truth['core_sigma']) < 0.03
    assert np.allclose(g['centre'], 0.05, atol=0.005)


def test_smooth_joint_fit_recovers_a_smooth_psf():
    """Stars made with a PSF whose parameters vary smoothly with wavelength (a spline over the
    nodes): the joint fit of all stars and ranges at once gives the profile back across the range,
    from a start off the truth (spline coefficients and the stars' centres and offsets in one problem)."""
    # the machinery (splines over the nodes, all ranges at once, per-star centres and offsets) on the
    # core + wing form with its own width and offset; the shoulder's trade with the wing is a property
    # of the data, tested on the stars by the held-out test
    form = PC.FORMS['wing']
    nodes = PC.SMOOTH_NODES
    x = np.log(nodes / 2.0)
    truth = dict(core_sigma=0.38 + 0.06 * x + 0.04 * x ** 2, wing_frac=0.25 - 0.05 * x,
                 wing_sigma=0.9 + 0.2 * x, wing_offset=0.08 + 0.0 * x)
    tab_true = PC.smooth_table(form, truth, 'NRS1')
    units = []
    for i, c in enumerate((-0.1, 0.05, 0.15)):
        exps = _exposures(src_y=c)
        _inject(exps, {'NRS1': tab_true['NRS1'], 'NRS2': tab_true['NRS1']}, c, flux=15000.0, seed=30 + i)
        for rng in ((0.8, 1.6), (1.6, 3.0), (3.0, 5.2)):
            RF = PC.RangeFit(exps, rng)
            fr = PC._range_start(RF)                     # floor, then the mask
            units.append(dict(key=f's{i}', range=rng, RF=RF, use=fr['_use'], x=fr['_x'][:2],
                              eoff=np.zeros(3), scale=1.0))
    start = {p: np.asarray(v) * 1.1 for p, v in truth.items()}
    start['wing_offset'] = np.zeros(nodes.size)
    out = PC.smooth_joint_fit_all(units, form, "NRS1", start, log=lambda *a: None, tol=1e-6, passes=1)
    fit = PC.smooth_table(form, {p: np.array(v) for p, v in out['coefs'].items()}, 'NRS1')
    u = np.linspace(-5, 5, 401)
    for lam in (1.0, 2.0, 4.0):
        def profile(table):
            comps = PF.psf_components(np.array([lam]), 'NRS1', table)
            return sum(c['frac'][0] * np.exp(-0.5 * ((u - c['offset'][0]) / c['sigma'][0]) ** 2) / c['sigma'][0]
                       for c in comps) / np.sqrt(2 * np.pi)
        p_true, p_fit = profile(tab_true), profile(fit)
        assert np.max(np.abs(p_fit - p_true)) < 0.01 * p_true.max(), lam


def test_split_halves_is_fixed_and_disjoint():
    keys = [f'k{i}' for i in range(21)]
    a1, b1 = PC.split_halves(keys)
    a2, b2 = PC.split_halves(list(reversed(keys)))
    assert a1 == a2 and b1 == b2 and not (a1 & b1) and len(a1 | b1) == 21


def test_centre_law_recovered():
    """Stars in two quadrants with a known drift per quadrant and slope with position."""
    rng = np.random.default_rng(7)
    D = {1: np.array([0.01, 0.005, 0.0, -0.005, -0.012, -0.02, -0.03]),
         2: np.array([0.012, 0.006, 0.0, -0.01, -0.02, -0.04, -0.06])}
    alpha = np.array([0.01, 0.005, 0.0, -0.01, -0.04, -0.07, -0.1])
    rows = []
    for q in (1, 2):
        for i in range(12):
            cref = rng.uniform(-0.35, 0.35)
            dc = D[q] + alpha * cref + 0.002 * rng.normal(size=7)
            dc = dc - np.mean(dc[list(PC.REF_RANGES)])     # zero point at the reference ranges
            rows.append(dict(key=f'{q}_{i}', det='NRS1', quad=q, cref=cref, srcy=cref, dc=dc, err=np.full(7, 0.002)))
    law = PC.centre_law_refit(rows)
    for q in (1, 2):
        got = np.array(law[f'Q{q}_NRS1']['D'])
        assert np.allclose(got - got[2], D[q] - D[q][2], atol=0.004)
    a = np.array(law['NRS1']['alpha'])
    assert np.allclose(a - np.mean(a[list(PC.REF_RANGES)]), alpha - np.mean(alpha[list(PC.REF_RANGES)]), atol=0.006)


# ---------------------------------------------------------------------------
# the shutter model: geometric shutter, transmission T = 1 - (1 - tedge) x^8 inside it
# ---------------------------------------------------------------------------
def _shutter(tedge=0.3, shift=0.0, disp='prism', lam=(0.5, 6.0)):
    n = len(lam)
    t = {k: np.full(n, float(v)) for k, v in dict(shift=shift, tedge=tedge).items()}
    t['lam'] = np.array(lam, float)
    return {disp: t, 'fallback': {}, 'version': 'test'}


def test_window_is_the_integral_of_T():
    """``_window`` is the integral of T(u) N(u; m, s) over the open shutters, from Gaussians far
    outside to ones straddling an edge or a bar, and ones many shutters wide (the moment
    recursion stays accurate there)."""
    from scipy.stats import norm
    iv = PF.open_intervals((-1, 0, 1), 0.01)
    tedge = 0.25
    m = np.array([-2.5, -1.1, -0.66, -0.48, 0.0, 0.3, 0.52, 0.9, 1.7, 0.1, -0.4, 0.2, -0.7, 3.0])
    s = np.array([0.05, 0.03, 0.05, 0.08, 0.04, 0.06, 0.2, 0.1, 0.07, 0.49, 0.51, 1.0, 5.0, 20.0])
    got = PF._window(m, s, iv, tedge)
    for mm, ss, g in zip(m, s, got):
        ref = 0.0
        for lo, hi in iv:
            u = np.linspace(lo, hi, 100001)
            x = (u - 0.5 * (lo + hi)) / (0.5 * (hi - lo))
            ref += np.trapezoid((1 - (1 - tedge) * x ** 8) * norm.pdf(u, mm, ss), u)
        assert g == pytest.approx(ref, abs=1e-8)


def test_window_with_T_one_is_the_clipped_gaussian():
    """With T = 1 the window is the Gaussian's mass inside the open shutters (the machinery's
    plain clip)."""
    from scipy.special import ndtr
    iv = PF.open_intervals((-1, 0, 1))
    m, s = np.linspace(-2.0, 2.0, 81), np.full(81, 0.07)
    ref = sum(ndtr((hi - m) / s) - ndtr((lo - m) / s) for lo, hi in iv)
    assert np.allclose(PF._window(m, s, iv, 1.0), ref, atol=1e-12)


def test_default_model_uses_the_shipped_shutter():
    exps = _exposures()
    p = np.array([0.3, 0.02, np.log(0.12), 0.0])
    A0 = PF.ProfileModel(exps).design(p, np.zeros(3)).toarray()
    A1 = PF.ProfileModel(exps, shutter=PF.SHUTTER).design(p, np.zeros(3)).toarray()
    A2 = PF.ProfileModel(exps, shutter=_shutter(1.0)).design(p, np.zeros(3)).toarray()
    assert np.array_equal(A0, A1)
    assert not np.allclose(A0, A2)


def test_uniform_light_through_the_model_is_uniform_profile():
    """A very wide source through the model is ``psf_calibration.uniform_profile``, the bar-shadow
    model the shutter is fitted with: T convolved with the after-shutter PSF."""
    exps = _exposures()
    M = PF.ProfileModel(exps, shutter=_shutter(0.35, 0.005))
    y = np.linspace(-1.0, 1.0, 201) * MSA_SLIT_PITCH
    for lam in (1.0, 4.0):
        L, D = np.full(y.size, lam), np.full(y.size, DY)
        img = M.image(np.array([0.0, 0.0, np.log(40.0), 0.0]), np.zeros(3), 0, y, L, D)
        img = img / np.interp(0.0, y, img)
        ref = PC.uniform_profile(y / MSA_SLIT_PITCH, lam, exps[0].detector, DY, 0.005, 0.35, n_open=3)
        assert np.max(np.abs(img - ref)) < 2e-3, lam


def test_transmission_lowers_an_edge_source_more_than_a_centred_one():
    exps = _exposures()
    p = np.array([0.0, 0.0, np.log(PF.W_FLOOR), 0.0])
    lam, dy = np.array([3.0]), np.array([DY])
    flat, T = PF.ProfileModel(exps, shutter=_shutter(1.0)), PF.ProfileModel(exps, shutter=_shutter(0.2))
    t = {n: [M.transmission(p, np.zeros(3), 0, lam, dy, width=0.0, aperture=(0,), centre=c)[0] for c in (0.0, 0.4)]
         for n, M in (('flat', flat), ('T', T))}
    assert t['T'][0] / t['flat'][0] > 0.98                 # centre: T(0) = 1
    assert t['T'][1] / t['flat'][1] < 0.85                 # near the edge: T < 1


def test_model_follows_a_change_of_shutter():
    """The per-exposure shutter cache follows the model's shutter."""
    exps = _exposures()
    M = PF.ProfileModel(exps, shutter=_shutter(0.3))
    p = np.array([0.3, 0.0, np.log(0.12), 0.0])
    A1 = M.design(p, np.zeros(3)).toarray()
    M.shutter = _shutter(1.0)
    A2 = M.design(p, np.zeros(3)).toarray()
    M.shutter = None
    A3 = M.design(p, np.zeros(3)).toarray()
    assert not np.allclose(A1, A2) and not np.allclose(A2, A3)
    assert np.allclose(A3, PF.ProfileModel(exps).design(p, np.zeros(3)).toarray())


def test_fit_shutter_recovers_a_known_transmission(tmp_path):
    """Bar-shadow tables made with a known transmission and shift: ``fit_shutter`` gives them
    back."""
    from astropy.io import fits
    lam = np.linspace(1.0, 4.0, 7)
    y = np.round(np.arange(-1.0, 1.0001, 0.01), 4)
    d11 = np.array([PC.uniform_profile(y, L, 'NRS1', DY, 0.004, 0.45, 1) for L in lam]).T
    d13 = np.array([PC.uniform_profile(y, L, 'NRS1', DY, 0.004, 0.45, 3) for L in lam]).T
    hdus = [fits.PrimaryHDU()]
    for name, d in (('DATA1X1', d11), ('DATA1X3', d13)):
        h = fits.ImageHDU(d.astype('f4'), name=name)
        h.header.update(CRVAL1=lam[0] * 1e-6, CDELT1=(lam[1] - lam[0]) * 1e-6, CRVAL2=-1.0, CDELT2=0.01)
        hdus.append(h)
    fits.HDUList(hdus).writeto(tmp_path / PC.BARSHADOW_FILE.format('prism'))
    r = PC.fit_shutter('prism', dy=DY, nodes=[1.0, 2.0, 4.0], dets=('NRS1',), ystep=1, data_dir=str(tmp_path),
                       log=lambda *a: None)
    assert np.allclose(r['coefs']['tedge'], 0.45, atol=0.03)
    assert np.allclose(r['coefs']['shift'], 0.004, atol=0.002)
    assert max(r['rms']) < 3e-3 < min(r['rms_flat'])


def test_shipped_shutter_file_is_sane():
    """The shipped shutter model: for every disperser with a bar-shadow table, T at the edge in
    [0, 1] (so T never exceeds 1 and falls from the centre to the edge) and below 1 somewhere, a
    small shift; the H gratings use the M grating with the same filter; a version for the
    headers."""
    S = PF.load_shutter()
    for d in ('prism', 'g140m', 'g235m', 'g395m'):
        t = S[d]
        assert np.all((t['tedge'] >= 0.0) & (t['tedge'] <= 1.0)) and np.min(t['tedge']) < 0.9
        assert np.all(np.abs(t['shift']) < 0.03)
    for h, m in (('G140H/F100LP', 'g140m'), ('G235H/F170LP', 'g235m'), ('G395H/F290LP', 'g395m')):
        a, b = PF.shutter_at(h, np.array([2.0])), PF.shutter_at(m.upper() + '/X', np.array([2.0]))
        assert all(np.allclose(a[k], b[k]) for k in a)
    assert S['version'] and S['version'] != '?'
