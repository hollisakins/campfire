"""Data releases P4d: publish / withdraw / diff / removal references, and the
`deploy remove` hook. The database side is supabase/tests/check_dr_publish.sql."""
import json

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire import dr
from tests.fakes import FakePostgrest

TAG = 'DR2026.0-rehearsal'
ROW = {'tag': TAG, 'state': 'published', 'counts': {'spectra': 3}, 'published_at': '2026-10-13T12:00:00+00:00',
       'doi': '10.5281/zenodo.1'}
DIFF = {'tag': TAG, 'state': 'ready', 'frozen_at': '2026-10-13T10:00:00+00:00',
        'counts': {'object_renamed': 1, 'spectrum_new': 2, 'some_future_kind': 1},
        'samples': {'object_renamed': [{'object_id': 7, 'name': 'J1', 'frozen': 'J1', 'live': 'J1-b'}],
                    'spectrum_new': [{'spectrum_id': 'obs_1_prism'}, {'spectrum_id': 'obs_2_prism'}],
                    'some_future_kind': [{'spectrum_id': 'x'}]}}
REFS = [{'tag': TAG, 'state': 'published', 'n_spectra': 4, 'n_members': 16, 'n_unarchived': 0, 'blocks': True},
        {'tag': 'DR-old', 'state': 'withdrawn', 'n_spectra': 4, 'n_members': 16, 'n_unarchived': 0, 'blocks': False}]


def _client(**handlers):
    return FakePostgrest(rpc_handlers=handlers)


def test_rpc_wrappers_pass_parameters():
    seen = {}

    def grab(name, value):
        def h(params):
            seen[name] = params
            return value
        return h
    c = _client(dr_publish=grab('pub', ROW), dr_withdraw=grab('wd', {**ROW, 'state': 'withdrawn'}),
                dr_diff=grab('diff', DIFF), dr_observation_references=grab('refs', REFS))
    assert dr.publish_release(c, TAG, doi='10.5281/zenodo.1')['state'] == 'published'
    assert seen['pub'] == {'p_tag': TAG, 'p_doi': '10.5281/zenodo.1', 'p_known_issues': None}
    assert dr.withdraw_release(c, TAG, 'bad')['state'] == 'withdrawn'
    assert seen['wd'] == {'p_tag': TAG, 'p_reason': 'bad'}
    assert dr.fetch_diff(c, TAG, limit=3)['counts']['object_renamed'] == 1
    assert seen['diff'] == {'p_tag': TAG, 'p_limit': 3}
    assert [r['tag'] for r in dr.removal_blockers(c, 'obs')] == [TAG]
    assert seen['refs'] == {'p_observation': 'obs'}


def test_unexpected_payload_raises():
    with pytest.raises(RuntimeError, match='unexpected payload'):
        dr.publish_release(_client(dr_publish=None), TAG)


def test_format_diff_and_references():
    text = dr.format_diff(DIFF, samples=1)
    assert 'objects renamed' in text and 'J1 (#7): J1 → J1-b' in text
    assert 'obs_1_prism' in text and 'obs_2_prism' not in text      # capped at 1 sample
    assert 'some_future_kind' in text                              # unknown kinds still shown
    assert dr.format_diff({**DIFF, 'counts': {}}).endswith('no differences')
    refs = dr.format_references('obs', REFS)
    assert 'BLOCKS removal' in refs and 'ok to remove' in refs
    assert dr.format_references('obs', []) == 'obs: in no data release'


def _patch(monkeypatch, client):
    import campfire.deploy.cli as dcli
    import campfire.deploy.config as cfg
    import campfire.deploy.supabase as sb
    monkeypatch.setattr(cfg, 'load_config', lambda *a, **k: {'supabase': {'_auth_mode': 'local'}})
    monkeypatch.setattr(sb, 'get_supabase_client', lambda config: client)
    monkeypatch.setattr(dcli, '_gate_admin', lambda config, **k: None)


def test_cli_publish_withdraw(monkeypatch):
    c = _client(dr_publish=ROW, dr_withdraw={**ROW, 'state': 'withdrawn', 'withdrawn_at': '2026-10-13T13:00:00+00:00'})
    _patch(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'publish', TAG, '--doi', '10.5281/zenodo.1', '--yes'])
    assert res.exit_code == 0, res.output
    assert f'{TAG}: published' in res.output
    res = CliRunner().invoke(main_cli.cli, ['dr', 'withdraw', TAG, '--reason', 'calibration', '--yes'])
    assert res.exit_code == 0 and 'withdrawn at' in res.output
    # the prompt defaults to no
    res = CliRunner().invoke(main_cli.cli, ['dr', 'publish', TAG], input='\n')
    assert res.exit_code == 1 and 'Aborted' in res.output
    assert c.rpc_names().count('dr_publish') == 1


