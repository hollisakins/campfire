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


def test_field_scope_lease_for_photometry(patched):
    seen = {}
    c, order = _client(lambda p: seen.update(p) or {'version': 2})
    patched(c)
    dcli._under_scope_lease({}, 'field', 'cosmos', False, lambda *a: order.append('write'), 'x')
    assert seen['p_scope_type'] == 'field' and seen['p_scope_key'] == 'cosmos'
    assert order == ['begin', 'write', 'claim']


def test_dry_run_takes_no_lease(patched):
    c, order = _client({'version': 4})
    patched(c)
    assert dcli._under_observation_lease({}, 'obs1', True, lambda *a: 'dry', 'obs1', {}) == 'dry'
    assert order == []


def test_a_degraded_lease_refuses_the_writer(patched):
    """begin_deploy_scope failing for a non-CF409 reason degrades to leased=False
    in the full deploy; a standalone writer refuses instead of running unguarded."""
    def broken(params):
        raise RuntimeError('connection reset')
    c, order = _client(broken)
    patched(c)
    ran = []
    with pytest.raises(SystemExit) as exc:
        dcli._under_scope_lease({}, 'field', 'cosmos', False, lambda *a: ran.append(a), 'x')
    assert exc.value.code == 1 and not ran and 'claim' not in order


def test_every_scope_is_leased_and_claimed(patched):
    keys = []
    c, order = _client(lambda p: keys.append(p['p_scope_key']) or {'version': 1})
    patched(c)
    dcli._under_scope_leases({}, [('field', 'a'), ('field', 'b'), ('field', 'a')], False,
                             lambda: order.append('write'))
    assert keys == ['a', 'b']
    assert order == ['begin', 'begin', 'write', 'claim', 'claim']


def test_field_lifecycle_flips_under_the_field_lease(patched, monkeypatch):
    seen = {}
    c, order = _client(lambda p: seen.update(p) or {'version': 3})
    patched(c)
    monkeypatch.setattr(dcli, 'load_config', lambda path, **kw: {})
    monkeypatch.setattr(dcli, 'get_supabase_client', lambda config: c)
    monkeypatch.setattr(dcli, 'get_user_id_from_token', lambda config: 'user-1')
    monkeypatch.setattr(dcli, 'get_field_deployment_ids', lambda client, field: [7, 8])
    monkeypatch.setattr(dcli, 'set_deployment_status',
                        lambda client, dep, status, actor=None: order.append(f'flip{dep}') or {})
    ctx = type('Ctx', (), {'obj': {}})()
    dcli._lifecycle_transition(ctx, None, (), False, False,
                               to_status='published', verb='Publish', field='cosmos')
    assert seen['p_scope_type'] == 'field' and seen['p_scope_key'] == 'cosmos'
    assert order == ['begin', 'flip7', 'flip8', 'claim']


def test_field_lifecycle_resolves_deployments_under_the_lease(patched, monkeypatch):
    # a field whose first deployment lands while the command starts: the lookup
    # happens only once the lease is held, never before it (Codex on #599)
    c, order = _client({'version': 3})
    patched(c)
    calls = []

    def ids(client, field):
        calls.append(list(order))
        return [9]
    monkeypatch.setattr(dcli, 'load_config', lambda path, **kw: {})
    monkeypatch.setattr(dcli, 'get_supabase_client', lambda config: c)
    monkeypatch.setattr(dcli, 'get_user_id_from_token', lambda config: 'user-1')
    monkeypatch.setattr(dcli, 'get_field_deployment_ids', ids)
    monkeypatch.setattr(dcli, 'set_deployment_status',
                        lambda client, dep, status, actor=None: order.append(f'flip{dep}') or {})
    ctx = type('Ctx', (), {'obj': {}})()
    dcli._lifecycle_transition(ctx, None, (), False, False,
                               to_status='revoked', verb='Revoke', field='cosmos')
    assert calls == [['begin']] and order == ['begin', 'flip9', 'claim']


