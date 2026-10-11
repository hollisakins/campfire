"""Behavioural pins for the in-process S3 fake (``tests/fake_s3.py``).

The conditional-write and multipart semantics here are the ones measured
against the real OSN gateway on 2026-10-11 (conditional PUT 412, conditional
copy 412, UploadPartCopy with a range, the 5 MiB minimum enforced at
CompleteMultipartUpload); the rest follows S3's documented behaviour.
"""
import gzip
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from tests.fake_s3 import MIN_PART_SIZE, ClientError, FakeS3Client


def _code(exc_info) -> str:
    return exc_info.value.response["Error"]["Code"]


def _status(exc_info) -> int:
    return exc_info.value.response["ResponseMetadata"]["HTTPStatusCode"]


# --- errors are botocore's ----------------------------------------------------

def test_errors_are_botocore_client_errors(fake_s3):
    botocore = pytest.importorskip("botocore.exceptions")
    with pytest.raises(botocore.ClientError) as ei:
        fake_s3.get_object(Bucket="b", Key="missing")
    assert ei.value.operation_name == "GetObject"
    assert "NoSuchKey" in str(ei.value) and "GetObject" in str(ei.value)
    assert ClientError is botocore.ClientError


# --- put / get / head ---------------------------------------------------------

def test_put_get_head_round_trip(fake_s3):
    body = b"spectrum bytes" * 100
    resp = fake_s3.put_object(Bucket="data", Key="a/b.fits", Body=body,
                              ContentType="application/fits", Metadata={"x": "1"})
    md5 = hashlib.md5(body).hexdigest()
    assert resp["ETag"] == f'"{md5}"'

    head = fake_s3.head_object(Bucket="data", Key="a/b.fits")
    assert head["ETag"] == f'"{md5}"'
    assert head["ContentLength"] == len(body)
    assert head["ContentType"] == "application/fits"
    assert head["Metadata"] == {"x": "1"}
    assert isinstance(head["LastModified"], datetime) and head["LastModified"].tzinfo is not None

    got = fake_s3.get_object(Bucket="data", Key="a/b.fits")
    assert got["Body"].read() == body
    assert got["ContentLength"] == len(body) and got["ETag"] == head["ETag"]
    # file-like and str bodies are accepted too
    import io
    fake_s3.put_object(Bucket="data", Key="f", Body=io.BytesIO(b"xyz"))
    assert fake_s3.body("data", "f") == b"xyz"


def test_get_object_range(fake_s3):
    fake_s3.seed("data", "k", bytes(range(100)))
    part = fake_s3.get_object(Bucket="data", Key="k", Range="bytes=10-19")
    assert part["Body"].read() == bytes(range(10, 20))
    assert part["ContentRange"] == "bytes 10-19/100" and part["ContentLength"] == 10
    assert part["ResponseMetadata"]["HTTPStatusCode"] == 206
    assert fake_s3.get_object(Bucket="data", Key="k", Range="bytes=-5")["Body"].read() == bytes(range(95, 100))
    with pytest.raises(ClientError) as ei:
        fake_s3.get_object(Bucket="data", Key="k", Range="bytes=500-600")
    assert _code(ei) == "InvalidRange" and _status(ei) == 416


def test_missing_key_codes_match_s3(fake_s3):
    with pytest.raises(ClientError) as ei:
        fake_s3.get_object(Bucket="data", Key="nope")
    assert _code(ei) == "NoSuchKey" and _status(ei) == 404
    # HEAD carries no body, so botocore reports the bare status as the code.
    with pytest.raises(ClientError) as ei:
        fake_s3.head_object(Bucket="data", Key="nope")
    assert _code(ei) == "404" and _status(ei) == 404
    # the modelled exception form catches by code
    with pytest.raises(fake_s3.exceptions.NoSuchKey):
        fake_s3.get_object(Bucket="data", Key="nope")
    with pytest.raises(ClientError) as ei:
        FakeS3Client(auto_create=False).head_object(Bucket="ghost", Key="k")
    assert _code(ei) == "NoSuchBucket"


