"""Unit tests for the B4 (#220) delete-local verified-in-cloud interlock.

Pure-Python with a fake Supabase client + tmp files: asserts plan_delete_local
only marks for deletion local files that have an active registry row with a
sha256 hash (and, with --verify, whose local bytes hash-match the cloud copy),
and never touches files with no registry row. No DB.
"""
import hashlib
from pathlib import Path

import types

from campfire.deploy import registry as reg
from campfire.config import products_relpath


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._eq = []

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def execute(self):
        rows = list(self._rows)
        for col, val in self._eq:
            rows = [r for r in rows if r.get(col) == val]
        return types.SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(self._rows if name == 'storage_objects' else [])


OBS = "ember_egs_p1"
SPEC_KEY = f"spectra/{OBS}/{OBS}_prism_clear_12345_spec.fits"
JSON_KEY = f"spectra/{OBS}/{OBS}_prism_clear_12345_spec.json"


def _sha256(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _make_local(products_root: Path, key: str, content: bytes) -> Path:
    p = products_root / products_relpath(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def test_deletable_only_when_registered_with_sha256(tmp_path):
    body = b"fits-bytes" * 100
    local = _make_local(tmp_path, SPEC_KEY, body)
    rows = [{
        "storage_key": SPEC_KEY, "content_hash": _sha256(body),
        "size_bytes": len(body), "observation": OBS, "bucket": "data", "status": "active",
    }]
    plan = reg.plan_delete_local(_FakeClient(rows), OBS, tmp_path)
    keys = [k for _, k, _ in plan.deletable]
    assert SPEC_KEY in keys
    assert local.exists()  # planning never deletes


def test_etag_only_hash_is_skipped(tmp_path):
    body = b"x" * 50
    _make_local(tmp_path, SPEC_KEY, body)
    rows = [{
        "storage_key": SPEC_KEY, "content_hash": "etag:deadbeef",
        "size_bytes": len(body), "observation": OBS, "bucket": "data", "status": "active",
    }]
    plan = reg.plan_delete_local(_FakeClient(rows), OBS, tmp_path)
    assert not plan.deletable
    assert any("provisional" in reason for _, _, reason in plan.skipped)


def test_verify_rejects_hash_mismatch(tmp_path):
    body = b"local-bytes"
    _make_local(tmp_path, SPEC_KEY, body)
    rows = [{
        "storage_key": SPEC_KEY, "content_hash": _sha256(b"different-cloud-bytes"),
        "size_bytes": 99, "observation": OBS, "bucket": "data", "status": "active",
    }]
    plan = reg.plan_delete_local(_FakeClient(rows), OBS, tmp_path, verify=True)
    assert not plan.deletable
    assert any("hash" in reason for _, _, reason in plan.skipped)


def test_verify_accepts_hash_match(tmp_path):
    body = b"matching-bytes" * 10
    _make_local(tmp_path, SPEC_KEY, body)
    rows = [{
        "storage_key": SPEC_KEY, "content_hash": _sha256(body),
        "size_bytes": len(body), "observation": OBS, "bucket": "data", "status": "active",
    }]
    plan = reg.plan_delete_local(_FakeClient(rows), OBS, tmp_path, verify=True)
    assert len(plan.deletable) == 1


def test_registered_but_absent_local_file(tmp_path):
    # registry row exists, but the local file does not -> 'absent', not deletable
    rows = [{
        "storage_key": JSON_KEY, "content_hash": _sha256(b"z"),
        "size_bytes": 1, "observation": OBS, "bucket": "data", "status": "active",
    }]
    plan = reg.plan_delete_local(_FakeClient(rows), OBS, tmp_path)
    assert not plan.deletable
    assert JSON_KEY in plan.absent


def test_unregistered_local_file_is_never_a_candidate(tmp_path):
    # A local file exists but has NO registry row -> never touched (not in the set).
    _make_local(tmp_path, SPEC_KEY, b"orphan-local")
    plan = reg.plan_delete_local(_FakeClient([]), OBS, tmp_path)
    assert not plan.deletable and not plan.skipped and not plan.absent
