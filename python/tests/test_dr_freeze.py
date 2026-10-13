"""`campfire dr freeze` / `dr purge` (data releases P4a): the components
builder, the enqueue → poll loop, the release formatter and the CLI wiring,
all against a fake client. The database side (dr_enqueue_freeze /
dr_freeze_job / dr_purge_release, RLS matrix) is supabase/tests/check_dr_freeze.sql.
"""
import json
import types

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire.dr import (components_from_options, enqueue_freeze, format_readiness,
                         format_release, format_releases, purge_release, wait_for_freeze)


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------

def test_components_from_options_normalises():
    comps = components_from_options(nirspec=True, fields=('egs', 'cosmos', 'egs'),
                                    photometry=('cosmos/COSMOS v1.2',))
    assert comps == ['nirspec', 'nircam/cosmos', 'nircam/egs', 'photometry/cosmos/COSMOS v1.2']


def test_components_from_options_rejects_empty_and_bad_photometry():
    with pytest.raises(ValueError, match='nothing to freeze'):
        components_from_options(nirspec=False)
    with pytest.raises(ValueError, match='<field>/<catalog_name>'):
        components_from_options(nirspec=True, photometry=('cosmos',))


# ---------------------------------------------------------------------------
# fake client: rpc + table().select() on dr_releases
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, client, table):
        self._client = client
        self._table = table
        self._tag = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        if col == 'tag':
            self._tag = val
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        rows = self._client.poll_rows
        if not rows:
            return _Resp([])
        row = rows.pop(0) if len(rows) > 1 else rows[0]
        self._client.polls += 1
        return _Resp([row] if row is not None else [])


class _RPC:
    def __init__(self, data):
        self._data = data

    def execute(self):
        if isinstance(self._data, Exception):
            raise self._data
        return _Resp(self._data)


class _FakeClient:
    def __init__(self, *, rpc_data=None, poll_rows=None):
        self.rpc_data = rpc_data or {}
        self.poll_rows = list(poll_rows or [])
        self.rpc_calls = []
        self.polls = 0

    def rpc(self, name, params=None):
        self.rpc_calls.append((name, params))
        data = self.rpc_data.get(name)
        return _RPC(data)

    def table(self, name):
        return _Query(self, name)


FROZEN = {'tag': 'DR-t', 'state': 'frozen', 'components': ['nirspec'],
          'counts': {'spectra': 163, 'objects': 101, 'line_fits': 19, 'photometry': 0, 'members': 489},
          'error': None, 'frozen_at': '2026-10-12T19:54:12+00:00', 'freeze_duration_ms': 20}


def test_enqueue_freeze_passes_params():
    c = _FakeClient(rpc_data={'dr_enqueue_freeze': {**FROZEN, 'mode': 'inline'}})
    out = enqueue_freeze(c, 'DR-t', ['nirspec'], notes='n', inline=True)
    assert out['state'] == 'frozen'
    assert c.rpc_calls == [('dr_enqueue_freeze', {'p_tag': 'DR-t', 'p_components': ['nirspec'],
                                                  'p_notes': 'n', 'p_inline': True})]


def test_enqueue_freeze_rejects_non_dict():
    c = _FakeClient(rpc_data={'dr_enqueue_freeze': [1]})
    with pytest.raises(RuntimeError, match='unexpected payload'):
        enqueue_freeze(c, 'DR-t', ['nirspec'])


def test_wait_for_freeze_polls_until_terminal():
    c = _FakeClient(poll_rows=[{'tag': 'DR-t', 'state': 'freezing'},
                               {'tag': 'DR-t', 'state': 'freezing'},
                               FROZEN])
    slept = []
    echoed = []
    row = wait_for_freeze(c, 'DR-t', poll_seconds=7, sleep=slept.append, echo=echoed.append)
    assert row['state'] == 'frozen'
    assert slept == [7, 7]
    assert echoed == ['  DR-t: freezing …']      # state echoed once per change


def test_wait_for_freeze_times_out():
    c = _FakeClient(poll_rows=[{'tag': 'DR-t', 'state': 'freezing'}])
    ticks = iter([0, 10, 20, 4000])
    with pytest.raises(TimeoutError):
        wait_for_freeze(c, 'DR-t', poll_seconds=1, timeout_seconds=3000,
                        sleep=lambda s: None, clock=lambda: next(ticks))


def test_wait_for_freeze_missing_row():
    c = _FakeClient(poll_rows=[None])
    with pytest.raises(RuntimeError, match='disappeared'):
        wait_for_freeze(c, 'DR-t', sleep=lambda s: None)


def test_purge_release_calls_rpc():
    c = _FakeClient(rpc_data={'dr_purge_release': {'tag': 'DR-t', 'state_before': 'frozen',
                                                   'deleted': {'spectra': 1}}})
    assert purge_release(c, 'DR-t')['deleted'] == {'spectra': 1}
    assert c.rpc_calls == [('dr_purge_release', {'p_tag': 'DR-t'})]


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def test_format_release_block():
    text = format_release(FROZEN)
    assert text.splitlines()[0] == 'DR-t: frozen'
    assert 'components: nirspec' in text
    assert 'frozen: 163 spectra · 101 objects · 19 line_fits · 0 photometry · 489 members' in text
    assert 'frozen at 2026-10-12 19:54 UTC in 0.0 s' in text
    assert 'error' not in text
    assert 'error: boom' in format_release({**FROZEN, 'state': 'failed', 'error': 'boom'})


