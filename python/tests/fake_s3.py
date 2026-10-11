"""In-process fake of an S3-compatible object store (boto3 client subset).

``FakeS3Client`` is what ``boto3.client('s3')`` looks like to the storage code
(``campfire/storage/*``, ``campfire/deploy/r2.py`` / ``push.py``): an
in-memory bucket→key→object map behind the client operations those modules
and the data-release work drive. ``FakeS3Transport`` is a tiny
``requests``-compatible session (``put`` / ``get`` / ``head`` / ``delete``)
that serves the URLs ``generate_presigned_url`` mints, so
``upload_files_presigned`` can be exercised end to end without a server.

The error semantics mirror what the real OSN gateway did when measured on
2026-10-11:

* ``put_object(IfNoneMatch='*')`` on an existing key → ``PreconditionFailed`` (412);
* ``copy_object(CopySourceIfMatch=...)`` with a stale ETag → ``PreconditionFailed`` (412);
* ``upload_part_copy`` with ``CopySourceRange`` works;
* every non-last multipart part must be ≥ 5 MiB, enforced at
  ``complete_multipart_upload`` → ``EntityTooSmall`` (400).

Other codes follow S3: ``NoSuchKey`` (404, GET/COPY source), a bare ``404``
for a missing HEAD (S3 returns no body on HEAD, so botocore surfaces the
status as the code), ``NoSuchBucket``, ``NoSuchUpload``, ``InvalidPart``,
``InvalidPartOrder``, ``InvalidRange`` (416), and ``InvalidRequest`` for a
self-copy that changes nothing. ETags are ``"<md5 hex>"`` for single-PUT
objects and the composite ``"<md5 of part md5s>-<n>"`` for multipart ones,
exactly as S3 reports them (quoted).

Errors are raised as ``botocore.exceptions.ClientError`` when botocore is
importable (it is in the campfire env); otherwise a local class with the same
``.response['Error']['Code']`` / ``.operation_name`` shape is used, so code
that catches by shape behaves identically.

A single ``threading.Lock`` guards the store: the presigned-upload path PUTs
from a thread pool.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

try:  # botocore is installed alongside boto3 in the campfire env
    from botocore.exceptions import ClientError
except Exception:  # pragma: no cover - exercised only where boto3 is absent
    class ClientError(Exception):
        """Shape-compatible stand-in for ``botocore.exceptions.ClientError``."""

        MSG_TEMPLATE = (
            "An error occurred ({error_code}) when calling the {operation_name} "
            "operation: {error_message}")

        def __init__(self, error_response, operation_name):
            err = error_response.get("Error", {})
            msg = self.MSG_TEMPLATE.format(
                error_code=err.get("Code", "Unknown"),
                error_message=err.get("Message", "Unknown"),
                operation_name=operation_name)
            super().__init__(msg)
            self.response = error_response
            self.operation_name = operation_name

try:
    from requests import HTTPError as _HTTPError
    from requests.structures import CaseInsensitiveDict as _Headers
except Exception:  # pragma: no cover
    class _HTTPError(IOError):
        def __init__(self, message, response=None):
            super().__init__(message)
            self.response = response

    _Headers = dict  # type: ignore


MIN_PART_SIZE = 5 * 1024 * 1024          # S3: every part but the last ≥ 5 MiB
FAKE_HOST = "fake-s3.invalid"

_STATUS = {
    "NoSuchKey": 404, "NoSuchBucket": 404, "NoSuchUpload": 404, "404": 404,
    "PreconditionFailed": 412, "EntityTooSmall": 400, "InvalidPart": 400,
    "InvalidPartOrder": 400, "InvalidRange": 416, "InvalidRequest": 400,
    "AccessDenied": 403, "SignatureDoesNotMatch": 403, "InvalidArgument": 400,
}


def make_client_error(code: str, operation: str, message: Optional[str] = None,
                      status: Optional[int] = None) -> ClientError:
    """A ``ClientError`` shaped like botocore's: ``e.response['Error']['Code']``."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": message or code},
            "ResponseMetadata": {"HTTPStatusCode": status or _STATUS.get(code, 400)},
        },
        operation,
    )


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _quote_etag(etag: str) -> str:
    return etag if etag.startswith('"') else f'"{etag}"'


def _unquote_etag(etag: Optional[str]) -> Optional[str]:
    return None if etag is None else etag.strip().strip('"')


def _read_body(body: Any) -> bytes:
    if body is None:
        return b""
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode()
    if hasattr(body, "read"):
        data = body.read()
        return data.encode() if isinstance(data, str) else bytes(data)
    # a generator / iterable of chunks (requests accepts those too)
    return b"".join(bytes(c) if not isinstance(c, str) else c.encode() for c in body)


def _parse_copy_source(src) -> tuple[str, str]:
    if isinstance(src, dict):
        return src["Bucket"], src["Key"]
    src = str(src)
    if src.startswith("/"):
        src = src[1:]
    bucket, _, key = src.partition("/")
    return bucket, unquote(key)


