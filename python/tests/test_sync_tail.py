"""The tail of ``campfire sync`` (P5-3): the newest release, a changed
program set."""
from __future__ import annotations

from campfire.cli import _ACCESS_META, _LATEST_DR_META, _sync_tail
from campfire.db.store import LocalStore


class Api:
    def __init__(self, answer):
        self.answer = answer

    def whoami(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _run(api, store, full):
    said = []
    _sync_tail(api, store, full=full, say=said.append)
    return "\n".join(said)


def test_announces_a_new_release_once_in_full_then_in_one_line(tmp_path):
    store = LocalStore(tmp_path / 'campfire.db')
    me = {'latest_dr': {'tag': 'DR2026.1', 'published_at': '2026-11-01T12:00:00Z'}, 'access_fingerprint': 'sha256:a'}
    out = _run(Api(me), store, full=True)
    assert 'DR2026.1 published 2026-11-01; you are on live' in out and 'campfire pull --dr DR2026.1' in out
    assert store.get_meta(_LATEST_DR_META) == 'DR2026.1' and store.get_meta(_ACCESS_META) == 'sha256:a'
    out = _run(Api(me), store, full=False)
    assert out.strip() == 'Latest data release: DR2026.1; you are on live.'
    # nothing published yet: nothing said
    assert _run(Api({'latest_dr': None, 'access_fingerprint': 'sha256:a'}), LocalStore(tmp_path / 'b.db'), full=True) == ''


def test_a_changed_program_set_asks_for_a_full_sync_until_one_runs(tmp_path):
    store = LocalStore(tmp_path / 'campfire.db')
    _run(Api({'latest_dr': None, 'access_fingerprint': 'sha256:a'}), store, full=True)
    out = _run(Api({'latest_dr': None, 'access_fingerprint': 'sha256:b'}), store, full=False)
    assert 'program access changed' in out and 'campfire sync --full' in out
    assert store.get_meta(_ACCESS_META) == 'sha256:a'          # kept until a full sync
    assert 'program access changed' in _run(Api({'latest_dr': None, 'access_fingerprint': 'sha256:b'}), store, full=False)
    assert _run(Api({'latest_dr': None, 'access_fingerprint': 'sha256:b'}), store, full=True) == ''
    assert store.get_meta(_ACCESS_META) == 'sha256:b'


def test_a_first_fingerprint_on_an_incremental_sync_is_adopted_silently(tmp_path):
    # an upgraded client's store has none yet: there is nothing to compare
    store = LocalStore(tmp_path / 'campfire.db')
    assert _run(Api({'latest_dr': None, 'access_fingerprint': 'sha256:a'}), store, full=False) == ''
    assert store.get_meta(_ACCESS_META) == 'sha256:a'


def test_the_tail_never_fails_a_sync(tmp_path):
    store = LocalStore(tmp_path / 'campfire.db')
    assert _run(Api(RuntimeError('down')), store, full=True) == ''
    assert _run(Api({}), store, full=True) == ''
