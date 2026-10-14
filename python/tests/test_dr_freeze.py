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
          'counts': {'spectra': 163, 'objects': 101, 'line_fits': 19, 'photometry': 0, 'manifest': 489},
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
    assert 'frozen: 163 spectra · 101 objects · 19 line_fits · 0 photometry · 489 manifest files' in text
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


def _purge_env(monkeypatch, c, *, osn=True):
    import campfire.deploy.tiles as tiles
    swept = []
    _patch(monkeypatch, c)
    monkeypatch.setattr(main_cli, '_dr_osn_s3',
                        lambda config, **kw: ('s3', 'campfire-data') if osn else (None, 'osn: no credentials'))

    def fake_delete(client, bucket, prefix):
        swept.append((client, bucket, prefix, len(c.rpc_calls)))
        return 4
    monkeypatch.setattr(tiles, 'delete_r2_prefix', fake_delete)
    return swept


def test_cli_purge(monkeypatch):
    c = _FakeClient(rpc_data={'dr_purge_release': {'tag': 'DR-t', 'state_before': 'frozen',
                                                   'deleted': {'spectra': 163, 'objects': 101, 'exports': 0}}})
    swept = _purge_env(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t', '--yes'])
    assert res.exit_code == 0, res.output
    assert 'Purged DR-t (was frozen): 163 spectra · 101 objects' in res.output
    # the export files go too, after the rows (Codex on #600)
    assert swept == [('s3', 'campfire-data', 'dr/DR-t/exports/', 1)]
    assert 'deleted 4 export file(s) under dr/DR-t/exports/' in res.output
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t'], input='n\n')
    assert res.exit_code == 1 and c.rpc_calls.count(('dr_purge_release', {'p_tag': 'DR-t'})) == 1
    assert len(swept) == 1


def test_cli_purge_without_osn_credentials_refuses_before_the_rows(monkeypatch):
    c = _FakeClient(rpc_data={'dr_purge_release': {'tag': 'DR-t', 'state_before': 'frozen', 'deleted': {}}})
    swept = _purge_env(monkeypatch, c, osn=False)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t', '--yes'])
    assert res.exit_code == 2 and 'needs OSN credentials' in res.output and '--keep-exports' in res.output
    assert c.rpc_calls == [] and swept == []
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t', '--yes', '--keep-exports'])
    assert res.exit_code == 0, res.output
    assert c.rpc_calls == [('dr_purge_release', {'p_tag': 'DR-t'})] and swept == []


def test_cli_purge_refused_keeps_the_export_files(monkeypatch):
    def refuse(params):
        raise RuntimeError('release DR-t is published; withdraw it, it is never purged')
    c = _FakeClient(rpc_data={'dr_purge_release': refuse})
    swept = _purge_env(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t', '--yes'])
    assert res.exit_code == 1 and 'Could not purge' in res.output
    assert swept == []


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


def test_status_shows_a_freeze_in_progress_and_manifest_by_type():
    # Fable review of #599: a freeze that never lands must be visible as such
    row = {'tag': 'DR-t', 'state': 'freezing', 'freeze_started_at': '2026-10-13T20:00:00+00:00', 'counts': {}}
    assert 'freezing since 2026-10-13 20:00 UTC' in format_release(row)
    report = {'generated_at': None, 'spectra_by_version': {}, 'leases': [], 'releases': [row]}
    assert any('DR-t: freezing since' in l for l in format_releases(report))
    frozen = {**FROZEN, 'counts': {**FROZEN['counts'], 'manifest_by_type': {'nirspec_spec': 163, 'zfit': 160}}}
    assert 'manifest: 163 nirspec_spec · 160 zfit' in format_release(frozen)


# ---------------------------------------------------------------------------
# export files for readers (P4b); the builder is tested in test_dr_export.py
# ---------------------------------------------------------------------------

from campfire.dr import fetch_files  # noqa: E402


class _HttpResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class _FakeSession:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, path, timeout=None):
        self.calls.append(('GET', path, None))
        return self.answers.pop(0)


def test_fetch_files_404_is_a_clear_error():
    s = _FakeSession([_HttpResp(404, {'error': 'not_found'})])
    with pytest.raises(RuntimeError, match='not published'):
        fetch_files(s, 'DR-t')
    s = _FakeSession([_HttpResp(200, {'tag': 'DR-t', 'files': []})])
    assert fetch_files(s, 'DR-t')['tag'] == 'DR-t'
    # relative to the session's base url, which already ends in /api/v1
    assert s.calls[-1][1] == '/dr/DR-t/files'


def test_cli_export_refuses_local():
    res = CliRunner().invoke(main_cli.cli, ['dr', 'export', '--tag', 'DR-t', '--local'])
    assert res.exit_code == 2 and 'shared one' in res.output


def test_cli_local_purge_never_touches_the_shared_bucket(monkeypatch):
    # Codex on #600: --local redirects Supabase only; the bucket is production's
    c = _FakeClient(rpc_data={'dr_purge_release': {'tag': 'DR-t', 'state_before': 'frozen', 'deleted': {}}})
    swept = _purge_env(monkeypatch, c)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', 'DR-t', '--yes', '--local'])
    assert res.exit_code == 0, res.output
    assert c.rpc_calls == [('dr_purge_release', {'p_tag': 'DR-t'})]
    assert swept == [] and 'left alone' in res.output
