"""The data-release lifecycle surface: `dr --help`, the next-step hints and
`dr build` (freeze -> archive -> export, resumable)."""
from click.testing import CliRunner

import campfire.cli as main_cli
import campfire.deploy.dr_bytes as B
import campfire.dr as D
import campfire.dr_export as X

TAG = 'DR-t'


def test_help_lists_the_steps_in_order():
    res = CliRunner().invoke(main_cli.cli, ['dr', '--help'])
    assert res.exit_code == 0
    out = res.output
    for word in ('1. freeze', '2. archive', '3. export', '4. publish', 'campfire dr build'):
        assert word in out
    assert out.index('1. freeze') < out.index('2. archive') < out.index('3. export') < out.index('4. publish')
    assert 'build' in out and 'archive' in out


def test_next_step_hint_walks_the_lifecycle():
    h = D.next_step_hint
    assert 'dr freeze --tag DR-t' in h(TAG, None)
    assert 'freezing' in h(TAG, 'freezing')
    assert 'dr purge DR-t' in h(TAG, 'failed')
    assert 'dr archive --tag DR-t' in h(TAG, 'frozen', archive_open=10)
    assert '10 manifest file(s) still pending' in h(TAG, 'frozen', archive_open=10)
    # a partial byte pass with every export built says both (the rehearsal's shape)
    partial = h(TAG, 'copying', archive_open=357940, exports_total=133)
    assert '357,940 manifest file(s) still pending; its 133 export file(s) are built' in partial
    assert 'are built' not in h(TAG, 'copying', archive_open=3, exports_total=133, exports_remaining=1)
    assert '--retry-failed' in h(TAG, 'copying', archive_open=3, archive_failed=2)
    assert 'dr export --tag DR-t' in h(TAG, 'frozen')                       # archived, never exported
    assert 'dr export --tag DR-t' in h(TAG, 'exporting', exports_total=5, exports_remaining=1)
    assert 'dr publish DR-t' in h(TAG, 'ready', exports_total=5)
    assert 'published' in h(TAG, 'published')
    assert 'dr purge DR-t' in h(TAG, 'purging') and 'finishes' in h(TAG, 'purging')


class _Steps:
    """Records which lifecycle steps dr build ran."""

    def __init__(self, monkeypatch, *, release, archive_left=0, archive_failed=0, export_failed=()):
        self.order = []
        self.release = release
        monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: ({}, object()))
        monkeypatch.setattr(main_cli, '_dr_osn_s3', lambda config, **kw: ('s3', 'bucket'))
        monkeypatch.setattr(main_cli, '_dr_hint', lambda sb, tag, say=None: self.order.append('hint'))
        monkeypatch.setattr(D, 'fetch_release', lambda sb, tag: self.release)

        def enqueue(sb, tag, components, notes=None, inline=False):
            self.order.append(('freeze', tuple(components)))
            return {'tag': tag, 'state': 'frozen'}
        monkeypatch.setattr(D, 'enqueue_freeze', enqueue)
        monkeypatch.setattr(D, 'wait_for_freeze', lambda sb, tag, **kw: {'tag': tag, 'state': 'frozen'})

        def byte_pass(sb, s3, bucket, tag, **kw):
            self.order.append('archive')
            return B.PassReport(tag=tag)
        monkeypatch.setattr(B, 'run_byte_pass', byte_pass)
        monkeypatch.setattr(B, 'fetch_summary', lambda sb, tag: {'by_state': {
            'pending': {'count': archive_left}, 'failed': {'count': archive_failed}}})

        def export(sb, s3, bucket, tag, **kw):
            self.order.append('export')
            return X.ExportReport(tag=tag, state='ready', failed=list(export_failed))
        monkeypatch.setattr(X, 'run_export', export)


def _build(*args):
    return CliRunner().invoke(main_cli.cli, ['dr', 'build', '--tag', TAG, *args])


def test_build_runs_freeze_archive_export_in_order(monkeypatch):
    steps = _Steps(monkeypatch, release=None)
    res = _build('--nirspec')
    assert res.exit_code == 0, res.output
    assert steps.order == [('freeze', ('nirspec',)), 'archive', 'export', 'hint']
    assert '1/3 Freezing' in res.output and '2/3' in res.output and '3/3' in res.output


def test_build_resumes_a_frozen_release_without_refreezing(monkeypatch):
    steps = _Steps(monkeypatch, release={'tag': TAG, 'state': 'copying'})
    res = _build('--nirspec')
    assert res.exit_code == 0, res.output
    assert steps.order == ['archive', 'export', 'hint']
    assert 'already frozen; the component options are ignored' in res.output


def test_build_needs_components_for_an_unfrozen_tag(monkeypatch):
    steps = _Steps(monkeypatch, release=None)
    res = _build()
    assert res.exit_code == 2 and 'not frozen yet' in res.output and steps.order == []


def test_build_refuses_a_published_release(monkeypatch):
    steps = _Steps(monkeypatch, release={'tag': TAG, 'state': 'published'})
    res = _build()
    assert res.exit_code == 1 and 'never rebuilt' in res.output and steps.order == []


def test_build_stops_before_exporting_while_files_are_not_archived(monkeypatch):
    steps = _Steps(monkeypatch, release={'tag': TAG, 'state': 'copying'}, archive_failed=2)
    res = _build()
    assert res.exit_code == 1 and steps.order == ['archive', 'hint']


def test_build_exits_1_when_an_export_file_failed(monkeypatch):
    failed = [{'stream': 'objects', 'scope': 'p', 'error': 'boom'}]
    steps = _Steps(monkeypatch, release={'tag': TAG, 'state': 'frozen'}, export_failed=failed)
    res = _build()
    assert res.exit_code == 1 and steps.order == ['archive', 'export', 'hint']


def test_fetch_next_step_never_raises():
    class Broken:
        def table(self, name):
            raise RuntimeError('network down')
    assert D.fetch_next_step(Broken(), TAG) is None
