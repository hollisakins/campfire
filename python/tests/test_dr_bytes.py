"""Data-release byte pass (P4c): the header stamp, the per-member paths, the loop."""
from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from astropy.io import fits
from campfire_layout import archive_key, parse_key

from campfire.deploy import dr_bytes as B
from tests.fake_s3 import FakeS3Client
from tests.fakes import FakePostgrest

BUCKET = 'campfire-data'
TAG = 'DR2026.0-rehearsal'


def _fits_bytes(n_extra_cards: int = 0, *, ext: bool = True, extra: dict | None = None) -> bytes:
    prim = fits.PrimaryHDU()
    for i in range(n_extra_cards):
        prim.header[f'X{i:05d}'] = i
    for k, v in (extra or {}).items():
        prim.header[k] = v
    hdus = [prim]
    if ext:
        hdus.append(fits.ImageHDU(np.arange(300, dtype='>f4').reshape(15, 20), name='SCI'))
        hdus.append(fits.BinTableHDU.from_columns([fits.Column('WAVE', 'E', array=np.linspace(1, 5, 7))], name='SPEC'))
    buf = io.BytesIO()
    fits.HDUList(hdus).writeto(buf)
    return buf.getvalue()


def _cards_before_end(data: bytes) -> int:
    return B.primary_header_length(data)[0]


def _fits_with_end_at(slot: int) -> bytes:
    """A FITS whose primary END card sits at *slot* (0-based) within its block."""
    base = _cards_before_end(_fits_bytes(0))
    data = _fits_bytes(slot - base)
    assert _cards_before_end(data) % 36 == slot
    return data


# ---------------------------------------------------------------------------
# stamp_fits
# ---------------------------------------------------------------------------

def test_stamp_in_free_slot_keeps_length_and_data():
    data = _fits_with_end_at(10)
    out = B.stamp_fits(data, TAG)
    assert len(out) == len(data)
    _, length = B.primary_header_length(data)
    assert out[length:] == data[length:]
    with fits.open(io.BytesIO(out)) as h:
        assert h[0].header['CMPFRDR'] == TAG
        np.testing.assert_array_equal(h['SCI'].data, np.arange(300, dtype='>f4').reshape(15, 20))
    assert B.sha256_hex(out) != B.sha256_hex(data)


def test_stamp_grows_one_block_when_end_is_last():
    data = _fits_with_end_at(35)
    out = B.stamp_fits(data, TAG)
    assert len(out) == len(data) + 2880
    _, old_len = B.primary_header_length(data)
    _, new_len = B.primary_header_length(out)
    assert new_len == old_len + 2880
    assert out[new_len:] == data[old_len:]
    with fits.open(io.BytesIO(out)) as h:
        assert h[0].header['CMPFRDR'] == TAG
        assert len(h) == 3
        np.testing.assert_allclose(h['SPEC'].data['WAVE'], np.linspace(1, 5, 7))


def test_stamp_slot_34_fits_without_growth():
    data = _fits_with_end_at(34)
    assert len(B.stamp_fits(data, TAG)) == len(data)


def test_stamp_replaces_an_existing_stamp_in_place():
    data = _fits_bytes(3, extra={'CMPFRDR': 'DR-old'})
    out = B.stamp_fits(data, TAG)
    assert len(out) == len(data)
    with fits.open(io.BytesIO(out)) as h:
        assert h[0].header['CMPFRDR'] == TAG
        assert list(h[0].header.keys()).count('CMPFRDR') == 1


def test_stamp_is_deterministic():
    data = _fits_bytes(5)
    assert B.stamp_fits(data, TAG) == B.stamp_fits(data, TAG)


def test_stamp_primary_only_file():
    data = _fits_bytes(2, ext=False)
    out = B.stamp_fits(data, TAG)
    with fits.open(io.BytesIO(out)) as h:
        assert h[0].header['CMPFRDR'] == TAG


@pytest.mark.parametrize('bad', [b'not a fits file' * 300, b'SIMPLE  =' + b' ' * 71])
def test_stamp_refuses_non_fits(bad):
    with pytest.raises(B.StampError):
        B.stamp_fits(bad, TAG)


def test_stamp_refuses_checksum_and_bad_tags():
    with pytest.raises(B.StampError, match='CHECKSUM'):
        B.stamp_fits(_fits_bytes(1, extra={'CHECKSUM': 'abcdefgh', 'DATASUM': '0'}), TAG)
    for tag in ('', "DR'1", 'x' * 65, 'DR 1'):
        with pytest.raises(B.StampError):
            B.stamp_fits(_fits_bytes(1), tag)