def test_conditional_put_if_none_match_star_is_412_when_key_exists(fake_s3):
    """Measured on OSN 2026-10-11: PutObject + If-None-Match: * on an existing
    key fails with 412 PreconditionFailed; on a fresh key it succeeds."""
    fake_s3.put_object(Bucket="data", Key="once", Body=b"first", IfNoneMatch="*")
    with pytest.raises(ClientError) as ei:
        fake_s3.put_object(Bucket="data", Key="once", Body=b"second", IfNoneMatch="*")
    assert _code(ei) == "PreconditionFailed" and _status(ei) == 412
    assert fake_s3.body("data", "once") == b"first"        # the loser wrote nothing
    # an unconditional PUT still overwrites
    fake_s3.put_object(Bucket="data", Key="once", Body=b"third")
    assert fake_s3.body("data", "once") == b"third"
    # If-Match guards an overwrite the same way
    etag = fake_s3.head_object(Bucket="data", Key="once")["ETag"]
    with pytest.raises(ClientError) as ei:
        fake_s3.put_object(Bucket="data", Key="once", Body=b"x", IfMatch='"stale"')
    assert _code(ei) == "PreconditionFailed"
    fake_s3.put_object(Bucket="data", Key="once", Body=b"fourth", IfMatch=etag)
    assert fake_s3.body("data", "once") == b"fourth"


# --- copy ---------------------------------------------------------------------

def test_copy_object_same_bucket_with_if_match(fake_s3):
    etag = fake_s3.seed("data", "src", b"payload", content_type="text/plain",
                        metadata={"m": "v"})
    out = fake_s3.copy_object(Bucket="data", Key="dst", CopySource={"Bucket": "data", "Key": "src"},
                              CopySourceIfMatch=f'"{etag}"')
    assert out["CopyObjectResult"]["ETag"] == f'"{etag}"'
    head = fake_s3.head_object(Bucket="data", Key="dst")
    assert fake_s3.body("data", "dst") == b"payload"
    assert head["ContentType"] == "text/plain" and head["Metadata"] == {"m": "v"}   # COPY directive
    # string CopySource form and an unquoted ETag work too
    fake_s3.copy_object(Bucket="data", Key="dst2", CopySource="data/src", CopySourceIfMatch=etag)
    assert fake_s3.exists("data", "dst2")


def test_copy_object_if_match_mismatch_is_412(fake_s3):
    """Measured on OSN 2026-10-11: CopyObject with a stale
    x-amz-copy-source-if-match fails with 412 PreconditionFailed."""
    fake_s3.seed("data", "src", b"payload")
    with pytest.raises(ClientError) as ei:
        fake_s3.copy_object(Bucket="data", Key="dst", CopySource="data/src",
                            CopySourceIfMatch='"0123456789abcdef0123456789abcdef"')
    assert _code(ei) == "PreconditionFailed" and _status(ei) == 412
    assert not fake_s3.exists("data", "dst")
    with pytest.raises(ClientError) as ei:
        fake_s3.copy_object(Bucket="data", Key="dst", CopySource="data/missing")
    assert _code(ei) == "NoSuchKey"


def test_copy_object_metadata_directive(fake_s3):
    fake_s3.seed("data", "src", b"payload", content_type="text/plain", metadata={"m": "v"})
    fake_s3.copy_object(Bucket="data", Key="dst", CopySource="data/src", MetadataDirective="REPLACE",
                        Metadata={"n": "w"}, ContentType="application/octet-stream")
    head = fake_s3.head_object(Bucket="data", Key="dst")
    assert head["Metadata"] == {"n": "w"} and head["ContentType"] == "application/octet-stream"
    # Copying onto itself without changing anything is illegal (S3 InvalidRequest)...
    with pytest.raises(ClientError) as ei:
        fake_s3.copy_object(Bucket="data", Key="src", CopySource="data/src")
    assert _code(ei) == "InvalidRequest"
    # ...but a REPLACE self-copy is how metadata gets rewritten in place.
    fake_s3.copy_object(Bucket="data", Key="src", CopySource="data/src", MetadataDirective="REPLACE",
                        Metadata={"rewritten": "yes"})
    assert fake_s3.head_object(Bucket="data", Key="src")["Metadata"] == {"rewritten": "yes"}
    assert fake_s3.body("data", "src") == b"payload"


# --- delete / list ------------------------------------------------------------

