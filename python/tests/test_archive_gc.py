"""`campfire archive gc` (P4e, D-P4-7): reference-based candidates through
`archive_collectable`, the report, delete-then-collect, and the CLI wiring."""
from datetime import datetime, timezone

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire.archive import (
    CollectError, collect, delete_archive_objects, format_gc_report, gc_candidates, gc_report,
)

from .fakes import FakePostgrest

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def _row(i, *, product_type='nircam_mosaic', field='cosmos', stored=None, size=1000, backend='osn'):
    return {
        'id': i, 'backend': backend, 'bucket': 'data', 'product_type': product_type,
        'storage_key': f"archive/{i:064x}/products/nircam/{field}/{field}_f444w_{i}.fits.gz",
        'content_hash': f"sha256:{i:064x}", 'size_bytes': size, 'stored_size_bytes': stored,
        'field': field, 'filter': 'f444w', 'observation': None,
        'archived_from_key': f"data/products/nircam/{field}/{field}_f444w.fits.gz",
        'archived_at': f"2026-10-{(i % 20) + 1:02d}T00:00:00+00:00",
    }


def _client(rows, *, admin=True, collected=None, begin_refuses=None):
    """``archive_collectable`` pages *rows* on id; ``dr_archive_collect_begin`` /
    ``dr_archive_collect_finish`` record ``('begin'|'finish', keys, tag)`` in
    *collected*; *begin_refuses* is a key the claim refuses (still referenced)."""
    collected = collected if collected is not None else []

    def collectable(params):
        after, limit = int(params['p_after_id']), int(params['p_limit'])
        return sorted((r for r in rows if r['id'] > after), key=lambda r: r['id'])[:limit]

    claims = {}

    def begin(params):
        if begin_refuses in params['p_keys']:
            raise RuntimeError(f"dr_archive_collect_begin: {begin_refuses} is still referenced by release(s) DR-o")
        collected.append(('begin', list(params['p_keys']), params.get('p_tag')))
        for k in params['p_keys']:
            claims[k] = params['p_claim']
        return {'claim': params['p_claim'], 'claimed': len(params['p_keys']), 'already_collecting': 0, 'bytes': 0}

    def verify(params):
        # the keys this run still owns (a taken-over key carries another claim)
        return [k for k in params['p_keys'] if claims.get(k) == params['p_claim']]

    def finish(params):
        assert params['p_claim'] and all(claims.get(k) == params['p_claim'] for k in params['p_keys'])
        collected.append(('finish', list(params['p_keys']), params.get('p_tag')))
        return {'collected': len(params['p_keys']), 'not_collecting': 0, 'bytes': len(params['p_keys'])}

    client = FakePostgrest(rpc_handlers={'is_admin': admin, 'archive_collectable': collectable,
                                         'dr_archive_collect_begin': begin, 'dr_archive_collect_verify': verify,
                                         'dr_archive_collect_finish': finish})
    client.claims = claims
    return client


class FakeS3:
    def __init__(self, objects, *, errors=None, drop_first=False):
        self.objects, self.errors, self.calls = set(objects), errors or {}, []
        self.drop_first = drop_first

    def delete_objects(self, Bucket, Delete):
        keys = [o['Key'] for o in Delete['Objects']]
        self.calls.append((Bucket, keys))
        if self.drop_first:                   # the delete ran, its answer was lost (#666)
            self.drop_first = False
            for k in keys:
                self.objects.discard(k)
            raise ConnectionResetError(54, 'Connection reset by peer')
        errs = []
        for k in keys:
            if k in self.errors:
                errs.append({'Key': k, 'Code': 'AccessDenied', 'Message': self.errors[k]})
            else:
                self.objects.discard(k)          # a missing key deletes cleanly (S3 semantics)
        return {'Errors': errs} if errs else {}


def test_gc_candidates_pages_archive_collectable_on_id():
    rows = [_row(i) for i in range(1, 6)]
    client = _client(rows)
    got = gc_candidates(client, page=2)
    assert [r['id'] for r in got] == [1, 2, 3, 4, 5]
    assert [c.params for c in client.rpcs if c.name == 'archive_collectable'] == [
        {'p_after_id': 0, 'p_limit': 2}, {'p_after_id': 2, 'p_limit': 2}, {'p_after_id': 4, 'p_limit': 2}]


