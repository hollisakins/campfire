"""Standalone `deploy json / zfit / lines` run under the observation's deploy
lease (Codex on #599): a data-release freeze must never snapshot one of them
half-way, and a freeze in progress refuses them."""
import pytest

import campfire.deploy.cli as dcli
import campfire.deploy.supabase as sb
from tests.fakes import FakePostgrest


def _client(begin):
    order = []

    def rec(name, value):
        def h(params):
            order.append(name)
            return value(params) if callable(value) else value
        return h
    c = FakePostgrest(rpc_handlers={
        'begin_deploy_scope': rec('begin', begin),
        'claim_deploy_scope': rec('claim', {'claimed': True, 'conflict': False}),
        'release_deploy_scope': rec('release', True),
        'heartbeat_deploy_scope': rec('heartbeat', True),
    })
    return c, order


@pytest.fixture
def patched(monkeypatch):
    def install(client):
        monkeypatch.setattr(sb, 'get_supabase_client', lambda config: client)
        monkeypatch.setattr(sb, 'get_user_id_from_token', lambda config: 'user-1')
    return install


def test_writer_runs_between_begin_and_claim(patched):
    c, order = _client({'version': 4})
    patched(c)

    def writer(obs, config):
        order.append('write')
        return 'done'
    assert dcli._under_observation_lease({}, 'obs1', False, writer, 'obs1', {}) == 'done'
    assert order == ['begin', 'write', 'claim']


def test_failure_releases_the_lease(patched):
    c, order = _client({'version': 4})
    patched(c)

    def writer(obs, config):
        raise RuntimeError('boom')
    with pytest.raises(RuntimeError, match='boom'):
        dcli._under_observation_lease({}, 'obs1', False, writer, 'obs1', {})
    assert order[0] == 'begin' and 'release' in order and 'claim' not in order


def test_a_freeze_in_progress_refuses_cleanly(patched):
    class Busy(Exception):
        code = 'CF409'
        message = 'deploy_scope_busy: a data-release freeze is running (DR-x)'

    def busy(params):
        raise Busy()
    c, order = _client(busy)
    patched(c)
    ran = []
    with pytest.raises(SystemExit) as exc:
        dcli._under_observation_lease({}, 'obs1', False, lambda *a: ran.append(a), 'obs1', {})
    assert exc.value.code == 1 and not ran


def test_dry_run_takes_no_lease(patched):
    c, order = _client({'version': 4})
    patched(c)
    assert dcli._under_observation_lease({}, 'obs1', True, lambda *a: 'dry', 'obs1', {}) == 'dry'
    assert order == []