def _parse_range(spec: Optional[str], size: int, operation: str) -> tuple[int, int]:
    """``bytes=a-b`` (inclusive) / ``bytes=a-`` / ``bytes=-n`` → (start, end)."""
    if not spec:
        return 0, size - 1
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", spec.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise make_client_error("InvalidArgument", operation, f"bad Range {spec!r}")
    a, b = m.group(1), m.group(2)
    if not a:                       # suffix: last n bytes
        n = int(b)
        return max(size - n, 0), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    if start >= size and size > 0 or start > end:
        raise make_client_error("InvalidRange", operation,
                                "The requested range is not satisfiable")
    return start, min(end, size - 1)


class StreamingBody(io.BytesIO):
    """``get_object()['Body']``: botocore's StreamingBody surface we need."""

    def iter_chunks(self, chunk_size: int = 1024):
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                return
            yield chunk

    iter_lines = None  # not used by the code under test


@dataclass
class _Object:
    body: bytes
    etag: str                          # unquoted
    content_type: Optional[str]
    metadata: dict
    last_modified: datetime
    cache_control: Optional[str] = None
    content_encoding: Optional[str] = None

    @property
    def size(self) -> int:
        return len(self.body)


@dataclass
class _Upload:
    bucket: str
    key: str
    content_type: Optional[str]
    metadata: dict
    cache_control: Optional[str]
    parts: dict = field(default_factory=dict)     # part_number -> (bytes, etag)


