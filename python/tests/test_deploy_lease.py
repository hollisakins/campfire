"""Deploy lease (data releases P1): the Python wrappers and the SQL state machine.

Pure-Python half: a fake Supabase client serves ``.rpc(name, params).execute()``
so the ``ScopeLease`` begin → heartbeat → claim | release flow, the busy
refusal and the flusher's heartbeat cadence are asserted without a database.
DB half: ``sql/deploy_lease.sql`` runs against the local Supabase Postgres (see
``_localdb.py``) and must reach its PASS marker — busy raises, stale takeover,
steal, holder-only heartbeat, claim clears, non-admin denied, anon revoked.
"""
import time
import types
from pathlib import Path

import pytest

from campfire.deploy import supabase as sb_mod
from campfire.deploy.supabase import (
    DeployScopeBusy, ScopeLease, begin_deploy_scope, heartbeat_deploy_scope,
    release_active_leases, release_deploy_scope,
)
from campfire.deploy.push import _HeartbeatingFlusher, registration_flusher  # noqa: F401

from ._localdb import requires_local_db, run_sql

SQL_FILE = Path(__file__).parent / "sql" / "deploy_lease.sql"
PASS_MARKER = "DEPLOY LEASE HARNESS: ALL ASSERTIONS PASSED"


# ---------------------------------------------------------------------------
# fake client: rpc(name, params).execute() → data, or raise
# ---------------------------------------------------------------------------

class _APIError(Exception):
    """Shape of postgrest's APIError: code / message / details / hint."""

    def __init__(self, code, message, details=None, hint=None):
        super().__init__(message)
        self.code, self.message, self.details, self.hint = code, message, details, hint


class _FakeRPC:
    def __init__(self, result):
        self._result = result

    def execute(self):
        if isinstance(self._result, Exception):
            raise self._result
        return types.SimpleNamespace(data=self._result)


class _FakeClient:
    def __init__(self, handlers):
        self._handlers = handlers
        self.calls = []

    def rpc(self, name, params=None):
        self.calls.append((name, params))
        h = self._handlers.get(name)
        result = h(params) if callable(h) else h
        return _FakeRPC(result)


BUSY = _APIError(
    'CF409',
    "deploy_scope_busy: observation obs_a is being deployed by 1111 from host-a since X (last heartbeat Y)",
    details='{"in_progress_by": "1111", "in_progress_host": "host-a", "in_progress_since": "X", "heartbeat_at": "Y", "live": true}',
    hint='Wait for that deploy to finish, or re-run with --steal-lease if you are certain it is dead.')


@pytest.fixture(autouse=True)
def _clean_active_leases():
    sb_mod._ACTIVE_LEASES.clear()
    yield
    sb_mod._ACTIVE_LEASES.clear()


# ---------------------------------------------------------------------------
# begin_deploy_scope
# ---------------------------------------------------------------------------

def test_begin_returns_version_and_lease_flags():
    client = _FakeClient({'begin_deploy_scope': {'version': 4, 'took_over_stale': False,
                                                 'stolen': False, 'previous': None}})
    info = begin_deploy_scope(client, 'observation', 'obs_a', actor='u1', host='h1')
    assert info == {'version': 4, 'took_over_stale': False, 'stolen': False,
                    'previous': None, 'leased': True}
    name, params = client.calls[0]
    assert name == 'begin_deploy_scope'
    assert params == {'p_scope_type': 'observation', 'p_scope_key': 'obs_a',
                      'p_actor': 'u1', 'p_host': 'h1', 'p_steal': False}


def test_begin_busy_raises_with_holder():
    client = _FakeClient({'begin_deploy_scope': BUSY})
    with pytest.raises(DeployScopeBusy) as ei:
        begin_deploy_scope(client, 'observation', 'obs_a', actor='u2', host='h2')
    exc = ei.value
    assert exc.scope_key == 'obs_a'
    assert exc.holder['in_progress_host'] == 'host-a'
    text = exc.describe()
    assert 'host-a' in text and '--steal-lease' in text


def test_begin_busy_detected_by_message_without_code():
    """A client that drops the SQLSTATE still surfaces the refusal."""
    err = _APIError(None, 'deploy_scope_busy: field cosmos is being deployed ...')
    client = _FakeClient({'begin_deploy_scope': err})
    with pytest.raises(DeployScopeBusy):
        begin_deploy_scope(client, 'field', 'cosmos')


def test_begin_other_failure_degrades_to_version_read():
    """RPC missing on an older DB: advisory, falls back to get_deploy_scope_version."""
    client = _FakeClient({'begin_deploy_scope': _APIError('PGRST202', 'Could not find the function'),
                          'get_deploy_scope_version': 7})
    info = begin_deploy_scope(client, 'observation', 'obs_a')
    assert info['version'] == 7 and info['leased'] is False and 'error' in info


