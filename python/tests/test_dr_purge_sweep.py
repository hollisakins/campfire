"""`campfire dr purge` sweeps the archive objects only the release references
(P4e, D-P4-6): plan -> delete bytes -> mark collected -> purge the rows."""
from click.testing import CliRunner

import campfire.cli as main_cli
import campfire.dr as D

from .fakes import FakePostgrest
from .test_archive_gc import FakeS3, _row

TAG = 'DR-r'


def _wire(monkeypatch, *, plan, s3, purge_log):
    marked = []

    def begin(params):
        marked.append(('begin', list(params['p_keys']), params.get('p_tag')))
        return {'claim': params['p_claim'], 'claimed': len(params['p_keys']), 'already_collecting': 0, 'bytes': 7}

    def verify(params):
        return list(params['p_keys'])

    def finish(params):
        marked.append(('finish', list(params['p_keys']), params.get('p_tag')))
        return {'collected': len(params['p_keys']), 'not_collecting': 0, 'bytes': 7}

    def paged_plan(params):
        rows = plan(params) if callable(plan) else plan
        after, limit = int(params['p_after_id']), int(params['p_limit'])
        return [r for r in rows if r['id'] > after][:limit]

    client = FakePostgrest(rpc_handlers={
        'dr_purge_archive_plan': paged_plan,
        'dr_archive_collect_begin': begin,
        'dr_archive_collect_verify': verify,
        'dr_archive_collect_finish': finish,
        'dr_purge_release': lambda p: purge_log.append(p['p_tag']) or {
            'tag': p['p_tag'], 'state_before': 'frozen', 'deleted': {'spectra': 3}},
    })
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: ({}, client))
    monkeypatch.setattr(main_cli, '_dr_osn_s3', lambda config, **kw: (s3, 'bucket') if s3 is not None else (None, 'no creds'))
    import campfire.deploy.tiles as tiles
    monkeypatch.setattr(tiles, 'delete_r2_prefix', lambda s3_, bucket, prefix: 0)
    return client, marked


def test_purge_sweeps_then_purges_in_order(monkeypatch):
    rows = [_row(1), _row(2)]
    s3 = FakeS3([r['storage_key'] for r in rows])
    purged = []
    client, marked = _wire(monkeypatch, plan=rows, s3=s3, purge_log=purged)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', TAG], input='y\n')
    assert res.exit_code == 0, res.output
    assert '2 archive object(s)' in res.output and 'only it references' in res.output
    keys = [r['storage_key'] for r in rows]
    assert not s3.objects and marked == [('begin', keys, TAG), ('finish', keys, TAG)] and purged == [TAG]
    names = [c.name for c in client.rpcs]
    assert (names.index('dr_purge_archive_plan') < names.index('dr_archive_collect_begin')
            < names.index('dr_archive_collect_finish') < names.index('dr_purge_release'))


def test_purge_declined_touches_nothing(monkeypatch):
    rows = [_row(1)]
    s3 = FakeS3([rows[0]['storage_key']])
    purged = []
    _, marked = _wire(monkeypatch, plan=rows, s3=s3, purge_log=purged)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', TAG], input='n\n')
    assert res.exit_code == 1 and len(s3.objects) == 1 and marked == [] and purged == []


def test_purge_stops_before_the_rows_when_a_delete_fails(monkeypatch):
    rows = [_row(1)]
    bad = rows[0]['storage_key']
    s3 = FakeS3([bad], errors={bad: 'nope'})
    purged = []
    _, marked = _wire(monkeypatch, plan=rows, s3=s3, purge_log=purged)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', TAG, '--yes'])
    assert res.exit_code == 1 and 'Archive sweep' in res.output and 'No frozen row was touched' in res.output
    assert [p for p, _, _ in marked] == ['begin'] and purged == []      # claimed; the next run finishes


def test_purge_keep_archive_skips_the_sweep(monkeypatch):
    rows = [_row(1)]
    s3 = FakeS3([rows[0]['storage_key']])
    purged = []
    client, marked = _wire(monkeypatch, plan=rows, s3=s3, purge_log=purged)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', TAG, '--yes', '--keep-archive'])
    assert res.exit_code == 0, res.output
    assert len(s3.objects) == 1 and marked == [] and purged == [TAG]
    assert 'dr_purge_archive_plan' not in [c.name for c in client.rpcs]


def test_purge_local_never_touches_the_bucket(monkeypatch):
    rows = [_row(1)]
    s3 = FakeS3([rows[0]['storage_key']])
    purged = []
    client, marked = _wire(monkeypatch, plan=rows, s3=s3, purge_log=purged)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', TAG, '--yes', '--local'])
    assert res.exit_code == 0, res.output
    assert 'left alone' in res.output and len(s3.objects) == 1 and marked == [] and purged == [TAG]


def test_purge_needs_credentials_before_anything_goes(monkeypatch):
    purged = []
    _, marked = _wire(monkeypatch, plan=[_row(1)], s3=None, purge_log=purged)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', TAG, '--yes'])
    assert res.exit_code == 2 and 'needs OSN credentials' in res.output and purged == []


def test_purge_plan_refusal_is_one_line(monkeypatch):
    def refuse(_p):
        raise RuntimeError('release DR-r is published; a published DR''s archive objects are never collected')
    purged = []
    _wire(monkeypatch, plan=refuse, s3=FakeS3([]), purge_log=purged)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'purge', TAG, '--yes'])
    assert res.exit_code == 1 and 'Could not plan the archive sweep' in res.output and purged == []


def test_purge_archive_plan_helper_pages_on_id():
    client = FakePostgrest(rpc_handlers={'dr_purge_archive_plan': None})
    assert D.purge_archive_plan(client, TAG) == []
    rows = [{'id': i, 'storage_key': f'archive/{i}'} for i in range(1, 6)]
    client = FakePostgrest(rpc_handlers={'dr_purge_archive_plan': lambda p: [r for r in rows if r['id'] > int(p['p_after_id'])][:int(p['p_limit'])]})
    assert D.purge_archive_plan(client, TAG, page=2) == rows
    assert [c.params['p_after_id'] for c in client.rpcs] == [0, 2, 4]


def test_purge_plan_refetches_a_page_whose_connection_dropped(monkeypatch):
    """The plan's keyset pages are reads: a dropped one is fetched again (#666)."""
    from campfire import dr as D
    from tests.fakes import FakePostgrest
    monkeypatch.setattr('campfire.transient._sleep', lambda s: None)
    rows = [{'id': i, 'storage_key': f'archive/{i:064x}/x', 'backend': 'osn'} for i in range(1, 4)]
    dropped = []

    def plan(params):
        if not dropped:
            dropped.append(1)
            raise ConnectionResetError(54, 'reset')
        after, limit = int(params['p_after_id']), int(params['p_limit'])
        return [r for r in rows if r['id'] > after][:limit]
    client = FakePostgrest(rpc_handlers={'dr_purge_archive_plan': plan})
    assert [r['id'] for r in D.purge_archive_plan(client, 'DR-x', page=2)] == [1, 2, 3]
    assert [c.params['p_after_id'] for c in client.rpcs] == [0, 0, 2]
