"""Retain-on-supersede on the direct (boto3) upload path (campfire.deploy.retain).

Mirrors web/lib/server/retain.test.ts: the plan against a fake registry and a
fake ``retain_required_hashes`` (which hashes a frozen release still needs,
P4e), the copy step against a fake S3 client with OSN's measured semantics
(HEAD 404, conditional copy 412), and the always-on gate.
"""
from pathlib import Path

import pytest

from campfire.deploy import retain as R
from campfire.deploy.r2 import UploadTask

SHA_A = 'sha256:' + 'a' * 64
SHA_B = 'sha256:' + 'b' * 64
LIVE_SPEC = 'data/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits'
LEGACY_SPEC = 'spectra/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits'
LIVE_EXPOSURE = 'data/products/nirspec/ember_egs_p1/jw07076020001_04101_00001_nrs1_15182.fits'
ARCHIVE_SPEC = f"archive/{'a' * 64}/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_15182_spec.fits"


def _row(**over):
    row = dict(id=1, storage_key=LIVE_SPEC, content_hash=SHA_A, size_bytes=1000,
               stored_size_bytes=None, content_type='application/fits',
               product_type='nirspec_spec', instrument='nirspec', observation='ember_egs_p1',
               spectrum_id='ember_egs_p1_prism_clear_15182', program_slug='ember',
               deployment_id=42, cfpipe_version='0.6.0', updated_at='2026-10-01T00:00:00Z')
    row.update(over)
    return row


class _Q:
    def __init__(self, db, table):
        self.db, self.table_name, self.keys = db, table, None

    def select(self, *_a, **_k):
        return self

    def in_(self, _col, keys):
        self.keys = keys
        return self

    def eq(self, *_a):
        return self

    def upsert(self, row, **opts):
        self.db.upserts.append((row, opts))
        return self

    def execute(self):
        if self.keys is not None:
            return type('R', (), {'data': [r for r in self.db.rows if r['storage_key'] in self.keys]})()
        return type('R', (), {'data': None})()


class _Rpc:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return type('R', (), {'data': self._data})()


class FakeDB:
    """``required`` = the hashes ``retain_required_hashes`` answers with (a frozen,
    unarchived release member); by default the live row's hash, so the plan tests
    exercise the retain path."""

    def __init__(self, rows, required=(SHA_A,), collecting=()):
        self.rows, self.upserts, self.rpcs = rows, [], []
        self.required = set(required)
        self.collecting = set(collecting)     # archive keys a collection has claimed
        self.s3 = None                        # set by a test to snapshot the bucket calls at registration
        self.calls_before_register = None

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, args):
        self.rpcs.append((name, args))
        if name == 'retain_required_hashes':
            return _Rpc(sorted(self.required & set(args['p_hashes'])))
        if name == 'archive_register_retained':
            if self.s3 is not None and self.calls_before_register is None:
                self.calls_before_register = list(self.s3.calls)
            if self.collecting and args['p_row']['storage_key'] in self.collecting:
                raise RuntimeError(f"archive object {args['p_row']['storage_key']} is being collected (status collecting); retry")
            self.upserts.append((args['p_row'], {'rpc': name}))
            return _Rpc({'inserted': True})
        return _Rpc('evt')


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {'ResponseMetadata': {'HTTPStatusCode': code}}


class FakeS3:
    def __init__(self, objects, copy412=False):
        self.objects, self.copy412, self.calls = dict(objects), copy412, []

    def head_object(self, Bucket, Key):
        self.calls.append(('head', Key))
        if Key not in self.objects:
            raise _ClientError(404)
        size, etag = self.objects[Key]
        return {'ContentLength': size, 'ETag': f'"{etag}"'}

    def copy_object(self, Bucket, Key, CopySource, **kw):
        self.calls.append(('copy', Key, CopySource['Key'], kw.get('CopySourceIfMatch')))
        if self.copy412:
            raise _ClientError(412)
        self.objects[Key] = self.objects[CopySource['Key']]
        return {}


def _task(key, old=None, new=None):
    return UploadTask(Path('/x'), key, 'application/fits', expected_old_hash=old, new_hash=new)