def test_delete_object_and_objects(fake_s3):
    for k in ("a", "b", "c"):
        fake_s3.seed("data", k, b"x")
    fake_s3.delete_object(Bucket="data", Key="a")
    fake_s3.delete_object(Bucket="data", Key="a")            # idempotent, like S3
    out = fake_s3.delete_objects(Bucket="data", Delete={"Objects": [{"Key": "b"}, {"Key": "zz"}]})
    assert [d["Key"] for d in out["Deleted"]] == ["b", "zz"] and out["Errors"] == []
    assert fake_s3.keys("data") == ["c"]


def test_list_objects_v2_prefix_delimiter_and_pagination(fake_s3):
    keys = ["p/a/1", "p/a/2", "p/b/1", "p/c", "q/1"]
    for k in keys:
        fake_s3.seed("data", k, b"x" * len(k))
    out = fake_s3.list_objects_v2(Bucket="data", Prefix="p/")
    assert [c["Key"] for c in out["Contents"]] == ["p/a/1", "p/a/2", "p/b/1", "p/c"]
    assert out["KeyCount"] == 4 and out["IsTruncated"] is False
    assert out["Contents"][0]["Size"] == 5 and out["Contents"][0]["ETag"].startswith('"')

    out = fake_s3.list_objects_v2(Bucket="data", Prefix="p/", Delimiter="/")
    assert [p["Prefix"] for p in out["CommonPrefixes"]] == ["p/a/", "p/b/"]
    assert [c["Key"] for c in out["Contents"]] == ["p/c"]

    page1 = fake_s3.list_objects_v2(Bucket="data", MaxKeys=2)
    assert [c["Key"] for c in page1["Contents"]] == ["p/a/1", "p/a/2"] and page1["IsTruncated"]
    page2 = fake_s3.list_objects_v2(Bucket="data", MaxKeys=2,
                                    ContinuationToken=page1["NextContinuationToken"])
    assert [c["Key"] for c in page2["Contents"]] == ["p/b/1", "p/c"]
    page3 = fake_s3.list_objects_v2(Bucket="data", MaxKeys=2,
                                    ContinuationToken=page2["NextContinuationToken"])
    assert [c["Key"] for c in page3["Contents"]] == ["q/1"] and not page3["IsTruncated"]
    assert "NextContinuationToken" not in page3

    empty = fake_s3.list_objects_v2(Bucket="data", Prefix="nothing/")
    assert "Contents" not in empty and empty["KeyCount"] == 0

    pages = list(fake_s3.get_paginator("list_objects_v2").paginate(
        Bucket="data", PaginationConfig={"PageSize": 2}))
    assert [c["Key"] for p in pages for c in p.get("Contents", [])] == keys
    assert fake_s3.list_objects_v2(Bucket="data", StartAfter="p/b/1")["Contents"][0]["Key"] == "p/c"


# --- multipart ----------------------------------------------------------------

def test_multipart_upload_with_upload_part_copy_and_range(fake_s3):
    """Measured on OSN 2026-10-11: UploadPartCopy with x-amz-copy-source-range
    works, and the assembled object is the concatenation of the parts."""
    big = bytes(MIN_PART_SIZE) + b"tail-of-source"
    src_etag = fake_s3.seed("data", "src", big)
    first = b"\x01" * MIN_PART_SIZE
    last = b"the end"

    up = fake_s3.create_multipart_upload(Bucket="data", Key="assembled",
                                         ContentType="application/fits", Metadata={"k": "v"})
    uid = up["UploadId"]
    p1 = fake_s3.upload_part(Bucket="data", Key="assembled", UploadId=uid, PartNumber=1, Body=first)
    p2 = fake_s3.upload_part_copy(Bucket="data", Key="assembled", UploadId=uid, PartNumber=2,
                                  CopySource="data/src", CopySourceRange=f"bytes=0-{MIN_PART_SIZE - 1}",
                                  CopySourceIfMatch=f'"{src_etag}"')
    p3 = fake_s3.upload_part(Bucket="data", Key="assembled", UploadId=uid, PartNumber=3, Body=last)
    listed = fake_s3.list_parts(Bucket="data", Key="assembled", UploadId=uid)["Parts"]
    assert [(p["PartNumber"], p["Size"]) for p in listed] == [(1, MIN_PART_SIZE), (2, MIN_PART_SIZE), (3, len(last))]

    done = fake_s3.complete_multipart_upload(
        Bucket="data", Key="assembled", UploadId=uid,
        MultipartUpload={"Parts": [
            {"PartNumber": 1, "ETag": p1["ETag"]},
            {"PartNumber": 2, "ETag": p2["CopyPartResult"]["ETag"]},
            {"PartNumber": 3, "ETag": p3["ETag"]},
        ]})
    assert fake_s3.body("data", "assembled") == first + big[:MIN_PART_SIZE] + last
    head = fake_s3.head_object(Bucket="data", Key="assembled")
    # composite ETag: md5 of the part md5s, dash, part count — never the body md5
    digests = b"".join(hashlib.md5(p).digest() for p in (first, big[:MIN_PART_SIZE], last))
    assert done["ETag"] == head["ETag"] == f'"{hashlib.md5(digests).hexdigest()}-3"'
    assert head["ContentType"] == "application/fits" and head["Metadata"] == {"k": "v"}
    # the upload is gone once completed
    with pytest.raises(ClientError) as ei:
        fake_s3.list_parts(Bucket="data", Key="assembled", UploadId=uid)
    assert _code(ei) == "NoSuchUpload"


