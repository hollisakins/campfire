"""scripts/backfill_stuck_shutters_physical.py: the pure mapping / dedupe / origin plan."""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'backfill_stuck_shutters_physical.py'
ROOT = 'jw09999001001_03101'


def _load():
    spec = importlib.util.spec_from_file_location('backfill_stuck_shutters_physical', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


# Two sources sharing one 4-shutter slitlet (columns 10..13, quadrant 2, row 100),
# plus a source in its own slitlet; source 9 is not in the metafile.
SLITS = {
    (ROOT, 1): [(2, 100, 10), (2, 100, 11), (2, 100, 12), (2, 100, 13)],
    (ROOT, 2): [(2, 100, 10), (2, 100, 11), (2, 100, 12), (2, 100, 13)],
    (ROOT, 3): [(3, 50, 7), (3, 50, 8), (3, 50, 9)],
}


def to_physical(root, sid, ordinals):
    cols = SLITS.get((root, sid))
    if cols is None:
        raise RuntimeError('No IDs matched in metafile!')
    out = set()
    for k in ordinals:
        if not 1 <= k <= len(cols):
            raise IndexError(f'stuck shutter ordinal {k} is out of range for a {len(cols)}-shutter slitlet')
        out.add(cols[k - 1])
    return out


def coverage(root, sid):
    if (root, sid) not in SLITS:
        raise RuntimeError('No IDs matched in metafile!')
    return set(SLITS[(root, sid)])


def _rev(sid, ordinals, root=ROOT):
    return {'exposure_root': root, 'source_id': sid, 'stuck_shutters': ordinals}


def test_maps_dedupes_and_reports():
    mod = _load()
    rows = [_rev(1, [1, 2]),          # q2r100c10, q2r100c11
            _rev(2, [2]),             # q2r100c11 again (same shutter): one row; c10 open for source 2
            _rev(3, [3]),             # q3r50c9
            _rev(9, [1]),             # source not in the metafile: reported
            _rev(3, [7], root='jw09999001001_05101'),  # root without a metafile entry: reported
            _rev(1, [])]              # empty list: nothing to do
    plan = mod.plan_observation('obs', rows, to_physical, coverage,
                                manual_keys={(ROOT, 'q3r50c9')}, existing_rows=[])
    got = {(r['exposure_root'], r['shutter']): (r['origin'], r['stuck'], r['note']) for r in plan.rows}
    assert got == {
        (ROOT, 'q2r100c10'): ('web', True, 'backfill: source 1'),
        (ROOT, 'q2r100c11'): ('web', True, 'backfill: source 1, 2'),
        (ROOT, 'q3r50c9'): ('manual', True, 'backfill: source 3'),
    }
    assert all(r['observation'] == 'obs' for r in plan.rows)
    # source 2's slitlet holds c10 but its list does not: a conflict, resolved stuck
    assert plan.conflicts == [(ROOT, 'q2r100c10', [1], [2])]
    assert [(s[0], s[1]) for s in plan.skipped] == [(ROOT, 9), ('jw09999001001_05101', 3)]
    assert plan.empty == 1
    assert plan.origin_counts() == {'manual': 1, 'web': 2}


def test_out_of_range_ordinal_is_reported_not_dropped():
    mod = _load()
    plan = mod.plan_observation('obs', [_rev(3, [1, 4])], to_physical, coverage, set(), [])
    assert plan.rows == []
    assert plan.skipped[0][:3] == (ROOT, 3, [1, 4]) and 'IndexError' in plan.skipped[0][3]


def test_rerun_inserts_nothing_and_never_overwrites():
    mod = _load()
    rows = [_rev(1, [1, 2]), _rev(3, [3])]
    first = mod.plan_observation('obs', rows, to_physical, coverage, {(ROOT, 'q3r50c9')}, [])
    assert len(first.rows) == 3
    # applied, then a web edit opened c10 afterwards
    db = [dict(r) for r in first.rows]
    db[0]['stuck'] = False
    again = mod.plan_observation('obs', rows, to_physical, coverage, {(ROOT, 'q3r50c9')}, db)
    assert again.rows == []
    assert again.existing_same == 2
    assert [(d[0], d[1]) for d in again.existing_diff] == [(ROOT, 'q2r100c10')]


@pytest.mark.parametrize('q,r,c,key', [(2, 195, 64, 'q2r195c64'), (1, 1, 1, 'q1r1c1')])
def test_shutter_key_matches_the_db_check(q, r, c, key):
    import re
    mod = _load()
    assert mod.shutter_key(q, r, c) == key
    assert re.match(r'^q[1-4]r[0-9]+c[0-9]+$', key)
