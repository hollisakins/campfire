"""Tests for the same-slitlet neighbour scan (nirspec/nod_scan.py) and the automatic
background overrides it proposes (stage3.write_scan_proposals,
Observation.effective_bkg_overrides).

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
    assert cards['PFNBRD'][0] == 1 and cards['PFNBRDRP'][0] == '03101:1>3;03101:3>2'
    # the product flag needs the stricter level (default: never in this call)
    assert cards['PFNBR'][0] == 0 and not S.candidate
    S2 = NS.run(R, dchi2_min=60.0, flag_dchi2=0.5 * S.dchi2)
    assert S2.candidate and S2.header_cards()['PFNBR'][0] == 1
    assert not NS.run(R, dchi2_min=60.0, flag_dchi2=2 * S.dchi2).candidate


def test_candidate_from_other_disperser_agreement():
    S = NS.ScanResult(detected=True, delta=1.15, dchi2=120.0, proposals={ROOT: {'1': [3]}})
    assert not S.candidate
    S.cross_check((True, 1.10), tol=0.2)
    assert S.candidate and S.header_cards()['PFNBR'][0] == 1


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


def test_effective_overrides_hand_wins(tmp_path):
    o = _obs(tmp_path)
    (tmp_path / 'nodded_background_overrides.toml').write_text(f'[{ROOT}]\n    7 = {{1 = [3], 2 = []}}\n')
    (tmp_path / 'nodded_background_overrides_auto.toml').write_text(
        f'[{ROOT}]\n    7 = {{1 = [2], 3 = [2]}}\n    8 = {{2 = [1]}}\n')
    eff, from_auto = o.effective_bkg_overrides()
    assert eff[ROOT]['7'] == {'1': [3], '2': [], '3': [2]}
    assert eff[ROOT]['8'] == {'2': [1]}
    assert from_auto == {(ROOT, '7'): {'3'}, (ROOT, '8'): {'2'}}
    assert o.bkg_overrides[ROOT]['7'] == {'1': [3], '2': []}          # the hand file is untouched


def test_auto_override_toml_roundtrip(tmp_path):
    o = _obs(tmp_path)
    o.auto_bkg_override_file  # property exists
    (tmp_path / 'nodded_background_overrides_auto.toml').write_text(
        f'[{ROOT}]\n    7 = {{1 = [2]}}\n    9 = {{2 = [3]}}\n')
    results = [dict(source_id=7, scan=dict(detected=True, delta=1.15, dchi2=120.0,
                                           proposals={ROOT: {'1': [3], '3': [2]}}, unresolved=[])),
               dict(source_id=11, scan=dict(detected=False, delta=0.0, dchi2=3.0, proposals={}, unresolved=[]))]
    S3.write_scan_proposals(o, results, run_sids={7, 11}, action='auto_override')
    d = toml.load(tmp_path / 'nodded_background_overrides_auto.toml')
    assert d[ROOT]['7'] == {'1': [3], '3': [2]}           # replaced
    assert d[ROOT]['9'] == {'2': [3]}                     # not re-run: kept
    assert '11' not in d[ROOT]
    eff, from_auto = o.effective_bkg_overrides()
    assert eff[ROOT]['7'] == {'1': [3], '3': [2]}
    S3.write_scan_proposals(o, results, run_sids={7}, action='report_only')
    assert toml.load(tmp_path / 'nodded_background_overrides_proposed.toml')[ROOT]['7'] == {'1': [3], '3': [2]}
    with pytest.raises(ValueError):
        S3.write_scan_proposals(o, results, run_sids={7}, action='apply')


def test_suppress_keeps_detection_withholds_proposals():
    S = NS.ScanResult(detected=True, delta=1.15, dchi2=500.0, proposals={ROOT: {'1': [3]}}, unresolved=[f'{ROOT}:2'])
    S.suppress('other-slitlet light, masked 0.57')
    assert S.detected and S.proposals == {} and S.unresolved == []
    assert S.header_cards()['PFNBRSUP'][0].startswith('other-slitlet')
