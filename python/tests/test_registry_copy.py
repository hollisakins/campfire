"""Tests for the R2->OSN copy + re-key + verify engine (epic #210, Track A / #215).

Exercises the engine with in-memory fakes (no boto3, no Supabase): a dict-backed
S3 client for the R2 source + OSN destination, and a list-backed Supabase client
whose ``upsert(on_conflict='id')`` mutates rows in place (so relocation and
resume/idempotency are observable).
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from campfire.deploy import registry as reg

SRC_BUCKET = "campfire"        # R2 data bucket
DST_BUCKET = "campfire-jwst"   # OSN bucket

CONTENT = b"FAKE-FITS-BYTES" * 64
CONTENT_SHA = "sha256:" + hashlib.sha256(CONTENT).hexdigest()

LEGACY_SPEC = "spectra/test_obs/test_obs_prism_100_spec.fits"
CANON_SPEC = "data/products/nirspec/test_obs/test_obs_prism_100_spec.fits"
LEGACY_RGB = "rgb/test_obs/test_obs_100_rgb.png"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# NOT NULL, no-default columns on storage_objects. The fake enforces these on
# the INSERT/upsert path to mirror Postgres — so a regression that re-homes rows
# via a partial-payload upsert (which raises a not-null violation on the candidate
# tuple BEFORE the ON CONFLICT arbiter) is caught instead of silently passing.
_REQUIRED_NOT_NULL = (
    "backend", "bucket", "storage_key", "content_hash",
    "size_bytes", "content_type", "product_type",
)


class _FakeS3:
    """In-memory S3: objects keyed by (bucket, key) -> bytes."""

    def __init__(self, objects=None, corrupt_download=False, head_size_delta=0):
        self.objects = dict(objects or {})
        self.corrupt_download = corrupt_download
        self.head_size_delta = head_size_delta  # report a wrong ContentLength

    def download_file(self, bucket, key, filename):
        data = self.objects[(bucket, key)]  # KeyError == missing object
        if self.corrupt_download:
            data = data + b"X"
        Path(filename).write_bytes(data)

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[(Bucket, Key)]) + self.head_size_delta}


class _FakeQuery:
    def __init__(self, store, table):
        self._store = store
        self._table = table
        self._filters = []
        self._limit = None
        self._mode = "select"
        self._update_payload = None

    def select(self, _cols):
        return self

    def update(self, payload):
        self._mode = "update"
        self._update_payload = dict(payload)
        return self

    def eq(self, col, val):
        self._filters.append((col, "eq", val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, "in", set(vals)))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _matches(self, row):
        for col, op, val in self._filters:
            if op == "eq" and row.get(col) != val:
                return False
            if op == "in" and row.get(col) not in val:
                return False
        return True

    def upsert(self, batch, on_conflict=None):
        rows = self._store.tables[self._table]
        by_id = {r["id"]: r for r in rows if "id" in r}
        for item in batch:
            # Postgres validates NOT NULL on the candidate INSERT tuple before the
            # ON CONFLICT arbiter — a partial payload raises regardless of conflict.
            missing = [c for c in _REQUIRED_NOT_NULL if item.get(c) is None]
            if missing:
                raise ValueError(
                    f"null value in column {missing[0]!r} violates not-null constraint"
                )
            if on_conflict == "id" and item.get("id") in by_id:
                by_id[item["id"]].update(item)
            else:
                rows.append(dict(item))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=batch))

    def execute(self):
        rows = self._store.tables[self._table]
        if self._mode == "update":
            n = 0
            for r in rows:
                if self._matches(r):
                    r.update(self._update_payload)
                    n += 1
            return SimpleNamespace(data=[], count=n)
        out = [dict(r) for r in rows if self._matches(r)]
        if self._limit is not None:
            out = out[: self._limit]
        return SimpleNamespace(data=out)


class _FakeSupabase:
    def __init__(self, storage_objects):
        # Assign stable ids if not present.
        rows = []
        for i, r in enumerate(storage_objects, start=1):
            row = dict(r)
            row.setdefault("id", i)
            rows.append(row)
        self.tables = {"storage_objects": rows}

    def table(self, name):
        return _FakeQuery(self, name)


def _row(key, product_type, content_hash, *, backend="r2", bucket="data",
         status="active", spectrum_id=None, exposure_ref=None, rid=None):
    r = {
        "storage_key": key, "backend": backend, "bucket": bucket,
        "content_hash": content_hash, "size_bytes": len(CONTENT),
        "content_type": "application/fits", "product_type": product_type,
        "status": status, "observation": "test_obs", "field": "cosmos",
        "spectrum_id": spectrum_id, "exposure_ref": exposure_ref,
    }
    if rid is not None:
        r["id"] = rid
    return r


def _run(sb, src, dst, **kw):
    return reg.copy_objects(
        sb, src_client=src, src_bucket=SRC_BUCKET,
        dst_client=dst, dst_bucket=DST_BUCKET, dst_backend="osn", **kw,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_copies_final_rekeys_and_relocates():
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False)

    assert report.ok and len(report.copied) == 1
    # OSN got the bytes under the canonical key.
    assert dst.objects[(DST_BUCKET, CANON_SPEC)] == CONTENT
    # Registry row relocated in place: osn + canonical + sha256, still one row.
    rows = sb.tables["storage_objects"]
    assert len(rows) == 1
    assert rows[0]["backend"] == "osn"
    assert rows[0]["storage_key"] == CANON_SPEC
    assert rows[0]["content_hash"] == CONTENT_SHA


def test_etag_upgraded_to_sha256():
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", "etag:abc123", spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False)

    assert report.ok and len(report.copied) == 1
    assert sb.tables["storage_objects"][0]["content_hash"] == CONTENT_SHA


def test_sha256_drift_skips_without_relocate_or_upload():
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", "sha256:deadbeef", spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False)

    assert not report.ok and len(report.failed) == 1
    # Drift is caught before the PUT: nothing landed on OSN, row untouched.
    assert (DST_BUCKET, CANON_SPEC) not in dst.objects
    assert sb.tables["storage_objects"][0]["backend"] == "r2"


def test_readback_mismatch_fails_and_does_not_relocate():
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3(corrupt_download=True)  # readback returns altered bytes

    report = _run(sb, src, dst, dry_run=False, verify_readback=True)

    assert not report.ok and len(report.failed) == 1
    assert sb.tables["storage_objects"][0]["backend"] == "r2"


def test_size_check_path_when_readback_disabled():
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False, verify_readback=False)

    assert report.ok and len(report.copied) == 1
    assert sb.tables["storage_objects"][0]["backend"] == "osn"


def test_dry_run_plans_without_transfer_or_write():
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=True)

    assert len(report.planned) == 1 and not report.copied
    assert report.planned[0][:2] == (LEGACY_SPEC, CANON_SPEC)
    assert dst.objects == {}  # nothing uploaded
    assert sb.tables["storage_objects"][0]["backend"] == "r2"  # nothing relocated


def test_default_product_types_exclude_rgb():
    sb = _FakeSupabase([
        _row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100"),
        _row(LEGACY_RGB, "rgb", CONTENT_SHA),
    ])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT, (SRC_BUCKET, LEGACY_RGB): CONTENT})
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False)

    # Only the spectrum migrated; the dead rgb product was never selected.
    assert len(report.copied) == 1
    assert report.copied[0][0] == LEGACY_SPEC
    assert (DST_BUCKET, CANON_SPEC) in dst.objects
    rgb_row = next(r for r in sb.tables["storage_objects"] if r["product_type"] == "rgb")
    assert rgb_row["backend"] == "r2"  # untouched


def test_unmappable_key_skipped_not_failed():
    sb = _FakeSupabase([_row("garbage/not/a/known/key", "nirspec_spec", CONTENT_SHA)])
    src = _FakeS3()
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False)

    assert len(report.skipped) == 1 and not report.failed and not report.copied


def test_idempotent_already_osn_not_reselected():
    # An already-migrated (osn) row is invisible to the backend='r2' candidate query.
    sb = _FakeSupabase([_row(CANON_SPEC, "nirspec_spec", CONTENT_SHA, backend="osn",
                             spectrum_id="test_obs_prism_100")])
    src = _FakeS3()
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False)

    assert not report.copied and not report.failed and not report.planned


def test_resume_second_run_is_noop():
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3()

    first = _run(sb, src, dst, dry_run=False)
    second = _run(sb, src, dst, dry_run=False)

    assert len(first.copied) == 1
    assert not second.copied  # row is now osn -> not a candidate
    assert len(sb.tables["storage_objects"]) == 1  # still exactly one active row


def test_size_check_mismatch_fails_without_relocate():
    # verify_readback=False path: a wrong uploaded size must fail (not relocate).
    sb = _FakeSupabase([_row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100")])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT})
    dst = _FakeS3(head_size_delta=7)  # HEAD reports a size that disagrees with the upload

    report = _run(sb, src, dst, dry_run=False, verify_readback=False)

    assert not report.ok and len(report.failed) == 1
    assert sb.tables["storage_objects"][0]["backend"] == "r2"


def test_continues_past_failure_to_later_object():
    # First candidate drifts (fails); a later candidate must still migrate.
    LEGACY2 = "spectra/test_obs/test_obs_prism_200_spec.fits"
    CANON2 = "data/products/nirspec/test_obs/test_obs_prism_200_spec.fits"
    sb = _FakeSupabase([
        _row(LEGACY_SPEC, "nirspec_spec", "sha256:deadbeef", spectrum_id="test_obs_prism_100"),
        _row(LEGACY2, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_200"),
    ])
    src = _FakeS3({(SRC_BUCKET, LEGACY_SPEC): CONTENT, (SRC_BUCKET, LEGACY2): CONTENT})
    dst = _FakeS3()

    report = _run(sb, src, dst, dry_run=False)

    assert len(report.failed) == 1 and len(report.copied) == 1
    rows = {r["storage_key"]: r for r in sb.tables["storage_objects"]}
    # Failed object untouched (still r2-legacy); good object relocated to osn.
    assert rows[LEGACY_SPEC]["backend"] == "r2"
    assert rows[CANON2]["backend"] == "osn"
    assert (DST_BUCKET, CANON2) in dst.objects
    assert (DST_BUCKET, CANON_SPEC) not in dst.objects


def test_find_migration_conflicts_detects_duplicate():
    # An osn-canonical row AND a fresh r2-legacy row whose canonical form collides.
    sb = _FakeSupabase([
        _row(CANON_SPEC, "nirspec_spec", CONTENT_SHA, backend="osn", spectrum_id="test_obs_prism_100"),
        _row(LEGACY_SPEC, "nirspec_spec", CONTENT_SHA, spectrum_id="test_obs_prism_100"),
    ])

    conflicts = reg.find_migration_conflicts(sb)

    assert conflicts == [LEGACY_SPEC]