def test_plan_skips_unreferenced_rows_and_asks_only_about_retainable_hashes():
    # the live hash is not a frozen, unarchived release member: overwrite freely,
    # even when the client planned against a different hash (no drift verdict)
    db = FakeDB([_row()], required=())
    ds = R.plan_retain([_task(LIVE_SPEC, new=SHA_B), _task(LIVE_SPEC, old=SHA_B), _task(LIVE_EXPOSURE)], db)
    assert [d.action for d in ds] == ['skip-unreferenced', 'skip-unreferenced', 'skip-type']
    assert db.rpcs == [('retain_required_hashes', {'p_hashes': [SHA_A]})]


def test_plan_asks_nothing_when_no_row_is_retainable():
    db = FakeDB([_row()], required=(SHA_A,))
    ds = R.plan_retain([_task(LIVE_SPEC, new=SHA_A), _task(LIVE_EXPOSURE)], db)
    assert [d.action for d in ds] == ['skip-unchanged', 'skip-type'] and db.rpcs == []


def test_required_hashes_chunks_and_dedups():
    db = FakeDB([], required=('h1',))
    hashes = [f'h{i}' for i in range(1500)] + ['h1']
    assert R.required_hashes(db, hashes) == {'h1'}
    assert [len(a['p_hashes']) for _, a in db.rpcs] == [1000, 500]


def test_plan_retains_changed_final_by_canonical_key_with_old_hash_archive_key():
    [d] = R.plan_retain([_task(LEGACY_SPEC, new=SHA_B)], FakeDB([_row()]))
    assert d.action == 'retain' and d.archive_key == ARCHIVE_SPEC and d.row['storage_key'] == LIVE_SPEC


def test_plan_skips_new_type_unchanged_and_provisional():
    db = FakeDB([_row(),
                 _row(id=2, storage_key=LIVE_EXPOSURE, product_type='nirspec_spectrum_exposure'),
                 _row(id=3, storage_key=LIVE_SPEC.replace('15182', '99999'), content_hash='etag:dead')])
    ds = R.plan_retain([_task(LIVE_SPEC.replace('15182', '1')), _task(LIVE_EXPOSURE),
                        _task(LIVE_SPEC, new=SHA_A), _task(LIVE_SPEC.replace('15182', '99999')),
                        _task('garbage/key.bin')], db)
    assert [d.action for d in ds] == ['skip-new', 'skip-type', 'skip-unchanged', 'skip-provisional-hash', 'skip-new']


def test_plan_reports_drift_when_registry_moved_past_the_plan():
    # the registry's current hash is the frozen member, the client planned against an older one
    [d] = R.plan_retain([_task(LIVE_SPEC, old=SHA_A)], FakeDB([_row(content_hash=SHA_B)], required=(SHA_B,)))
    assert d.action == 'drift'


def _decision():
    [d] = R.plan_retain([_task(LIVE_SPEC, new=SHA_B)], FakeDB([_row()]))
    assert d.action == 'retain'
    return d


def test_execute_copies_with_etag_precondition_and_registers():
    s3, db = FakeS3({LIVE_SPEC: (1000, 'e1')}), FakeDB([])
    out = R.execute_retain(_decision(), s3=s3, bucket='b', client=db, actor='u1')
    assert (out.status, out.copied, out.archive_key) == ('archived', True, ARCHIVE_SPEC)
    assert ('copy', ARCHIVE_SPEC, LIVE_SPEC, 'e1') in s3.calls
    [(row, opts)] = db.upserts
    assert row['storage_key'] == ARCHIVE_SPEC and row['root'] == 'archive' and row['status'] == 'archived'
    assert row['archived_from_key'] == LIVE_SPEC and row['content_hash'] == SHA_A and row['uploaded_by'] == 'u1'
    assert opts == {'rpc': 'archive_register_retained'}
    assert [n for n, _ in db.rpcs] == ['archive_register_retained', 'log_deploy_event']
    assert db.rpcs[1][1]['p_action'] == 'archive'


