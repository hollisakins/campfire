"""Tests for the NIRCam exposure-association layer (nircam/association.py).

Covers the per-exposure grouping that the field-level ``align`` phase consumes:
one ``ExposureGroup`` == one physical exposure (one dither), pooling every
detector that shares the exposure token across the SW + LW filter directories.
The module reads filenames + parent dir only, so fixtures are zero-byte files
(mirrors test_nircam_exclusions.py) under a real, set-up ``Field``.
"""

import os

import pytest

from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.association import (
    ExposureGroup,
    build_exposure_groups,
    channel_of,
    detector_of,
    exposure_key,
    module_of,
)

_TOKEN = 'jw01727028001_04101_00003'
_TOKEN2 = 'jw01727028001_04101_00004'
_SW = ('nrca1', 'nrca2', 'nrca3', 'nrca4', 'nrcb1', 'nrcb2', 'nrcb3', 'nrcb4')
_LW = ('nrcalong', 'nrcblong')


def _make_field(tmp_path, filters=('f200w', 'f444w'), **kw):
    f = Field(name='cosmos', filters=list(filters), files=['jw01727*'],
              tangent_point=(150.0, 2.0), tiles={}, **kw)
    f.setup_workspace(campfire_root=str(tmp_path))
    return f


def _touch(field, filt, root):
    path = os.path.join(field.filter_dir(filt), f'{root}.fits')
    open(path, 'w').close()
    return path


def _dets(group):
    return [m.detector for m in group.members]


# --- core grouping ----------------------------------------------------------

def test_multi_filter_grouping_one_token(tmp_path):
    f = _make_field(tmp_path)
    for det in _SW:
        _touch(f, 'f200w', f'{_TOKEN}_{det}')
    for det in _LW:
        _touch(f, 'f444w', f'{_TOKEN}_{det}')

    groups = build_exposure_groups(f)
    assert len(groups) == 1
    g = groups[0]
    assert g.key == _TOKEN
    assert g.n_members == 10
    assert g.filters == frozenset({'f200w', 'f444w'})
    assert g.modules == frozenset({'a', 'b'})
    assert len(g.sw_members) == 8
    assert len(g.lw_members) == 2
    assert not g.is_single_member


def test_two_dithers_do_not_cross_group(tmp_path):
    f = _make_field(tmp_path)
    for token in (_TOKEN, _TOKEN2):
        for det in _SW:
            _touch(f, 'f200w', f'{token}_{det}')
        for det in _LW:
            _touch(f, 'f444w', f'{token}_{det}')

    groups = build_exposure_groups(f)
    assert [g.key for g in groups] == [_TOKEN, _TOKEN2]
    for g in groups:
        assert all(exposure_key(m.path) == g.key for m in g.members)


def test_five_member_single_module(tmp_path):
    f = _make_field(tmp_path)
    for det in ('nrca1', 'nrca2', 'nrca3', 'nrca4'):
        _touch(f, 'f200w', f'{_TOKEN}_{det}')
    _touch(f, 'f444w', f'{_TOKEN}_nrcalong')

    (g,) = build_exposure_groups(f)
    assert g.n_members == 5
    assert g.modules == frozenset({'a'})
    assert len(g.sw_members) == 4
    assert len(g.lw_members) == 1


def test_one_member_lw_only_is_single(tmp_path):
    f = _make_field(tmp_path)
    _touch(f, 'f444w', f'{_TOKEN}_nrcalong')

    (g,) = build_exposure_groups(f)
    assert g.n_members == 1
    assert g.is_single_member
    assert g.members[0].channel == 'lw'


def test_two_member_lw_only_not_single(tmp_path):
    f = _make_field(tmp_path)
    for det in _LW:
        _touch(f, 'f444w', f'{_TOKEN}_{det}')

    (g,) = build_exposure_groups(f)
    assert g.n_members == 2
    assert not g.is_single_member
    assert g.modules == frozenset({'a', 'b'})


# --- effective skip ---------------------------------------------------------

def test_skip_via_field_skip(tmp_path):
    f = _make_field(tmp_path, skip=[f'{_TOKEN2}*'])
    for token in (_TOKEN, _TOKEN2):
        for det in _SW:
            _touch(f, 'f200w', f'{token}_{det}')

    groups = build_exposure_groups(f)
    assert [g.key for g in groups] == [_TOKEN]


def test_skip_via_excluded_exposures(tmp_path):
    f = _make_field(tmp_path)
    for det in _SW:
        _touch(f, 'f200w', f'{_TOKEN}_{det}')
    # Reviewer exclusion is per-filter and drops a single detector.
    f.excluded_exposures = {'f200w': [f'{_TOKEN}_nrca1']}

    (g,) = build_exposure_groups(f)
    assert 'nrca1' not in g.detectors
    assert g.n_members == 7


# --- classification ---------------------------------------------------------