def test_upload_part_copy_if_match_mismatch_is_412(fake_s3):
    fake_s3.seed("data", "src", b"abc")
    uid = fake_s3.create_multipart_upload(Bucket="data", Key="k")["UploadId"]
    with pytest.raises(ClientError) as ei:
        fake_s3.upload_part_copy(Bucket="data", Key="k", UploadId=uid, PartNumber=1,
                                 CopySource="data/src", CopySourceIfMatch='"nope"')
    assert _code(ei) == "PreconditionFailed" and _status(ei) == 412


def test_complete_enforces_the_five_mib_minimum_on_non_last_parts(fake_s3):
    """Measured on OSN 2026-10-11: a non-last part under 5 MiB is accepted by
    UploadPart but rejected at CompleteMultipartUpload with EntityTooSmall."""
    uid = fake_s3.create_multipart_upload(Bucket="data", Key="k")["UploadId"]
    small = fake_s3.upload_part(Bucket="data", Key="k", UploadId=uid, PartNumber=1, Body=b"tiny")
    last = fake_s3.upload_part(Bucket="data", Key="k", UploadId=uid, PartNumber=2, Body=b"also tiny")
    with pytest.raises(ClientError) as ei:
        fake_s3.complete_multipart_upload(
            Bucket="data", Key="k", UploadId=uid,
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": small["ETag"]},
                                       {"PartNumber": 2, "ETag": last["ETag"]}]})
    assert _code(ei) == "EntityTooSmall" and _status(ei) == 400
    assert not fake_s3.exists("data", "k")
    # the upload survives the failed complete; a single small part is fine (it is the last)
    fake_s3.complete_multipart_upload(
        Bucket="data", Key="k", UploadId=uid,
        MultipartUpload={"Parts": [{"PartNumber": 2, "ETag": last["ETag"]}]})
    assert fake_s3.body("data", "k") == b"also tiny"


def test_complete_rejects_bad_etags_and_misordered_parts(fake_s3):
    uid = fake_s3.create_multipart_upload(Bucket="data", Key="k")["UploadId"]
    p1 = fake_s3.upload_part(Bucket="data", Key="k", UploadId=uid, PartNumber=1, Body=bytes(MIN_PART_SIZE))
    p2 = fake_s3.upload_part(Bucket="data", Key="k", UploadId=uid, PartNumber=2, Body=b"end")
    with pytest.raises(ClientError) as ei:
        fake_s3.complete_multipart_upload(
            Bucket="data", Key="k", UploadId=uid,
            MultipartUpload={"Parts": [{"PartNumber": 2, "ETag": p2["ETag"]},
                                       {"PartNumber": 1, "ETag": p1["ETag"]}]})
    assert _code(ei) == "InvalidPartOrder"
    with pytest.raises(ClientError) as ei:
        fake_s3.complete_multipart_upload(
            Bucket="data", Key="k", UploadId=uid,
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": '"wrong"'},
                                       {"PartNumber": 2, "ETag": p2["ETag"]}]})
    assert _code(ei) == "InvalidPart"
    with pytest.raises(ClientError) as ei:
        fake_s3.upload_part(Bucket="data", Key="k", UploadId="bogus", PartNumber=1, Body=b"x")
    assert _code(ei) == "NoSuchUpload"


