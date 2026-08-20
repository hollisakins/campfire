"""Per-backend upload concurrency defaults.

OSN rejects a large fraction of concurrent PutObject requests with 500 once the
stream count is high. Measured on a 621-file pg004 deploy: 16 streams failed
68% of uploads (81% on the retry), the same files at 4 streams failed 0/344,
and a single sequential PUT of a just-failed object returned 200.
"""

import pytest

from campfire.deploy.push import default_upload_workers

ENV_VAR = "CAMPFIRE_DEPLOY_UPLOAD_WORKERS"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


def test_osn_default_is_low():
    assert default_upload_workers("osn") == 4


def test_r2_default_is_unchanged():
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
    assert default_upload_workers("osn") == 4
