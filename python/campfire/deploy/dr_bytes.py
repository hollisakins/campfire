"""
Data-release byte pass (P4c, docs/design-data-releases-impl-plan.md §6).

``campfire dr archive --tag T`` makes every member of a frozen release exist
under ``archive/`` on the data bucket, so the release keeps resolving after the
live objects are redeployed or removed:

* **NIRSpec FITS finals** (``nirspec_spec``, ``nirspec_lines``) are downloaded,
  verified against the frozen content hash, stamped (``CMPFRDR = '<tag>'`` in
  the primary header, a raw card insert that grows the header by one 2880-byte
  block only when it has no free card slot, so every data byte stays where it
  was) and uploaded with ``If-None-Match: *`` to
  ``archive/<sha256 of the stamped bytes>/<relpath>``. The science hash is
  recomputed on the stamped bytes and must equal the frozen one (the stamp is
  a volatile card, so it cannot change it). No multipart upload anywhere: the
  largest final is 8 MB.
* **Everything else** (sidecar JSON, zfit, line-fit JSON, mosaics) is archived
  unstamped to ``archive/<content_hash>/<relpath>``. A live source is always
  verified against the frozen content hash first (its logical bytes, gunzipped
  for a ``.fits.gz`` mosaic, read under ``If-Match`` on the ETag just HEADed):
  a modification time is no proof of identity, because ``campfire push``
  writes an object before it registers its hash. Objects up to 64 MiB are
  then uploaded from the verified body; larger ones are copied server-side
  with ``CopySourceIfMatch`` on the same ETag (OSN answers 412 when the object
  changed in between), so the bytes archived are the bytes verified.
  CopyObject's 5 GB ceiling is checked up front; no stored mosaic exceeds
  4.3 GB today.
* **Reuse (D-P4-2)**: a FITS another release already stamped from the same
  science is reused (the first release to archive a file owns its stamp; two
  passes racing on the same file may each stamp their own copy, which costs one
  duplicate object and nothing else); an unstamped object already
  archived with the frozen content hash (retain-on-supersede, an earlier
  release) is reused as it is.
* **Drift**: when the live object no longer holds the frozen bytes and no
  retained copy exists, the member is marked ``failed`` with the reason; the
  bytes the release named are gone and the release cannot become ``ready``.

The database side is three RPCs: ``dr_bytes_pending`` (a keyset page of work,
with the source of each member's frozen bytes resolved server-side),
``dr_bytes_mark`` (a batch of outcomes, the archive registry rows, one deploy
event, export invalidation, the derived state) and ``dr_bytes_summary``.
Every step is idempotent: a crash after a PUT is healed by the existence check
on the next run (the archive key is a function of the bytes).
"""
from __future__ import annotations

import functools
import hashlib
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from campfire_layout import LayoutError, archive_key, parse_key

STAMPED_PRODUCT_TYPES = frozenset({'nirspec_spec', 'nirspec_lines'})
STAMP_KEYWORD = 'CMPFRDR'
STAMP_COMMENT = 'CAMPFIRE data release that first archived it'   # fits the card for tags up to 12 chars
COPY_LIMIT_BYTES = 5 * 1000 ** 3          # S3 CopyObject: 5 GB per object
VERIFY_LIMIT_BYTES = 64 * 1024 * 1024     # live copies verified by download (else streamed) up to this
BLOCK = 2880
CARD = 80
_TAG_RE = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
_END_CARD = b'END' + b' ' * 77


class StampError(ValueError):
    """The FITS bytes cannot be stamped safely (not FITS, CHECKSUM present, ...)."""


# ---------------------------------------------------------------------------
# The header stamp
# ---------------------------------------------------------------------------

_STAMP_CARD_LOCK = threading.Lock()


def _stamp_card(tag: str) -> bytes:
    # one card per tag, built once under a lock: warnings.catch_warnings is not
    # thread-safe and the pass stamps from a thread pool (lru_cache alone does
    # not coalesce concurrent first calls; Codex on #601)
    with _STAMP_CARD_LOCK:
        return _stamp_card_cached(tag)