def test_execute_skips_copy_when_archive_exists_but_registers():
    s3, db = FakeS3({LIVE_SPEC: (1000, 'e1'), ARCHIVE_SPEC: (1000, 'e1')}), FakeDB([])
    out = R.execute_retain(_decision(), s3=s3, bucket='b', client=db)
    assert out.status == 'archived' and not out.copied
    assert not [c for c in s3.calls if c[0] == 'copy'] and len(db.upserts) == 1


@pytest.mark.parametrize('s3', [
    FakeS3({LIVE_SPEC: (999, 'e1')}),              # size drift
    FakeS3({}),                                    # dangling row
    FakeS3({LIVE_SPEC: (1000, 'e1')}, copy412=True),  # precondition failed
])
def test_execute_reports_drift(s3):
    # a drift found before the registration (live HEAD) registers nothing; one
    # found by the copy (412) leaves the row registered first (P4e: the row goes
    # in before the copy), a dangling row `verify --cloud` reports, which is the
    # signal that the frozen bytes were lost to a write outside a deploy
    db = FakeDB([])
    assert R.execute_retain(_decision(), s3=s3, bucket='b', client=db).status == 'drift'
    assert len(db.upserts) == (1 if s3.copy412 else 0)


def test_gate_archives_a_frozen_member_then_allows():
    s3, db = FakeS3({LIVE_SPEC: (1000, 'e1')}), FakeDB([_row()])
    out = R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B)], client=db, s3=s3,
                                        bucket='b', log=lambda *_: None)
    assert [o.status for o in out] == ['archived'] and ARCHIVE_SPEC in s3.objects


def test_gate_refuses_on_drift_and_copy_failure():
    with pytest.raises(R.RetainError, match='drift'):
        R.retain_before_direct_upload([_task(LIVE_SPEC, old=SHA_B)], client=FakeDB([_row()]),
                                      s3=FakeS3({LIVE_SPEC: (1000, 'e1')}), bucket='b')
    with pytest.raises(R.RetainError, match='could not archive'):
        R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B)], client=FakeDB([_row()]),
                                      s3=FakeS3({LIVE_SPEC: (1000, 'e1')}, copy412=True), bucket='b')


def test_execute_registers_before_it_copies_and_fails_closed_on_a_claimed_key():
    # the row goes in first, so a collection can never claim it under the copy
    s3, db = FakeS3({LIVE_SPEC: (1000, 'e1')}), FakeDB([_row()])
    db.s3 = s3
    [d] = R.plan_retain([_task(LIVE_SPEC, new=SHA_B)], db)
    out = R.execute_retain(d, s3=s3, bucket='b', client=db)
    assert out.status == 'archived' and out.copied
    i_reg = next(i for i, (n, _) in enumerate(db.rpcs) if n == 'archive_register_retained')
    assert db.calls_before_register == [('head', LIVE_SPEC)]       # the copy came after the registration
    assert i_reg < len(db.rpcs) - 1
    # the object exists in the bucket (a purge is deleting it); the registration
    # refuses, so the gate reports an error, copies nothing and the deploy retries later
    s3 = FakeS3({LIVE_SPEC: (1000, 'e1'), ARCHIVE_SPEC: (1000, 'e1')})
    db = FakeDB([_row()], collecting=(ARCHIVE_SPEC,))
    [d] = R.plan_retain([_task(LIVE_SPEC, new=SHA_B)], db)
    out = R.execute_retain(d, s3=s3, bucket='b', client=db)
    assert out.status == 'error' and 'being collected' in out.reason and db.upserts == []
    assert not [c for c in s3.calls if c[0] == 'copy']
    with pytest.raises(R.RetainError, match='could not archive'):
        R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B)], client=db, s3=s3, bucket='b')


def test_gate_touches_nothing_for_an_unreferenced_overwrite():
    s3 = FakeS3({LIVE_SPEC: (1000, 'e1')})
    out = R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B), _task(LIVE_SPEC, old=SHA_B)],
                                        client=FakeDB([_row()], required=()), s3=s3, bucket='b')
    assert out == [] and s3.calls == []


def test_gate_with_no_tasks_asks_nothing():
    assert R.retain_before_direct_upload([], client=None, s3=None, bucket='b') == []
