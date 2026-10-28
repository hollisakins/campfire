"""The byte pass and the collection tombstone (P4e): a copy target a collection
deleted after the object the pass found was written is copied again, never
healed on; every outcome carries the page time so dr_bytes_mark can refuse
one whose key a collection deleted after the page was handed out."""
from datetime import datetime, timedelta, timezone

import campfire.deploy.dr_bytes as B

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
KEY = 'data/products/nirspec/zzz_obs/zzz_obs_prism_clear_1_zfit.json'
HASH = 'sha256:' + 'a' * 64
TARGET = f"archive/{'a' * 64}/products/nirspec/zzz_obs/zzz_obs_prism_clear_1_zfit.json"


class _S3:
    """head / get / put / copy on a dict; LastModified per object."""

    def __init__(self, objects):
        self.objects, self.calls = dict(objects), []

    def head_object(self, Bucket, Key):
        self.calls.append(('head', Key))
        if Key not in self.objects:
            raise _Err(404)
        body, lm = self.objects[Key]
        return {'ContentLength': len(body), 'ETag': '"e1"', 'LastModified': lm}

    def get_object(self, Bucket, Key, **kw):
        self.calls.append(('get', Key))
        body, _ = self.objects[Key]
        return {'Body': _Body(body)}

    def put_object(self, Bucket, Key, Body, **kw):
        self.calls.append(('put', Key, kw.get('IfNoneMatch')))
        if kw.get('IfNoneMatch') == '*' and Key in self.objects:
            raise _Err(412)
        self.objects[Key] = (Body, NOW + timedelta(minutes=1))
        return {}

    def copy_object(self, Bucket, Key, CopySource, **kw):
        self.calls.append(('copy', Key))
        self.objects[Key] = self.objects[CopySource['Key']]
        return {}


class _Body:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b


class _Err(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {'ResponseMetadata': {'HTTPStatusCode': code}}


def _row(**over):
    body = b'{"z": 1}'
    import hashlib
    h = 'sha256:' + hashlib.sha256(body).hexdigest()
    row = {'storage_key': KEY, 'product_type': 'zfit', 'backend': 'osn', 'bucket': 'data',
           'content_hash': h, 'size_bytes': len(body), 'source_key': KEY, 'source_root': 'live',
           'source_stored_size': len(body), 'registry': {'content_type': 'application/json'},
           'target_collected_at': None}
    row.update(over)
    return row, body, h


def _target(h):
    return f"archive/{h[7:]}/products/nirspec/zzz_obs/zzz_obs_prism_clear_1_zfit.json"


def test_collected_since_compares_the_object_time_with_the_tombstone():
    stone = NOW.isoformat()
    older = {'LastModified': NOW - timedelta(minutes=5)}
    newer = {'LastModified': NOW + timedelta(seconds=1)}
    assert B._collected_since({'target_collected_at': stone}, older) is True
    assert B._collected_since({'target_collected_at': stone}, newer) is False
    assert B._collected_since({'target_collected_at': None}, older) is False
    # no time from the store while a tombstone exists: assume the worst
    assert B._collected_since({'target_collected_at': stone}, {}) is True


def test_copy_heals_on_an_existing_object_and_every_outcome_carries_the_page_time():
    row, body, h = _row(page_at='2026-10-27T12:00:00+00:00')
    s3 = _S3({KEY: (body, NOW - timedelta(days=2)), _target(h): (body, NOW - timedelta(days=1))})
    out = B.process_member(row, tag='DR-t', s3=s3, bucket='b', verify_science=False)
    assert (out.state, out.action) == ('copied', 'heal')
    assert out.page_at == '2026-10-27T12:00:00+00:00' and out.as_result()['page_at'] == out.page_at
    assert not [c for c in s3.calls if c[0] in ('put', 'copy')]
    # a fresh copy carries it too (the fence covers fresh writes, not only heals)
    row, body, h = _row(page_at='2026-10-27T12:00:00+00:00')
    out = B.process_member(row, tag='DR-t', s3=_S3({KEY: (body, NOW)}), bucket='b', verify_science=False)
    assert out.action == 'copy-verified' and out.as_result()['page_at'] == '2026-10-27T12:00:00+00:00'


def test_copy_overwrites_a_target_a_collection_deleted_after_the_object_was_written():
    row, body, h = _row(target_collected_at=NOW.isoformat())
    stale = NOW - timedelta(hours=1)                        # the object the collector is deleting
    s3 = _S3({KEY: (body, NOW - timedelta(days=2)), _target(h): (body, stale)})
    out = B.process_member(row, tag='DR-t', s3=s3, bucket='b', verify_science=False)
    assert (out.state, out.action) == ('copied', 'copy-verified')
    assert ('put', _target(h), None) in s3.calls             # overwrote, no If-None-Match
    assert s3.objects[_target(h)][1] > NOW                   # new bytes, new time


def test_copy_heals_on_an_object_written_after_the_collection():
    row, body, h = _row(target_collected_at=NOW.isoformat())
    fresh = NOW + timedelta(minutes=2)                       # someone copied it again already
    s3 = _S3({KEY: (body, NOW - timedelta(days=2)), _target(h): (body, fresh)})
    out = B.process_member(row, tag='DR-t', s3=s3, bucket='b', verify_science=False)
    assert (out.state, out.action) == ('copied', 'heal')