def test_abort_multipart_upload(fake_s3):
    uid = fake_s3.create_multipart_upload(Bucket="data", Key="k")["UploadId"]
    fake_s3.upload_part(Bucket="data", Key="k", UploadId=uid, PartNumber=1, Body=b"x")
    assert fake_s3.list_multipart_uploads(Bucket="data")["Uploads"][0]["UploadId"] == uid
    fake_s3.abort_multipart_upload(Bucket="data", Key="k", UploadId=uid)
    assert fake_s3.list_multipart_uploads(Bucket="data")["Uploads"] == []
    with pytest.raises(ClientError) as ei:
        fake_s3.abort_multipart_upload(Bucket="data", Key="k", UploadId=uid)
    assert _code(ei) == "NoSuchUpload"
    assert not fake_s3.exists("data", "k")


# --- managed transfers --------------------------------------------------------

def test_upload_file_and_download_file(fake_s3, tmp_path):
    src = tmp_path / "in.bin"
    src.write_bytes(b"\x07" * 1234)
    fake_s3.upload_file(str(src), "data", "k", ExtraArgs={"ContentType": "application/fits",
                                                          "CacheControl": "max-age=1"})
    head = fake_s3.head_object(Bucket="data", Key="k")
    assert head["ContentLength"] == 1234 and head["ContentType"] == "application/fits"
    assert head["CacheControl"] == "max-age=1"
    dst = tmp_path / "out.bin"
    fake_s3.download_file("data", "k", str(dst))
    assert dst.read_bytes() == src.read_bytes()
    with pytest.raises(ClientError) as ei:
        fake_s3.download_file("data", "missing", str(tmp_path / "x"))
    assert _code(ei) == "404"
    assert [op for op, _ in fake_s3.calls] == ["UploadFile", "HeadObject", "DownloadFile", "DownloadFile"]


# --- presigned URLs + transport ----------------------------------------------

def test_presigned_put_and_get_through_the_transport(fake_s3):
    url = fake_s3.generate_presigned_url(
        "put_object", Params={"Bucket": "data", "Key": "dir/o.fits", "ContentType": "application/fits"},
        ExpiresIn=600)
    assert url.startswith("https://fake-s3.invalid/data/dir/o.fits?")
    t = fake_s3.transport
    resp = t.put(url, data=b"hello", headers={"Content-Type": "application/fits"}, timeout=5)
    assert resp.status_code == 200 and resp.ok
    resp.raise_for_status()
    head = fake_s3.head_object(Bucket="data", Key="dir/o.fits")
    assert head["ContentLength"] == 5 and head["ContentType"] == "application/fits"
    assert resp.headers["ETag"] == head["ETag"]

    get_url = fake_s3.generate_presigned_url("get_object", Params={"Bucket": "data", "Key": "dir/o.fits"})
    got = t.get(get_url, stream=True)
    assert got.status_code == 200 and got.content == b"hello"
    assert got.headers["Content-Length"] == "5" and got.headers["Content-Type"] == "application/fits"
    assert b"".join(got.iter_content(2)) == b"hello"
    ranged = t.get(get_url, headers={"Range": "bytes=1-2"})
    assert ranged.status_code == 206 and ranged.content == b"el"
    # SigV4 signs the method: a HEAD needs its own URL (a GET-signed one is a 403).
    head_url = fake_s3.generate_presigned_url("head_object", Params={"Bucket": "data", "Key": "dir/o.fits"})
    assert t.head(head_url).status_code == 200 and t.head(head_url).content == b""
    assert t.head(get_url).status_code == 403
    assert [m for m, _u, _h in t.requests][:2] == ["PUT", "GET"]


