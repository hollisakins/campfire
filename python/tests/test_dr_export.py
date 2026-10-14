"""The local export builder (campfire.dr_export, P4b)."""
import gzip
import json

from campfire import dr_export as X
from tests.fake_s3 import FakeS3Client
from tests.fakes import FakePostgrest

TAG = 'DR-t'
BUCKET = 'campfire-data'


def _entry(stream, scope, state='pending'):
    return {'tag': TAG, 'stream': stream, 'partition': scope, 'state': state,
            'storage_key': f'dr/{TAG}/exports/{stream}/{scope}.jsonl.gz',
            'sha256': None, 'size_bytes': None, 'rows': None, 'built_at': None, 'error': None}


def _db(ledger, *, rows=None, busy=(), fail_rows=(), lose_claim=(), lose_ready_answer=()):
    """A fake dr_export_* API over a mutable ledger, as the SQL behaves."""
    rows = rows or {}
    claims = {}
    n = {'claim': 0}

    def find(p):
        return next(r for r in ledger if r['stream'] == p['p_stream'] and r['partition'] == p['p_partition'])

    def mark(p):
        key = f"{p['p_stream']}/{p['p_partition']}"
        r = find(p)
        if p['p_state'] == 'building':
            if key in busy:
                raise RuntimeError(f'export file {key} is being built by another run')
            n['claim'] += 1
            claim = f'{n["claim"]:08x}-0000-4000-8000-000000000000'
            claims[key] = claim
            r['state'] = 'building'
            return {'claim': claim, 'base_key': f'dr/{TAG}/exports/{key}.jsonl.gz', 'state': 'exporting'}
        if key in lose_claim:            # another run took it over and finished it
            r['state'] = 'ready'
            if p['p_state'] == 'failed':
                return {'stale_claim': True, 'state': 'exported'}
            raise RuntimeError(f'export file {key} is no longer claimed by this run')
        if p['p_state'] == 'ready':
            r.update(state='ready', sha256=p['p_sha256'], rows=p['p_rows'], size_bytes=p['p_size_bytes'],
                     storage_key=X.content_export_key(f'dr/{TAG}/exports/{key}.jsonl.gz',
                                                      p['p_sha256'], p['p_claim']))
            if key in lose_ready_answer:
                raise RuntimeError('fetch failed')     # committed, answer lost
            return {'state': 'exporting'}
        if p['p_state'] == 'failed':
            if r['state'] == 'ready':
                return {'stale_claim': True, 'state': 'exported'}
            r.update(state='failed', error=p['p_error'])
            return {'state': 'exporting'}

    def export_rows(p):
        key = f"{p['p_stream']}/{p['p_partition']}"
        if key in fail_rows:
            raise RuntimeError('boom')
        data = rows.get(key, [])
        start = 0 if p['p_after'] is None else next(i for i, r in enumerate(data) if r['next_cursor'] == p['p_after']) + 1
        return data[start:start + p['p_limit']]

    def status(p):
        remaining = sum(1 for r in ledger if r['state'] != 'ready')
        return {'state': 'exported' if remaining == 0 else 'exporting', 'remaining': remaining}

    return FakePostgrest(tables={'dr_exports': ledger}, rpc_handlers={
        'dr_export_begin': {'state': 'exporting'}, 'dr_export_mark': mark,
        'dr_export_rows': export_rows, 'dr_export_status': status})


def _s3(**objects):
    s3 = FakeS3Client()
    s3.create_bucket(Bucket=BUCKET)
    for k, v in objects.items():
        s3.seed(BUCKET, k, v)
    return s3


ROWS = {'objects/pub': [{'next_cursor': 'A', 'row_json': {'object_id': 'A', 'z': 1.5}},
                        {'next_cursor': 'B', 'row_json': {'object_id': 'B', 'name': 'ü'}}],
        'manifest/nircam/f': [{'next_cursor': 'k', 'row_json': {'storage_key': 'k'}}]}


def test_content_key_matches_the_sql_format():
    sha = 'sha256:' + '0123456789abcdef' * 4
    assert (X.content_export_key('dr/T/exports/spectra/pub.jsonl.gz', sha, 'a1b2c3d4-e5f6-4711-8899-aabbccddeeff')
            == 'dr/T/exports/spectra/pub.0123456789abcdef.a1b2c3d4.jsonl.gz')


def test_builds_every_open_file_under_its_build_key_and_marks_it_ready():
    ledger = [_entry('objects', 'pub'), _entry('spectra', 'pub', 'ready'), _entry('manifest', 'nircam/f', 'failed')]
    ledger[1]['storage_key'] = 'dr/DR-t/exports/spectra/pub.aaaaaaaaaaaaaaaa.11111111.jsonl.gz'
    db, s3 = _db(ledger, rows=ROWS), _s3(**{ledger[1]['storage_key']: b'x'})
    rep = X.run_export(db, s3, BUCKET, TAG, log=lambda *_: None)
    assert sorted((b['stream'], b['scope'], b['rows']) for b in rep.built) == [('manifest', 'nircam/f', 1), ('objects', 'pub', 2)]
    assert rep.failed == [] and rep.remaining == 0 and rep.state == 'exported'
    obj = ledger[0]
    assert obj['state'] == 'ready' and s3.exists(BUCKET, obj['storage_key'])
    lines = gzip.decompress(s3.body(BUCKET, obj['storage_key'])).decode().splitlines()
    assert [json.loads(l) for l in lines] == [r['row_json'] for r in ROWS['objects/pub']]
    # the already-ready file was neither rebuilt nor touched
    assert not [c for c in db.rpcs if c.name == 'dr_export_rows' and c.params['p_stream'] == 'spectra']