def test_gc_report_totals_bytes_at_rest_by_product_type():
    rows = [_row(1), _row(6, stored=400, field='egs', product_type='nirspec_spec')]
    rep = gc_report(rows, now=NOW)
    assert rep['dry_run'] is True and rep['candidates'] == 2 and rep['bytes_at_rest'] == 1400
    assert rep['by_product_type'] == {'nircam_mosaic': {'objects': 1, 'bytes': 1000},
                                      'nirspec_spec': {'objects': 1, 'bytes': 400}}
    text = format_gc_report(rep)
    assert 'dry run' in text and 'no data release references' in text
    assert 'Candidates: 2 object(s)' in text and 'nircam_mosaic' in text and 'nirspec_spec' in text
    assert 'dry run' not in format_gc_report(gc_report(rows, now=NOW, dry_run=False))


def test_gc_report_limit_caps_rows_not_totals():
    rows = [_row(1), _row(6, stored=400, field='egs')]
    rep = gc_report(rows, now=NOW, max_rows=1)
    assert rep['candidates'] == 2 and rep['bytes_at_rest'] == 1400
    assert rep['rows_shown'] == 1 and len(rep['rows']) == 1
    text = format_gc_report(rep, max_rows=1)
    assert 'Candidates: 2 object(s)' in text and '… 1 more (totals above include them' in text
    full = gc_report(rows, now=NOW)
    assert full['rows_shown'] == 2 and '… ' not in format_gc_report(full)


def test_delete_batches_and_refuses_non_archive_keys():
    keys = [_row(i)['storage_key'] for i in range(1, 1502)]
    s3 = FakeS3(keys)
    assert delete_archive_objects(s3, 'b', keys) == 1501
    assert [len(k) for _, k in s3.calls] == [1000, 501] and not s3.objects
    with pytest.raises(CollectError, match='non-archive key'):
        delete_archive_objects(FakeS3([]), 'b', ['data/products/nirspec/x_spec.fits'])


def test_delete_missing_key_succeeds_and_bucket_errors_raise():
    s3 = FakeS3([])                                    # nothing there: a re-run after a crash
    assert delete_archive_objects(s3, 'b', [_row(1)['storage_key']]) == 1
    bad = _row(2)['storage_key']
    with pytest.raises(CollectError, match='refused to delete'):
        delete_archive_objects(FakeS3([bad], errors={bad: 'nope'}), 'b', [bad])


def test_collect_claims_then_deletes_then_finishes_in_chunks_with_the_tag():
    rows = [_row(i) for i in range(1, 1202)]
    marked = []
    client = _client(rows, collected=marked)
    s3 = FakeS3([r['storage_key'] for r in rows])
    out = collect(client, s3, 'b', rows, tag='DR-x')
    assert out['claimed'] == 1201 and out['deleted'] == 1201 and out['collected'] == 1201
    # every claim precedes the first delete; every finish follows the last one
    assert [(p, len(k)) for p, k, _ in marked] == [('begin', 1000), ('begin', 201), ('finish', 1000), ('finish', 201)]
    assert {t for _, _, t in marked} == {'DR-x'} and not s3.objects
    # rpc order: every claim first, then per delete batch a verify and a finish, all with one token
    assert [c.name for c in client.rpcs] == (['dr_archive_collect_begin'] * 2
                                             + ['dr_archive_collect_verify', 'dr_archive_collect_finish'] * 2)
    assert len({c.params['p_claim'] for c in client.rpcs}) == 1
    assert out['lost'] == 0
    assert collect(client, s3, 'b', [], tag=None) == {'claimed': 0, 'deleted': 0, 'collected': 0, 'lost': 0, 'bytes': 0}