def test_presigned_url_rejections(fake_s3):
    now = [datetime(2026, 10, 11, tzinfo=timezone.utc)]
    fake_s3.clock = lambda: now[0]
    t = fake_s3.transport
    put_url = fake_s3.generate_presigned_url(
        "put_object", Params={"Bucket": "data", "Key": "k", "ContentType": "image/png"}, ExpiresIn=60)

    # method other than the signed one
    r = t.get(put_url)
    assert r.status_code == 403 and b"SignatureDoesNotMatch" in r.content
    # Content-Type differs from the signed header
    r = t.put(put_url, data=b"x", headers={"Content-Type": "application/fits"})
    assert r.status_code == 403 and b"SignatureDoesNotMatch" in r.content
    with pytest.raises(Exception, match="403"):
        r.raise_for_status()
    # tampered signature
    r = t.put(put_url[:-4] + "0000", data=b"x", headers={"Content-Type": "image/png"})
    assert r.status_code == 403
    # expired
    now[0] += timedelta(seconds=61)
    r = t.put(put_url, data=b"x", headers={"Content-Type": "image/png"})
    assert r.status_code == 403 and b"Request has expired" in r.content
    assert not fake_s3.exists("data", "k")
    # GET of a key that is not there is a 404 with an S3 error body
    get_url = fake_s3.generate_presigned_url("get_object", Params={"Bucket": "data", "Key": "k"})
    r = t.get(get_url)
    assert r.status_code == 404 and b"NoSuchKey" in r.content
    head_url = fake_s3.generate_presigned_url("head_object", Params={"Bucket": "data", "Key": "k"})
    assert t.head(head_url).status_code == 404
    # a presign for a key without ContentType accepts any header
    url = fake_s3.generate_presigned_url("put_object", Params={"Bucket": "data", "Key": "free"})
    assert t.put(url, data=b"x", headers={"Content-Type": "whatever/else"}).status_code == 200


def test_transport_is_thread_safe_under_parallel_puts(fake_s3):
    t = fake_s3.transport
    urls = {i: fake_s3.generate_presigned_url("put_object", Params={"Bucket": "data", "Key": f"k{i}"})
            for i in range(50)}

    def put(i):
        return t.put(urls[i], data=bytes([i]) * 10).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(put, range(50))) == {200}
    assert len(fake_s3.keys("data")) == 50
    assert fake_s3.body("data", "k7") == b"\x07" * 10


def test_upload_files_presigned_end_to_end(fake_s3, tmp_path):
    """The real ``upload_files_presigned`` driving the fake through its
    transport: bytes land under the presigned keys with the signed
    Content-Type, and a ``.fits.gz`` product is gzipped on the way (its
    stored size reported) exactly as against OSN."""
    from campfire.deploy.r2 import UploadTask, upload_files_presigned
    from campfire_layout import KeyScheme, Scope, storage_key

    plain = tmp_path / "x_spec.fits"
    plain.write_bytes(b"\x00" * 20_000)
    mosaic = tmp_path / "mosaic_nircam_f444w_cosmos_30mas_A1_sci.fits"
    mosaic.write_bytes(b"\x01" * 50_000)
    plain_key = storage_key("nirspec_spec", Scope(obs="o"), plain.name, scheme=KeyScheme.CANONICAL)
    gz_key = storage_key("nircam_mosaic", Scope(field="cosmos", filt="f444w"), mosaic.name,
                         scheme=KeyScheme.CANONICAL)
    assert gz_key.endswith(".fits.gz")
    tasks = [UploadTask(plain, plain_key, "application/fits"),
             UploadTask(mosaic, gz_key, "application/gzip")]
    urls = {t.r2_key: fake_s3.generate_presigned_url(
        "put_object", Params={"Bucket": "campfire-jwst", "Key": t.r2_key, "ContentType": t.content_type})
        for t in tasks}

    landed, stored = set(), {}
    ok, failed, msgs = upload_files_presigned(urls, tasks, max_workers=2, session=fake_s3.transport,
                                              succeeded_out=landed, stored_sizes_out=stored)
    assert (ok, failed, msgs) == (2, 0, [])
    assert landed == {plain_key, gz_key}
    assert fake_s3.body("campfire-jwst", plain_key) == plain.read_bytes()
    assert fake_s3.head_object(Bucket="campfire-jwst", Key=plain_key)["ContentType"] == "application/fits"
    gz_head = fake_s3.head_object(Bucket="campfire-jwst", Key=gz_key)
    assert gz_head["ContentType"] == "application/gzip"
    assert stored == {gz_key: gz_head["ContentLength"]} and 0 < stored[gz_key] < 50_000
    assert gzip.decompress(fake_s3.body("campfire-jwst", gz_key)) == mosaic.read_bytes()

    # An expired URL surfaces as a failed upload, not an exception.
    fake_s3.clock = lambda: datetime.now(timezone.utc) + timedelta(hours=2)
    ok, failed, msgs = upload_files_presigned(urls, tasks[:1], max_workers=1, session=fake_s3.transport)
    assert (ok, failed) == (0, 1) and "403" in msgs[0]