def test_stamp_keeps_the_science_hash():
    pytest.importorskip('campfire_pipeline.metadata.science_hash')
    for data in (_fits_with_end_at(10), _fits_with_end_at(35)):
        out = B.stamp_fits(data, TAG)
        assert B.science_hash_of(out) == B.science_hash_of(data)
        assert B.sha256_hex(out) != B.sha256_hex(data)


# ---------------------------------------------------------------------------
# process_member
# ---------------------------------------------------------------------------

SPEC_KEY = 'data/products/nirspec/abc_p1/abc_p1_123_prism_spec.fits'
JSON_KEY = 'data/products/nirspec/abc_p1/abc_p1_123_prism_spec.json'


def _row(key, body, *, product_type='nirspec_spec', source_root='live', source_key=None, **kw):
    row = {
        'storage_key': key, 'product_type': product_type, 'component': 'nirspec',
        'program_slug': 'abc', 'field': None, 'spectrum_id': 'abc_p1_123_prism',
        'backend': 'osn', 'bucket': 'data', 'content_hash': B.sha256_hex(body),
        'science_hash': None, 'size_bytes': len(body), 'byte_state': 'pending',
        'source_key': key if source_key is None else source_key, 'source_root': source_root,
        'source_stored_size': len(body),
        'reuse_archive_key': None, 'reuse_stamped_hash': None, 'reuse_size_bytes': None,
        'registry': {'content_type': 'application/fits', 'instrument': 'nirspec',
                     'observation': 'abc_p1', 'deployment_id': 7, 'cfpipe_version': '1.2.0',
                     'retained_key': None},
    }
    row.update(kw)
    return row


def _s3(**objects):
    s3 = FakeS3Client()
    s3.create_bucket(Bucket=BUCKET)
    for k, v in objects.items():
        s3.seed(BUCKET, k, v)
    return s3


