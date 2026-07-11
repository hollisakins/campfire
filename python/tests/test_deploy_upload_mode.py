"""Tests for upload-path dispatch in ``upload_files_parallel`` (issue #250).

Login mode uploads ONLY via presigned URLs and fails loudly when they are
unavailable — it must never silently fall back to direct boto3. The explicit
``service_role`` / ``local`` modes take the direct-boto3 path.
"""
from pathlib import Path

import pytest

from campfire.deploy import r2
from campfire.deploy.r2 import UploadTask, upload_files_parallel


def _task():
    return UploadTask(Path("/nonexistent/file.fits"), "spectra/x/file.fits",
                      "application/fits")


def test_login_mode_no_presign_raises_not_fallback(monkeypatch):
    # Presign unavailable + login mode -> hard error, never a boto3 fallback.
    monkeypatch.setattr(r2, "request_presigned_urls", lambda *a, **k: None)
    # A boto3 fallback would import the backend factory; make that explode so a
    # silent fallback would surface as the wrong error.
    def _boom(*a, **k):
        raise AssertionError("login mode must not fall back to direct boto3")
    monkeypatch.setattr("campfire.deploy.backend.resolve_backend", _boom)

    config = {"supabase": {"_auth_mode": "login"}}
    with pytest.raises(RuntimeError, match="presigned"):
        upload_files_parallel(config, [_task()], backend="osn")


def test_untagged_mode_treated_as_presigned_only(monkeypatch):
    # A config with no explicit direct mode is treated like login: presigned only.
    monkeypatch.setattr(r2, "request_presigned_urls", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="presigned"):
        upload_files_parallel({"supabase": {}}, [_task()], backend="osn")


def test_service_role_mode_skips_presign_goes_direct(monkeypatch):
    # Direct mode must NOT consult the presign endpoint at all; it goes straight
    # to the boto3 backend (which here errors on the missing storage section).
    called = {"presign": False}

    def _presign(*a, **k):
        called["presign"] = True
        return {}
    monkeypatch.setattr(r2, "request_presigned_urls", _presign)

    config = {"supabase": {"_auth_mode": "service_role"}}  # no 'r2' section
    with pytest.raises(ValueError, match="No storage credentials"):
        upload_files_parallel(config, [_task()], backend="osn")
    assert called["presign"] is False


def test_no_tasks_short_circuits():
    # Empty task list returns cleanly regardless of mode (no presign, no boto3).
    assert upload_files_parallel(
        {"supabase": {"_auth_mode": "login"}}, [], backend="osn") == (0, 0, [])


def test_backend_is_required():
    # Data lives on OSN; tiles are the sole R2 exception. There is deliberately
    # no default backend — every call site must say where bytes land.
    with pytest.raises(TypeError):
        upload_files_parallel({"supabase": {"_auth_mode": "login"}}, [_task()])
