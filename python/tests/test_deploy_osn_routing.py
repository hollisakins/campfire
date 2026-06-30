"""Unit tests for deploy → OSN routing (epic #210 / #216).

Pure/mocked: the presign request carries the ``backend`` field, the OSN-host
guard rejects R2 URLs (a web deployment that predates OSN upload support), and
canonical NIRSpec keys map to ``backend='osn'`` registry rows with the right
identity. The full upload + registry round-trip is exercised live against local
Supabase in CI (``campfire deploy --obs … --local``).
"""
import types

import pytest

from campfire.deploy import r2 as r2mod
from campfire.deploy.r2 import UploadTask, _assert_presigned_backend_osn
from campfire.deploy.registry import row_for_key
from campfire_layout import KeyScheme, Scope, storage_key

OSN_URL = (
    "https://uaz1.osn.mghpcc.org/campfire-jwst/"
    "data/products/nirspec/o/x_spec.fits?X-Amz-Signature=abc"
)
R2_URL = (
    "https://acct.r2.cloudflarestorage.com/campfire/"
    "data/products/nirspec/o/x_spec.fits?X-Amz-Signature=abc"
)


# --- OSN-host guard ---------------------------------------------------------

def test_assert_presigned_backend_osn_passes_for_osn_hosts():
    _assert_presigned_backend_osn({"k1": OSN_URL, "k2": OSN_URL})  # no raise


def test_assert_presigned_backend_osn_raises_on_any_r2_host():
    with pytest.raises(RuntimeError, match="predates OSN upload support"):
        _assert_presigned_backend_osn({"k1": OSN_URL, "k2": R2_URL})


# --- presign request carries the backend ------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return {"urls": {u["key"]: OSN_URL for u in self._p["uploads"]}}


def test_request_presigned_urls_sends_backend(monkeypatch, tmp_path):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(json)
        return _FakeResp(json)

    monkeypatch.setattr(r2mod, "http_requests", types.SimpleNamespace(post=fake_post))
    monkeypatch.setattr(r2mod, "_get_presign_headers", lambda cfg: {"Authorization": "Bearer x"})
    monkeypatch.setattr(r2mod, "_get_presign_base_url", lambda: "https://example.com/api/v1")

    task = UploadTask(tmp_path / "x.fits", "data/products/nirspec/o/x_spec.fits", "application/fits")
    urls = r2mod.request_presigned_urls({}, [task], backend="osn")
    assert captured["backend"] == "osn"
    assert urls == {task.r2_key: OSN_URL}


def test_upload_files_parallel_threads_backend_and_guards(monkeypatch, tmp_path):
    f = tmp_path / "x.fits"
    f.write_bytes(b"\x00")
    task = UploadTask(f, "data/products/nirspec/o/x_spec.fits", "application/fits")

    # A web deployment without OSN support signs R2 → the guard must abort.
    monkeypatch.setattr(r2mod, "request_presigned_urls", lambda *a, **k: {task.r2_key: R2_URL})
    with pytest.raises(RuntimeError, match="predates OSN upload support"):
        r2mod.upload_files_parallel({}, [task], backend="osn")

    # With OSN URLs the backend is threaded through and the upload proceeds.
    seen = {}

    def fake_presign(config, tasks, **kw):
        seen.update(kw)
        return {t.r2_key: OSN_URL for t in tasks}

    monkeypatch.setattr(r2mod, "request_presigned_urls", fake_presign)
    monkeypatch.setattr(r2mod, "upload_files_presigned", lambda urls, tasks, **kw: (len(tasks), 0, []))
    success, failed, msgs = r2mod.upload_files_parallel({}, [task], backend="osn")
    assert seen["backend"] == "osn"
    assert (success, failed, msgs) == (1, 0, [])


# --- canonical/osn row shape (what makes the upsert idempotent) -------------

SHA = "sha256:" + "a" * 64


def test_canonical_final_registers_as_osn_with_canonical_key():
    canon = storage_key(
        "nirspec_spec", Scope(obs="ember_egs_p1"),
        "ember_egs_p1_prism_clear_123_spec.fits", scheme=KeyScheme.CANONICAL,
    )
    row = row_for_key(canon, backend="osn", content_hash=SHA, size_bytes=1,
                      content_type="application/fits")
    assert row["backend"] == "osn"
    assert row["bucket"] == "data"
    assert row["storage_key"] == canon == "data/products/nirspec/ember_egs_p1/ember_egs_p1_prism_clear_123_spec.fits"
    assert row["product_type"] == "nirspec_spec"
    assert row["observation"] == "ember_egs_p1"
    assert row["exposure_ref"] is None  # finals never collide on the partial-unique


def test_canonical_exposure_row_carries_osn_key_and_exposure_ref():
    # The A1 migration wrote (osn, data, <canonical key>) for this exposure. A
    # re-deploy now writes the SAME conflict tuple, so upsert_storage_objects
    # UPDATEs in place instead of inserting a colliding legacy/r2 row — the fix
    # for the partial-unique (product_type, exposure_ref) WHERE status='active'.
    fname = "jw07076020001_04101_00001_nrs1_117757.fits"
    canon = storage_key("nirspec_spectrum_exposure", Scope(obs="ember_egs_p1"),
                        fname, scheme=KeyScheme.CANONICAL)
    row = row_for_key(canon, backend="osn", content_hash=SHA, size_bytes=1,
                      content_type="application/fits")
    assert row["backend"] == "osn"
    assert row["storage_key"].startswith("data/products/nirspec/")
    assert row["product_type"] == "nirspec_spectrum_exposure"
    assert row["exposure_ref"] == "jw07076020001_04101_00001_nrs1_117757"
