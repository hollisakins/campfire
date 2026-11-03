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
# shutter edges
# ---------------------------------------------------------------------------
def _edges(half=0.5, shift=0.0, after=0.5, disp='prism', lam=(0.5, 6.0)):
    n = len(lam)
    t = dict(lam=np.array(lam, float), half=np.broadcast_to(np.asarray(half, float), (n,)).copy(),
             shift=np.broadcast_to(np.asarray(shift, float), (n,)).copy(),
             after=np.broadcast_to(np.asarray(after, float), (n,)).copy())
    return {disp: t, 'fallback': {}}


def test_hard_edge_as_measured_edges_is_the_default_model():
    """The default (no edges) is the hard edge at +-OPEN_HALF with an even split; written as
    measured edges it gives the same image and transmission."""
    exps = _exposures()
    p = np.array([0.08, 0.02, np.log(0.12), 0.0])
    A0 = PF.ProfileModel(exps).design(p, np.zeros(3)).toarray()
    A1 = PF.ProfileModel(exps, shutter_edges=_edges(PF.OPEN_HALF, 0.0, 0.5)).design(p, np.zeros(3)).toarray()
    assert np.allclose(A0, A1, rtol=1e-12, atol=1e-14)
    assert PF.shutter_edge(None, 'PRISM/CLEAR', np.array([1.0]))[0] == PF.OPEN_HALF


def test_measured_edge_is_what_uniform_light_sees():
    """A very wide source (uniform light) through the model with a measured edge is
    ``psf_calibration.uniform_profile``, the bar-shadow model the edge is fitted with: the
    open area at shift +- half, blurred after the shutter by the share ``after`` of the PSF."""
    exps = _exposures()
    E = _edges(0.46, 0.01, 0.62)
    M = PF.ProfileModel(exps, shutter_edges=E)
    e = exps[0]                                       # open shutters -1, 0, +1
    y = np.linspace(-1.0, 1.0, 201) * MSA_SLIT_PITCH
    for lam in (1.0, 4.0):
        L, D = np.full(y.size, lam), np.full(y.size, DY)
        p = np.array([0.0, 0.0, np.log(40.0), 0.0])   # a 40-slit-unit galaxy: uniform over the slitlet
        img = M.image(p, np.zeros(3), 0, y, L, D)
        img = img / np.interp(0.0, y, img)
        ref = PC.uniform_profile(y / MSA_SLIT_PITCH, lam, e.detector, DY, 0.46, 0.01, 0.62, n_open=3)
        assert np.max(np.abs(img - ref)) < 2e-3, lam


def test_measured_edge_narrows_the_opening():
    """A narrower opening passes less of a centred point source and moves the 50 % point of
    uniform light inward by the change of half-width."""
    exps = _exposures()
    p = np.array([0.0, 0.0, np.log(PF.W_FLOOR), 0.0])
    lam, dy = np.array([4.0]), np.array([DY])
    t_hard = PF.ProfileModel(exps).transmission(p, np.zeros(3), 0, lam, dy, width=0.0, aperture=(0,), centre=0.3)
    t_new = PF.ProfileModel(exps, shutter_edges=_edges(0.45, 0.0, 0.5)).transmission(
        p, np.zeros(3), 0, lam, dy, width=0.0, aperture=(0,), centre=0.3)
    assert t_new < t_hard
    y = np.linspace(0.0, 0.9, 901)
    for half in (0.5, 0.45):
        u = PC.uniform_profile(y, 4.0, 'NRS1', DY, half, 0.0, 0.5)
        y50 = y[np.argmin(np.abs(u - 0.5))] * MSA_SLIT_PITCH
        assert abs(y50 - half) < 0.01


def test_shutter_edge_fallback_and_unknown_disperser():
    E = _edges(0.47, 0.0, 0.6, disp='g395m')
    E['fallback'] = {'g395h': 'g395m'}
    lam = np.array([3.0, 4.5])
    assert np.allclose(PF.shutter_edge(E, 'G395H/F290LP', lam)[0], PF.shutter_edge(E, 'G395M/F290LP', lam)[0])
    with pytest.raises(KeyError):
        PF.shutter_edge(E, 'G140M/F100LP', lam)