def test_module_and_channel_tagging(tmp_path):
    f = _make_field(tmp_path)
    _touch(f, 'f200w', f'{_TOKEN}_nrca1')
    _touch(f, 'f200w', f'{_TOKEN}_nrcb3')
    _touch(f, 'f444w', f'{_TOKEN}_nrcalong')
    _touch(f, 'f444w', f'{_TOKEN}_nrcblong')

    (g,) = build_exposure_groups(f)
    tags = {m.detector: (m.module, m.channel) for m in g.members}
    assert tags['nrca1'] == ('a', 'sw')
    assert tags['nrcb3'] == ('b', 'sw')
    assert tags['nrcalong'] == ('a', 'lw')
    assert tags['nrcblong'] == ('b', 'lw')


def test_public_helpers():
    p = '/x/f444w/jw01727028001_04101_00003_nrcalong.fits'
    assert exposure_key(p) == 'jw01727028001_04101_00003'
    assert detector_of(p) == 'nrcalong'
    assert channel_of('nrcalong') == 'lw'
    assert channel_of('nrca5') == 'lw'
    assert channel_of('nrcb2') == 'sw'
    assert module_of('nrca1') == 'a'
    assert module_of('nrcblong') == 'b'
    assert module_of('weird') == ''


# --- ordering ---------------------------------------------------------------

def test_deterministic_ordering(tmp_path):
    f = _make_field(tmp_path)
    # Create in a deliberately non-canonical order.
    for det in ('nrcb2', 'nrca4', 'nrca1'):
        _touch(f, 'f200w', f'{_TOKEN}_{det}')
    _touch(f, 'f444w', f'{_TOKEN}_nrcalong')

    g1 = build_exposure_groups(f)[0]
    g2 = build_exposure_groups(f)[0]
    assert _dets(g1) == _dets(g2)
    # Canonical order: SW module A, then that module's LW, then module B.
    assert _dets(g1) == ['nrca1', 'nrca4', 'nrcalong', 'nrcb2']


# --- passthrough params -----------------------------------------------------

def test_filters_arg_subsets_to_lw(tmp_path):
    f = _make_field(tmp_path)
    for det in _SW:
        _touch(f, 'f200w', f'{_TOKEN}_{det}')
    for det in _LW:
        _touch(f, 'f444w', f'{_TOKEN}_{det}')

    (g,) = build_exposure_groups(f, filters=['f444w'])
    assert g.detectors == ('nrcalong', 'nrcblong')


class _StatusStub:
    """Minimal StepStatus stand-in: only ``has(path, key)`` is consulted."""

    def __init__(self, keep):
        self.keep = set(keep)

    def has(self, path, key):
        return path in self.keep


def test_with_step_via_status_stub(tmp_path):
    f = _make_field(tmp_path)
    keep = []
    for token in (_TOKEN, _TOKEN2):
        for det in _SW:
            p = _touch(f, 'f200w', f'{token}_{det}')
            if token == _TOKEN:
                keep.append(p)

    groups = build_exposure_groups(f, with_step='CFP_ALGN',
                                   status=_StatusStub(keep))
    assert [g.key for g in groups] == [_TOKEN]


# --- edge cases -------------------------------------------------------------

def test_empty_filters_returns_empty(tmp_path):
    f = _make_field(tmp_path)
    assert build_exposure_groups(f, filters=[]) == []


def test_runtime_error_if_not_setup():
    f = Field(name='cosmos', filters=['f200w'], files=['jw01727*'],
              tangent_point=(150.0, 2.0), tiles={})
    with pytest.raises(RuntimeError):
        build_exposure_groups(f)


# --- advisory, non-fatal anomalies ------------------------------------------

def test_unexpected_detector_kept_and_sorted_last(tmp_path, capsys):
    f = _make_field(tmp_path)
    _touch(f, 'f200w', f'{_TOKEN}_nrca1')
    _touch(f, 'f200w', f'{_TOKEN}_nrcz9')

    (g,) = build_exposure_groups(f)
    assert 'nrcz9' in g.detectors
    assert g.detectors[-1] == 'nrcz9'  # unknown sorts last
    assert 'unexpected detector' in capsys.readouterr().out


def test_channel_filter_mismatch_kept_and_warned(tmp_path, capsys):
    f = _make_field(tmp_path)
    # SW detector placed in the LW filter directory.
    _touch(f, 'f444w', f'{_TOKEN}_nrca1')

    (g,) = build_exposure_groups(f)
    assert g.detectors == ('nrca1',)
    out = capsys.readouterr().out
    assert 'SW detector' in out and 'f444w' in out


# --- dataclass sanity -------------------------------------------------------

def test_group_is_frozen_and_hashable(tmp_path):
    f = _make_field(tmp_path)
    _touch(f, 'f444w', f'{_TOKEN}_nrcalong')
    (g,) = build_exposure_groups(f)
    assert isinstance(g, ExposureGroup)
    with pytest.raises(Exception):
        g.key = 'mutated'  # frozen
    _ = {g}  # hashable (frozen dataclass of hashable fields)
