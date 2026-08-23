"""Presigned URLs age out mid-batch on a slow uplink.

Every URL in a presign batch is minted at the same instant with a 1-hour TTL,
so the batch is also the unit that must finish inside that hour. At 2.16 MB/s
(a domestic asymmetric uplink -- 267 Mbps down, ~20 Mbps up) a 500-file batch
of ~42 MB products needs ~2.7 h, and a pg004 deploy lost 438 of 552 files in
one go, all sharing a single X-Amz-Date.

Two guards: a batch small enough that expiry is rare, and a re-mint + retry
round so expiry is survivable regardless of link speed or file size.
"""

from pathlib import Path

import pytest

from campfire.deploy import r2
from campfire.deploy.push import default_upload_workers
from campfire.deploy.r2 import PRESIGN_BATCH_SIZE, UploadTask


def test_batch_fits_inside_the_presign_ttl_on_a_slow_uplink():
    """The regression that motivated this: 500 x 42 MB at 2.16 MB/s >> 1 h."""
    slow_bytes_per_s = 2.16e6
    typical_product_bytes = 42e6
    ttl_s = 3600
    seconds = PRESIGN_BATCH_SIZE * typical_product_bytes / slow_bytes_per_s
    assert seconds < ttl_s / 2, (
        f"a {PRESIGN_BATCH_SIZE}-file batch needs {seconds/60:.0f} min on a "
        f"2.16 MB/s uplink; that leaves no margin against the 1 h presign TTL"
    )
    # And the old value would not have.
    assert 500 * typical_product_bytes / slow_bytes_per_s > ttl_s


def test_worker_default_is_not_lowered_for_osn():
    """OSN=4 was a misdiagnosis of the expiry bug; CANDIDE wants the streams."""
    assert default_upload_workers("osn") == 16
    assert default_upload_workers("r2") == 16
    assert default_upload_workers() == 16


class _Recorder:
    """Fails every upload on the first round, succeeds once URLs are re-minted."""

    def __init__(self, fail_rounds=1):
        self.fail_rounds = fail_rounds
        self.presign_calls = 0
        self.upload_rounds = 0
        self.uploaded: list[str] = []

    def request_presigned_urls(self, config, tasks, **kw):
        self.presign_calls += 1
        return {t.r2_key: f"https://osn.example/{t.r2_key}?round={self.presign_calls}"
                for t in tasks}

    def upload_files_presigned(self, urls, tasks, **kw):
        self.upload_rounds += 1
        succeeded_out = kw.get("succeeded_out")
        on_success = kw.get("on_success")
        if self.upload_rounds <= self.fail_rounds:
            # Everything "expires" -- nothing lands, no callbacks fire.
            return 0, len(tasks), [f"{t.local_path.name}: 500 Server Error"
                                   for t in tasks]
        for t in tasks:
            if succeeded_out is not None:
                succeeded_out.add(t.r2_key)
            if on_success is not None:
                on_success(t)
            self.uploaded.append(t.r2_key)
        return len(tasks), 0, []


@pytest.fixture()
def patched(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(r2, "request_presigned_urls", rec.request_presigned_urls)
    monkeypatch.setattr(r2, "upload_files_presigned", rec.upload_files_presigned)
    monkeypatch.setattr(r2, "create_transfer_session", lambda *a, **k: object())
    monkeypatch.setattr(r2, "_assert_presigned_backend_osn", lambda urls: None)
    return rec


def _tasks(n):
    return [UploadTask(local_path=Path(f"/tmp/f{i}.fits"),
                       r2_key=f"data/products/x/f{i}.fits",
                       content_type="application/fits")
            for i in range(n)]


def test_expired_batch_is_reminted_and_retried(patched):
    """A wholly-expired batch must recover, not be reported as lost."""
    tasks = _tasks(3)
    cfg = {"supabase": {"_auth_mode": "login"}}
    landed: set[str] = set()
    ok, failed, msgs = r2.upload_files_parallel(
        cfg, tasks, backend="osn", succeeded_out=landed, max_workers=4)

    assert failed == 0, f"expected recovery after re-mint, got {msgs}"
    assert ok == 3
    assert landed == {t.r2_key for t in tasks}
    # Re-minted rather than reusing the aged-out URLs.
    assert patched.presign_calls == 2
    assert patched.upload_rounds == 2


def test_success_callback_fires_once_per_file(patched):
    """Retries must not double-count: registry rows are built in on_success."""
    tasks = _tasks(4)
    seen: list[str] = []
    r2.upload_files_parallel(
        {"supabase": {"_auth_mode": "login"}}, tasks, backend="osn",
        on_success=lambda t: seen.append(t.r2_key), max_workers=4)
    assert sorted(seen) == sorted(t.r2_key for t in tasks)
    assert len(seen) == len(set(seen))


def test_permanent_failure_still_reported(monkeypatch):
    """Retries are bounded -- a genuinely dead upload must still surface."""
    rec = _Recorder(fail_rounds=99)
    monkeypatch.setattr(r2, "request_presigned_urls", rec.request_presigned_urls)
    monkeypatch.setattr(r2, "upload_files_presigned", rec.upload_files_presigned)
    monkeypatch.setattr(r2, "create_transfer_session", lambda *a, **k: object())
    monkeypatch.setattr(r2, "_assert_presigned_backend_osn", lambda urls: None)

    ok, failed, msgs = r2.upload_files_parallel(
        {"supabase": {"_auth_mode": "login"}}, _tasks(2), backend="osn",
        max_workers=2)
    assert ok == 0
    assert failed == 2
    assert msgs
    assert rec.upload_rounds == 1 + r2._PRESIGN_RETRY_ROUNDS