class FakeS3Client:
    """In-memory S3 (see module docstring).

    Parameters
    ----------
    buckets : iterable of str, optional
        Buckets that exist up front.
    auto_create : bool
        Create a bucket the first time any operation names it (default
        True). With ``False`` an unknown bucket raises ``NoSuchBucket``.
    clock : callable, optional
        Returns the current tz-aware ``datetime`` (``LastModified``, presign
        expiry). Swap it to freeze or advance time.
    endpoint_url : str
        Reported on ``client.meta.endpoint_url`` and used as the presigned
        URL host.
    """

    def __init__(self, buckets: Iterable[str] = (), *, auto_create: bool = True,
                 clock: Optional[Callable[[], datetime]] = None,
                 endpoint_url: str = f"https://{FAKE_HOST}",
                 region_name: str = "us-east-1"):
        self._store: dict[str, dict[str, _Object]] = {b: {} for b in buckets}
        self._uploads: dict[str, _Upload] = {}
        self._lock = threading.RLock()
        self.auto_create = auto_create
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.endpoint_url = endpoint_url.rstrip("/")
        self.calls: list[tuple[str, dict]] = []        # (operation, kwargs)
        self._signing_key = os.urandom(16)
        self.exceptions = _Exceptions()
        self.meta = _Meta(endpoint_url=self.endpoint_url, region_name=region_name)

    # ------------------------------------------------------------------
    # helpers for tests
    # ------------------------------------------------------------------
    def seed(self, bucket: str, key: str, body: bytes, *, content_type: Optional[str] = None,
             metadata: Optional[dict] = None) -> str:
        """Store *body* directly (no call recorded); returns the unquoted ETag."""
        with self._lock:
            b = self._bucket(bucket, "PutObject")
            obj = _Object(body=bytes(body), etag=_md5(body), content_type=content_type,
                          metadata=dict(metadata or {}), last_modified=self.clock())
            b[key] = obj
            return obj.etag

    def keys(self, bucket: str) -> list[str]:
        with self._lock:
            return sorted(self._store.get(bucket, {}))

    def body(self, bucket: str, key: str) -> bytes:
        return self._get(bucket, key, "GetObject").body

    def exists(self, bucket: str, key: str) -> bool:
        with self._lock:
            return key in self._store.get(bucket, {})

    @property
    def transport(self) -> "FakeS3Transport":
        """A ``requests``-compatible session that serves this fake's presigned URLs."""
        return FakeS3Transport(self)

    def ops(self, operation: Optional[str] = None) -> list[tuple[str, dict]]:
        return [c for c in self.calls if operation is None or c[0] == operation]

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _record(self, op: str, kwargs: dict) -> None:
        self.calls.append((op, {k: v for k, v in kwargs.items() if k != "Body"}))

    def _bucket(self, name: str, operation: str) -> dict[str, _Object]:
        try:
            return self._store[name]
        except KeyError:
            if self.auto_create:
                return self._store.setdefault(name, {})
            raise make_client_error("NoSuchBucket", operation,
                                    "The specified bucket does not exist")

    def _get(self, bucket: str, key: str, operation: str, *, head: bool = False) -> _Object:
        with self._lock:
            b = self._bucket(bucket, operation)
            obj = b.get(key)
        if obj is None:
            if head:
                raise make_client_error("404", operation, "Not Found", status=404)
            raise make_client_error("NoSuchKey", operation, "The specified key does not exist.")
        return obj

    @staticmethod
    def _head_fields(obj: _Object) -> dict:
        d = {
            "ETag": _quote_etag(obj.etag),
            "ContentLength": obj.size,
            "LastModified": obj.last_modified,
            "Metadata": dict(obj.metadata),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }
        if obj.content_type is not None:
            d["ContentType"] = obj.content_type
        if obj.cache_control is not None:
            d["CacheControl"] = obj.cache_control
        if obj.content_encoding is not None:
            d["ContentEncoding"] = obj.content_encoding
        return d

    # ------------------------------------------------------------------
    # bucket ops
    # ------------------------------------------------------------------
    def create_bucket(self, Bucket: str, **kwargs) -> dict:
        self._record("CreateBucket", {"Bucket": Bucket, **kwargs})
        with self._lock:
            self._store.setdefault(Bucket, {})
        return {"Location": f"/{Bucket}", "ResponseMetadata": {"HTTPStatusCode": 200}}

    def list_buckets(self) -> dict:
        self._record("ListBuckets", {})
        return {"Buckets": [{"Name": b} for b in sorted(self._store)]}

    def head_bucket(self, Bucket: str) -> dict:
        self._record("HeadBucket", {"Bucket": Bucket})
        self._bucket(Bucket, "HeadBucket")
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    # ------------------------------------------------------------------
    # object ops
    # ------------------------------------------------------------------
    def put_object(self, Bucket: str, Key: str, Body: Any = b"", ContentType: Optional[str] = None,
                   Metadata: Optional[dict] = None, CacheControl: Optional[str] = None,
                   ContentEncoding: Optional[str] = None, IfNoneMatch: Optional[str] = None,
                   IfMatch: Optional[str] = None, **kwargs) -> dict:
        self._record("PutObject", dict(Bucket=Bucket, Key=Key, ContentType=ContentType,
                                       IfNoneMatch=IfNoneMatch, IfMatch=IfMatch, **kwargs))
        data = _read_body(Body)
        with self._lock:
            b = self._bucket(Bucket, "PutObject")
            existing = b.get(Key)
            if IfNoneMatch is not None:
                if IfNoneMatch != "*":
                    raise make_client_error("NotImplemented", "PutObject",
                                            "IfNoneMatch supports only '*'", status=501)
                if existing is not None:
                    raise make_client_error("PreconditionFailed", "PutObject",
                                            "At least one of the pre-conditions you "
                                            "specified did not hold")
            if IfMatch is not None and (existing is None or
                                        _unquote_etag(IfMatch) != existing.etag):
                raise make_client_error("PreconditionFailed", "PutObject",
                                        "At least one of the pre-conditions you "
                                        "specified did not hold")
            obj = _Object(body=data, etag=_md5(data), content_type=ContentType,
                          metadata=dict(Metadata or {}), last_modified=self.clock(),
                          cache_control=CacheControl, content_encoding=ContentEncoding)
            b[Key] = obj
        return {"ETag": _quote_etag(obj.etag), "ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, Bucket: str, Key: str, Range: Optional[str] = None,
                   IfMatch: Optional[str] = None, IfNoneMatch: Optional[str] = None,
                   **kwargs) -> dict:
        self._record("GetObject", dict(Bucket=Bucket, Key=Key, Range=Range, **kwargs))
        obj = self._get(Bucket, Key, "GetObject")
        if IfMatch is not None and _unquote_etag(IfMatch) != obj.etag:
            raise make_client_error("PreconditionFailed", "GetObject")
        if IfNoneMatch is not None and _unquote_etag(IfNoneMatch) == obj.etag:
            raise make_client_error("304", "GetObject", "Not Modified", status=304)
        out = self._head_fields(obj)
        if Range:
            start, end = _parse_range(Range, obj.size, "GetObject")
            data = obj.body[start:end + 1]
            out["ContentRange"] = f"bytes {start}-{end}/{obj.size}"
            out["ContentLength"] = len(data)
            out["ResponseMetadata"]["HTTPStatusCode"] = 206
        else:
            data = obj.body
        out["Body"] = StreamingBody(data)
        return out

    def head_object(self, Bucket: str, Key: str, IfMatch: Optional[str] = None, **kwargs) -> dict:
        self._record("HeadObject", dict(Bucket=Bucket, Key=Key, **kwargs))
        obj = self._get(Bucket, Key, "HeadObject", head=True)
        if IfMatch is not None and _unquote_etag(IfMatch) != obj.etag:
            raise make_client_error("412", "HeadObject", "Precondition Failed", status=412)
        return self._head_fields(obj)

    def copy_object(self, Bucket: str, Key: str, CopySource, CopySourceIfMatch: Optional[str] = None,
                    CopySourceIfNoneMatch: Optional[str] = None,
                    MetadataDirective: str = "COPY", Metadata: Optional[dict] = None,
                    ContentType: Optional[str] = None, CacheControl: Optional[str] = None,
                    ContentEncoding: Optional[str] = None, **kwargs) -> dict:
        self._record("CopyObject", dict(Bucket=Bucket, Key=Key, CopySource=CopySource,
                                        CopySourceIfMatch=CopySourceIfMatch,
                                        MetadataDirective=MetadataDirective, **kwargs))
        src_bucket, src_key = _parse_copy_source(CopySource)
        with self._lock:
            src = self._get(src_bucket, src_key, "CopyObject")
            if CopySourceIfMatch is not None and _unquote_etag(CopySourceIfMatch) != src.etag:
                raise make_client_error("PreconditionFailed", "CopyObject",
                                        "At least one of the pre-conditions you "
                                        "specified did not hold")
            if CopySourceIfNoneMatch is not None and _unquote_etag(CopySourceIfNoneMatch) == src.etag:
                raise make_client_error("PreconditionFailed", "CopyObject")
            directive = (MetadataDirective or "COPY").upper()
            if directive not in ("COPY", "REPLACE"):
                raise make_client_error("InvalidArgument", "CopyObject",
                                        f"Unknown metadata directive {MetadataDirective!r}")
            if (src_bucket, src_key) == (Bucket, Key) and directive == "COPY":
                raise make_client_error(
                    "InvalidRequest", "CopyObject",
                    "This copy request is illegal because it is trying to copy an "
                    "object to itself without changing the object's metadata, "
                    "storage class, website redirect location or encryption attributes.")
            if directive == "REPLACE":
                meta, ctype = dict(Metadata or {}), ContentType
                cc, enc = CacheControl, ContentEncoding
            else:
                meta, ctype = dict(src.metadata), src.content_type
                cc, enc = src.cache_control, src.content_encoding
            dst = _Object(body=src.body, etag=src.etag, content_type=ctype, metadata=meta,
                          last_modified=self.clock(), cache_control=cc, content_encoding=enc)
            self._bucket(Bucket, "CopyObject")[Key] = dst
        return {
            "CopyObjectResult": {"ETag": _quote_etag(dst.etag), "LastModified": dst.last_modified},
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def delete_object(self, Bucket: str, Key: str, **kwargs) -> dict:
        self._record("DeleteObject", dict(Bucket=Bucket, Key=Key, **kwargs))
        with self._lock:
            self._bucket(Bucket, "DeleteObject").pop(Key, None)   # idempotent, like S3
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def delete_objects(self, Bucket: str, Delete: dict, **kwargs) -> dict:
        self._record("DeleteObjects", dict(Bucket=Bucket, Delete=Delete, **kwargs))
        deleted = []
        with self._lock:
            b = self._bucket(Bucket, "DeleteObjects")
            for entry in Delete.get("Objects", []):
                b.pop(entry["Key"], None)
                deleted.append({"Key": entry["Key"]})
        out = {"Errors": [], "ResponseMetadata": {"HTTPStatusCode": 200}}
        if not Delete.get("Quiet"):
            out["Deleted"] = deleted
        return out

    def list_objects_v2(self, Bucket: str, Prefix: str = "", Delimiter: Optional[str] = None,
                        MaxKeys: int = 1000, ContinuationToken: Optional[str] = None,
                        StartAfter: Optional[str] = None, **kwargs) -> dict:
        self._record("ListObjectsV2", dict(Bucket=Bucket, Prefix=Prefix, Delimiter=Delimiter,
                                           MaxKeys=MaxKeys, ContinuationToken=ContinuationToken,
                                           StartAfter=StartAfter, **kwargs))
        with self._lock:
            b = self._bucket(Bucket, "ListObjectsV2")
            keys = sorted(k for k in b if k.startswith(Prefix or ""))
            snapshot = {k: b[k] for k in keys}
        after = ContinuationToken or StartAfter
        if after:
            keys = [k for k in keys if k > after]
        # Collapse into entries: (sort key, kind, value) — common prefixes sort by
        # their prefix string like S3 does (keys and prefixes interleave).
        entries: list[tuple[str, str]] = []
        seen_prefixes: set[str] = set()
        for k in keys:
            if Delimiter:
                rest = k[len(Prefix or ""):]
                idx = rest.find(Delimiter)
                if idx >= 0:
                    cp = (Prefix or "") + rest[:idx + len(Delimiter)]
                    if cp not in seen_prefixes:
                        seen_prefixes.add(cp)
                        entries.append((cp, "prefix"))
                    continue
            entries.append((k, "key"))
        page = entries[:MaxKeys]
        truncated = len(entries) > MaxKeys
        contents = [{
            "Key": k, "Size": snapshot[k].size, "ETag": _quote_etag(snapshot[k].etag),
            "LastModified": snapshot[k].last_modified, "StorageClass": "STANDARD",
        } for k, kind in page if kind == "key"]
        prefixes = [{"Prefix": k} for k, kind in page if kind == "prefix"]
        out: dict[str, Any] = {
            "Name": Bucket, "Prefix": Prefix or "", "MaxKeys": MaxKeys,
            "KeyCount": len(page), "IsTruncated": truncated,
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }
        if contents:
            out["Contents"] = contents
        if prefixes:
            out["CommonPrefixes"] = prefixes
        if Delimiter:
            out["Delimiter"] = Delimiter
        if ContinuationToken:
            out["ContinuationToken"] = ContinuationToken
        if truncated:
            out["NextContinuationToken"] = page[-1][0]
        return out

    def get_paginator(self, operation_name: str):
        if operation_name != "list_objects_v2":
            raise ValueError(f"fake paginator supports list_objects_v2 only, not {operation_name}")
        return _Paginator(self)

    # ------------------------------------------------------------------
    # multipart
    # ------------------------------------------------------------------
    def create_multipart_upload(self, Bucket: str, Key: str, ContentType: Optional[str] = None,
                                Metadata: Optional[dict] = None, CacheControl: Optional[str] = None,
                                **kwargs) -> dict:
        self._record("CreateMultipartUpload", dict(Bucket=Bucket, Key=Key, ContentType=ContentType,
                                                   **kwargs))
        with self._lock:
            self._bucket(Bucket, "CreateMultipartUpload")
            upload_id = uuid.uuid4().hex
            self._uploads[upload_id] = _Upload(Bucket, Key, ContentType, dict(Metadata or {}),
                                               CacheControl)
        return {"Bucket": Bucket, "Key": Key, "UploadId": upload_id,
                "ResponseMetadata": {"HTTPStatusCode": 200}}

    def _upload(self, upload_id: str, bucket: str, key: str, operation: str) -> _Upload:
        up = self._uploads.get(upload_id)
        if up is None or (up.bucket, up.key) != (bucket, key):
            raise make_client_error("NoSuchUpload", operation,
                                    "The specified upload does not exist. The upload ID "
                                    "may be invalid, or the upload may have been aborted "
                                    "or completed.")
        return up

    def upload_part(self, Bucket: str, Key: str, UploadId: str, PartNumber: int, Body: Any = b"",
                    **kwargs) -> dict:
        self._record("UploadPart", dict(Bucket=Bucket, Key=Key, UploadId=UploadId,
                                        PartNumber=PartNumber, **kwargs))
        if not 1 <= int(PartNumber) <= 10000:
            raise make_client_error("InvalidArgument", "UploadPart",
                                    "Part number must be an integer between 1 and 10000, inclusive")
        data = _read_body(Body)
        with self._lock:
            up = self._upload(UploadId, Bucket, Key, "UploadPart")
            etag = _md5(data)
            up.parts[int(PartNumber)] = (data, etag)
        return {"ETag": _quote_etag(etag), "ResponseMetadata": {"HTTPStatusCode": 200}}

    def upload_part_copy(self, Bucket: str, Key: str, UploadId: str, PartNumber: int, CopySource,
                         CopySourceRange: Optional[str] = None,
                         CopySourceIfMatch: Optional[str] = None, **kwargs) -> dict:
        self._record("UploadPartCopy", dict(Bucket=Bucket, Key=Key, UploadId=UploadId,
                                            PartNumber=PartNumber, CopySource=CopySource,
                                            CopySourceRange=CopySourceRange,
                                            CopySourceIfMatch=CopySourceIfMatch, **kwargs))
        src_bucket, src_key = _parse_copy_source(CopySource)
        with self._lock:
            up = self._upload(UploadId, Bucket, Key, "UploadPartCopy")
            src = self._get(src_bucket, src_key, "UploadPartCopy")
            if CopySourceIfMatch is not None and _unquote_etag(CopySourceIfMatch) != src.etag:
                raise make_client_error("PreconditionFailed", "UploadPartCopy",
                                        "At least one of the pre-conditions you "
                                        "specified did not hold")
            start, end = _parse_range(CopySourceRange, src.size, "UploadPartCopy")
            data = src.body[start:end + 1]
            etag = _md5(data)
            up.parts[int(PartNumber)] = (data, etag)
        return {"CopyPartResult": {"ETag": _quote_etag(etag), "LastModified": self.clock()},
                "ResponseMetadata": {"HTTPStatusCode": 200}}

    def complete_multipart_upload(self, Bucket: str, Key: str, UploadId: str,
                                  MultipartUpload: dict, **kwargs) -> dict:
        self._record("CompleteMultipartUpload", dict(Bucket=Bucket, Key=Key, UploadId=UploadId,
                                                     MultipartUpload=MultipartUpload, **kwargs))
        parts = MultipartUpload.get("Parts") or []
        if not parts:
            raise make_client_error("InvalidRequest", "CompleteMultipartUpload",
                                    "You must specify at least one part")
        with self._lock:
            up = self._upload(UploadId, Bucket, Key, "CompleteMultipartUpload")
            numbers = [int(p["PartNumber"]) for p in parts]
            if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
                raise make_client_error("InvalidPartOrder", "CompleteMultipartUpload",
                                        "The list of parts was not in ascending order. "
                                        "Parts must be ordered by part number.")
            chunks, digests = [], []
            for i, p in enumerate(parts):
                n = int(p["PartNumber"])
                stored = up.parts.get(n)
                if stored is None or _unquote_etag(p.get("ETag")) != stored[1]:
                    raise make_client_error(
                        "InvalidPart", "CompleteMultipartUpload",
                        "One or more of the specified parts could not be found. The "
                        "part may not have been uploaded, or the specified entity tag "
                        "may not match the part's entity tag.")
                data, etag = stored
                if i < len(parts) - 1 and len(data) < MIN_PART_SIZE:
                    raise make_client_error(
                        "EntityTooSmall", "CompleteMultipartUpload",
                        "Your proposed upload is smaller than the minimum allowed size")
                chunks.append(data)
                digests.append(bytes.fromhex(etag))
            body = b"".join(chunks)
            etag = f"{_md5(b''.join(digests))}-{len(parts)}"
            obj = _Object(body=body, etag=etag, content_type=up.content_type,
                          metadata=up.metadata, last_modified=self.clock(),
                          cache_control=up.cache_control)
            self._bucket(Bucket, "CompleteMultipartUpload")[Key] = obj
            del self._uploads[UploadId]
        return {"Bucket": Bucket, "Key": Key, "ETag": _quote_etag(etag),
                "Location": f"{self.endpoint_url}/{Bucket}/{quote(Key)}",
                "ResponseMetadata": {"HTTPStatusCode": 200}}

    def abort_multipart_upload(self, Bucket: str, Key: str, UploadId: str, **kwargs) -> dict:
        self._record("AbortMultipartUpload", dict(Bucket=Bucket, Key=Key, UploadId=UploadId, **kwargs))
        with self._lock:
            self._upload(UploadId, Bucket, Key, "AbortMultipartUpload")
            del self._uploads[UploadId]
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def list_multipart_uploads(self, Bucket: str, Prefix: str = "", **kwargs) -> dict:
        self._record("ListMultipartUploads", dict(Bucket=Bucket, Prefix=Prefix, **kwargs))
        with self._lock:
            ups = [{"Key": u.key, "UploadId": uid} for uid, u in self._uploads.items()
                   if u.bucket == Bucket and u.key.startswith(Prefix or "")]
        return {"Bucket": Bucket, "Uploads": ups, "IsTruncated": False,
                "ResponseMetadata": {"HTTPStatusCode": 200}}

    def list_parts(self, Bucket: str, Key: str, UploadId: str, **kwargs) -> dict:
        self._record("ListParts", dict(Bucket=Bucket, Key=Key, UploadId=UploadId, **kwargs))
        with self._lock:
            up = self._upload(UploadId, Bucket, Key, "ListParts")
            parts = [{"PartNumber": n, "ETag": _quote_etag(e), "Size": len(d)}
                     for n, (d, e) in sorted(up.parts.items())]
        return {"Bucket": Bucket, "Key": Key, "UploadId": UploadId, "Parts": parts,
                "IsTruncated": False, "ResponseMetadata": {"HTTPStatusCode": 200}}

    # ------------------------------------------------------------------
    # managed transfers (boto3 adds these on top of the client API)
    # ------------------------------------------------------------------
    def upload_file(self, Filename, Bucket: str, Key: str, ExtraArgs: Optional[dict] = None,
                    Callback=None, Config=None) -> None:
        with open(Filename, "rb") as f:
            self.upload_fileobj(f, Bucket, Key, ExtraArgs=ExtraArgs, Callback=Callback)

    def upload_fileobj(self, Fileobj, Bucket: str, Key: str, ExtraArgs: Optional[dict] = None,
                       Callback=None, Config=None) -> None:
        data = _read_body(Fileobj)
        extra = dict(ExtraArgs or {})
        self.put_object(Bucket=Bucket, Key=Key, Body=data, **extra)
        self.calls[-1] = ("UploadFile", self.calls[-1][1])
        if Callback is not None:
            Callback(len(data))

    def download_file(self, Bucket: str, Key: str, Filename, ExtraArgs=None, Callback=None,
                      Config=None) -> None:
        self._record("DownloadFile", dict(Bucket=Bucket, Key=Key, Filename=str(Filename)))
        obj = self._get(Bucket, Key, "HeadObject", head=True)   # boto3 HEADs first → 404 code
        with open(Filename, "wb") as f:
            f.write(obj.body)
        if Callback is not None:
            Callback(obj.size)

    def download_fileobj(self, Bucket: str, Key: str, Fileobj, ExtraArgs=None, Callback=None,
                         Config=None) -> None:
        self._record("DownloadFileobj", dict(Bucket=Bucket, Key=Key))
        obj = self._get(Bucket, Key, "HeadObject", head=True)
        Fileobj.write(obj.body)
        if Callback is not None:
            Callback(obj.size)

    # ------------------------------------------------------------------
    # presigning
    # ------------------------------------------------------------------
    _METHODS = {"put_object": "PUT", "get_object": "GET", "head_object": "HEAD",
                "delete_object": "DELETE"}

    def generate_presigned_url(self, ClientMethod: str, Params: Optional[dict] = None,
                               ExpiresIn: int = 3600, HttpMethod: Optional[str] = None) -> str:
        params = dict(Params or {})
        if ClientMethod not in self._METHODS:
            raise ValueError(f"fake presigning supports {sorted(self._METHODS)}, not {ClientMethod!r}")
        bucket, key = params["Bucket"], params["Key"]
        method = (HttpMethod or self._METHODS[ClientMethod]).upper()
        issued = int(self.clock().timestamp())
        content_type = params.get("ContentType") or ""
        sig = self._sign(method, bucket, key, issued, int(ExpiresIn), content_type)
        query = (f"X-Amz-Algorithm=FAKE-HMAC-SHA256&X-Amz-Date={issued}"
                 f"&X-Amz-Expires={int(ExpiresIn)}&X-Amz-Method={method}")
        if content_type:
            query += f"&X-Amz-ContentType={quote(content_type, safe='')}"
        query += f"&X-Amz-Signature={sig}"
        self._record("GeneratePresignedUrl", dict(ClientMethod=ClientMethod, Params=params,
                                                  ExpiresIn=ExpiresIn))
        return f"{self.endpoint_url}/{bucket}/{quote(key, safe='/')}?{query}"

    def _sign(self, method: str, bucket: str, key: str, issued: int, expires: int,
              content_type: str) -> str:
        msg = "\n".join([method, bucket, key, str(issued), str(expires), content_type]).encode()
        return hmac.new(self._signing_key, msg, hashlib.sha256).hexdigest()

    def _authorize(self, method: str, url: str, content_type: Optional[str]) -> tuple[str, str]:
        """Validate a presigned URL for *method*; returns (bucket, key) or raises
        a ``ClientError`` whose code the transport turns into a 403 body."""
        parsed = urlparse(url)
        expected_host = urlparse(self.endpoint_url).netloc
        if parsed.netloc != expected_host:
            raise make_client_error("SignatureDoesNotMatch", method,
                                    f"URL host {parsed.netloc!r} is not this fake "
                                    f"({expected_host!r})")
        path = unquote(parsed.path.lstrip("/"))
        bucket, _, key = path.partition("/")
        q = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            issued, expires = int(q["X-Amz-Date"]), int(q["X-Amz-Expires"])
            signed_method, sig = q["X-Amz-Method"], q["X-Amz-Signature"]
        except KeyError:
            raise make_client_error("AccessDenied", method, "missing presign parameters")
        signed_ct = unquote(q.get("X-Amz-ContentType", ""))
        if signed_method != method.upper():
            raise make_client_error("SignatureDoesNotMatch", method,
                                    f"URL was signed for {signed_method}, not {method}")
        if not hmac.compare_digest(sig, self._sign(signed_method, bucket, key, issued,
                                                   expires, signed_ct)):
            raise make_client_error("SignatureDoesNotMatch", method,
                                    "The request signature we calculated does not match "
                                    "the signature you provided.")
        if int(self.clock().timestamp()) > issued + expires:
            raise make_client_error("AccessDenied", method, "Request has expired")
        if signed_ct and (content_type or "") != signed_ct:
            # The Content-Type is part of the signed headers: a PUT whose header
            # differs from the minted one is a signature mismatch on S3/OSN too.
            raise make_client_error("SignatureDoesNotMatch", method,
                                    f"Content-Type {content_type!r} was not the signed "
                                    f"{signed_ct!r}")
        return bucket, key


class _Paginator:
    def __init__(self, client: FakeS3Client):
        self._c = client

    def paginate(self, **kwargs):
        token = None
        config = kwargs.pop("PaginationConfig", None) or {}
        if "PageSize" in config:
            kwargs["MaxKeys"] = config["PageSize"]
        while True:
            if token:
                kwargs["ContinuationToken"] = token
            page = self._c.list_objects_v2(**kwargs)
            yield page
            token = page.get("NextContinuationToken")
            if not token:
                return


class _Meta:
    def __init__(self, endpoint_url: str, region_name: str):
        self.endpoint_url = endpoint_url
        self.region_name = region_name


class _Exceptions:
    """``client.exceptions.NoSuchKey`` etc. — subclasses of ClientError so both
    ``except ClientError`` and the named form catch the fake's errors."""

    def __init__(self):
        self.ClientError = ClientError
        for name in ("NoSuchKey", "NoSuchBucket", "NoSuchUpload"):
            setattr(self, name, _code_exception(name))


_CODE_EXCEPTIONS: dict[str, type] = {}


def _code_exception(code: str) -> type:
    cls = _CODE_EXCEPTIONS.get(code)
    if cls is None:
        # A metaclass hook so `except client.exceptions.NoSuchKey` matches a
        # plain ClientError carrying that code (botocore's modeled exceptions
        # are real subclasses; here isinstance is decided by the code).
        class _Meta(type):
            def __instancecheck__(cls_, inst):
                return isinstance(inst, ClientError) and (
                    inst.response.get("Error", {}).get("Code") == code)

        cls = _Meta(code, (ClientError,), {})
        _CODE_EXCEPTIONS[code] = cls
    return cls


# ----------------------------------------------------------------------
# requests-compatible transport for presigned URLs
# ----------------------------------------------------------------------

class FakeHTTPResponse:
    """The slice of ``requests.Response`` the transfer code touches."""

    def __init__(self, status_code: int, content: bytes = b"", headers: Optional[dict] = None,
                 url: str = "", reason: str = ""):
        self.status_code = status_code
        self._content = content
        self.headers = _Headers(headers or {})
        self.url = url
        self.reason = reason or {200: "OK", 204: "No Content", 206: "Partial Content",
                                 403: "Forbidden", 404: "Not Found", 412: "Precondition Failed",
                                 416: "Range Not Satisfiable"}.get(status_code, "")
        self.request = None

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        return self._content.decode("utf-8", "replace")

    def json(self):
        import json
        return json.loads(self.text)

    def iter_content(self, chunk_size: int = 1, decode_unicode: bool = False):
        for i in range(0, len(self._content), max(int(chunk_size or 1), 1)):
            yield self._content[i:i + max(int(chunk_size or 1), 1)]

    @property
    def raw(self):
        return io.BytesIO(self._content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            kind = "Client" if self.status_code < 500 else "Server"
            raise _HTTPError(f"{self.status_code} {kind} Error: {self.reason} for url: {self.url}",
                             response=self)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeS3Transport:
    """A ``requests.Session`` look-alike bound to one :class:`FakeS3Client`.

    ``put`` stores the body under the presigned key (recording the
    Content-Type header), ``get`` / ``head`` read it (``Range`` honoured),
    ``delete`` removes it. Any signature / expiry problem is a 403 and a
    missing key a 404, each with an S3-style XML error body, so
    ``raise_for_status()`` behaves as it would against the real gateway.
    ``requests`` (list of ``(method, url, headers)``) is the assertion log.
    """

    def __init__(self, client: FakeS3Client):
        self._c = client
        self.requests: list[tuple[str, str, dict]] = []
        self.headers: dict = {}
        self.closed = False

    # requests.Session plumbing the code may poke at
    def mount(self, prefix, adapter) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def request(self, method: str, url: str, data=None, headers: Optional[dict] = None,
                stream: bool = False, timeout=None, **kwargs) -> FakeHTTPResponse:
        method = method.upper()
        hdrs = _Headers(self.headers)
        hdrs.update(headers or {})
        self.requests.append((method, url, dict(hdrs)))
        content_type = hdrs.get("Content-Type")
        try:
            bucket, key = self._c._authorize(method, url, content_type if method == "PUT" else None)
            if method == "PUT":
                body = _read_body(data)
                resp = self._c.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
                return FakeHTTPResponse(200, b"", {"ETag": resp["ETag"]}, url)
            if method in ("GET", "HEAD"):
                obj = self._c.get_object(Bucket=bucket, Key=key, Range=hdrs.get("Range"))
                out_headers = {
                    "ETag": obj["ETag"], "Content-Length": str(obj["ContentLength"]),
                    "Last-Modified": obj["LastModified"].strftime("%a, %d %b %Y %H:%M:%S GMT"),
                    "Accept-Ranges": "bytes",
                }
                if obj.get("ContentType"):
                    out_headers["Content-Type"] = obj["ContentType"]
                if obj.get("ContentRange"):
                    out_headers["Content-Range"] = obj["ContentRange"]
                status = obj["ResponseMetadata"]["HTTPStatusCode"]
                content = obj["Body"].read() if method == "GET" else b""
                return FakeHTTPResponse(status, content, out_headers, url)
            if method == "DELETE":
                self._c.delete_object(Bucket=bucket, Key=key)
                return FakeHTTPResponse(204, b"", {}, url)
            return FakeHTTPResponse(405, b"", {}, url, reason="Method Not Allowed")
        except ClientError as e:
            err = e.response.get("Error", {})
            code = err.get("Code", "InternalError")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode") or _STATUS.get(code, 400)
            body = (f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
                    f'<Message>{err.get("Message", "")}</Message></Error>').encode()
            if method == "HEAD":
                body = b""
            return FakeHTTPResponse(status, body, {"Content-Type": "application/xml"}, url)

    def put(self, url, data=None, **kwargs) -> FakeHTTPResponse:
        return self.request("PUT", url, data=data, **kwargs)

    def get(self, url, **kwargs) -> FakeHTTPResponse:
        return self.request("GET", url, **kwargs)

    def head(self, url, **kwargs) -> FakeHTTPResponse:
        return self.request("HEAD", url, **kwargs)

    def delete(self, url, **kwargs) -> FakeHTTPResponse:
        return self.request("DELETE", url, **kwargs)


__all__ = [
    "FakeS3Client", "FakeS3Transport", "FakeHTTPResponse", "StreamingBody",
    "ClientError", "make_client_error", "MIN_PART_SIZE", "FAKE_HOST",
]
