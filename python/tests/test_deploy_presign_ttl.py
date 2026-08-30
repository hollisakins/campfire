"""Presigned URLs age out mid-batch on a slow uplink.

Every URL in a presign batch is minted at the same instant with a 1-hour TTL,
so the batch is also the unit that must finish inside that hour. At 2.16 MB/s
(a domestic asymmetric uplink -- 267 Mbps down, ~20 Mbps up) a 500-file batch
of ~42 MB products needs ~2.7 h, and a pg004 deploy lost 438 of 552 files in
one go, all sharing a single X-Amz-Date.

Two guards: batches bounded by total *bytes* (the quantity the TTL actually
races -- 25 NIRCam mosaics take far longer to push than 25 NIRSpec spectra,
so a file-count bound over- or under-shoots depending on product mix), and a
re-mint + retry round so expiry is survivable regardless of link speed or
file size.
"""

from pathlib import Path

import pytest

from campfire.deploy import r2
from campfire.deploy.push import default_upload_workers
from campfire.deploy.r2 import (
    PRESIGN_BATCH_MAX_BYTES, PRESIGN_BATCH_SIZE, UploadTask,
    iter_presign_batches,
)


def test_batch_budget_fits_inside_the_presign_ttl_on_a_slow_uplink():
    """The regression that motivated this: 500 x 42 MB at 2.16 MB/s >> 1 h."""
    slow_bytes_per_s = 2.16e6
    ttl_s = 3600
    seconds = PRESIGN_BATCH_MAX_BYTES / slow_bytes_per_s
    assert seconds < ttl_s / 2, (
        f"a {PRESIGN_BATCH_MAX_BYTES}-byte batch needs {seconds/60:.0f} min "
        f"on a 2.16 MB/s uplink; that leaves no margin against the 1 h "
        f"presign TTL"
    )
    # And the original unbounded 500-file batch of ~42 MB products would not
    # have fit.
    assert 500 * 42e6 / slow_bytes_per_s > ttl_s


def _sized_task(tmp_path, name, size):
    p = tmp_path / name
    p.write_bytes(b"\0" * size)
    return UploadTask(local_path=p, r2_key=f"data/products/x/{name}",
                      content_type="application/fits")


def test_batches_split_on_total_bytes_not_file_count(tmp_path):
    """Big products form small batches, small products form big ones."""
    tasks = [_sized_task(tmp_path, f"m{i}.fits", 400) for i in range(5)]
    batches = list(iter_presign_batches(tasks, max_bytes=1000, max_files=500))
    assert [len(b) for b in batches] == [2, 2, 1]
    # Order preserved, nothing lost or duplicated.
    assert [t.r2_key for b in batches for t in b] == [t.r2_key for t in tasks]


def test_oversized_file_becomes_a_singleton_batch(tmp_path):
    """A single mosaic bigger than the budget must still upload (alone)."""
    tasks = [
        _sized_task(tmp_path, "small1.fits", 10),
        _sized_task(tmp_path, "huge.fits", 5000),
        _sized_task(tmp_path, "small2.fits", 10),
    ]
    batches = list(iter_presign_batches(tasks, max_bytes=1000, max_files=500))
    assert [[t.local_path.name for t in b] for b in batches] == [
        ["small1.fits"], ["huge.fits"], ["small2.fits"]]


def test_file_count_cap_still_bounds_tiny_file_batches(tmp_path):
    """Many tiny files hit the presign route's URL-count cap, not the bytes."""
    tasks = [_sized_task(tmp_path, f"s{i}.json", 1) for i in range(7)]
    batches = list(iter_presign_batches(tasks, max_bytes=10**9, max_files=3))
    assert [len(b) for b in batches] == [3, 3, 1]


def test_unstatable_files_count_as_zero_bytes():
    """Batching must not die on a missing file; the upload reports it."""
    tasks = [UploadTask(local_path=Path(f"/nonexistent/f{i}.fits"),
                        r2_key=f"data/products/x/f{i}.fits",
                        content_type="application/fits")
             for i in range(4)]
    batches = list(iter_presign_batches(tasks, max_bytes=100, max_files=500))
    assert [len(b) for b in batches] == [4]


def test_default_count_cap_matches_presign_route_limit():
    """The web /deploy/presign route accepts at most 500 URLs per request."""
    assert PRESIGN_BATCH_SIZE == 500


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