def test_format_releases_section_and_readiness_hook():
    report = {'generated_at': None, 'spectra_by_version': {}, 'leases': [],
              'science_hash_missing': {}, 'releases': [FROZEN, {**FROZEN, 'tag': 'DR-x', 'state': 'failed',
                                                                 'error': 'boom', 'counts': {}}]}
    lines = format_releases(report)
    assert lines[1] == 'Data releases: 2'
    assert any('DR-t' in ln and 'frozen' in ln and '163' in ln for ln in lines)
    assert any('DR-x: boom' in ln for ln in lines)
    text = format_readiness(report)
    assert 'Data releases: 2' in text
    # a pre-P4a database (no key) renders no section at all
    assert 'Data releases' not in format_readiness({**report, 'releases': None})


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _patch(monkeypatch, client):
    import campfire.deploy.cli as dcli
    import campfire.deploy.config as cfg
    import campfire.deploy.supabase as sb
    monkeypatch.setattr(cfg, 'load_config', lambda *a, **k: {'supabase': {'_auth_mode': 'local'}})
    monkeypatch.setattr(sb, 'get_supabase_client', lambda config: client)
    monkeypatch.setattr(dcli, '_gate_admin', lambda config, **k: None)


def test_cli_freeze_inline_prints_block(monkeypatch):
    c = _FakeClient(rpc_data={'dr_enqueue_freeze': {**FROZEN, 'mode': 'inline'}})
    _patch(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'freeze', '--tag', 'DR-t', '--nirspec', '--local'])
    assert res.exit_code == 0, res.output
    assert 'Freeze of DR-t (nirspec): frozen [inline]' in res.output
    assert '163 spectra' in res.output
    # --local forces the inline path
    assert c.rpc_calls[0][1]['p_inline'] is True


def test_cli_freeze_waits_for_cron_job(monkeypatch):
    c = _FakeClient(rpc_data={'dr_enqueue_freeze': {'tag': 'DR-t', 'state': 'freezing', 'mode': 'cron'}},
                    poll_rows=[{'tag': 'DR-t', 'state': 'freezing'}, FROZEN])
    _patch(monkeypatch, c)
    monkeypatch.setattr('time.sleep', lambda s: None)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'freeze', '--tag', 'DR-t', '--nirspec',
                                            '--field', 'cosmos', '--poll', '0', '--json'])
    assert res.exit_code == 0, res.output
    assert c.rpc_calls[0][1]['p_components'] == ['nirspec', 'nircam/cosmos']
    assert c.rpc_calls[0][1]['p_inline'] is False
    payload = json.loads(res.output[res.output.index('{'):])
    assert payload['state'] == 'frozen'


def test_cli_freeze_failed_exits_1(monkeypatch):
    c = _FakeClient(rpc_data={'dr_enqueue_freeze': {**FROZEN, 'state': 'failed', 'error': 'boom',
                                                    'mode': 'inline'}})
    _patch(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'freeze', '--tag', 'DR-t', '--nirspec', '--inline'])
    assert res.exit_code == 1
    assert 'error: boom' in res.output


def test_cli_freeze_refused_is_one_line(monkeypatch):
    c = _FakeClient(rpc_data={'dr_enqueue_freeze': Exception('a deploy is in progress (live deploy lease)')})
    _patch(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'freeze', '--tag', 'DR-t', '--nirspec'])
    assert res.exit_code == 1
    assert 'Could not start the freeze: a deploy is in progress' in res.output


def test_cli_freeze_needs_a_component(monkeypatch):
    _patch(monkeypatch, _FakeClient())
    res = CliRunner().invoke(main_cli.cli, ['dr', 'freeze', '--tag', 'DR-t'])
    assert res.exit_code == 2
    assert 'nothing to freeze' in res.output


def test_cli_purge(monkeypatch):
    c = _FakeClient(rpc_data={'dr_purge_release': {'tag': 'DR-t', 'state_before': 'frozen',
                                                   'deleted': {'spectra': 163, 'objects': 101, 'exports': 0}}})
    _patch(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t', '--yes'])
    assert res.exit_code == 0, res.output
    assert 'Purged DR-t (was frozen): 163 spectra · 101 objects' in res.output
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t'], input='n\n')
    assert res.exit_code == 1 and c.rpc_calls.count(('dr_purge_release', {'p_tag': 'DR-t'})) == 1


def test_cli_help_lists_verbs():
    res = CliRunner().invoke(main_cli.cli, ['dr', '--help'])
    assert res.exit_code == 0
    assert 'freeze' in res.output and 'purge' in res.output and 'status' in res.output


def test_cli_freeze_json_stdout_is_pure_json(monkeypatch):
    # Codex on #599: progress lines go to stderr so `--json | jq` works
    c = _FakeClient(rpc_data={'dr_enqueue_freeze': {**FROZEN, 'mode': 'inline'}})
    _patch(monkeypatch, c)
    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:  # click >= 8.2 always separates the streams
        runner = CliRunner()
    res = runner.invoke(main_cli.cli, ['dr', 'freeze', '--tag', 'DR-t', '--nirspec', '--local', '--json'])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)['state'] == 'frozen'
    assert 'Freeze of DR-t' in res.stderr