@functools.lru_cache(maxsize=64)
def _stamp_card_cached(tag: str) -> bytes:
    import warnings

    from astropy.io import fits
    with warnings.catch_warnings():
        # a long tag leaves no room for the whole comment: astropy truncates it
        warnings.simplefilter('ignore', fits.verify.VerifyWarning)
        image = fits.Card(STAMP_KEYWORD, tag, STAMP_COMMENT).image
    if len(image) != CARD:  # a long tag would need CONTINUE cards; tags are short
        raise StampError(f"stamp card for {tag!r} does not fit one card")
    return image.encode('ascii')


def primary_header_length(data: bytes) -> tuple[int, int]:
    """(index of the END card, header length in bytes incl. padding) of the primary HDU."""
    if not data.startswith(b'SIMPLE  ='):
        raise StampError('not a FITS file (no SIMPLE card)')
    n = 0
    while True:
        card = data[n * CARD:(n + 1) * CARD]
        if len(card) < CARD:
            raise StampError('primary header has no END card')
        if card == _END_CARD or card[:8] == b'END     ':
            end = n
            break
        n += 1
    length = -(-((end + 1) * CARD) // BLOCK) * BLOCK
    if len(data) < length:
        raise StampError('file shorter than its primary header')
    return end, length


def stamp_fits(data: bytes, tag: str) -> bytes:
    """Return *data* with ``CMPFRDR = '<tag>'`` in the primary header.

    A raw card edit: an existing CMPFRDR card is replaced in place; otherwise
    the card goes right before END, and the header grows by one 2880-byte
    block only when END sat in the last slot of its block. Everything after the
    primary header is copied verbatim. A primary header carrying CHECKSUM is
    refused (the stamp would invalidate it; no pipeline writer emits one).
    """
    if not _TAG_RE.match(tag or ''):
        raise StampError(f'invalid release tag {tag!r}')
    end, length = primary_header_length(data)
    cards = [data[i * CARD:(i + 1) * CARD] for i in range(end)]
    if any(c[:8] == b'CHECKSUM' for c in cards):
        raise StampError('primary header carries CHECKSUM; stamping would invalidate it')
    new = _stamp_card(tag)
    existing = [i for i, c in enumerate(cards) if c[:8] == STAMP_KEYWORD.ljust(8).encode()]
    if existing:
        cards[existing[0]] = new
    else:
        cards.append(new)
    header = b''.join(cards) + _END_CARD
    new_length = -(-len(header) // BLOCK) * BLOCK
    header = header + b' ' * (new_length - len(header))
    return header + data[length:]


def sha256_hex(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _stored_compressed(storage_key: str) -> bool:
    """True when the layout stores this key gzipped (``compressed_suffixes``)."""
    from campfire_layout import is_compressed_key
    return is_compressed_key(storage_key)


INFLATE_OUT_BYTES = 8 << 20   # cap on any one decompressed piece


def _inflate_into(h, d, data) -> None:
    """Feed *data* through decompressor *d* into hash *h*, never producing more
    than INFLATE_OUT_BYTES at a time: a NaN-heavy mosaic can compress 100:1, so
    an unbounded ``d.decompress(chunk)`` would materialize the whole expansion
    of each input chunk per worker (Codex on #601)."""
    buf = data
    while buf:
        out = d.decompress(buf, INFLATE_OUT_BYTES)
        h.update(out)
        buf = d.unconsumed_tail
        # bytes after the end of the gzip stream (a second concatenated
        # member, or garbage) would otherwise be accepted unhashed: the hash
        # must cover the whole stored object (Codex on #601)
        if d.eof and d.unused_data:
            raise ValueError('trailing data after the gzip stream (concatenated members are not accepted)')
    # drain anything still buffered internally, in bounded pieces too
    while not d.eof:
        out = d.decompress(b'', INFLATE_OUT_BYTES)
        if not out:
            break
        h.update(out)


def logical_sha256(stored: bytes, *, compressed: bool) -> str:
    """sha256 of the logical bytes: the stored body, gunzipped first when the
    product is stored transport-compressed (streamed, never fully inflated)."""
    if not compressed:
        return sha256_hex(stored)
    import zlib
    h = hashlib.sha256()
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    view = memoryview(stored)
    for i in range(0, len(view), 1 << 20):
        _inflate_into(h, d, bytes(view[i:i + (1 << 20)]))
    h.update(d.flush())
    if not d.eof:
        raise ValueError('truncated gzip body')
    if d.unused_data:
        raise ValueError('trailing data after the gzip stream (concatenated members are not accepted)')
    return 'sha256:' + h.hexdigest()


def science_hash_of(data: bytes) -> str:
    """The pipeline's science hash over in-memory bytes (it reads a path)."""
    from campfire_pipeline.metadata.science_hash import science_hash
    fd, path = tempfile.mkstemp(suffix='.fits')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        return science_hash(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# One member
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    storage_key: str
    state: str                          # copied | stamped | failed
    action: str                         # stamp | copy | reuse-stamp | reuse-copy | heal | drift | error
    archive_key: Optional[str] = None
    stamped_hash: Optional[str] = None
    error: Optional[str] = None
    registry: Optional[dict] = None
    bytes_moved: int = 0
    science_hash: Optional[str] = None   # computed here for a member frozen without one

    def as_result(self) -> dict:
        r = {'storage_key': self.storage_key, 'state': self.state}
        if self.archive_key:
            r['archive_key'] = self.archive_key
        if self.stamped_hash:
            r['stamped_hash'] = self.stamped_hash
        if self.error:
            r['error'] = self.error
        if self.registry is not None:
            r['registry'] = self.registry
        if self.science_hash:
            r['science_hash'] = self.science_hash
        return r


def _status_code(exc: Exception) -> Optional[int]:
    from campfire.deploy.retain import _status_code as code
    return code(exc)


def _head(s3, bucket: str, key: str) -> Optional[dict]:
    """HEAD *key*; None when it does not exist (other errors raise)."""
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001 - botocore ClientError and friends
        if _status_code(e) == 404:
            return None
        raise


def _check_existing(s3, bucket: str, key: str, size: Optional[int]) -> Optional[str]:
    """None when *key* exists (with *size*, when known); else what is wrong."""
    head = _head(s3, bucket, key)
    if head is None:
        return f'{key} is registered but missing from the bucket'
    if size is not None and int(head.get('ContentLength', -1)) != int(size):
        return f"{key} is {head.get('ContentLength')} bytes, the registry says {size}"
    return None


def _registry(row: dict, **overrides) -> dict:
    reg = dict(row.get('registry') or {})
    reg.pop('retained_key', None)
    reg.update(overrides)
    return {k: v for k, v in reg.items() if v is not None}


def plan_action(row: dict) -> str:
    """What the pass will do with one pending row (no I/O; also the --dry-run report)."""
    if row.get('backend') != 'osn' or row.get('bucket') != 'data':
        return 'error'
    if row['product_type'] in STAMPED_PRODUCT_TYPES:
        if row.get('reuse_archive_key'):
            return 'reuse-stamp'
        return 'stamp' if row.get('source_key') else 'drift'
    if not row.get('source_key'):
        return 'drift'
    try:
        target = _copy_target(row)
    except LayoutError:
        return 'error'
    if row.get('source_root') == 'archive' and row['source_key'] == target:
        return 'reuse-copy'
    return 'copy'


def _copy_target(row: dict) -> str:
    pk = parse_key(row['storage_key'])
    return archive_key(pk.product_type, pk.scope, pk.filename, content_hash=row['content_hash'])


def process_member(row: dict, *, tag: str, s3, bucket: str,
                   verify_science: bool = True) -> Outcome:
    """Make one member's bytes exist under archive/ and say how."""
    key = row['storage_key']
    action = plan_action(row)
    try:
        if action == 'error':
            if row.get('backend') != 'osn' or row.get('bucket') != 'data':
                return Outcome(key, 'failed', 'error',
                               error=f"member lives on {row.get('backend')}/{row.get('bucket')}; only osn/data is archived")
            return Outcome(key, 'failed', 'error', error='storage key does not parse as an archivable product')
        if action == 'drift':
            return Outcome(key, 'failed', 'drift',
                           error='frozen bytes are gone: the live object changed (or was revoked) since '
                                 'the freeze and no archived copy of the frozen content exists')
        if action == 'reuse-stamp':
            # every candidate, earliest archived first: the first whose object
            # exists is reused (the first release to archive a file owns its
            # stamp); a registry row is not proof the object exists, so each is
            # HEAD-checked and a missing one falls through (Codex on #601)
            cands = row.get('reuse_candidates') or [
                {'archive_key': row['reuse_archive_key'], 'stamped_hash': row['reuse_stamped_hash'],
                 'size_bytes': row.get('reuse_size_bytes')}]
            problems = []
            for c in cands:
                problem = _check_existing(s3, bucket, c['archive_key'], c.get('size_bytes'))
                if problem is None:
                    return Outcome(key, 'stamped', 'reuse-stamp', archive_key=c['archive_key'],
                                   stamped_hash=c['stamped_hash'])
                problems.append(problem)
            problem = '; '.join(problems)
            if not row.get('source_key'):
                return Outcome(key, 'failed', 'drift',
                               error=f"stamped object to reuse {problem}, and the frozen bytes are gone")
            # stamp afresh from the frozen bytes (a new object under this tag)
            return _stamp_member(row, tag=tag, s3=s3, bucket=bucket, verify_science=verify_science)
        if action == 'reuse-copy':
            problem = _check_existing(s3, bucket, row['source_key'], row.get('source_stored_size'))
            if problem is not None:
                return Outcome(key, 'failed', 'drift',
                               error=f"archived copy {problem} (registry/bucket drift: run `campfire verify --cloud`)")
            return Outcome(key, 'copied', 'reuse-copy', archive_key=row['source_key'])
        if action == 'stamp':
            return _stamp_member(row, tag=tag, s3=s3, bucket=bucket, verify_science=verify_science)
        return _copy_member(row, s3=s3, bucket=bucket)
    except StampError as e:
        return Outcome(key, 'failed', 'error', error=f'stamp: {e}')
    except Exception as e:  # noqa: BLE001 - one member never stops the pass
        return Outcome(key, 'failed', 'error', error=f'{type(e).__name__}: {e}'[:2000])


def _stamp_member(row: dict, *, tag: str, s3, bucket: str, verify_science: bool) -> Outcome:
    key, src = row['storage_key'], row['source_key']
    body = s3.get_object(Bucket=bucket, Key=src)['Body'].read()
    got = sha256_hex(body)
    if got != row['content_hash']:
        return Outcome(key, 'failed', 'drift',
                       error=f'source {src} holds {got}, the release froze {row["content_hash"]}')
    stamped = stamp_fits(body, tag)
    stamped_hash = sha256_hex(stamped)
    computed = None
    if verify_science:
        expected = row.get('science_hash')
        if not expected:
            expected = computed = science_hash_of(body)
        after = science_hash_of(stamped)
        if after != expected:
            return Outcome(key, 'failed', 'error',
                           error=f'science hash changed by the stamp ({expected} -> {after})')
    pk = parse_key(key)
    target = archive_key(pk.product_type, pk.scope, pk.filename, content_hash=stamped_hash)
    reg = _registry(row, size_bytes=len(stamped), stored_size_bytes=None,
                    content_type=(row.get('registry') or {}).get('content_type') or 'application/fits')
    try:
        s3.put_object(Bucket=bucket, Key=target, Body=stamped, IfNoneMatch='*',
                      ContentType=reg['content_type'])
    except Exception as e:  # noqa: BLE001
        if _status_code(e) != 412:
            raise
        # Already there (a crash after the PUT, or a concurrent run): the key
        # is the hash of these exact bytes, so a matching size is the same object.
        head = _head(s3, bucket, target)
        if head is None or int(head.get('ContentLength', -1)) != len(stamped):
            return Outcome(key, 'failed', 'error', error=f'{target} exists with different bytes')
        return Outcome(key, 'stamped', 'heal', archive_key=target, stamped_hash=stamped_hash,
                       registry=reg, science_hash=computed)
    return Outcome(key, 'stamped', 'stamp', archive_key=target, stamped_hash=stamped_hash,
                   registry=reg, bytes_moved=len(body) + len(stamped), science_hash=computed)


def _copy_member(row: dict, *, s3, bucket: str) -> Outcome:
    key, src = row['storage_key'], row['source_key']
    target = _copy_target(row)
    stored = row.get('source_stored_size')
    if stored is not None and int(stored) > COPY_LIMIT_BYTES:
        return Outcome(key, 'failed', 'error',
                       error=f'{int(stored):,} bytes exceeds the 5 GB CopyObject limit (UploadPartCopy not implemented)')
    reg = _registry(row, size_bytes=row.get('size_bytes'))
    existing = _head(s3, bucket, target)
    if existing is not None:
        # The archive key names the LOGICAL content; a gzipped product's stored
        # size depends on the compressor, so it is compared only for plain ones
        # (Codex on #601).
        if (stored is not None and not _stored_compressed(row['storage_key'])
                and int(existing.get('ContentLength', -1)) != int(stored)):
            return Outcome(key, 'failed', 'error', error=f'{target} exists with a different size')
        if _stored_compressed(row['storage_key']) and existing.get('ContentLength') is not None:
            # register what is actually there (the live object may have been
            # recompressed to another size since; Codex on #601)
            reg = {**reg, 'stored_size_bytes': int(existing['ContentLength'])}
        return Outcome(key, 'copied', 'heal', archive_key=target, registry=reg)
    head = _head(s3, bucket, src)
    if head is None:
        return Outcome(key, 'failed', 'drift', error=f'source {src} is missing in the bucket')
    if stored is not None and int(head.get('ContentLength', -1)) != int(stored):
        return Outcome(key, 'failed', 'drift',
                       error=f"source {src} is {head.get('ContentLength')} bytes, the registry says {stored}")
    # the stored size may be unknown (an old .fits.gz row): the ceiling is
    # checked against what the store reports (Codex on #601)
    if int(head.get('ContentLength') or 0) > COPY_LIMIT_BYTES:
        return Outcome(key, 'failed', 'error',
                       error=f"{int(head['ContentLength']):,} bytes exceeds the 5 GB CopyObject limit "
                             f"(UploadPartCopy not implemented)")
    etag = (head.get('ETag') or '').strip('"')
    if row.get('source_root') == 'live':
        # A live object may hold bytes the registry never recorded: `campfire
        # push` writes the object before it registers the hash, so an upload
        # that has not flushed (or died) leaves replacement bytes under the
        # old hash, with any modification time (Codex on #601). Every live
        # source is verified against the frozen hash before it is archived.
        # Small objects are verified by their sha256 and uploaded; large ones
        # by a streamed hash under If-Match on this ETag, then copied
        # server-side bound to the SAME ETag, so the bytes copied are the
        # bytes verified.
        size = int(head.get('ContentLength') or 0)
        if size > VERIFY_LIMIT_BYTES:
            try:
                got = _stream_logical_sha256(s3, bucket, src, etag,
                                             compressed=_stored_compressed(row['storage_key']))
            except Exception as e:  # noqa: BLE001
                if _status_code(e) == 412:   # replaced between the HEAD and the GET
                    return Outcome(key, 'failed', 'drift', error=f'source {src} changed during the read')
                raise
            if got != row['content_hash']:
                return Outcome(key, 'failed', 'drift',
                               error=f'source {src} holds {got}, the release froze {row["content_hash"]}')
            try:
                s3.copy_object(Bucket=bucket, Key=target, CopySource={'Bucket': bucket, 'Key': src},
                               CopySourceIfMatch=etag, MetadataDirective='COPY')
            except Exception as e:  # noqa: BLE001
                if _status_code(e) == 412:
                    return Outcome(key, 'failed', 'drift', error=f'source {src} changed during the copy')
                raise
            return Outcome(key, 'copied', 'copy-verified', archive_key=target, registry=reg,
                           bytes_moved=2 * size)
        try:
            body = s3.get_object(Bucket=bucket, Key=src, IfMatch=etag)['Body'].read()
        except Exception as e:  # noqa: BLE001
            if _status_code(e) == 412:       # replaced between the HEAD and the GET
                return Outcome(key, 'failed', 'drift', error=f'source {src} changed during the read')
            raise
        # The registry hashes the LOGICAL bytes: a transport-compressed product
        # (mosaic FITS stored as .fits.gz) is hashed uncompressed (Codex on #601).
        got = logical_sha256(body, compressed=_stored_compressed(row['storage_key']))
        if got != row['content_hash']:
            return Outcome(key, 'failed', 'drift',
                           error=f'source {src} holds {got}, the release froze {row["content_hash"]}')
        try:
            s3.put_object(Bucket=bucket, Key=target, Body=body, IfNoneMatch='*',
                          ContentType=reg.get('content_type') or 'application/octet-stream')
        except Exception as e:  # noqa: BLE001
            if _status_code(e) != 412:
                raise
            # another run created it first, maybe with another gzip encoding of
            # the same content: register the winner's size (Codex on #601)
            winner = _head(s3, bucket, target)
            if winner is not None and winner.get('ContentLength') is not None:
                reg = {**reg, 'stored_size_bytes': int(winner['ContentLength'])}
            return Outcome(key, 'copied', 'heal', archive_key=target, registry=reg)
        return Outcome(key, 'copied', 'copy-verified', archive_key=target, registry=reg,
                       bytes_moved=2 * len(body))
    try:
        s3.copy_object(Bucket=bucket, Key=target, CopySource={'Bucket': bucket, 'Key': src},
                       CopySourceIfMatch=etag, MetadataDirective='COPY')
    except Exception as e:  # noqa: BLE001
        if _status_code(e) == 412:
            return Outcome(key, 'failed', 'drift', error=f'source {src} changed during the copy')
        raise
    return Outcome(key, 'copied', 'copy', archive_key=target, registry=reg,
                   bytes_moved=int(head.get('ContentLength') or 0))


def _stream_logical_sha256(s3, bucket: str, key: str, etag: str, *, compressed: bool,
                           chunk: int = 8 << 20) -> str:
    """sha256 of an object's logical bytes, streamed (never held in memory),
    under If-Match on *etag*; gunzipped on the fly when stored compressed."""
    import zlib
    body = s3.get_object(Bucket=bucket, Key=key, IfMatch=etag)['Body']
    h = hashlib.sha256()
    d = zlib.decompressobj(16 + zlib.MAX_WBITS) if compressed else None
    while True:
        piece = body.read(chunk)
        if not piece:
            break
        if d is not None:
            _inflate_into(h, d, piece)
        else:
            h.update(piece)
    if d is not None:
        h.update(d.flush())
        if not d.eof:
            raise ValueError(f'{key}: truncated gzip body')
        if d.unused_data:
            raise ValueError(f'{key}: trailing data after the gzip stream')
    return 'sha256:' + h.hexdigest()


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

@dataclass
class PassReport:
    tag: str
    dry_run: bool = False
    seen: int = 0
    by_action: dict = field(default_factory=dict)
    by_state: dict = field(default_factory=dict)
    bytes_moved: int = 0
    failures: list = field(default_factory=list)
    state: Optional[str] = None

    def count(self, outcome: Outcome) -> None:
        self.by_action[outcome.action] = self.by_action.get(outcome.action, 0) + 1
        self.by_state[outcome.state] = self.by_state.get(outcome.state, 0) + 1
        self.bytes_moved += outcome.bytes_moved
        if outcome.state == 'failed' and len(self.failures) < 20:
            self.failures.append({'storage_key': outcome.storage_key, 'error': outcome.error})

    def as_dict(self) -> dict:
        return {'tag': self.tag, 'dry_run': self.dry_run, 'seen': self.seen,
                'by_action': self.by_action, 'by_state': self.by_state,
                'bytes_moved': self.bytes_moved, 'failures': self.failures, 'state': self.state}


def fetch_pending(client, tag: str, *, after: Optional[str] = None, limit: int = 500,
                  include_failed: bool = False, prefix: Optional[str] = None) -> list[dict]:
    resp = client.rpc('dr_bytes_pending', {
        'p_tag': tag, 'p_after': after, 'p_limit': limit,
        'p_include_failed': include_failed, 'p_prefix': prefix,
    }).execute()
    return list(resp.data or [])


def mark_results(client, tag: str, results: list[dict]) -> dict:
    resp = client.rpc('dr_bytes_mark', {'p_tag': tag, 'p_results': results}).execute()
    return resp.data or {}


def fetch_summary(client, tag: str) -> dict:
    return client.rpc('dr_bytes_summary', {'p_tag': tag}).execute().data or {}


def run_byte_pass(client, s3, bucket: str, tag: str, *, workers: int = 8, page_size: int = 500,
                  mark_batch: int = 200, limit: Optional[int] = None, include_failed: bool = False,
                  prefix: Optional[str] = None, dry_run: bool = False, verify_science: bool = True,
                  log: Callable[[str], None] = print) -> PassReport:
    """Walk the pending members of *tag* and archive their bytes.

    Pages come from ``dr_bytes_pending`` (keyset on storage_key, so rows the
    pass marks never shift the cursor); each page is processed on a thread
    pool and its outcomes are recorded in batches of *mark_batch*. A failing
    member is marked ``failed`` and the pass continues; ``--retry-failed``
    (``include_failed``) takes them up again on a later run.
    """
    report = PassReport(tag=tag, dry_run=dry_run)
    need_pipeline = verify_science and not dry_run
    after: Optional[str] = None
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        while True:
            want = page_size if limit is None else min(page_size, limit - report.seen)
            if want <= 0:
                break
            rows = fetch_pending(client, tag, after=after, limit=want,
                                 include_failed=include_failed, prefix=prefix)
            if not rows:
                break
            after = rows[-1]['storage_key']
            report.seen += len(rows)
            if dry_run:
                for r in rows:
                    a = plan_action(r)
                    report.by_action[a] = report.by_action.get(a, 0) + 1
                continue
            if need_pipeline and any(plan_action(r) == 'stamp' for r in rows):
                # checked once, before the first stamp, so a sidecar-only run
                # (or one on a machine without the pipeline) is not blocked
                try:
                    import campfire_pipeline.metadata.science_hash  # noqa: F401
                except ImportError as e:
                    raise RuntimeError('stamping verifies science hashes with campfire-pipeline; '
                                       'install it (pip install -e ./pipeline) on this machine') from e
                need_pipeline = False
            outcomes = list(pool.map(
                lambda r: process_member(r, tag=tag, s3=s3, bucket=bucket, verify_science=verify_science),
                rows))
            for o in outcomes:
                with lock:
                    report.count(o)
            for i in range(0, len(outcomes), mark_batch):
                # each outcome names the frozen content it is for, so a mark
                # never lands on a re-frozen member of another generation
                batch = [{**o.as_result(), 'content_hash': r['content_hash']}
                         for r, o in zip(rows[i:i + mark_batch], outcomes[i:i + mark_batch])]
                res = mark_results(client, tag, batch)
                report.state = res.get('state', report.state)
            log(f"  {report.seen:,} members: " +
                ', '.join(f"{k} {v:,}" for k, v in sorted(report.by_state.items())) +
                f" ({report.bytes_moved / 1e9:.2f} GB moved)")
            if len(rows) < want:
                break
    return report


def format_summary(summary: dict) -> str:
    by = summary.get('by_state') or {}
    lines = [f"{summary.get('tag')}: state {summary.get('state')}, {int(summary.get('manifest') or 0):,} manifest files"]
    for st in ('pending', 'failed', 'copied', 'stamped'):
        if st in by:
            lines.append(f"  {st:<8} {int(by[st]['count']):>9,}  {int(by[st]['bytes']) / 1e9:10.2f} GB")
    for f in summary.get('failed_sample') or []:
        lines.append(f"  ✗ {f['storage_key']}: {f['error']}")
    return '\n'.join(lines)


def format_report(report: PassReport) -> str:
    if report.dry_run:
        head = f"Dry run on {report.tag}: {report.seen:,} pending member(s)"
        return '\n'.join([head] + [f"  {k:<12} {v:>9,}" for k, v in sorted(report.by_action.items())])
    lines = [f"Byte pass on {report.tag}: {report.seen:,} member(s) processed, "
             f"{report.bytes_moved / 1e9:.2f} GB moved; release state {report.state}"]
    lines += [f"  {k:<12} {v:>9,}" for k, v in sorted(report.by_action.items())]
    for f in report.failures:
        lines.append(f"  ✗ {f['storage_key']}: {f['error']}")
    return '\n'.join(lines)