def test_fit_shutter_edges_recovers_a_known_edge(tmp_path):
    """Bar-shadow tables made with a known edge (half-width falling with wavelength, a shift, an
    after-shutter share) through ``uniform_profile``: ``fit_shutter_edges`` gives them back."""
    from astropy.io import fits
    lam = np.linspace(1.0, 4.0, 7)
    y = np.round(np.arange(-1.0, 1.0001, 0.01), 4)
    half = 0.49 - 0.012 * np.log(lam)
    d11 = np.array([PC.uniform_profile(y, L, 'NRS1', DY, h, 0.006, 0.62, 1) for L, h in zip(lam, half)]).T
    d13 = np.array([PC.uniform_profile(y, L, 'NRS1', DY, h, 0.006, 0.62, 3) for L, h in zip(lam, half)]).T
    hdus = [fits.PrimaryHDU()]
    for name, d in (('DATA1X1', d11), ('DATA1X3', d13)):
        h = fits.ImageHDU(d.astype('f4'), name=name)
        h.header.update(CRVAL1=lam[0] * 1e-6, CDELT1=(lam[1] - lam[0]) * 1e-6, CRVAL2=-1.0, CDELT2=0.01)
        hdus.append(h)
    fits.HDUList(hdus).writeto(tmp_path / PC.BARSHADOW_FILE.format('prism'))
    r = PC.fit_shutter_edges('prism', dy=DY, nodes=[1.0, 2.0, 4.0], dets=('NRS1',), ystep=1,
                             data_dir=str(tmp_path), log=lambda *a: None)
    got = PC.spline_eval(np.array(r['coefs']['half']), lam, np.array(r['nodes']))
    assert np.max(np.abs(got - half)) < 0.003
    assert np.allclose(r['coefs']['shift'], 0.006, atol=0.002)
    assert np.allclose(r['coefs']['after'], 0.62, atol=0.05)
    assert max(r['rms_edge']) < 2e-3 < min(r['rms_default'])


def test_shipped_shutter_edge_file_is_sane():
    """The measured-edge data file (not the extraction's default): every disperser with a table
    has an opening narrower than the nominal shutter, a small shift, an after-shutter share in
    (0, 1), and the H gratings resolve to their M grating."""
    E = PF.load_shutter_edges()
    for d in ('prism', 'g140m', 'g235m', 'g395m'):
        t = E[d]
        assert np.all((t['half'] > 0.40) & (t['half'] <= PF.OPEN_HALF))
        assert np.all(np.abs(t['shift']) < 0.03)
        assert np.all((t['after'] > 0.0) & (t['after'] < 1.0))
    for h, m in (('G140H/F100LP', 'g140m'), ('G235H/F170LP', 'g235m'), ('G395H/F290LP', 'g395m')):
        assert np.allclose(PF.shutter_edge(E, h, np.array([2.0]))[0], np.interp(2.0, E[m]['lam'], E[m]['half']))


# ---------------------------------------------------------------------------
# shutter transmission T (geometric edge, T = 1 - c2 x^2 - c4 x^4 inside each shutter)
# ---------------------------------------------------------------------------
def _T_edges(c2=-0.3, c4=0.8, after=0.5, shift=0.0, disp='prism', lam=(0.5, 6.0)):
    E = _edges(PF.OPEN_HALF, shift, after, disp=disp, lam=lam)
    E[disp]['c2'] = np.full(len(lam), float(c2))
    E[disp]['c4'] = np.full(len(lam), float(c4))
    return E


def test_transmission_window_is_the_integral_of_T():
    """``_window`` with a polynomial transmission is the integral of T(u) N(u; m, s) over the open
    shutters, from Gaussians far outside to ones straddling an edge or a bar."""
    from scipy.stats import norm
    iv = PF.open_intervals((-1, 0, 1), PF.OPEN_HALF, 0.0)
    tp = {2: -0.3, 4: 0.8}
    m = np.array([-2.5, -1.1, -0.66, -0.48, 0.0, 0.3, 0.52, 0.9, 1.7])
    s = np.array([0.05, 0.03, 0.05, 0.08, 0.04, 0.06, 0.2, 0.1, 0.07])
    got = PF._window(m, s, iv, tp)
    for mm, ss, g in zip(m, s, got):
        ref = 0.0
        for lo, hi in iv:
            u = np.linspace(lo, hi, 100001)
            x = (u - 0.5 * (lo + hi)) / (0.5 * (hi - lo))
            ref += np.trapezoid((1 + 0.3 * x ** 2 - 0.8 * x ** 4) * norm.pdf(u, mm, ss), u)
        assert g == pytest.approx(ref, abs=1e-8)


def test_flat_transmission_is_the_hard_edge():
    """T = 1 (all coefficients zero) through the compiled path gives the default model's image."""
    exps = _exposures()
    p = np.array([0.08, 0.02, np.log(0.12), 0.0])
    A0 = PF.ProfileModel(exps).design(p, np.zeros(3)).toarray()
    A1 = PF.ProfileModel(exps, shutter_edges=_T_edges(0.0, 0.0, 0.5)).design(p, np.zeros(3)).toarray()
    assert np.allclose(A0, A1, rtol=1e-10, atol=1e-13)


def test_transmission_is_what_uniform_light_sees():
    """A very wide source through the model with T is ``uniform_profile`` with the same T: the
    bar-shadow model T is fitted with."""
    exps = _exposures()
    E = _T_edges(-0.3, 0.8, 0.55, 0.005)
    M = PF.ProfileModel(exps, shutter_edges=E)
    y = np.linspace(-1.0, 1.0, 201) * MSA_SLIT_PITCH
    for lam in (1.0, 4.0):
        L, D = np.full(y.size, lam), np.full(y.size, DY)
        img = M.image(np.array([0.0, 0.0, np.log(40.0), 0.0]), np.zeros(3), 0, y, L, D)
        img = img / np.interp(0.0, y, img)
        ref = PC.uniform_profile(y / MSA_SLIT_PITCH, lam, exps[0].detector, DY, PF.OPEN_HALF, 0.005, 0.55, 3,
                                 None, {2: -0.3, 4: 0.8})
        assert np.max(np.abs(img - ref)) < 2e-3, lam


