"""Retain-on-supersede on the direct (boto3) upload path (campfire.deploy.retain).

Mirrors web/lib/server/retain.test.ts: the plan against a fake registry, the
copy step against a fake S3 client with OSN's measured semantics (HEAD 404,
conditional copy 412), and the gate's three modes.
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


class FakeDB:
    def __init__(self, rows):
        self.rows, self.upserts, self.rpcs = rows, [], []

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, args):
        self.rpcs.append((name, args))
        return self

    def execute(self):
        return type('R', (), {'data': 'evt'})()


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


def test_mode_defaults_to_log():
    assert R.retain_mode({}) == 'log'
    assert R.retain_mode({'CAMPFIRE_RETAIN_MODE': 'ENFORCE'}) == 'enforce'
    assert R.retain_mode({'CAMPFIRE_RETAIN_MODE': 'bogus'}) == 'log'


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
    [d] = R.plan_retain([_task(LIVE_SPEC, old=SHA_A)], FakeDB([_row(content_hash=SHA_B)]))
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
    assert opts == {'on_conflict': 'backend,bucket,storage_key', 'ignore_duplicates': True}
    assert db.rpcs[0][0] == 'log_deploy_event' and db.rpcs[0][1]['p_action'] == 'archive'


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
def test_execute_reports_drift_without_registering(s3):
    db = FakeDB([])
    assert R.execute_retain(_decision(), s3=s3, bucket='b', client=db).status == 'drift'
    assert db.upserts == []


def test_gate_log_mode_only_logs():
    lines = []
    s3 = FakeS3({LIVE_SPEC: (1000, 'e1')})
    out = R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B)], client=FakeDB([_row()]), s3=s3,
                                        bucket='b', mode='log', log=lines.append)
    assert out == [] and not [c for c in s3.calls if c[0] == 'copy']
    assert any('would archive' in ln for ln in lines)


def test_gate_enforce_archives_then_allows():
    s3, db = FakeS3({LIVE_SPEC: (1000, 'e1')}), FakeDB([_row()])
    out = R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B)], client=db, s3=s3,
                                        bucket='b', mode='enforce', log=lambda *_: None)
    assert [o.status for o in out] == ['archived'] and ARCHIVE_SPEC in s3.objects


def test_gate_enforce_refuses_on_drift_and_copy_failure():
    with pytest.raises(R.RetainError, match='drift'):
        R.retain_before_direct_upload([_task(LIVE_SPEC, old=SHA_B)], client=FakeDB([_row()]),
                                      s3=FakeS3({LIVE_SPEC: (1000, 'e1')}), bucket='b', mode='enforce')
    with pytest.raises(R.RetainError, match='could not archive'):
        R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B)], client=FakeDB([_row()]),
                                      s3=FakeS3({LIVE_SPEC: (1000, 'e1')}, copy412=True),
                                      bucket='b', mode='enforce')


def test_gate_off_touches_nothing():
    assert R.retain_before_direct_upload([_task(LIVE_SPEC, new=SHA_B)], client=None, s3=None,
                                         bucket='b', mode='off') == []
