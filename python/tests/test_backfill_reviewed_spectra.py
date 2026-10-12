"""scripts/backfill_reviewed_spectra.py: the paged seed loop (data releases P3)."""
import importlib.util
from pathlib import Path

from .fakes import FakePostgrest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'backfill_reviewed_spectra.py'


def _load():
    spec = importlib.util.spec_from_file_location('backfill_reviewed_spectra', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _server(pending_ids):
    """A fake seed_reviewed_spectra(): keyset page over *pending_ids*, each seeded once."""
    state = {'pending': sorted(pending_ids), 'calls': []}

    def handler(params):
        state['calls'].append(dict(params))
        after, limit, dry = params['p_after_id'], params['p_limit'], params['p_dry_run']
        page = [i for i in state['pending'] if i > after][:limit]
        if not page:
            return {'scanned': 0, 'seeded': 0, 'flipped': 0, 'last_id': None}
        if not dry:
            state['pending'] = [i for i in state['pending'] if i not in page]
        return {'scanned': len(page), 'seeded': 0 if dry else len(page),
                'flipped': 0 if dry else sum(1 for i in page if i % 7 == 0), 'last_id': page[-1]}

    return state, FakePostgrest(rpc_handlers={'seed_reviewed_spectra': handler})


def test_run_pages_until_exhausted_and_totals():
    mod = _load()
    state, sb = _server([3, 5, 7, 14, 20, 21, 30])
    lines = []
    totals = mod.run(sb, page=3, echo=lines.append)
    assert totals == {'scanned': 7, 'seeded': 7, 'flipped': 3}
    assert state['pending'] == []
    # 3 + 3 + 1 pending pages, then one empty page that ends the loop
    assert [c['p_after_id'] for c in state['calls']] == [0, 7, 21, 30]
    assert len(lines) == 3


def test_run_dry_run_writes_nothing_and_limit_caps():
    mod = _load()
    state, sb = _server([1, 2, 3, 4, 5])
    totals = mod.run(sb, dry_run=True, page=2, echo=lambda _l: None)
    assert totals['scanned'] == 5 and totals['seeded'] == 0 and state['pending'] == [1, 2, 3, 4, 5]
    state, sb = _server([1, 2, 3, 4, 5])
    totals = mod.run(sb, limit=3, page=2, echo=lambda _l: None)
    assert totals['scanned'] == 3 and state['pending'] == [4, 5]
    assert [c['p_limit'] for c in state['calls']] == [2, 1]