def test_cli_publish_refused_is_one_line(monkeypatch):
    def refuse(params):
        raise RuntimeError('release DR is copying (publish needs ready)')
    _patch(monkeypatch, _client(dr_publish=refuse))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'publish', TAG, '--yes'])
    assert res.exit_code == 1 and 'publish needs ready' in res.output


def test_cli_diff_and_refs(monkeypatch):
    _patch(monkeypatch, _client(dr_diff=DIFF, dr_observation_references=REFS))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'diff', TAG, 'live', '--limit', '2'])
    assert res.exit_code == 0 and 'objects renamed' in res.output
    res = CliRunner().invoke(main_cli.cli, ['dr', 'diff', TAG, '--json'])
    assert json.loads(res.output)['counts']['spectrum_new'] == 2
    res = CliRunner().invoke(main_cli.cli, ['dr', 'refs', '--obs', 'obs'])
    assert res.exit_code == 1 and 'BLOCKS removal' in res.output
    _patch(monkeypatch, _client(dr_observation_references=[]))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'refs', '--obs', 'obs'])
    assert res.exit_code == 0 and 'in no data release' in res.output


# ---------------------------------------------------------------------------
# deploy remove consults the releases
# ---------------------------------------------------------------------------

def _remove_env(monkeypatch, client):
    import campfire.deploy.remove as rm
    deleted = []
    monkeypatch.setattr(rm, 'get_supabase_client', lambda cfg: client)
    monkeypatch.setattr(rm, 'get_user_id_from_token', lambda cfg: 'user-1')
    monkeypatch.setattr(rm, '_fetch_observation',
                        lambda sb, o: {'name': o, 'field': 'f', 'program_slug': 'p', 'latest_deployment_id': 1})
    monkeypatch.setattr(rm, '_fetch_targets', lambda sb, o: [{'target_id': 't1', 'id': 1}])
    monkeypatch.setattr(rm, '_count_spectra', lambda sb, ids: 3)
    monkeypatch.setattr(rm, '_count_by_obs', lambda sb, t, o: 0)
    monkeypatch.setattr(rm, '_count_comments', lambda sb, ids: 0)
    monkeypatch.setattr(rm, '_is_inspected', lambda t: False)
    def fake_delete(sb, obs, ids, check=lambda step: None):
        # two batches, each guarded like the real helper
        for step in ('spectra batch 1', 'targets batch 1'):
            check(step)
            deleted.append(step)
    monkeypatch.setattr(rm, '_delete_db_rows', fake_delete)
    monkeypatch.setattr(rm, 'log_deploy_event', lambda *a, **k: None)
    monkeypatch.setattr(rm, 'refresh_filter_options', lambda sb: None)
    monkeypatch.setattr(rm, 'refresh_programs_overview', lambda sb: None)
    monkeypatch.setattr(rm, 'refresh_observations_overview', lambda sb: None)
    return rm, deleted