def test_stamp_member_puts_conditionally_under_the_stamped_hash():
    body = _fits_bytes(4)
    s3 = _s3(**{SPEC_KEY: body})
    o = B.process_member(_row(SPEC_KEY, body), tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert (o.state, o.action) == ('stamped', 'stamp')
    stamped = B.stamp_fits(body, TAG)
    assert o.stamped_hash == B.sha256_hex(stamped)
    pk = parse_key(SPEC_KEY)
    assert o.archive_key == archive_key(pk.product_type, pk.scope, pk.filename, content_hash=o.stamped_hash)
    assert s3.body(BUCKET, o.archive_key) == stamped
    puts = s3.ops('PutObject')
    assert puts and puts[-1][1].get('IfNoneMatch') == '*'
    assert o.registry['size_bytes'] == len(stamped)
    assert 'retained_key' not in o.registry
    assert s3.body(BUCKET, SPEC_KEY) == body          # live object untouched


def test_stamp_member_rerun_heals_an_existing_object():
    body = _fits_bytes(4)
    s3 = _s3(**{SPEC_KEY: body})
    first = B.process_member(_row(SPEC_KEY, body), tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    again = B.process_member(_row(SPEC_KEY, body), tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert (again.state, again.action) == ('stamped', 'heal')
    assert again.archive_key == first.archive_key and again.registry is not None


def test_stamp_member_detects_changed_bytes():
    body = _fits_bytes(4)
    s3 = _s3(**{SPEC_KEY: _fits_bytes(5)})
    o = B.process_member(_row(SPEC_KEY, body), tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert (o.state, o.action) == ('failed', 'drift')
    assert 'the release froze' in o.error
    assert not [k for k in s3.keys(BUCKET) if k.startswith('archive/')]


def test_stamp_member_from_a_retained_copy():
    body = _fits_bytes(4)
    retained = 'archive/' + B.sha256_hex(body).split(':')[1] + '/products/nirspec/abc_p1/x.fits'
    s3 = _s3(**{retained: body, SPEC_KEY: _fits_bytes(6)})
    o = B.process_member(_row(SPEC_KEY, body, source_key=retained, source_root='archive'),
                         tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert o.state == 'stamped'
    assert s3.body(BUCKET, o.archive_key) == B.stamp_fits(body, TAG)


def test_stamp_member_verifies_the_science_hash():
    pytest.importorskip('campfire_pipeline.metadata.science_hash')
    body = _fits_bytes(4)
    s3 = _s3(**{SPEC_KEY: body})
    ok = B.process_member(_row(SPEC_KEY, body, science_hash=B.science_hash_of(body)),
                          tag=TAG, s3=s3, bucket=BUCKET)
    assert ok.state == 'stamped'
    bad = B.process_member(_row(SPEC_KEY, body, science_hash='sha256:' + '0' * 64),
                           tag=TAG, s3=_s3(**{SPEC_KEY: body}), bucket=BUCKET)
    assert bad.state == 'failed' and 'science hash changed' in bad.error


def test_reuse_stamp_only_heads_the_object():
    body = _fits_bytes(4)
    s3 = _s3(**{SPEC_KEY: body, 'archive/aa/x.fits': b'stamped bytes'})
    o = B.process_member(_row(SPEC_KEY, body, reuse_archive_key='archive/aa/x.fits',
                              reuse_stamped_hash='sha256:' + 'a' * 64, reuse_size_bytes=13),
                         tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert (o.state, o.action, o.archive_key, o.registry) == ('stamped', 'reuse-stamp', 'archive/aa/x.fits', None)
    assert not s3.ops('GetObject') and not s3.ops('PutObject')
    assert s3.ops('HeadObject')


def test_reuse_stamp_missing_object_stamps_afresh_or_fails():
    # Codex on #601: a registry row whose object is gone is not reused
    body = _fits_bytes(4)
    s3 = _s3(**{SPEC_KEY: body})
    row = _row(SPEC_KEY, body, reuse_archive_key='archive/aa/x.fits', reuse_stamped_hash='sha256:' + 'a' * 64)
    o = B.process_member(row, tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert (o.state, o.action) == ('stamped', 'stamp') and o.archive_key != 'archive/aa/x.fits'
    gone = B.process_member(dict(row, source_key='', source_root=None), tag=TAG, s3=_s3(), bucket=BUCKET,
                            verify_science=False)
    assert gone.state == 'failed' and 'missing from the bucket' in gone.error
    # wrong size is not reused either
    s3 = _s3(**{SPEC_KEY: body, 'archive/aa/x.fits': b'short'})
    o = B.process_member(dict(row, reuse_size_bytes=999), tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert o.action == 'stamp'


def test_reuse_stamp_takes_the_first_existing_candidate():
    # the earliest archived candidate whose object exists is reused, and a
    # missing one falls through to the next (Codex on #601)
    body = _fits_bytes(4)
    cands = [{'archive_key': 'archive/aa/first.fits', 'stamped_hash': 'sha256:' + 'a' * 64, 'size_bytes': 5},
             {'archive_key': 'archive/bb/second.fits', 'stamped_hash': 'sha256:' + 'b' * 64, 'size_bytes': 6}]
    row = _row(SPEC_KEY, body, reuse_archive_key=cands[0]['archive_key'],
               reuse_stamped_hash=cands[0]['stamped_hash'], reuse_candidates=cands)
    both = _s3(**{SPEC_KEY: body, 'archive/aa/first.fits': b'first', 'archive/bb/second.fits': b'second'})
    o = B.process_member(row, tag=TAG, s3=both, bucket=BUCKET, verify_science=False)
    assert (o.action, o.archive_key) == ('reuse-stamp', 'archive/aa/first.fits')
    only_second = _s3(**{SPEC_KEY: body, 'archive/bb/second.fits': b'second'})
    o = B.process_member(row, tag=TAG, s3=only_second, bucket=BUCKET, verify_science=False)
    assert (o.action, o.archive_key, o.stamped_hash) == ('reuse-stamp', 'archive/bb/second.fits', 'sha256:' + 'b' * 64)


def test_trailing_gzip_members_are_rejected():
    # Codex on #601: gzip(frozen) + gzip(other) must not verify as frozen
    import gzip
    logical = _fits_bytes(2)
    stored = gzip.compress(logical) + gzip.compress(b'extra payload')
    with pytest.raises(ValueError, match='trailing data'):
        B.logical_sha256(stored, compressed=True)
    s3 = _s3(**{MOSAIC_KEY: stored})
    etag = s3.head_object(Bucket=BUCKET, Key=MOSAIC_KEY)['ETag'].strip('"')
    with pytest.raises(ValueError, match='trailing data'):
        B._stream_logical_sha256(s3, BUCKET, MOSAIC_KEY, etag, compressed=True)


def test_reuse_copy_missing_object_fails():
    body = b'{"x": 1}'
    row = _row(JSON_KEY, body, product_type='spectrum_json')
    target = B._copy_target(row)
    o = B.process_member(dict(row, source_key=target, source_root='archive'), tag=TAG, s3=_s3(), bucket=BUCKET)
    assert o.state == 'failed' and 'verify --cloud' in o.error


def test_stamp_reports_a_science_hash_it_had_to_compute(monkeypatch):
    # Codex on #601: persisted so later releases can reuse the stamped object
    monkeypatch.setattr(B, 'science_hash_of', lambda data: 'sha256:' + '5' * 64)
    body = _fits_bytes(4)
    o = B.process_member(_row(SPEC_KEY, body), tag=TAG, s3=_s3(**{SPEC_KEY: body}), bucket=BUCKET)
    assert o.state == 'stamped' and o.science_hash == 'sha256:' + '5' * 64
    assert o.as_result()['science_hash'] == 'sha256:' + '5' * 64
    known = B.process_member(_row(SPEC_KEY, body, science_hash='sha256:' + '5' * 64), tag=TAG,
                             s3=_s3(**{SPEC_KEY: body}), bucket=BUCKET)
    assert known.science_hash is None and 'science_hash' not in known.as_result()


def test_copy_member_copies_conditionally():
    body = b'{"x": 1}'
    s3 = _s3(**{JSON_KEY: body})
    o = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json'), tag=TAG, s3=s3, bucket=BUCKET)
    assert (o.state, o.action) == ('copied', 'copy-verified')
    assert o.archive_key.startswith('archive/' + B.sha256_hex(body).split(':')[1] + '/')
    assert s3.body(BUCKET, o.archive_key) == body
    assert s3.ops('PutObject')[-1][1].get('IfNoneMatch') == '*'
    assert o.registry['size_bytes'] == len(body)
    # and a re-run heals
    again = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json'), tag=TAG, s3=s3, bucket=BUCKET)
    assert (again.state, again.action) == ('copied', 'heal')


def test_live_copy_is_always_verified_by_hash():
    # Codex on #601: neither an ETag nor a modification time proves the frozen
    # hash -- `campfire push` writes an object before it registers its hash, so
    # unregistered replacement bytes can predate the freeze
    body = b'{"x": 1}'
    row = _row(JSON_KEY, body, product_type='spectrum_json')
    # the frozen bytes: verified, uploaded, not copied
    s3 = _s3(**{JSON_KEY: body})
    o = B.process_member(row, tag=TAG, s3=s3, bucket=BUCKET)
    assert (o.state, o.action) == ('copied', 'copy-verified')
    assert s3.body(BUCKET, o.archive_key) == body and not s3.ops('CopyObject')
    assert s3.ops('PutObject')[-1][1].get('IfNoneMatch') == '*'
    # replacement bytes of the same length (an unflushed push): refused, nothing archived
    s3 = _s3(**{JSON_KEY: b'{"x": 2}'})
    o = B.process_member(row, tag=TAG, s3=s3, bucket=BUCKET)
    assert (o.state, o.action) == ('failed', 'drift') and 'the release froze' in o.error
    assert not [k for k in s3.keys(BUCKET) if k.startswith('archive/')]
    # too large to download whole: verified by a streamed hash, then copied
    # server-side under the same ETag -- never failed unverified (Codex on #601)
    big = dict(row, source_stored_size=None)
    s3 = _s3(**{JSON_KEY: body})
    monkey_limit = B.VERIFY_LIMIT_BYTES
    try:
        B.VERIFY_LIMIT_BYTES = 4
        o = B.process_member(big, tag=TAG, s3=s3, bucket=BUCKET)
        assert (o.state, o.action) == ('copied', 'copy-verified')
        assert s3.ops('CopyObject')[-1][1].get('CopySourceIfMatch')
        assert s3.body(BUCKET, o.archive_key) == body
        # and different bytes are still refused
        o = B.process_member(big, tag=TAG, s3=_s3(**{JSON_KEY: b'{"x": 2}'}), bucket=BUCKET)
        assert o.state == 'failed' and 'the release froze' in o.error
    finally:
        B.VERIFY_LIMIT_BYTES = monkey_limit


MOSAIC_KEY = 'data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_full_sci.fits.gz'


def test_gzipped_mosaic_is_verified_on_its_logical_bytes():
    # Codex on #601: the registry hashes the uncompressed FITS
    import gzip
    logical = _fits_bytes(2)
    stored = gzip.compress(logical)
    assert B._stored_compressed(MOSAIC_KEY) and not B._stored_compressed(JSON_KEY)
    assert B.logical_sha256(stored, compressed=True) == B.sha256_hex(logical)
    row = _row(MOSAIC_KEY, logical, product_type='nircam_mosaic',
               source_stored_size=len(stored), component='nircam/cosmos', program_slug=None)
    s3 = _s3(**{MOSAIC_KEY: stored})
    o = B.process_member(row, tag=TAG, s3=s3, bucket=BUCKET)
    assert (o.state, o.action) == ('copied', 'copy-verified')
    assert o.archive_key.endswith('.fits.gz') and s3.body(BUCKET, o.archive_key) == stored
    # a different logical mosaic is still refused (by size or by hash)
    other = gzip.compress(_fits_bytes(3))
    o = B.process_member(dict(row, source_stored_size=len(other)), tag=TAG,
                         s3=_s3(**{MOSAIC_KEY: other}), bucket=BUCKET)
    assert (o.state, o.action) == ('failed', 'drift') and 'the release froze' in o.error


def test_gunzip_output_is_bounded(monkeypatch):
    # Codex on #601: a highly compressible body must not inflate a whole
    # chunk's expansion at once
    import gzip
    import zlib
    logical = b'\x00' * (5 << 20)           # 5 MiB of zeros -> a few KiB gzipped
    stored = gzip.compress(logical)
    monkeypatch.setattr(B, 'INFLATE_OUT_BYTES', 64 << 10)
    seen = []
    real = zlib.decompressobj

    class Spy:
        def __init__(self, *a):
            self.d = real(*a)

        def decompress(self, data, max_length=0):
            out = self.d.decompress(data, max_length)
            seen.append((max_length, len(out)))
            return out

        def __getattr__(self, name):
            return getattr(self.d, name)
    monkeypatch.setattr(zlib, 'decompressobj', Spy)
    assert B.logical_sha256(stored, compressed=True) == B.sha256_hex(logical)
    assert seen and all(m == 64 << 10 and n <= 64 << 10 for m, n in seen)
    # and the streamed variant agrees
    s3 = _s3(**{MOSAIC_KEY: stored})
    etag = s3.head_object(Bucket=BUCKET, Key=MOSAIC_KEY)['ETag'].strip('"')
    assert B._stream_logical_sha256(s3, BUCKET, MOSAIC_KEY, etag, compressed=True) == B.sha256_hex(logical)


def test_unknown_stored_size_of_a_gzipped_mosaic_is_not_drift(monkeypatch):
    # Codex on #601: an old .fits.gz row without stored_size_bytes is unknown,
    # not its logical size; the CopyObject ceiling uses the HEAD instead
    import gzip
    stored = gzip.compress(_fits_bytes(2))
    row = _row(MOSAIC_KEY, _fits_bytes(2), product_type='nircam_mosaic', source_stored_size=None,
               component='nircam/cosmos', program_slug=None)
    o = B.process_member(row, tag=TAG, s3=_s3(**{MOSAIC_KEY: stored}), bucket=BUCKET)
    assert (o.state, o.action) == ('copied', 'copy-verified')
    monkeypatch.setattr(B, 'COPY_LIMIT_BYTES', len(stored) - 1)
    o = B.process_member(row, tag=TAG, s3=_s3(**{MOSAIC_KEY: stored}), bucket=BUCKET)
    assert o.state == 'failed' and '5 GB CopyObject limit' in o.error


def test_copy_from_a_retained_copy_stays_server_side():
    body = b'{"x": 1}'
    row = _row(JSON_KEY, body, product_type='spectrum_json')
    retained = 'archive/' + 'e' * 64 + '/elsewhere.json'      # a different key than the target
    s3 = _s3(**{retained: body})
    o = B.process_member(dict(row, source_key=retained, source_root='archive'), tag=TAG, s3=s3, bucket=BUCKET)
    assert (o.state, o.action) == ('copied', 'copy') and s3.ops('CopyObject')


def test_copy_member_reuses_the_retained_copy():
    body = b'{"x": 1}'
    row = _row(JSON_KEY, body, product_type='spectrum_json')
    target = B._copy_target(row)
    s3 = _s3(**{target: body})
    o = B.process_member(dict(row, source_key=target, source_root='archive'), tag=TAG, s3=s3, bucket=BUCKET)
    assert (o.state, o.action, o.archive_key, o.registry) == ('copied', 'reuse-copy', target, None)
    assert not s3.ops('CopyObject')


def test_copy_member_failures():
    body = b'{"x": 1}'
    # bytes gone
    o = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json', source_key='', source_root=None),
                         tag=TAG, s3=_s3(), bucket=BUCKET)
    assert (o.state, o.action) == ('failed', 'drift')
    # registry row dangling
    o = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json'), tag=TAG, s3=_s3(), bucket=BUCKET)
    assert o.state == 'failed' and 'missing' in o.error
    # size mismatch
    o = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json'), tag=TAG,
                         s3=_s3(**{JSON_KEY: body + b' '}), bucket=BUCKET)
    assert o.state == 'failed' and 'registry says' in o.error
    # over the CopyObject ceiling
    o = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json', source_stored_size=6 * 10 ** 9),
                         tag=TAG, s3=_s3(**{JSON_KEY: body}), bucket=BUCKET)
    assert o.state == 'failed' and '5 GB' in o.error
    # wrong backend
    o = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json', backend='r2'),
                         tag=TAG, s3=_s3(), bucket=BUCKET)
    assert o.state == 'failed' and 'osn/data' in o.error


def test_unexpected_errors_fail_one_member_only():
    class Boom(FakeS3Client):
        def get_object(self, **kw):
            raise RuntimeError('network down')
    s3 = Boom()
    body = _fits_bytes(2)
    o = B.process_member(_row(SPEC_KEY, body), tag=TAG, s3=s3, bucket=BUCKET, verify_science=False)
    assert o.state == 'failed' and 'network down' in o.error


# ---------------------------------------------------------------------------
# run_byte_pass
# ---------------------------------------------------------------------------

def _fake_db(rows):
    state = {'rows': rows, 'marks': [], 'calls': []}

    def pending(params):
        state['calls'].append(params)
        after, lim = params['p_after'], params['p_limit']
        pre = params.get('p_prefix')
        todo = [r for r in state['rows']
                if r['byte_state'] == 'pending' or (params['p_include_failed'] and r['byte_state'] == 'failed')]
        todo = [r for r in todo if (after is None or r['storage_key'] > after)
                and (pre is None or r['storage_key'].startswith(pre))]
        return sorted(todo, key=lambda r: r['storage_key'])[:lim]

    def mark(params):
        state['marks'].append(params['p_results'])
        by_key = {r['storage_key']: r for r in state['rows']}
        for res in params['p_results']:
            # as dr_bytes_mark: the outcome must name the member's frozen content
            assert res.get('content_hash') == by_key[res['storage_key']]['content_hash'], res
            by_key[res['storage_key']]['byte_state'] = res['state']
        open_ = sum(1 for r in state['rows'] if r['byte_state'] in ('pending', 'failed'))
        return {'state': 'copying' if open_ else 'ready', 'marked': len(params['p_results'])}

    def summary(params):
        by = {}
        for r in state['rows']:
            by.setdefault(r['byte_state'], {'count': 0, 'bytes': 0})['count'] += 1
        return {'tag': params['p_tag'], 'state': 'copying', 'manifest': len(state['rows']),
                'by_state': by, 'failed_sample': []}

    client = FakePostgrest(rpc_handlers={'dr_bytes_pending': pending, 'dr_bytes_mark': mark,
                                         'dr_bytes_summary': summary})
    return client, state


def test_pass_pages_batches_and_converges():
    s3 = _s3()
    rows = []
    for i in range(7):
        key = f'data/products/nirspec/abc_p1/abc_p1_{i}_prism_spec.json'
        body = f'{{"i": {i}}}'.encode()
        s3.seed(BUCKET, key, body)
        rows.append(_row(key, body, product_type='spectrum_json'))
    client, state = _fake_db(rows)
    rep = B.run_byte_pass(client, s3, BUCKET, TAG, workers=3, page_size=3, mark_batch=2,
                          verify_science=False, log=lambda *_: None)
    assert rep.seen == 7 and rep.by_state == {'copied': 7} and rep.state == 'ready'
    assert [len(m) for m in state['marks']] == [2, 1, 2, 1, 1]
    assert all(r['byte_state'] == 'copied' for r in rows)
    # nothing left: a second run sees nothing
    rep2 = B.run_byte_pass(client, s3, BUCKET, TAG, verify_science=False, log=lambda *_: None)
    assert rep2.seen == 0


def test_pass_records_failures_and_retries_them():
    body = b'{"x": 1}'
    rows = [_row(JSON_KEY, body, product_type='spectrum_json')]
    client, state = _fake_db(rows)
    rep = B.run_byte_pass(client, _s3(), BUCKET, TAG, verify_science=False, log=lambda *_: None)
    assert rep.by_state == {'failed': 1} and rep.failures[0]['storage_key'] == JSON_KEY
    assert state['marks'][0][0]['error']
    # not retried by default; retried with include_failed once the source is back
    assert B.run_byte_pass(client, _s3(), BUCKET, TAG, verify_science=False, log=lambda *_: None).seen == 0
    rep = B.run_byte_pass(client, _s3(**{JSON_KEY: body}), BUCKET, TAG, include_failed=True,
                          verify_science=False, log=lambda *_: None)
    assert rep.by_state == {'copied': 1}


def test_pass_dry_run_limit_and_prefix():
    body = b'{"x": 1}'
    rows = [_row(f'data/products/nirspec/obs{i % 2}/obs{i % 2}_{i}_prism_spec.json', body,
                 product_type='spectrum_json') for i in range(6)]
    rows.append(_row(SPEC_KEY, body, source_key='', source_root=None))
    client, state = _fake_db(rows)
    rep = B.run_byte_pass(client, None, None, TAG, dry_run=True, log=lambda *_: None)
    assert rep.seen == 7 and rep.by_action == {'copy': 6, 'drift': 1} and not state['marks']
    rep = B.run_byte_pass(client, None, None, TAG, dry_run=True, limit=4, page_size=3, log=lambda *_: None)
    assert rep.seen == 4
    rep = B.run_byte_pass(client, None, None, TAG, dry_run=True, prefix='data/products/nirspec/obs1/',
                          log=lambda *_: None)
    assert rep.seen == 3
    assert B.format_report(rep).startswith('Dry run on')


def test_pass_requires_the_pipeline_for_science_checks(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, *a, **kw):
        if name.startswith('campfire_pipeline'):
            raise ImportError(name)
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, '__import__', fake)
    # sidecars only: no pipeline needed
    body = b'{"x": 1}'
    client, _ = _fake_db([_row(JSON_KEY, body, product_type='spectrum_json')])
    rep = B.run_byte_pass(client, _s3(**{JSON_KEY: body}), BUCKET, TAG, log=lambda *_: None)
    assert rep.by_state == {'copied': 1}
    # a FITS to stamp: refused before anything moves
    fits_body = _fits_bytes(1)
    client, state = _fake_db([_row(SPEC_KEY, fits_body)])
    s3 = _s3(**{SPEC_KEY: fits_body})
    with pytest.raises(RuntimeError, match='campfire-pipeline'):
        B.run_byte_pass(client, s3, BUCKET, TAG, log=lambda *_: None)
    assert not state['marks'] and not s3.ops('GetObject')


def test_format_summary():
    text = B.format_summary({'tag': TAG, 'state': 'copying', 'manifest': 3,
                             'by_state': {'pending': {'count': 1, 'bytes': 10},
                                          'stamped': {'count': 2, 'bytes': 2_000_000_000}},
                             'failed_sample': [{'storage_key': 'k', 'error': 'gone'}]})
    assert 'copying' in text and 'stamped' in text and '✗ k: gone' in text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _patch_cli(monkeypatch, client, s3=None, backend='osn'):
    import campfire.deploy.backend as be
    import campfire.deploy.cli as dcli
    import campfire.deploy.config as cfg
    import campfire.deploy.supabase as sb
    monkeypatch.setattr(cfg, 'load_config', lambda *a, **k: {'supabase': {'_auth_mode': 'local'}})
    monkeypatch.setattr(sb, 'get_supabase_client', lambda config: client)
    monkeypatch.setattr(dcli, '_gate_admin', lambda config, **k: None)
    monkeypatch.setattr(be, 'resolve_backend',
                        lambda config, purpose='data': type('B', (), {'backend': backend, 'bucket': BUCKET})())
    monkeypatch.setattr(be, 'make_s3_client', lambda bcfg, **k: s3)
    monkeypatch.setattr(B, 'science_hash_of', lambda data: 'sha256:' + '0' * 64)


def test_cli_bytes_dry_run_with_obs_prefix(monkeypatch):
    from click.testing import CliRunner

    import campfire.cli as main_cli
    body = b'{"x": 1}'
    rows = [_row(JSON_KEY, body, product_type='spectrum_json'),
            _row('data/products/nirspec/other/other_1_prism_spec.json', body, product_type='spectrum_json')]
    client, state = _fake_db(rows)
    _patch_cli(monkeypatch, client)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'archive', '--tag', TAG, '--obs', 'abc_p1', '--dry-run'])
    assert res.exit_code == 0, res.output
    assert 'under data/products/nirspec/abc_p1/' in res.output
    assert 'Dry run on' in res.output and 'copy' in res.output
    assert state['calls'][0]['p_prefix'] == 'data/products/nirspec/abc_p1/'
    assert not state['marks']


def test_cli_bytes_fails_while_earlier_failures_remain(monkeypatch):
    # Codex on #601: a rerun without --retry-failed fetches nothing, but the
    # stored failure still keeps the release from becoming ready
    from click.testing import CliRunner

    import campfire.cli as main_cli
    body = b'{"x": 1}'
    rows = [_row(JSON_KEY, body, product_type='spectrum_json', byte_state='failed')]
    client, _ = _fake_db(rows)
    _patch_cli(monkeypatch, client, s3=_s3())
    res = CliRunner().invoke(main_cli.cli, ['dr', 'archive', '--tag', TAG])
    assert res.exit_code == 1, res.output
    assert '1 member(s) of' in res.output and '--retry-failed' in res.output


def test_cli_bytes_fails_when_the_summary_cannot_be_read(monkeypatch):
    # Codex on #601: an unknown failure count is not a clean run
    from click.testing import CliRunner

    import campfire.cli as main_cli
    client, _ = _fake_db([])

    def broken(params):
        raise RuntimeError('network down')
    client.rpc_handlers['dr_bytes_summary'] = broken
    _patch_cli(monkeypatch, client, s3=_s3())
    res = CliRunner().invoke(main_cli.cli, ['dr', 'archive', '--tag', TAG])
    assert res.exit_code == 1, res.output
    assert 'could not read the byte-pass summary' in res.output


def test_cli_bytes_runs_and_exits_1_on_failures(monkeypatch):
    from click.testing import CliRunner

    import campfire.cli as main_cli
    body = b'{"x": 1}'
    s3 = _s3(**{JSON_KEY: body})
    rows = [_row(JSON_KEY, body, product_type='spectrum_json'),
            _row('data/products/nirspec/abc_p1/abc_p1_9_prism_spec.json', body, product_type='spectrum_json')]
    client, state = _fake_db(rows)
    _patch_cli(monkeypatch, client, s3=s3)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'archive', '--tag', TAG, '--workers', '2'])
    assert res.exit_code == 1, res.output
    assert 'copy' in res.output and '✗ data/products/nirspec/abc_p1/abc_p1_9_prism_spec.json' in res.output
    assert {r['byte_state'] for r in rows} == {'copied', 'failed'}


