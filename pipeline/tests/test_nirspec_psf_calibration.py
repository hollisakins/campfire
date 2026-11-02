"""Tests for the cross-dispersion PSF calibration (``nirspec.psf_calibration``).

Synthetic three-nod stars on a common detector frame (the slitlet fixed, the star
moving one shutter between nods), generated with ``profile_fit``'s own image model
from a known PSF: a star is a point source (galaxy width at the floor), the PSF a
core of sigma S rows plus a wing of fraction f. The calibration must give that PSF
back through the same model.
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


def _inject(exps, S_rows, f, centre, drift=0.0, flux=3000.0, noise=1.0, seed=1):
    """Nod differences of a point source with a known PSF, through the model; noise per
    frame, differenced like the data. Centre law off: ``centre`` is where the light is."""
    tabs = PC.constant_tables(S_rows, f) if np.isscalar(S_rows) else S_rows
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
    A2 = PF.ProfileModel(exps, psf_tables=PC.constant_tables(0.7, 0.1)).design(p, np.zeros(3))
    assert not np.allclose(A0.toarray(), A2.toarray())


def test_no_centre_law_puts_the_light_at_the_centre():
    M = PF.ProfileModel(_exposures(), centre_law_tables=PF.NO_CENTRE_LAW)
    lam = np.array([1.0, 4.5])
    c = M.centre(np.array([0.1, 0.0, np.log(0.1), 0.0]), np.zeros(3), 0, lam)
    assert np.allclose(c, 0.1)


# ---------------------------------------------------------------------------
# one star
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('S_true,f_true,centre', [(0.40, 0.25, 0.05), (0.55, 0.15, -0.2), (0.33, 0.35, 0.25)])
def test_core_width_and_centre_recovered_at_the_true_wing_fraction(S_true, f_true, centre):
    exps = _exposures(src_y=centre)
    _inject(exps, S_true, f_true, centre)
    RF = PC.RangeFit(exps, (2.0, 2.6))
    RF.set_floor(np.zeros(RF.M.npix))
    ref = RF.fit(tables=PF.PSF_TABLES)
    RF.set_floor(ref['_model'])
    fr = RF.fit(f_wing=f_true, x0=ref['_x'] + [0.45])
    assert abs(fr['S'] - S_true) < 0.01
    assert abs(fr['c_node'] - centre) < 0.005
    assert fr['peak_snr'] > 30


def test_wrong_wing_fraction_trades_against_the_core_width():
    """Held at a larger wing fraction, the fitted core is narrower (the degeneracy the
    joint wing fit resolves), and the fit is worse."""
    exps = _exposures()
    _inject(exps, 0.42, 0.25, 0.05)
    RF = PC.RangeFit(exps, (1.0, 1.5))
    RF.set_floor(np.zeros(RF.M.npix))
    ref = RF.fit(tables=PF.PSF_TABLES)
    RF.set_floor(ref['_model'])
    true = RF.fit(f_wing=0.25, x0=ref['_x'] + [0.45], use=ref['_use'])
    wide = RF.fit(f_wing=0.45, x0=ref['_x'] + [0.45], use=ref['_use'])
    assert wide['S'] < true['S'] - 0.02
    assert wide['chi2'] > true['chi2']


def test_fit_star_profile_minimum_at_the_true_wing_fraction():
    exps = _exposures()
    _inject(exps, 0.45, 0.2, 0.05, flux=6000.0)
    rec, _ = PC.fit_star(_task(), exps=exps, f_grid=np.round(np.arange(0.05, 0.41, 0.05), 3))
    for row in rec['ranges'][1:6]:
        f, c, S = PC._profile_curve(row)
        assert abs(PC._parabola_min(f, c) - 0.2) < 0.04
        assert abs(S[np.argmin(np.abs(f - 0.2))] - 0.45) < 0.01     # the width at the true f


# ---------------------------------------------------------------------------
# the population
# ---------------------------------------------------------------------------
def _population(n=8, S_mean=0.42, S_scatter=0.03, f_true=0.25, seed=3):
    rng = np.random.default_rng(seed)
    recs, truth = [], []
    for i in range(n):
        S_i = S_mean + S_scatter * rng.normal()
        c = rng.uniform(-0.2, 0.2)
        exps = _exposures(src_y=c)
        _inject(exps, S_i, f_true, c, flux=4000.0, seed=10 + i)
        rec, _ = PC.fit_star(_task(key=f'syn_{i + 1}_NRS1', srcy=c), exps=exps,
                             f_grid=np.round(np.arange(0.1, 0.41, 0.05), 3))
        recs.append(rec)
        truth.append(S_i)
    return recs, np.array(truth)


@pytest.fixture(scope='module')
def population():
    return _population()


def test_joint_wing_fraction_and_table(population):
    recs, truth = population
    sel = PC.select(recs)
    assert not sel['outliers']
    joint = PC.joint_wing_fraction(recs, sel['clean'])
    for r in range(1, 6):
        assert abs(joint[('NRS1', r)]['f'] - 0.25) < 0.03
        assert joint[('NRS1', r)]['n'] == len(recs)
    wing = {k: v['f'] for k, v in joint.items()}
    table = PC.build_table(recs, sel['clean'], stage='free', wing=wing)
    t = table['NRS1']
    for r in range(1, 6):
        assert t['f'][r] == pytest.approx(wing[('NRS1', r)])
        assert t['S_rows'][r] == pytest.approx(np.percentile(
            [rec['ranges'][r]['free']['S'] for rec in recs], 25))
        # held at the current table's f (not 0.25) the widths trade off a little
        assert abs(t['S_p50'][r] - np.median(truth)) < 0.04


def test_selection_rejects_faint_edge_and_odd_stars():
    recs, _ = _population(n=6, seed=5)
    faint = _exposures()
    _inject(faint, 0.42, 0.25, 0.05, flux=30.0, seed=99)
    rec_f, _ = PC.fit_star(_task(key='syn_90_NRS1'), exps=faint, f_grid=None)
    edge = _exposures(src_y=0.38)
    _inject(edge, 0.42, 0.25, 0.38, flux=4000.0, seed=98)
    rec_e, _ = PC.fit_star(_task(key='syn_91_NRS1', srcy=0.38), exps=edge, f_grid=None)
    broad = _exposures()
    _inject(broad, 0.75, 0.25, 0.05, flux=4000.0, seed=97)
    rec_b, _ = PC.fit_star(_task(key='syn_92_NRS1'), exps=broad, f_grid=None)
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
    _inject(exps, 0.42, 0.25, 0.05)                 # law on: each nod at its planned position
    RF = PC.RangeFit(exps, (1.0, 2.6), nod_law=False)
    RF.set_floor(np.zeros(RF.M.npix))
    ref = RF.fit(tables=PF.PSF_TABLES)
    RF.set_floor(ref['_model'])
    fr = RF.fit(f_wing=0.25, x0=ref['_x'] + [0.45])
    off, err, chi2 = PC._fit_offsets(RF, 0.25, fr['_x'], fr['_use'])
    assert np.allclose(off, [0.0, 0.016, -0.016], atol=0.002)
    assert chi2 < fr['chi2']
    rows = PC.offset_rows([dict(key='syn', obs='syn', detector='NRS1', grating='PRISM/CLEAR',
                                exposures=[dict(nod=i + 1, src_y=e.src_y, has_source=True, yoffset=0.5 * (i % 2),
                                                y_start=100, x_start=100, quadrant=1, shutter_row=50,
                                                shutter_col=50) for i, e in enumerate(exps)],
                                ranges=[dict(range=0, lam=[1.0, 2.6], offsets=off, errors=err, peak_snr=50.0,
                                             dy=DY)])])
    assert np.allclose([r['off'] for r in rows], [r['planned'] for r in rows], atol=0.002)


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