def test_transmission_lowers_an_edge_source_more_than_a_centred_one():
    exps = _exposures()
    p = np.array([0.0, 0.0, np.log(PF.W_FLOOR), 0.0])
    lam, dy = np.array([3.0]), np.array([DY])
    hard, T = PF.ProfileModel(exps), PF.ProfileModel(exps, shutter_edges=_T_edges(-0.2, 0.9, 0.5))
    t = {n: [M.transmission(p, np.zeros(3), 0, lam, dy, width=0.0, aperture=(0,), centre=c)[0] for c in (0.0, 0.4)]
         for n, M in (('hard', hard), ('T', T))}
    assert t['T'][0] / t['hard'][0] > 0.98                  # centre: T(0) = 1
    assert t['T'][1] / t['hard'][1] < 0.9                   # near the edge: T < 1


def test_model_follows_a_change_of_shutter_edges():
    """The per-exposure shutter cache follows the model's edges: the same model with another set of
    edges gives another image."""
    exps = _exposures()
    M = PF.ProfileModel(exps, shutter_edges=_edges(0.46, 0.0, 0.6))
    p = np.array([0.3, 0.0, np.log(0.12), 0.0])
    A1 = M.design(p, np.zeros(3)).toarray()
    M.shutter_edges = _T_edges(-0.3, 0.8, 0.5)
    A2 = M.design(p, np.zeros(3)).toarray()
    M.shutter_edges = None
    A3 = M.design(p, np.zeros(3)).toarray()
    assert not np.allclose(A1, A2) and not np.allclose(A2, A3)
    assert np.allclose(A3, PF.ProfileModel(exps).design(p, np.zeros(3)).toarray())


def test_fit_shutter_edges_recovers_a_known_transmission(tmp_path):
    """Bar-shadow tables made with the geometric edge and a known T and after-shutter share:
    ``fit_shutter_edges(form='T')`` gives them back."""
    from astropy.io import fits
    lam = np.linspace(1.0, 4.0, 7)
    y = np.round(np.arange(-1.0, 1.0001, 0.01), 4)
    tp = {2: -0.25, 4: 0.7}
    d11 = np.array([PC.uniform_profile(y, L, 'NRS1', DY, PF.OPEN_HALF, 0.004, 0.55, 1, None, tp) for L in lam]).T
    d13 = np.array([PC.uniform_profile(y, L, 'NRS1', DY, PF.OPEN_HALF, 0.004, 0.55, 3, None, tp) for L in lam]).T
    hdus = [fits.PrimaryHDU()]
    for name, d in (('DATA1X1', d11), ('DATA1X3', d13)):
        h = fits.ImageHDU(d.astype('f4'), name=name)
        h.header.update(CRVAL1=lam[0] * 1e-6, CDELT1=(lam[1] - lam[0]) * 1e-6, CRVAL2=-1.0, CDELT2=0.01)
        hdus.append(h)
    fits.HDUList(hdus).writeto(tmp_path / PC.BARSHADOW_FILE.format('prism'))
    r = PC.fit_shutter_edges('prism', dy=DY, nodes=[1.0, 2.0, 4.0], dets=('NRS1',), ystep=1,
                             data_dir=str(tmp_path), log=lambda *a: None, form='T')
    x = np.linspace(0, 1, 11)
    T_true = 1 - tp[2] * x ** 2 - tp[4] * x ** 4
    for L in lam:
        c2 = PC.spline_eval(np.array(r['coefs']['c2']), L, np.array(r['nodes']))
        c4 = PC.spline_eval(np.array(r['coefs']['c4']), L, np.array(r['nodes']))
        assert np.max(np.abs(1 - c2 * x ** 2 - c4 * x ** 4 - T_true)) < 0.03
    assert np.allclose(r['coefs']['after'], 0.55, atol=0.05)
    assert max(r['rms_edge']) < 3e-3 < min(r['rms_default'])


def test_shipped_shutter_transmission_file_is_sane():
    """The shutter-transmission data file (not the extraction's default): the geometric half-width,
    T between 0 and a few percent above 1 across every shutter, and the H gratings resolved."""
    E = PF.load_shutter_edges(PF.SHUTTER_TRANSMISSION_FILE)
    x = np.linspace(0, 1, 21)
    for d in ('prism', 'g140m', 'g235m', 'g395m'):
        t = E[d]
        assert np.allclose(t['half'], PF.OPEN_HALF)
        assert np.all((t['after'] > 0.0) & (t['after'] < 1.0))
        T = 1 - t['c2'][:, None] * x ** 2 - t['c4'][:, None] * x ** 4
        assert np.all(T >= 0.0) and np.all(T <= 1.1)
    W = PF.shutter_window(E, 'G395H/F290LP', np.array([4.0]))
    assert W['tpoly'] is not None and np.allclose(W['half'], PF.OPEN_HALF)
