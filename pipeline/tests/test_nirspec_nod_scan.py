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
    R = PF.fit(exps, chromatic=False)
    S = NS.run(R, dchi2_min=60.0)
    assert S.detected, (S.dchi2, S.dchi2_null)
    assert S.delta == pytest.approx(MSA_SLIT_PITCH, abs=0.1)
    assert S.proposals == {ROOT: {'1': [3], '3': [2]}}
    assert not S.unresolved
    cards = S.header_cards()
    assert cards['PFNBR'][0] == 1 and cards['PFNBRDRP'][0] == '03101:1>3;03101:3>2'


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