def test_collect_resends_a_delete_whose_answer_was_lost_through_the_verify_gate(monkeypatch):
    naps = []
    monkeypatch.setattr('campfire.transient._sleep', naps.append)
    rows = [_row(i) for i in range(1, 6)]
    marked = []
    client = _client(rows, collected=marked)
    s3 = FakeS3([r['storage_key'] for r in rows], drop_first=True)
    logs = []
    out = collect(client, s3, 'b', rows, tag='DR-x', log=logs.append)
    assert out['deleted'] == 5 and out['collected'] == 5 and out['lost'] == 0
    assert len(s3.calls) == 2 and not s3.objects and naps == [1.0]
    # the re-sent delete went through a fresh verify, not the stale key list
    assert [c.name for c in client.rpcs] == ['dr_archive_collect_begin', 'dr_archive_collect_verify',
                                             'dr_archive_collect_verify', 'dr_archive_collect_finish']
    assert [p for p, _, _ in marked] == ['begin', 'finish'] and any('retrying the bucket delete' in m for m in logs)


def test_collect_deletes_nothing_it_lost_while_a_delete_was_being_retried(monkeypatch):
    """The claim expired during the dropped delete and another run took the keys
    over (Codex on the follow-ups PR): the retry re-verifies and finds nothing."""
    monkeypatch.setattr('campfire.transient._sleep', lambda s: None)
    rows = [_row(i) for i in range(1, 4)]
    marked = []
    client = _client(rows, collected=marked)
    s3 = FakeS3([r['storage_key'] for r in rows])
    first = {'done': False}
    real_delete = s3.delete_objects

    def delete_then_takeover(Bucket, Delete):
        if not first['done']:
            first['done'] = True
            for k in client.claims:                  # another collector took every key over
                client.claims[k] = 'other-run'
            raise ConnectionResetError(54, 'reset')
        return real_delete(Bucket, Delete)
    s3.delete_objects = delete_then_takeover
    out = collect(client, s3, 'b', rows, tag='DR-x')
    assert out['deleted'] == 0 and out['collected'] == 0 and out['lost'] == 3
    assert s3.objects == {r['storage_key'] for r in rows}        # nothing deleted on the retry
    assert [p for p, _, _ in marked] == ['begin']                  # and nothing finished


def test_collect_gives_up_a_delete_that_keeps_dropping(monkeypatch):
    monkeypatch.setattr('campfire.transient._sleep', lambda s: None)
    rows = [_row(i) for i in range(1, 3)]
    client = _client(rows)
    s3 = FakeS3([r['storage_key'] for r in rows])

    def dead(Bucket, Delete):
        raise ConnectionResetError(54, 'reset')
    s3.delete_objects = dead
    with pytest.raises(ConnectionResetError):
        collect(client, s3, 'b', rows, tag='DR-x')
    assert [c.name for c in client.rpcs].count('dr_archive_collect_verify') == 3


def test_collect_verifies_again_after_a_stall(monkeypatch):
    import campfire.archive as mod
    rows = [_row(1)]
    client = _client(rows)
    s3 = FakeS3([rows[0]['storage_key']])
    clock = iter([0.0, 100.0, 200.0, 201.0])             # the first verify "took" 100 s: not trusted
    monkeypatch.setattr(mod.time, 'time', lambda: next(clock))
    out = collect(client, s3, 'b', rows)
    assert out['deleted'] == 1
    assert [c.name for c in client.rpcs].count('dr_archive_collect_verify') == 2


def test_collect_deletes_only_what_it_still_owns_after_a_takeover():
    rows = [_row(1), _row(2)]
    marked = []
    client = _client(rows, collected=marked)
    s3 = FakeS3([r['storage_key'] for r in rows])
    taken = rows[1]['storage_key']
    # between the claim and the delete another run took row 2 over (a stalled run's claim is transferable)
    real_begin = client.rpc_handlers['dr_archive_collect_begin']

    def begin_then_takeover(params):
        res = real_begin(params)
        client.claims[taken] = 'someone-else'
        return res
    client.rpc_handlers['dr_archive_collect_begin'] = begin_then_takeover
    out = collect(client, s3, 'b', rows, tag='DR-x')
    assert out['deleted'] == 1 and out['collected'] == 1 and out['lost'] == 1
    assert taken in s3.objects and rows[0]['storage_key'] not in s3.objects
    assert marked[-1] == ('finish', [rows[0]['storage_key']], 'DR-x')