def test_interactive_objects_edit_confirms_before_leasing(patched, monkeypatch):
    c, order = _client({'version': 1})
    patched(c)
    monkeypatch.setattr(dcli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(dcli.click, 'confirm', lambda *a, **k: order.append('confirm'))

    def edit(sb, ref, *, dry_run, yes):
        order.append(f'edit dry={dry_run} yes={yes}')
    dcli._confirm_then_lease({}, 'cosmos', False, False, edit, None, 'J1')
    # previewed and confirmed with no lease; applied under it without a second prompt
    assert order == ['edit dry=True yes=False', 'confirm', 'begin', 'edit dry=False yes=True', 'claim']


def test_objects_edit_with_yes_skips_the_preview(patched):
    c, order = _client({'version': 1})
    patched(c)
    dcli._confirm_then_lease({}, 'cosmos', False, True,
                             lambda sb, ref, *, dry_run, yes: order.append('edit'), None, 'J1')
    assert order == ['begin', 'edit', 'claim']


def test_writer_keywords_named_like_the_helper_pass_through(patched):
    # every real caller forwards dry_run= (and the objects edits yes=) to the writer
    c, order = _client({'version': 4})
    patched(c)
    seen = {}

    def writer(obs, config, *, dry_run, source_ids=None):
        seen.update(dry_run=dry_run, source_ids=source_ids)
        return 'ok'
    assert dcli._under_observation_lease({}, 'obs1', False, writer, 'obs1', {},
                                         dry_run=False, source_ids=['a']) == 'ok'
    assert seen == {'dry_run': False, 'source_ids': ['a']}
    assert dcli._under_scope_lease({}, 'field', 'f', True, writer, 'f', {}, dry_run=True) == 'ok'
    assert seen['dry_run'] is True


@pytest.mark.parametrize('dry', [True, False])
def test_deploy_json_command_end_to_end(patched, monkeypatch, dry):
    from click.testing import CliRunner
    c, order = _client({'version': 4})
    patched(c)
    monkeypatch.setattr(dcli, '_gate_admin', lambda *a, **k: None)
    monkeypatch.setattr(dcli, 'load_config', lambda *a, **k: {})
    calls = []
    monkeypatch.setattr(dcli, 'deploy_json', lambda obs, config, **kw: calls.append((obs, kw)))
    args = ['json', '--obs', 'obs1'] + (['--dry-run'] if dry else [])
    res = CliRunner().invoke(dcli.deploy_group, args, obj={})
    assert res.exit_code == 0, (res.output, res.exception)
    assert calls == [('obs1', {'dry_run': dry, 'source_ids': None})]
    assert order == ([] if dry else ['begin', 'claim'])


def test_multi_obs_deferred_reconcile_holds_the_field_lease(patched, monkeypatch):
    # Codex on #599: the per-observation leases are gone by the deferred phase
    from click.testing import CliRunner
    import campfire.deploy.reconcile as rec
    keys = []
    c, order = _client(lambda p: keys.append((p['p_scope_type'], p['p_scope_key'])) or {'version': 1})
    patched(c)
    monkeypatch.setattr(dcli, '_gate_admin', lambda *a, **k: None)
    monkeypatch.setattr(dcli, 'load_config', lambda *a, **k: {'supabase': {}})
    monkeypatch.setattr(dcli, 'get_supabase_client', lambda config: c)
    for name in ('refresh_filter_options', 'refresh_programs_overview', 'refresh_observations_overview'):
        monkeypatch.setattr(dcli, name, lambda sb: None)
    monkeypatch.setattr(dcli, 'deploy_observation',
                        lambda obs, config, **kw: {'needs_reconcile': True, 'field': 'cosmos'})
    monkeypatch.setattr(rec, 'reconcile_field_objects',
                        lambda sb, field, **kw: order.append(f'reconcile {field}') or (0, {}, []))
    res = CliRunner().invoke(dcli.deploy_group, ['--obs', 'a', 'b', '--no-photometry'],
                             obj={'service_role': False})
    assert res.exit_code == 0, (res.output, res.exception)
    assert keys == [('field', 'cosmos')]
    assert order == ['begin', 'reconcile cosmos', 'claim']
