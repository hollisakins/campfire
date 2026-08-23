"""Upload concurrency defaults.

An earlier revision lowered the OSN default to 4, on the theory that
concurrency was provoking 500s. That was a misdiagnosis: the 500s were
presigned URLs ageing out mid-batch on a slow uplink (see
test_deploy_presign_ttl.py), which depends on throughput and is indifferent to
stream count -- a 16-way probe against the same endpoint went 16/16 while a
4-way production run failed 79%. The default is 16 for every backend.
"""

import pytest

from campfire.deploy.push import default_upload_workers

ENV_VAR = "CAMPFIRE_DEPLOY_UPLOAD_WORKERS"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


def test_osn_is_not_special_cased():
    assert default_upload_workers("osn") == 16


def test_r2_default():
    assert default_upload_workers("r2") == 16


def test_unknown_backend_falls_back():
    assert default_upload_workers() == 16
    assert default_upload_workers("something-else") == 16


@pytest.mark.parametrize("backend", ["osn", "r2", None])
def test_env_override_wins_for_every_backend(monkeypatch, backend):
    monkeypatch.setenv(ENV_VAR, "9")
    assert default_upload_workers(backend) == 9


@pytest.mark.parametrize("raw,expected", [("0", 1), ("-5", 1), ("999", 64)])
def test_env_override_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv(ENV_VAR, raw)
    assert default_upload_workers("osn") == expected


def test_garbage_env_falls_back_to_the_backend_default(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "not-a-number")
    assert default_upload_workers("osn") == 16