def test_remove_refuses_while_a_release_blocks(monkeypatch, capsys):
    rm, deleted = _remove_env(monkeypatch, _client(dr_observation_references=REFS))
    # an error, not a silent return: automation must see the refusal (Codex on #602)
    with pytest.raises(rm.RemovalRefused, match='a data release still needs it'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    out = capsys.readouterr().out
    assert 'REFUSED' in out and 'BLOCKS removal' in out and 'dr withdraw' in out
    assert not deleted


def test_remove_fails_closed_when_the_check_cannot_run(monkeypatch, capsys):
    def boom(params):
        raise RuntimeError('Could not find the function public.dr_observation_references')
    rm, deleted = _remove_env(monkeypatch, _client(dr_observation_references=boom))
    with pytest.raises(rm.RemovalRefused):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert 'could not check the data releases' in capsys.readouterr().out
    assert not deleted


def test_remove_proceeds_when_nothing_blocks(monkeypatch):
    refs = [{**REFS[1]}]
    rm, deleted = _remove_env(monkeypatch, _client(dr_observation_references=refs, heartbeat_deploy_scope=True))
    rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert deleted


# Codex on #602: the check runs under the observation's deploy lease

def _ordered_client(refs, *, begin=None):
    order = []

    def rec(name, value):
        def h(params):
            order.append(name)
            return value(params) if callable(value) else value
        return h
    c = _client(begin_deploy_scope=rec('begin', begin or {'version': 3}),
                get_deploy_scope_version=3,
                dr_observation_references=rec('refs', refs),
                claim_deploy_scope=rec('claim', True),
                release_deploy_scope=rec('release', True),
                heartbeat_deploy_scope=rec('heartbeat', True))
    return c, order


def test_remove_takes_the_lease_before_checking_and_claims_after(monkeypatch):
    c, order = _ordered_client([{**REFS[1]}])
    rm, deleted = _remove_env(monkeypatch, c)
    rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert deleted
    assert order[:2] == ['begin', 'refs'] and order[-1] == 'claim' and 'release' not in order


def test_remove_releases_the_lease_when_refused(monkeypatch):
    c, order = _ordered_client(REFS)
    rm, deleted = _remove_env(monkeypatch, c)
    with pytest.raises(rm.RemovalRefused):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert not deleted
    assert order == ['begin', 'refs', 'release']


def test_remove_refused_while_a_freeze_or_deploy_holds_the_scope(monkeypatch, capsys):
    class Busy(Exception):
        code = 'CF409'
        message = 'deploy_scope_busy: a data-release freeze is running (DR-x)'

    def busy(params):
        raise Busy()
    c, order = _ordered_client([], begin=busy)
    rm, deleted = _remove_env(monkeypatch, c)
    with pytest.raises(rm.RemovalRefused, match='freeze is running'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    out = capsys.readouterr().out
    assert 'REFUSED' in out and 'freeze is running' in out
    assert not deleted and order == ['begin']


def test_remove_refused_without_a_lease(monkeypatch, capsys):
    def broken(params):
        raise RuntimeError('network down')
    c, order = _ordered_client([], begin=broken)
    rm, deleted = _remove_env(monkeypatch, c)
    monkeypatch.setattr('campfire.deploy.supabase.get_deploy_scope_version', lambda *a, **k: 0)
    with pytest.raises(rm.RemovalRefused, match='lease could not be taken'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert 'could not take the deploy lease' in capsys.readouterr().out
    assert not deleted and 'refs' not in order


def _lease_client(heartbeats, *, claim=None):
    beats = iter(heartbeats)
    return _client(begin_deploy_scope={'version': 3},
                   dr_observation_references=[{**REFS[1]}],
                   heartbeat_deploy_scope=lambda p: next(beats),
                   claim_deploy_scope=claim or {'claimed': True, 'conflict': False},
                   release_deploy_scope=True)


def test_remove_stops_before_deleting_when_the_lease_is_lost(monkeypatch):
    # Codex on #602: a stolen / taken-over lease means another deploy may be
    # writing the observation
    rm, deleted = _remove_env(monkeypatch, _lease_client([False]))
    with pytest.raises(rm.RemovalLeaseLost, match='nothing was deleted'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert not deleted


def test_remove_stops_mid_batch_when_the_lease_is_lost(monkeypatch):
    # Codex on #602: re-checked before EVERY batch, not once before the helper
    rm, deleted = _remove_env(monkeypatch, _lease_client([True, False]))
    wiped = []
    monkeypatch.setattr(rm, '_delete_r2_prefixes', lambda *a, **k: wiped.append(a) or {})
    with pytest.raises(rm.RemovalLeaseLost, match='deleted so far: spectra batch 1; stopped before targets batch 1'):
        rm.remove_observation('obs1', {}, supabase_only=False, auto_approve=True, skip_rebuild=True)
    assert deleted == ['spectra batch 1'] and not wiped


def test_remove_keeps_the_bucket_when_the_lease_is_lost_after_the_rows(monkeypatch):
    rm, deleted = _remove_env(monkeypatch, _lease_client([True, True, None]))
    wiped = []

    def fake_prefixes(config, obs, check=lambda step: None):
        check('prefix data/products/nirspec/obs1/')
        wiped.append(obs)
        return {}
    monkeypatch.setattr(rm, '_delete_r2_prefixes', fake_prefixes)
    with pytest.raises(rm.RemovalLeaseLost, match='its rows were deleted; bucket pages deleted: none; stopped before bucket prefix'):
        rm.remove_observation('obs1', {}, supabase_only=False, auto_approve=True, skip_rebuild=True)
    assert deleted == ['spectra batch 1', 'targets batch 1'] and not wiped


def test_remove_rechecks_the_lease_before_every_bucket_page(monkeypatch):
    # Codex on #602: one prefix can span many delete_objects pages
    from campfire.deploy import tiles

    class Paged:
        def __init__(self):
            self.deleted = []

        def get_paginator(self, name):
            pages = [{'Contents': [{'Key': 'k1'}]}, {'Contents': [{'Key': 'k2'}]}]
            return type('P', (), {'paginate': lambda self, **kw: iter(pages)})()

        def delete_objects(self, Bucket, Delete):
            self.deleted.extend(o['Key'] for o in Delete['Objects'])
    store = Paged()
    beats = iter([True, False])
    with pytest.raises(RuntimeError, match='lost'):
        def guard(n):
            if not next(beats):
                raise RuntimeError('lease lost')
        tiles.delete_r2_prefix(store, 'b', 'p/', before_page=guard)
    assert store.deleted == ['k1']
    # and without a callback it still deletes every page
    store2 = Paged()
    assert tiles.delete_r2_prefix(store2, 'b', 'p/') == 2 and store2.deleted == ['k1', 'k2']


def test_remove_reports_a_claim_conflict(monkeypatch):
    rm, deleted = _remove_env(monkeypatch, _lease_client([True, True], claim={'claimed': False, 'conflict': True}))
    with pytest.raises(rm.RemovalLeaseLost, match='another deploy took the lease'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert deleted


def test_remove_reports_an_indeterminate_claim(monkeypatch):
    # Codex on #602: claim_deploy_scope turns RPC errors into a nominal success
    rm, deleted = _remove_env(monkeypatch, _lease_client(
        [True, True], claim={'claimed': True, 'conflict': False, 'error': 'timeout'}))
    with pytest.raises(rm.RemovalLeaseLost, match='final lease claim could not be confirmed'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert deleted


def test_remove_inspection_refusal_raises(monkeypatch):
    # Codex on #602: the inspected-targets refusal is a refusal too
    rm, deleted = _remove_env(monkeypatch, _client(dr_observation_references=[], heartbeat_deploy_scope=True))
    monkeypatch.setattr(rm, '_is_inspected', lambda t: True)
    with pytest.raises(rm.RemovalRefused, match='user inspection data'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert not deleted


def test_remove_rechecks_the_lease_before_reconciling(monkeypatch):
    # Codex on #602: reconciliation writes objects; never after the lease is lost
    import campfire.deploy.reconcile as rc
    rm, deleted = _remove_env(monkeypatch, _lease_client([True, True, False]))
    steps = []

    def fake_reconcile(client, field, *, abort_on_split_merge, before_write=None):
        steps.append('read')            # reads happen first...
        before_write()                  # ...the lease is checked at the write boundary
        steps.append('write')
        return 0, {}, set()
    monkeypatch.setattr(rc, 'reconcile_field_objects', fake_reconcile)
    with pytest.raises(rm.RemovalLeaseLost, match='reconciliation of field .f. was NOT applied'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=True, skip_rebuild=False)
    assert deleted and steps == ['read']


def test_reconcile_calls_before_write_at_its_write_points(monkeypatch):
    import campfire.deploy.reconcile as rc
    calls = []
    monkeypatch.setattr(rc, 'fetch_field_targets', lambda c, f: [])
    monkeypatch.setattr(rc, 'fetch_existing_objects',
                        lambda c, f: ([{'id': 1, 'is_active': True}, {'id': 2, 'is_active': True}], {}, None))

    class Q:
        def update(self, *a, **k):
            calls.append('write')
            return self

        def eq(self, *a):
            return self

        def execute(self):
            return None
    client = type('C', (), {'table': lambda self, name: Q()})()
    rc.reconcile_field_objects(client, 'f', before_write=lambda: calls.append('check'))
    assert calls == ['check', 'write', 'check', 'write']


def test_reconcile_checks_before_apply_and_before_the_redshift_recompute(monkeypatch):
    # Codex on #602: the redshift recompute is a field-wide write as well
    import campfire.deploy.reconcile as rc
    calls = []
    monkeypatch.setattr(rc, 'fetch_field_targets', lambda c, f: [{'target_id': 't1'}])
    monkeypatch.setattr(rc, 'fetch_existing_objects', lambda c, f: ([], {}, None))
    monkeypatch.setattr(rc, 'fetch_spectra_metadata', lambda c, ids: {})
    monkeypatch.setattr(rc, 'cluster_targets', lambda t, r: [[0]])
    monkeypatch.setattr(rc, 'classify', lambda **k: object())
    monkeypatch.setattr(rc, 'print_summary', lambda *a, **k: None)
    monkeypatch.setattr(rc, 'confirm_proposals', lambda *a, **k: None)
    monkeypatch.setattr(rc, 'apply_proposals', lambda c, f, p: (calls.append('apply') or ({}, set())))
    monkeypatch.setattr(rc, 'compute_object_redshift_auto', lambda c, f: calls.append('redshift') or 0)
    rc.reconcile_field_objects(object(), 'f', before_write=lambda: calls.append('check'))
    assert calls == ['check', 'apply', 'check', 'redshift']


def test_cli_remove_exits_1_when_refused(monkeypatch):
    # Codex on #602: `deploy remove --auto-approve` must not exit 0 on a refusal
    import campfire.deploy.cli as dcli
    import campfire.deploy.remove as rm

    def refuse(obs_name, config, **kw):
        raise rm.RemovalRefused(f'{obs_name}: a data release still needs it (see above)')
    monkeypatch.setattr(dcli, 'load_config', lambda *a, **k: {})
    monkeypatch.setattr(rm, 'remove_observation', refuse)
    res = CliRunner().invoke(dcli.remove, ['--obs', 'obs1', '--auto-approve'])
    assert res.exit_code == 1, res.output
    assert 'a data release still needs it' in res.output


def test_remove_dry_run_checks_without_leasing(monkeypatch):
    c, order = _ordered_client([{**REFS[1]}])
    rm, deleted = _remove_env(monkeypatch, c)
    rm.remove_observation('obs1', {}, dry_run=True, supabase_only=True, auto_approve=True, skip_rebuild=True)
    assert not deleted and order == ['refs']


# Codex on #602: an interactive removal confirms before it takes the lease

def test_interactive_remove_confirms_before_leasing_then_rechecks(monkeypatch):
    c, order = _ordered_client([{**REFS[1]}])
    rm, deleted = _remove_env(monkeypatch, c)
    monkeypatch.setattr('builtins.input', lambda prompt: order.append('prompt') or 'y')
    rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=False, skip_rebuild=True)
    assert deleted
    # previewed and confirmed with no lease, then every check again under it
    assert order[:4] == ['refs', 'prompt', 'begin', 'refs'] and order[-1] == 'claim'
    assert order.count('prompt') == 1


def test_interactive_remove_declined_takes_no_lease(monkeypatch):
    c, order = _ordered_client([{**REFS[1]}])
    rm, deleted = _remove_env(monkeypatch, c)
    monkeypatch.setattr('builtins.input', lambda prompt: 'n')
    rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=False, skip_rebuild=True)
    assert not deleted and 'begin' not in order


def test_interactive_remove_refuses_when_the_observation_changed(monkeypatch):
    c, order = _ordered_client([{**REFS[1]}])
    rm, deleted = _remove_env(monkeypatch, c)
    counts = iter([3, 5])        # a deploy added spectra while the prompt waited
    monkeypatch.setattr(rm, '_count_spectra', lambda sb, ids: next(counts))
    monkeypatch.setattr('builtins.input', lambda prompt: 'y')
    with pytest.raises(rm.RemovalRefused, match='changed between the confirmation'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=False, skip_rebuild=True)
    assert not deleted and order[-1] == 'release'


def test_interactive_remove_refuses_a_same_size_redeploy(monkeypatch):
    # Codex on #602: equal counts, different targets / deployment
    c, order = _ordered_client([{**REFS[1]}])
    rm, deleted = _remove_env(monkeypatch, c)
    targets = iter([[{'target_id': 't1', 'id': 1}], [{'target_id': 't9', 'id': 9}]])
    monkeypatch.setattr(rm, '_fetch_targets', lambda sb, o: next(targets))
    monkeypatch.setattr('builtins.input', lambda prompt: 'y')
    with pytest.raises(rm.RemovalRefused, match='its targets'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=False, skip_rebuild=True)
    assert not deleted and order[-1] == 'release'


def test_interactive_remove_refuses_when_redeployed_during_the_prompt(monkeypatch):
    # every finished deploy bumps the scope version
    c, order = _ordered_client([{**REFS[1]}], begin={'version': 4})
    rm, deleted = _remove_env(monkeypatch, c)
    monkeypatch.setattr('builtins.input', lambda prompt: 'y')
    with pytest.raises(rm.RemovalRefused, match='redeployed after the confirmation'):
        rm.remove_observation('obs1', {}, supabase_only=True, auto_approve=False, skip_rebuild=True)
    assert not deleted and order[-1] == 'release'
