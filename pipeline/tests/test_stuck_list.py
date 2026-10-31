"""The physical-shutter stuck list (``stuck_list``): keys, the legacy mapping, the
origin precedence, stage 2a's removal and the auto writer. Synthetic metafile from
the stage 2a single-call tests (source 1001 in q1 r100 columns 20-22, source 1002 in
columns 30-32, a background-only slitlet in columns 40-42)."""
import math
import os

import pytest

pytest.importorskip("campfire_pipeline")
from campfire_pipeline.nirspec import stuck_list as S  # noqa: E402
from campfire_pipeline.nirspec.stuck_shutters import write_stuck_shutters_toml  # noqa: E402
from test_nirspec_stage2a_single_call import _metafile  # noqa: E402

ROOT = 'jw07076001001_05101'


def _write(path, text):
    path.write_text(text)
    return str(path)


def test_keys_round_trip_and_reject_garbage():
    assert S.shutter_key(2, 195, 64) == 'q2r195c64'
    assert S.parse_key('q2r195c64') == (2, 195, 64)
    for bad in ('q5r1c1', 'q2r195', '2-195-64', 'q2 r195 c64'):
        with pytest.raises(ValueError):
            S.parse_key(bad)


def test_read_file_physical_and_legacy(tmp_path):
    path = _write(tmp_path / 's.toml', f'''
[{ROOT}]
    1001 = [1]  # hand
    1002 = [3]  # web
[{ROOT}.shutters]
    "q1r100c41" = {{origin = "auto", stuck = true, a = 0.04}}
    "q1r100c21" = {{origin = "manual", stuck = false, note = "fine"}}
''')
    phys, legacy = S.read_file(path)
    assert phys[ROOT][(1, 100, 41)] == S.Entry('auto', True, 0.04, '')
    assert phys[ROOT][(1, 100, 21)].stuck is False
    assert legacy[ROOT] == {1001: ([1], 'manual'), 1002: ([3], 'web')}


def test_read_file_rejects_the_string_shorthand_and_unknown_origins(tmp_path):
    for value in ('"manual"', '{origin = "hand", stuck = true}', '{origin = "auto", stuck = "yes"}'):
        path = _write(tmp_path / 's.toml', f'[{ROOT}.shutters]\n"q1r100c20" = {value}\n')
        with pytest.raises(ValueError):
            S.read_file(path)


def test_legacy_ordinals_map_to_the_columns_stage2a_always_removed(tmp_path):
    mf = _metafile(tmp_path)
    assert S.legacy_to_physical(mf, 1001, [1, 3]) == {(1, 100, 20), (1, 100, 22)}
    with pytest.raises(IndexError):
        S.legacy_to_physical(mf, 1001, [4])


def test_precedence_manual_web_auto_and_physical_over_legacy(tmp_path):
    mf = _metafile(tmp_path)
    phys = {ROOT: {
        (1, 100, 20): S.Entry('auto', True),       # legacy manual 1001=[1] outranks it
        (1, 100, 30): S.Entry('manual', False),     # manual veto of the legacy auto below
        (1, 100, 41): S.Entry('auto', True),        # background-only slitlet
        (1, 100, 32): S.Entry('web', False),        # web veto of the auto legacy entry
    }}
    legacy = {ROOT: {1001: ([1], 'manual'), 1002: ([1, 3], 'auto')}}
    res = S.resolve(phys, legacy, {ROOT: mf})[ROOT]
    assert res[(1, 100, 20)].origin == 'manual' and res[(1, 100, 20)].stuck
    assert res[(1, 100, 30)].origin == 'manual' and not res[(1, 100, 30)].stuck
    assert res[(1, 100, 32)].origin == 'web' and not res[(1, 100, 32)].stuck
    assert S.stuck_set({ROOT: res}, ROOT) == {(1, 100, 20), (1, 100, 41)}


def test_unmappable_legacy_entries_are_dropped_not_fatal(tmp_path):
    mf = _metafile(tmp_path)
    res = S.resolve({}, {ROOT: {9999: ([1], 'manual'), 1001: ([7], 'manual')}}, {ROOT: mf})
    assert res == {}


def test_remove_stuck_covers_background_slitlets_and_ordinals(tmp_path):
    mf = _metafile(tmp_path)
    bkg = mf.background_slitlet_rows()
    out = S.remove_stuck(bkg, {(1, 100, 41)})
    assert sorted(set(out['shutter_column'])) == [40, 42]
    assert len(out) == len(bkg) - 3                      # every dither point
    rows = mf.rows_for_source(1002)
    assert S.source_ordinals(rows, {(1, 100, 32), (1, 100, 41)}) == [3]