def test_the_same_rows_give_the_same_bytes(tmp_path):
    db = _db([_entry('objects', 'pub')], rows=ROWS)
    a = X.build_file(db, TAG, 'objects', 'pub', str(tmp_path / 'a'))
    b = X.build_file(db, TAG, 'objects', 'pub', str(tmp_path / 'b'))
    assert a == b and a[0] == 2


def test_pages_with_the_cursor(tmp_path):
    data = [{'next_cursor': str(i), 'row_json': {'i': i}} for i in range(5)]
    db = _db([_entry('spectra', 'p')], rows={'spectra/p': data})
    rows, _sha, _size = X.build_file(db, TAG, 'spectra', 'p', str(tmp_path / 'f'), page=2)
    assert rows == 5
    assert [c.params['p_after'] for c in db.rpcs if c.name == 'dr_export_rows'] == [None, '1', '3']


def test_a_failing_file_is_marked_failed_and_the_run_goes_on_without_a_sweep():
    ledger = [_entry('objects', 'pub'), _entry('manifest', 'nircam/f')]
    stray = 'dr/DR-t/exports/objects/pub.dddddddddddddddd.22222222.jsonl.gz'
    db, s3 = _db(ledger, rows=ROWS, fail_rows=('objects/pub',)), _s3(**{stray: b'old'})
    rep = X.run_export(db, s3, BUCKET, TAG, log=lambda *_: None)
    assert rep.failed == [{'stream': 'objects', 'scope': 'pub', 'error': 'boom'}]
    assert [b['stream'] for b in rep.built] == ['manifest'] and rep.remaining == 1
    assert ledger[0]['state'] == 'failed' and s3.exists(BUCKET, stray) and rep.swept == 0


def test_a_file_another_run_holds_is_skipped():
    ledger = [_entry('objects', 'pub')]
    rep = X.run_export(_db(ledger, rows=ROWS, busy=('objects/pub',)), _s3(), BUCKET, TAG, log=lambda *_: None)
    assert rep.skipped and 'another run' in rep.skipped[0]['reason'] and rep.remaining == 1


def test_a_lost_claim_is_skipped_and_its_upload_deleted():
    ledger = [_entry('objects', 'pub')]
    s3 = _s3()
    rep = X.run_export(_db(ledger, rows=ROWS, lose_claim=('objects/pub',)), s3, BUCKET, TAG, log=lambda *_: None)
    assert rep.failed == [] and 'claim lost' in rep.skipped[0]['reason'] and rep.remaining == 0
    assert not [k for k in s3.keys(BUCKET) if k.startswith('dr/')]


def test_a_lost_answer_to_a_committed_ready_mark_keeps_the_file():
    ledger = [_entry('objects', 'pub')]
    s3 = _s3()
    X.run_export(_db(ledger, rows=ROWS, lose_ready_answer=('objects/pub',)), s3, BUCKET, TAG, log=lambda *_: None)
    assert ledger[0]['state'] == 'ready' and s3.exists(BUCKET, ledger[0]['storage_key'])


def test_a_complete_run_deletes_superseded_files_only():
    ledger = [_entry('objects', 'pub')]
    old = 'dr/DR-t/exports/objects/pub.aaaaaaaaaaaaaaaa.11111111.jsonl.gz'
    other = 'dr/DR-u/exports/objects/pub.bbbbbbbbbbbbbbbb.22222222.jsonl.gz'
    s3 = _s3(**{old: b'old', other: b'other'})
    rep = X.run_export(_db(ledger, rows=ROWS), s3, BUCKET, TAG, log=lambda *_: None)
    assert rep.swept == 1 and not s3.exists(BUCKET, old)
    assert s3.exists(BUCKET, other) and s3.exists(BUCKET, ledger[0]['storage_key'])


def test_the_sweep_waits_while_any_file_is_being_built():
    # an upload whose ready mark is pending has its row in `building`
    ledger = [_entry('objects', 'pub', 'ready'), _entry('spectra', 'pub', 'building')]
    ledger[0]['storage_key'] = 'dr/DR-t/exports/objects/pub.aaaaaaaaaaaaaaaa.11111111.jsonl.gz'
    inflight = 'dr/DR-t/exports/spectra/pub.cccccccccccccccc.33333333.jsonl.gz'
    s3 = _s3(**{ledger[0]['storage_key']: b'x', inflight: b'y'})
    assert X.sweep_superseded(_db(ledger), s3, BUCKET, TAG) == 0
    assert s3.exists(BUCKET, inflight)


def test_a_failure_repaired_by_another_run_leaves_nothing_remaining(monkeypatch):
    # another exporter rebuilds the failed file just before this run reconciles
    ledger = [_entry('objects', 'pub')]
    db, s3 = _db(ledger, rows=ROWS, fail_rows=('objects/pub',)), _s3()
    real, calls = X.fetch_ledger, []

    def fetch(client, tag):
        calls.append(1)
        if len(calls) == 2:
            ledger[0]['state'] = 'ready'
        return real(client, tag)
    monkeypatch.setattr(X, 'fetch_ledger', fetch)
    rep = X.run_export(db, s3, BUCKET, TAG, log=lambda *_: None)
    assert rep.failed == [] and rep.remaining == 0 and rep.state == 'exported'
    assert 'built by another run' in rep.skipped[0]['reason']