def test_begin_steal_passes_flag():
    client = _FakeClient({'begin_deploy_scope': {'version': 1, 'stolen': True,
                                                 'previous': {'in_progress_host': 'host-a'}}})
    info = begin_deploy_scope(client, 'field', 'cosmos', steal=True)
    assert info['stolen'] is True
    assert client.calls[0][1]['p_steal'] is True


# ---------------------------------------------------------------------------
# heartbeat / release wrappers
# ---------------------------------------------------------------------------

def test_heartbeat_and_release_pass_the_version_token():
    c = _FakeClient({'heartbeat_deploy_scope': True, 'release_deploy_scope': True})
    heartbeat_deploy_scope(c, 'observation', 'o', actor='u', host='h', version=7)
    release_deploy_scope(c, 'observation', 'o', actor='u', host='h', version=7)
    assert [p['p_version'] for _, p in c.calls] == [7, 7]
    assert c.calls[0][1] == {'p_scope_type': 'observation', 'p_scope_key': 'o',
                             'p_actor': 'u', 'p_host': 'h', 'p_version': 7}


def test_heartbeat_tristate():
    assert heartbeat_deploy_scope(_FakeClient({'heartbeat_deploy_scope': True}), 'observation', 'o') is True
    assert heartbeat_deploy_scope(_FakeClient({'heartbeat_deploy_scope': False}), 'observation', 'o') is False
    assert heartbeat_deploy_scope(_FakeClient({'heartbeat_deploy_scope': _APIError('X', 'boom')}),
                                  'observation', 'o') is None


def test_release_never_raises():
    assert release_deploy_scope(_FakeClient({'release_deploy_scope': True}), 'observation', 'o') is True
    assert release_deploy_scope(_FakeClient({'release_deploy_scope': _APIError('X', 'boom')}),
                                'observation', 'o') is False


# ---------------------------------------------------------------------------
# ScopeLease: begin → heartbeat* → claim | release, and the abort registry
# ---------------------------------------------------------------------------

def test_scope_lease_lifecycle_claim(capsys):
    client = _FakeClient({
        'begin_deploy_scope': {'version': 2, 'previous': None},
        'heartbeat_deploy_scope': True,
        'claim_deploy_scope': {'claimed': True, 'version': 3, 'conflict': False},
    })
    lease = ScopeLease(client, 'observation', 'obs_a', actor='u1', host='h1',
                       heartbeat_interval_s=None)
    assert lease.begin() == 2
    assert lease in sb_mod._ACTIVE_LEASES
    lease.heartbeat(); lease.heartbeat()
    assert lease.heartbeats == 2
    # every heartbeat carried the ownership token begin() returned
    assert [p['p_version'] for n, p in client.calls if n == 'heartbeat_deploy_scope'] == [2, 2]
    claim = lease.claim()
    assert claim['claimed'] and claim['version'] == 3
    # the claim passed the version begin() returned
    claim_call = [c for c in client.calls if c[0] == 'claim_deploy_scope'][0]
    assert claim_call[1]['p_expected_version'] == 2
    assert lease not in sb_mod._ACTIVE_LEASES and lease.leased is False
    # a late release after the claim is a no-op (no RPC)
    assert lease.release() is False
    assert not [c for c in client.calls if c[0] == 'release_deploy_scope']


def test_scope_lease_lost_warns_once(capsys):
    client = _FakeClient({'begin_deploy_scope': {'version': 0}, 'heartbeat_deploy_scope': False})
    lease = ScopeLease(client, 'field', 'cosmos', actor='u1', host='h1')
    lease.begin()
    lease.heartbeat(); lease.heartbeat(); lease.heartbeat()
    out = capsys.readouterr().out
    assert out.count('no longer held by this process') == 1
    assert lease.heartbeats == 0


def test_scope_lease_not_leased_skips_heartbeat():
    client = _FakeClient({'begin_deploy_scope': _APIError('PGRST202', 'missing'),
                          'get_deploy_scope_version': 0})
    lease = ScopeLease(client, 'observation', 'obs_a')
    # a lease that could not be taken fails closed (Codex on #599): no deploy runs unleased
    with pytest.raises(sb_mod.DeployScopeUnavailable, match='missing'):
        lease.begin()
    assert isinstance(sb_mod.DeployScopeUnavailable('observation', 'o', 'x'), sb_mod.DeployScopeBusy)
    lease.heartbeat()
    assert not [c for c in client.calls if c[0] == 'heartbeat_deploy_scope']
    assert lease not in sb_mod._ACTIVE_LEASES


def test_release_active_leases_on_abort():
    client = _FakeClient({'begin_deploy_scope': {'version': 1}, 'release_deploy_scope': True})
    a = ScopeLease(client, 'observation', 'obs_a', actor='u1', host='h')
    b = ScopeLease(client, 'observation', 'obs_b', actor='u1', host='h')
    a.begin(); b.begin()
    assert release_active_leases() == 2
    assert sb_mod._ACTIVE_LEASES == []
    rel = [(c[1]['p_scope_key'], c[1]['p_version']) for c in client.calls if c[0] == 'release_deploy_scope']
    assert sorted(rel) == [('obs_a', 1), ('obs_b', 1)]