def test_source_table_for_the_nods_plots(tmp_path):
    mf = _metafile(tmp_path)
    res = {ROOT: {(1, 100, 22): S.Entry('auto'), (1, 100, 30): S.Entry('manual'),
                  (1, 100, 31): S.Entry('web', stuck=False)}}
    t = S.as_source_table(res, {ROOT: mf})
    got = {(r['root'], int(r['source_id'])): list(r['shutters']) for r in t}
    assert got == {(ROOT, 1001): [3], (ROOT, 1002): [1]}


def _calls(**kw):
    return {S.parse_key(k): v for k, v in kw.items()}


def test_write_auto_merges_keeps_comments_and_skips_identical_writes(tmp_path):
    mf = _metafile(tmp_path)
    path = _write(tmp_path / 's.toml', f'''# my own notes
[{ROOT}]
    1002 = [1]  # hand
[{ROOT}.shutters]
    "q1r100c21" = {{origin = "manual", stuck = false}}  # looked at the nods
    "q1r100c42" = {{origin = "auto", stuck = true, a = 0.02}}
''')
    calls = _calls(q1r100c20={'stuck': True, 'a': 0.0123, 'n': 6, 'note': ''},
                   q1r100c21={'stuck': True, 'a': 0.05, 'n': 6, 'note': ''},     # manual decides
                   q1r100c30={'stuck': True, 'a': 0.01, 'n': 6, 'note': ''},     # legacy hand decides
                   q1r100c42={'stuck': False, 'a': 0.95, 'n': 6, 'note': ''})    # now open: removed
    assert S.write_auto(path, ROOT, calls, {ROOT: mf}) is True
    text = open(path).read()
    assert '# my own notes' in text and '# looked at the nods' in text and '1002 = [1]  # hand' in text
    phys, legacy = S.read_file(path)
    assert phys[ROOT] == {(1, 100, 21): S.Entry('manual', False),
                          (1, 100, 20): S.Entry('auto', True, 0.012)}
    mtime = os.path.getmtime(path)
    assert S.write_auto(path, ROOT, calls, {ROOT: mf}) is False
    assert os.path.getmtime(path) == mtime


def test_write_auto_outranks_a_legacy_auto_entry_with_an_open_call(tmp_path):
    mf = _metafile(tmp_path)
    path = _write(tmp_path / 's.toml', f'[{ROOT}]\n    1001 = [2]  # auto\n')
    S.write_auto(path, ROOT, _calls(q1r100c21={'stuck': False, 'a': 1.01, 'n': 6, 'note': ''}), {ROOT: mf})
    res = S.load(path, {ROOT: mf})
    assert res[ROOT][(1, 100, 21)].stuck is False
    assert S.stuck_set(res, ROOT) == set()


def test_write_auto_on_a_new_file_and_nothing_to_write(tmp_path):
    path = str(tmp_path / 's.toml')
    assert S.write_auto(path, ROOT, _calls(q1r100c20={'stuck': False, 'a': 1.0, 'n': 6, 'note': ''}),
                        obs_name='obs') is True                 # header only
    assert S.read_file(path) == ({}, {})
    assert S.write_auto(path, ROOT, _calls(q1r100c20={'stuck': True, 'a': math.nan, 'n': 2, 'note': ''}),
                        obs_name='obs') is True
    assert S.read_file(path)[0][ROOT][(1, 100, 20)].stuck


def test_the_legacy_writer_carries_the_physical_table_through(tmp_path):
    import toml
    path = _write(tmp_path / 's.toml', f'''[{ROOT}]
    1002 = [1]  # hand
[{ROOT}.shutters]
    "q1r100c41" = {{origin = "auto", stuck = true, a = 0.04}}
''')
    data = toml.load(path)
    data[ROOT]['1001'] = [2]
    write_stuck_shutters_toml(data, path, 'obs', provenance={(ROOT, '1001'): 'auto'})
    phys, legacy = S.read_file(path)
    assert phys[ROOT] == {(1, 100, 41): S.Entry('auto', True, 0.04)}
    assert legacy[ROOT] == {1001: ([2], 'auto'), 1002: ([1], 'manual')}