def test_collect_refused_claim_deletes_nothing():
    rows = [_row(1), _row(2)]
    marked = []
    s3 = FakeS3([r['storage_key'] for r in rows])
    with pytest.raises(RuntimeError, match='still referenced'):
        collect(_client(rows, collected=marked, begin_refuses=rows[1]['storage_key']), s3, 'b', rows)
    assert marked == [] and len(s3.objects) == 2


def test_collect_stops_before_finishing_when_a_delete_fails_or_a_row_is_foreign():
    rows = [_row(1), _row(2)]
    marked = []
    bad = rows[1]['storage_key']
    with pytest.raises(CollectError, match='refused to delete'):
        collect(_client(rows, collected=marked), FakeS3([bad], errors={bad: 'nope'}), 'b', rows)
    assert [p for p, _, _ in marked] == ['begin']            # claimed, not finished: the next run resumes
    marked.clear()
    with pytest.raises(CollectError, match='not on OSN'):
        collect(_client(rows, collected=marked), FakeS3([]), 'b', [_row(3, backend='r2')])
    assert marked == []


def _wire(monkeypatch, rows, *, admin=True, marked=None, s3=None):
    import campfire.deploy.config as cfg
    import campfire.deploy.supabase as sb
    client = _client(rows, admin=admin, collected=marked)
    monkeypatch.setattr(cfg, 'load_config', lambda *_a, **_k: {})
    monkeypatch.setattr(sb, 'get_supabase_client', lambda _c: client)
    monkeypatch.setattr(main_cli, '_dr_osn_s3', lambda config, **kw: (s3, 'bucket') if s3 is not None else (None, 'no creds'))
    return client


def test_cli_gc_dry_run_renders_and_touches_nothing(monkeypatch):
    rows = [_row(1)]
    _wire(monkeypatch, rows)
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--dry-run'])
    assert res.exit_code == 0, res.output
    assert 'Archive gc (dry run)' in res.output and 'Candidates: 1 object(s)' in res.output
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--dry-run', '--json'])
    assert res.exit_code == 0 and '"dry_run": true' in res.output and '"candidates": 1' in res.output


def test_cli_gc_limit_keeps_full_totals(monkeypatch):
    rows = [_row(1), _row(6, stored=400, field='egs')]
    _wire(monkeypatch, rows)
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--dry-run', '--limit', '1'])
    assert res.exit_code == 0, res.output
    assert 'Candidates: 2 object(s)' in res.output and '1.4 KB at rest' in res.output
    assert res.output.count('archive/') == 1 and '… 1 more' in res.output


def test_cli_gc_collects_after_confirmation(monkeypatch):
    rows = [_row(1), _row(2)]
    marked = []
    s3 = FakeS3([r['storage_key'] for r in rows])
    _wire(monkeypatch, rows, marked=marked, s3=s3)
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc'], input='n\n')
    assert res.exit_code == 1 and marked == [] and len(s3.objects) == 2        # declined
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--yes'])
    assert res.exit_code == 0, res.output
    assert 'deleted 2 archive object(s)' in res.output and 'removed 2 registry row(s)' in res.output
    assert not s3.objects and [t for _, _, t in marked] == [None, None]


def test_cli_gc_needs_osn_credentials_to_collect_and_local_only_lists(monkeypatch):
    rows = [_row(1)]
    _wire(monkeypatch, rows, s3=None)
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--yes'])
    assert res.exit_code == 2 and 'needs OSN credentials' in res.output
    marked = []
    _wire(monkeypatch, rows, marked=marked, s3=FakeS3([rows[0]['storage_key']]))
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--yes', '--local'])
    assert res.exit_code == 0, res.output
    assert 'listing only' in res.output and marked == []


def test_cli_gc_refuses_non_admin(monkeypatch):
    _wire(monkeypatch, [_row(1)], admin=False)
    res = CliRunner().invoke(main_cli.cli, ['archive', 'gc', '--dry-run'])
    assert res.exit_code != 0
    assert 'admin' in res.output.lower()