def test_cli_bytes_refuses_a_non_osn_backend(monkeypatch):
    from click.testing import CliRunner

    import campfire.cli as main_cli
    client, _ = _fake_db([])
    _patch_cli(monkeypatch, client, s3=_s3(), backend='r2')
    res = CliRunner().invoke(main_cli.cli, ['dr', 'archive', '--tag', TAG])
    assert res.exit_code == 2 and 'needs OSN credentials' in res.output
    assert 'osn: resolves to r2' in res.output


def test_cli_bytes_status(monkeypatch):
    from click.testing import CliRunner

    import campfire.cli as main_cli
    client = FakePostgrest(rpc_handlers={'dr_bytes_summary': {
        'tag': TAG, 'state': 'copying', 'manifest': 2,
        'by_state': {'pending': {'count': 1, 'bytes': 5}, 'copied': {'count': 1, 'bytes': 5}},
        'failed_sample': []}})
    _patch_cli(monkeypatch, client)
    res = CliRunner().invoke(main_cli.cli, ['dr', 'archive', '--tag', TAG, '--status'])
    assert res.exit_code == 0, res.output
    assert f'{TAG}: state copying, 2 manifest files' in res.output


def test_cli_bytes_json_stdout_is_pure_json(monkeypatch):
    import json

    from click.testing import CliRunner

    import campfire.cli as main_cli
    body = b'{"x": 1}'
    client, _ = _fake_db([_row(JSON_KEY, body, product_type='spectrum_json')])
    _patch_cli(monkeypatch, client, s3=_s3(**{JSON_KEY: body}))
    try:
        runner = CliRunner(mix_stderr=False)
    except TypeError:  # click >= 8.2 always separates the streams
        runner = CliRunner()
    res = runner.invoke(main_cli.cli, ['dr', 'archive', '--tag', TAG, '--json'])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)['by_state'] == {'copied': 1}
    assert 'Archiving the member bytes' in res.stderr