# ---------------------------------------------------------------------------
# background timer: heartbeats arrive without any upload completing, and stop
# the moment the lease is claimed or released
# ---------------------------------------------------------------------------

def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_scope_lease_timer_heartbeats_without_progress():
    client = _FakeClient({'begin_deploy_scope': {'version': 3}, 'heartbeat_deploy_scope': True,
                          'release_deploy_scope': True})
    lease = ScopeLease(client, 'field', 'cosmos', actor='u1', host='h', heartbeat_interval_s=0.02)
    lease.begin()
    # no add()/flush anywhere: the timer alone keeps the lease alive
    assert _wait_for(lambda: lease.heartbeats >= 3)
    assert all(p['p_version'] == 3 for n, p in client.calls if n == 'heartbeat_deploy_scope')
    assert lease.release() is True
    assert lease._timer is None
    n_after = len([c for c in client.calls if c[0] == 'heartbeat_deploy_scope'])
    time.sleep(0.1)
    # nothing beats after the release
    assert len([c for c in client.calls if c[0] == 'heartbeat_deploy_scope']) == n_after
    assert client.calls[-1][0] == 'release_deploy_scope'


def test_scope_lease_timer_stops_on_claim_and_survives_rpc_errors():
    client = _FakeClient({'begin_deploy_scope': {'version': 1},
                          'heartbeat_deploy_scope': _APIError('X', 'network'),
                          'claim_deploy_scope': {'claimed': True, 'version': 2, 'conflict': False}})
    lease = ScopeLease(client, 'observation', 'o', actor='u1', host='h', heartbeat_interval_s=0.02)
    lease.begin()
    assert _wait_for(lambda: len([c for c in client.calls if c[0] == 'heartbeat_deploy_scope']) >= 2)
    assert lease.heartbeats == 0 and lease.leased   # failures are swallowed, the lease stays
    assert lease.claim()['claimed']
    assert lease._timer is None and lease not in sb_mod._ACTIVE_LEASES
    assert client.calls[-1][0] == 'claim_deploy_scope'


def test_scope_lease_timer_not_started_without_lease():
    client = _FakeClient({'begin_deploy_scope': _APIError('PGRST202', 'missing'),
                          'get_deploy_scope_version': 0})
    lease = ScopeLease(client, 'observation', 'o', heartbeat_interval_s=0.01)
    with pytest.raises(sb_mod.DeployScopeUnavailable):
        lease.begin()
    time.sleep(0.05)
    assert lease._timer is None
    assert not [c for c in client.calls if c[0] == 'heartbeat_deploy_scope']


def test_scope_lease_announces_steal_and_stale(capsys):
    client = _FakeClient({'begin_deploy_scope': {
        'version': 1, 'stolen': True, 'previous': {'in_progress_host': 'host-a', 'heartbeat_at': 'T'}}})
    ScopeLease(client, 'field', 'cosmos', actor='u1', host='h').begin()
    assert 'Stole the deploy lease' in capsys.readouterr().out
    client = _FakeClient({'begin_deploy_scope': {
        'version': 1, 'took_over_stale': True, 'previous': {'in_progress_host': 'host-a'}}})
    ScopeLease(client, 'field', 'cosmos', actor='u1', host='h').begin()
    assert 'stale deploy lease' in capsys.readouterr().out


# ---------------------------------------------------------------------------
# flusher heartbeat cadence: per batch and by wall clock, best-effort
# ---------------------------------------------------------------------------

def test_heartbeating_flusher_per_batch():
    flushed, beats = [], []
    f = _HeartbeatingFlusher(flushed.append, batch_size=200, heartbeat=lambda: beats.append(1),
                             interval_s=1e9, clock=lambda: 0.0)
    for i in range(450):
        f.add(i)
    f.flush()
    assert sum(len(b) for b in flushed) == 450
    assert len(beats) == 2 and f.heartbeats == 2   # at 200 and 400 landed files


def test_heartbeating_flusher_by_wall_clock_and_swallows_errors():
    now = [0.0]
    calls = []

    def hb():
        calls.append(1)
        raise RuntimeError("network")

    f = _HeartbeatingFlusher(lambda b: None, batch_size=1000, heartbeat=hb,
                             interval_s=300.0, clock=lambda: now[0])
    f.add(1)
    assert calls == []
    now[0] = 301.0
    f.add(2)          # a handful of multi-GB files still heartbeat on time
    assert len(calls) == 1 and f.heartbeats == 0   # fired, failure swallowed
    now[0] = 400.0
    f.add(3)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# DB-backed state machine
# ---------------------------------------------------------------------------

@requires_local_db
def test_deploy_lease_sql_harness():
    result = run_sql(SQL_FILE.read_text(), timeout=60)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"deploy lease harness FAILED (psql exit {result.returncode}).\nOutput:\n{combined}")
    assert PASS_MARKER in result.stdout, (
        f"deploy lease harness did not reach its PASS marker.\nOutput:\n{combined}")
