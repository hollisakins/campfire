"""Tests for the same-slitlet neighbour scan (nirspec/nod_scan.py), the background
overrides it proposes (stage3.write_scan_proposals) and their review figure
(profile_product.neighbour_preview, plots.plot_neighbour_proposal).

Synthetic three-nod slitlet from test_nirspec_profile_fit: source in the middle,
bottom and top shutter of a 3-shutter slitlet. A neighbour one shutter pitch above
the target (Delta = +1.15) is in an open shutter in nods 1 and 2; nod 2's frame puts
it on the target's rows of nod 1, and nod 1's frame on the target's rows of nod 3.
"""

import types

import numpy as np
import pytest
import toml

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import nod_scan as NS
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH
from campfire_pipeline.nirspec.observation import Observation
from campfire_pipeline.nirspec import stage3 as S3

import test_nirspec_profile_fit as T

NY, NX = T.NY, T.NX
ROOT = 'jw00000001001_03101'


def _data(exps, target_scale, nbr_scale, delta, seed=1):
    M = PF.ProfileModel(exps)
    c = M.c_prior
    Rt = T._truth_result(exps, dict(centre=c, drift=0.0, width=0.1, width_change=0.0), np.zeros(len(exps)),
                         target_scale)
    Rn = T._truth_result(exps, dict(centre=c + delta, drift=0.0, width=0.1, width_change=0.0),
                         np.zeros(len(exps)), nbr_scale)
    frames = [np.nan_to_num(PF.frame_image(Rt, j)) + np.nan_to_num(PF.frame_image(Rn, j)) for j in range(len(exps))]
    rng = np.random.default_rng(seed)
    nz = [rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = frames[k] + nz[k] - np.mean([frames[j] + nz[j] for j in e.partners], axis=0)
        e.var = np.ones((NY, NX))


def test_scan_recovers_injected_neighbour_delta_and_partner():
    exps = T._exposures(src_y=0.0)
    _data(exps, 30.0, 30.0, MSA_SLIT_PITCH)
    # the target at its true shape: a free fit of a target with an equally bright,
    # unmodelled neighbour one shutter away has its lowest chi2 merged with the neighbour
    # (centre +0.6, width 0.9), under any PSF; which basin a free fit ends in depends
    # on its starting grid, and this test is about the scan
    R = PF.fit_fixed(exps=exps, shape=dict(centre_offset=0.0, width=0.1, drift=0.0, width_change=0.0),
                     chromatic=False)
    S = NS.run(R, dchi2_min=60.0)
    assert S.detected, (S.dchi2, S.dchi2_null)
    assert S.delta == pytest.approx(MSA_SLIT_PITCH, abs=0.1)
    assert S.proposals == {ROOT: {'1': [3], '3': [2]}}
    assert not S.unresolved
    cards = S.header_cards()
    assert cards['NBRDET'] and cards['NBRPROP'] == '03101:1>3;03101:3>2'
    assert 'NBRFLAG' not in cards
    assert S.dchi2_nuisance >= 0.0


def test_a_nods_detectors_give_one_proposal():
    """NRS1 and NRS2 exposures of the same nod: a partner dirty on either detector is
    dropped for the nod (one choice for both), at its larger fraction."""
    exps = T._exposures(src_y=0.0)
    exps2 = T._exposures(src_y=0.0)
    for e in exps2:
        e.name = e.name.replace('nrs1', 'nrs2')
    M = types.SimpleNamespace(exps=exps + exps2)
    R = types.SimpleNamespace(model=M)
    # nod 1 on NRS1: partner nod 2 dirty; nod 1 on NRS2: partner nod 3 dirty a little more
    fr = {0: {1: 0.5, 2: 0.0}, 3: {4: 0.1, 5: 0.3}}
    orig = NS.partner_fractions
    NS.partner_fractions = lambda *a, **k: fr
    try:
        props, unres, dirty, _ = NS.propose(R, 1.15, None)
    finally:
        NS.partner_fractions = orig
    assert props == {} and unres == [f'{ROOT}:1']             # both partners dirty on one or the other
    assert dirty[f'{ROOT}:1'] == {2: 0.5, 3: 0.3}


def test_close_neighbour_leaves_nothing_to_review():
    """A neighbour too close to separate: no proposal and no nod listed as unresolved."""
    exps = T._exposures(src_y=0.0)[:2]
    exps[0].partners, exps[1].partners = [1], [0]
    _data(exps, 30.0, 15.0, 0.75, seed=2)
    S = NS.run(PF.fit_fixed(exps), dchi2_min=60.0, null_ratio=0.0)
    assert S.detected and abs(S.delta) < 0.9
    assert S.proposals == {} and S.unresolved == []


def test_proposal_gain_and_cost():
    # partner 2 puts 0.6 of the target's light on its rows, partner 3 none: dropping 2
    # removes 0.3^2 of squared error; the cost is a third of the variance (1/snr^2)
    g, c = NS.proposal_gain({2: 0.6, 3: 0.0}, [3], snr=10.0)
    assert g == pytest.approx(0.09) and c == pytest.approx(1.0 / 300.0)
    g, c = NS.proposal_gain({2: 0.25, 3: 0.2}, [3], snr=2.0)
    assert g < c                                      # a faint target: not worth it


def test_scan_two_nods_writes_no_override():
    exps = T._exposures(src_y=0.0)[:2]
    exps[0].partners, exps[1].partners = [1], [0]
    _data(exps, 30.0, 30.0, MSA_SLIT_PITCH)
    R = PF.fit(exps, chromatic=False)
    S = NS.run(R, dchi2_min=60.0)
    assert S.detected
    assert S.proposals == {} and S.unresolved == [f'{ROOT}:1']


def test_scan_null_below_threshold():
    exps = T._exposures(src_y=0.0)
    _data(exps, 30.0, 0.0, MSA_SLIT_PITCH, seed=4)
    R = PF.fit(exps, chromatic=False)
    S = NS.run(R, dchi2_min=60.0)
    assert not S.detected and S.dchi2 < 60.0
    assert S.proposals == {}


def test_scan_ignores_faint_neighbour_on_bright_target():
    """Detected, but its light on the target's rows is far below 20 % of the
    target's: no partner is dropped."""
    exps = T._exposures(src_y=0.0)
    _data(exps, 400.0, 20.0, MSA_SLIT_PITCH, seed=6)
    R = PF.fit(exps, chromatic=False)
    S = NS.run(R, dchi2_min=60.0, drop_frac=0.2)
    assert S.proposals == {}


def test_scan_divides_by_noise_scale():
    """Errors understated by 2x: the raw chi2 drops scale by 4; divided by the noise
    scale of the target fit's residuals they are back to the correct ones."""
    exps = T._exposures(src_y=0.0)
    _data(exps, 30.0, 30.0, MSA_SLIT_PITCH, seed=3)
    R = PF.fit(exps, chromatic=False)
    out = {}
    for f in (1.0, 0.5):
        R.model.pix['v'] = R.model.pix['v'] * f ** 2      # same fit, errors understated by 1/f
        out[f] = NS.run(R, dchi2_min=60.0)
    raw = {f: S.dchi2 * S.noise for f, S in out.items()}
    assert raw[0.5] == pytest.approx(4 * raw[1.0], rel=1e-6)
    assert out[0.5].dchi2 == pytest.approx(out[1.0].dchi2, rel=1e-6)
    assert out[0.5].noise == pytest.approx(4 * out[1.0].noise, rel=1e-6)
    exps = T._exposures(src_y=0.0)
    _data(exps, 30.0, 0.0, MSA_SLIT_PITCH, seed=3)                 # no neighbour: correct errors read ~1
    assert 0.85 < NS.run(PF.fit(exps, chromatic=False), dchi2_min=60.0).noise < 1.15


def test_brighter_close_neighbour_is_unresolvable():
    """A neighbour brighter than the target 0.75 slit units away: detected, but no
    partner choice separates it; no proposal."""
    exps = T._exposures(src_y=0.0)
    _data(exps, 10.0, 30.0, 0.75, seed=2)
    R = PF.fit_fixed(exps)                     # the target at its planned position (default shape)
    S = NS.run(R, dchi2_min=60.0, null_ratio=0.0)
    assert S.detected and abs(S.delta) < 0.9 and S.brightness > 1.0
    assert S.proposals == {} and 'unresolvable' in S.suppressed


def test_close_fainter_neighbour_withheld():
    """A fainter neighbour 0.75 slit units away is withheld too (its copies overlap the
    target's rows under every partner choice)."""
    exps = T._exposures(src_y=0.0)
    _data(exps, 30.0, 15.0, 0.75, seed=2)
    R = PF.fit_fixed(exps)
    S = NS.run(R, dchi2_min=60.0, null_ratio=0.0)
    assert S.detected and abs(S.delta) < 0.9 and S.brightness < 1.0
    assert S.proposals == {} and 'too close' in S.suppressed


def test_cross_check_withholds_disagreeing_proposals():
    S = NS.ScanResult(detected=True, delta=1.15, proposals={ROOT: {'1': [3]}})
    S.cross_check((True, 1.20), tol=0.2)
    assert S.xdisp == 'agree' and S.proposals
    S.cross_check((True, 0.40), tol=0.2)
    assert S.xdisp == 'disagree' and not S.proposals and 'other disperser' in S.suppressed
    T2 = NS.ScanResult(detected=True, delta=1.15, proposals={ROOT: {'1': [3]}})
    T2.cross_check((False, np.nan))
    assert T2.xdisp == 'disagree' and not T2.proposals
    U = NS.ScanResult(detected=True, delta=1.15, proposals={ROOT: {'1': [3]}})
    U.cross_check(None)
    assert U.xdisp == 'single' and U.proposals


def _obs(tmp_path):
    o = Observation.__new__(Observation)
    o.name = 'test_obs'
    o.reference_dir = str(tmp_path)
    return o


def _frames_with_neighbour(target_scale, nbr_scale, delta, seed=1):
    """Single frames (target + neighbour + noise) of the three synthetic nods, and a
    stand-in for ``profile_fit.load_source`` that builds their nod differences for any
    background-nod overrides, as the loader does from the frames before subtraction."""
    exps = T._exposures(src_y=0.0)
    M = PF.ProfileModel(exps)
    c = M.c_prior
    Rt = T._truth_result(exps, dict(centre=c, drift=0.0, width=0.1, width_change=0.0), np.zeros(3), target_scale)
    Rn = T._truth_result(exps, dict(centre=c + delta, drift=0.0, width=0.1, width_change=0.0), np.zeros(3),
                         nbr_scale)
    rng = np.random.default_rng(seed)
    frames = [np.nan_to_num(PF.frame_image(Rt, j)) + np.nan_to_num(PF.frame_image(Rn, j)) + rng.normal(size=(NY, NX))
              for j in range(3)]

    def load(groups, bkg_overrides=None, science=None, pre_bkgsub=False, **kw):
        ov = (bkg_overrides or [None])[0] or {}
        ex = T._exposures(src_y=0.0)
        for k, e in enumerate(ex):
            e.path = f'nod{k + 1}.fits'
            want = ov.get(str(k + 1))
            if want is not None:
                e.partners = [j for j in range(3) if j + 1 in want] or None
            if e.partners is not None:
                e.diff = frames[k] - np.mean([frames[j] for j in e.partners], axis=0)
            e.var = np.ones((NY, NX))
        return ex
    return load, Rt


def test_preview_shows_what_the_proposal_changes(tmp_path, monkeypatch):
    """A neighbour one shutter above an equally bright target: its copies through the
    partners are subtracted from the target's rows in nods 1 and 3, so the spectrum with
    every nod as background reads low; with the proposed nods it moves toward the truth,
    with a larger error (fewer background nods). (Since the fits stopped clipping against
    their model, 2026-11-10, the neighbour's own light that stays on the fitted pixels is
    no longer clipped away, so the proposed spectrum is closer to the truth but not within
    half the error of the current one, as it was.) The figure shows each nod's rectified
    difference with both choices, rebuilt from the cached S2D_SCI views."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import profile_product as PP
    from campfire_pipeline.nirspec import plots
    load, Rt = _frames_with_neighbour(30.0, 30.0, MSA_SLIT_PITCH)
    monkeypatch.setattr(PF, 'load_source', load)
    raw = load([['a', 'b', 'c']])
    R = PF.fit_fixed(exps=raw, shape=dict(centre_offset=0.0, width=0.1, drift=0.0, width_change=0.0), chromatic=False)
    S = NS.run(R, dchi2_min=60.0)
    assert S.proposals == {ROOT: {'1': [3], '3': [2]}}
    pv = PP.neighbour_preview([['a', 'b', 'c']], raw, R, None, 1, [e.name for e in raw], {}, S.proposals)
    assert pv is not None
    truth = np.mean(np.interp(pv['lam'], Rt.lam, Rt.flux))
    now, prop = np.nanmean(pv['now'][0]), np.nanmean(pv['proposed'][0])
    assert abs(prop / truth - 1) < 0.75 * abs(now / truth - 1)
    assert np.nanmedian(pv['proposed'][1]) > np.nanmedian(pv['now'][1])
    nod1 = next(f for f in pv['frames'] if f['nod'] == 1)
    assert nod1['path'] == 'nod1.fits'
    assert nod1['now'] == [(2, 'nod2.fits'), (3, 'nod3.fits')]
    assert nod1['proposed'] == [(3, 'nod3.fits')]
    rng = np.random.default_rng(2)
    for f in pv['frames']:
        p = tmp_path / f['path']
        fits.HDUList([fits.PrimaryHDU(), fits.ImageHDU(rng.normal(size=(21, 60)), name='S2D_SCI')]).writeto(p)
        f['path'] = str(p)
        for k in ('now', 'proposed'):
            f[k] = [(n, str(tmp_path / q)) for n, q in f[k]]
    out = tmp_path / 'p.pdf'
    plots.plot_neighbour_proposal(str(out), 1, [('obs_prism_1', dict(S.__dict__, preview=pv))])
    assert out.stat().st_size > 0


def test_proposals_already_in_force_are_left_out():
    """Nod 1 already subtracts only nod 3 (e.g. a catalogued neighbour's override): that
    part of the proposal is dropped, nod 3's stays."""
    exps = T._exposures(src_y=0.0)
    exps[0].partners = [2]
    S = NS.ScanResult(detected=True, delta=1.15, proposals={ROOT: {'1': [3], '3': [2]}})
    assert S.drop_in_force(exps) == 1
    assert S.proposals == {ROOT: {'3': [2]}}
    exps[2].partners = [1]
    S.drop_in_force(exps)
    assert S.proposals == {}


def test_preview_needs_the_frames_before_subtraction(monkeypatch):
    """When the loader cannot rebuild the differences (it returns the live partners),
    there is no preview rather than a figure of two identical fits."""
    from campfire_pipeline.nirspec import profile_product as PP
    load, _ = _frames_with_neighbour(30.0, 30.0, MSA_SLIT_PITCH)
    raw = load([['a', 'b', 'c']])
    monkeypatch.setattr(PF, 'load_source', lambda groups, bkg_overrides=None, **kw: load(groups))
    R = PF.fit_fixed(exps=raw, shape=dict(centre_offset=0.0, width=0.1, drift=0.0, width_change=0.0), chromatic=False)
    assert PP.neighbour_preview([['a', 'b', 'c']], raw, R, None, 1, [e.name for e in raw], {},
                                {ROOT: {'1': [3]}}) is None


def test_proposals_file_and_figures(tmp_path, monkeypatch):
    """The proposed file: entries of the re-run sources replaced, others kept; a figure
    per proposed source with a preview, and a stale figure of a re-run source removed."""
    o = _obs(tmp_path)
    o.workspace_dir = str(tmp_path)
    drawn = []
    monkeypatch.setattr('campfire_pipeline.nirspec.plots.plot_neighbour_proposal',
                        lambda path, sid, prods: (drawn.append((sid, [p for p, _ in prods])), open(path, 'w').close()))
    (tmp_path / 'nodded_background_overrides_proposed.toml').write_text(
        f'[{ROOT}]\n    7 = {{1 = [2]}}\n    9 = {{2 = [3]}}   # Delta=+1.00 dchi2=90 prism=single 2026-11-01\n')
    stale = tmp_path / 'test_obs_11_neighbor.pdf'
    stale.write_text('old')
    results = [dict(source_id=7, product='o_prism_7', scan=dict(detected=True, delta=1.15, dchi2=120.0,
                    proposals={ROOT: {'1': [3], '3': [2]}}, unresolved=[], preview={'frames': []})),
               dict(source_id=11, product='o_prism_11', scan=dict(detected=False, delta=0.0, dchi2=3.0, proposals={},
                    unresolved=[]))]
    S3.write_scan_proposals(o, results, scanned={(ROOT, 7), (ROOT, 11)})
    d = toml.load(tmp_path / 'nodded_background_overrides_proposed.toml')
    assert d[ROOT]['7'] == {'1': [3], '3': [2]}           # replaced
    assert d[ROOT]['9'] == {'2': [3]}                     # not re-run: kept
    text = (tmp_path / 'nodded_background_overrides_proposed.toml').read_text()
    assert '9 = {2 = [3]}   # Delta=+1.00 dchi2=90 prism=single 2026-11-01' in text     # with its comment
    assert '11' not in d[ROOT]
    assert drawn == [(7, ['o_prism_7'])]
    assert (tmp_path / 'test_obs_7_neighbor.pdf').exists() and not stale.exists()


def test_proposals_of_a_disperser_not_rerun_are_kept(tmp_path, monkeypatch):
    """Source 7 has proposals on a PRISM root and a grating root; a run of the grating
    alone, which finds nothing now, keeps the PRISM entry and its figure."""
    o = _obs(tmp_path)
    o.workspace_dir = str(tmp_path)
    monkeypatch.setattr('campfire_pipeline.nirspec.plots.plot_neighbour_proposal',
                        lambda path, sid, prods: open(path, 'w').close())
    other = 'jw01234001001_05101'
    (tmp_path / 'nodded_background_overrides_proposed.toml').write_text(
        f'[{ROOT}]\n    7 = {{1 = [2]}}\n\n[{other}]\n    7 = {{2 = [3]}}\n')
    fig = tmp_path / 'test_obs_7_neighbor.pdf'
    fig.write_text('old')
    results = [dict(source_id=7, product='o_g395m_7', scan=dict(detected=False, delta=0.0, dchi2=3.0, proposals={},
                    unresolved=[]))]
    S3.write_scan_proposals(o, results, scanned={(other, 7)})
    d = toml.load(tmp_path / 'nodded_background_overrides_proposed.toml')
    assert d[ROOT]['7'] == {'1': [2]} and other not in d
    assert fig.exists()


def test_suppress_keeps_detection_withholds_proposals():
    S = NS.ScanResult(detected=True, delta=1.15, dchi2=500.0, proposals={ROOT: {'1': [3]}}, unresolved=[f'{ROOT}:2'])
    S.suppress('other-slitlet light, masked 0.57')
    assert S.detected and S.proposals == {} and S.unresolved == []
    assert S.suppressed.startswith('other-slitlet') and S.header_cards()['NBRPROP'] == ''