def test_stamp_card_keeps_its_whole_comment():
    # Fable review of #601: the comment used to be cut in every archived file
    image = B._stamp_card('DR2026.0').decode()
    assert len(image) == 80 and image.rstrip().endswith(B.STAMP_COMMENT)


def test_existing_gzipped_archive_of_another_size_heals():
    # Codex on #601: the key names the logical content; gzip sizes vary
    import gzip
    logical = _fits_bytes(2)
    row = _row(MOSAIC_KEY, logical, product_type='nircam_mosaic', source_stored_size=len(gzip.compress(logical)),
               component='nircam/cosmos', program_slug=None)
    target = B._copy_target(row)
    o = B.process_member(row, tag=TAG, s3=_s3(**{target: gzip.compress(logical, compresslevel=1) + b''}),
                         bucket=BUCKET)
    assert (o.state, o.action) == ('copied', 'heal')


def test_a_source_replaced_between_head_and_get_is_drift(monkeypatch):
    # Fable review of #601: the 412 of the conditional GET is drift, not error
    body = b'{"x": 1}'
    s3 = _s3(**{JSON_KEY: body})

    class PreconditionFailed(Exception):
        response = {'Error': {'Code': 'PreconditionFailed'}, 'ResponseMetadata': {'HTTPStatusCode': 412}}

    def get_object(**kw):
        raise PreconditionFailed()
    monkeypatch.setattr(s3, 'get_object', get_object)
    o = B.process_member(_row(JSON_KEY, body, product_type='spectrum_json'), tag=TAG, s3=s3, bucket=BUCKET)
    assert (o.state, o.action) == ('failed', 'drift') and 'changed during the read' in o.error
