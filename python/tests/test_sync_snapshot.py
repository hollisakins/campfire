"""A first-time sync bootstraps from the nightly catalog snapshot.

Real LocalStore, fake API: the snapshot rows load, the extras walk overrides
them, rows neither touched are purged, and the normal walk then runs as the
catch-up from the snapshot's start time. Every way the snapshot can be
unusable falls back to the live walk.
"""

import gzip
import hashlib
import io
import json
from pathlib import Path

import pytest
import requests

from campfire.api.client import SyncStream
from campfire.exceptions import APIError
from campfire.db.store import LocalStore
from campfire.sync import sync_metadata

STARTED_AT = "2026-09-25T05:00:00.000000+00:00"


def _obj(i, **kw):
    row = {
        "id": i, "object_id": f"CAMPFIRE-J{i:04d}", "field": "uds", "ra": 34.0 + i, "dec": -5.0,
        "redshift": 1.0, "n_targets": 1, "n_spectra": 1, "programs": ["pub"],
        "gratings": ["PRISM"], "member_target_ids": [f"t{i}"], "is_active": True,
        "has_photometry": False, "lists": [], "updated_at": "2026-09-01T00:00:00Z",
    }
    row.update(kw)
    return row


def _spec(i, **kw):
    row = {
        "id": 100 + i, "spectrum_id": f"obs_prism_clear_{i}", "target_id": f"t{i}",
        "object_id": f"CAMPFIRE-J{i:04d}", "grating": "PRISM",
        "fits_path": f"obs/obs_PRISM_CLEAR_{i}_spec.fits", "file_hash": f"sha256:{i:064d}",
        "program_slug": "pub", "observation": "obs", "field": "uds",
        "updated_at": "2026-09-01T00:00:00Z",
    }
    row.update(kw)
    return row


def _storage(i):
    return {
        "storage_key": f"data/products/nirspec/obs/obs_PRISM_CLEAR_{i}_spec.fits", "id": 1000 + i,
        "backend": "osn", "bucket": "data", "content_hash": f"sha256:{i:064d}", "size_bytes": 10,
        "content_type": "application/fits", "product_type": "nirspec_spec", "instrument": "nirspec",
        "status": "active", "observation": "obs", "field": "uds",
        "spectrum_id": f"obs_prism_clear_{i}", "updated_at": "2026-09-01T00:00:00Z",
    }


def _phot(i):
    return {"id": 500 + i, "object_id": f"CAMPFIRE-J{i:04d}", "field": "uds",
            "photometry": {"bands": {}}, "updated_at": "2026-09-01T00:00:00Z"}


def _gz(rows):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        for r in rows:
            f.write((json.dumps(r) + "\n").encode())
    return buf.getvalue()


SNAPSHOT_ROWS = {
    "objects": [_obj(1), _obj(2)],
    "spectra": [_spec(1), _spec(2)],
    "storage": [_storage(1), _storage(2)],
    "photometry": [_phot(1)],
    "lines": [],
}


def _snapshot_info(rows=SNAPSHOT_ROWS, **overrides):
    blobs = {s: _gz(r) for s, r in rows.items()}
    info = {
        "available": True, "snapshot_id": 7, "started_at": STARTED_AT, "format_version": 1,
        "files": [
            {"stream": s, "url": f"https://osn.example/sync-snapshots/7/{s}.jsonl.gz",
             "sha256": "sha256:" + hashlib.sha256(b).hexdigest(), "size": len(b), "rows": len(rows[s])}
            for s, b in blobs.items()
        ],
    }
    info.update(overrides)
    return info, {f["url"]: blobs[f["stream"]] for f in info["files"]}


class FakeAPI:
    """fetch_all_* answer per walk kind: live (full), extras (snapshot=), catch-up (updated_since)."""

    def __init__(self, info=None, extras=None, catchup=None, live=None, info_error=None,
                 fail=(), deleted=None):
        self.info, self.info_error = info, info_error
        self.deleted = {} if deleted is None else deleted   # None-valued: journal too old
        self.deletion_requests = []
        self.fail = set(fail)              # walk kinds that raise: "extras" / "catchup"
        self.extras = extras or {}
        self.catchup = catchup or {}
        self.live = live or {}
        self.calls = []
        self.snapshot_requests = 0

    def get_sync_snapshot(self):
        self.snapshot_requests += 1
        if self.info_error:
            raise self.info_error
        return self.info

    def _fetch(self, key):
        def fetch(updated_since=None, on_page_complete=None, snapshot=None, **_kw):
            self.calls.append((key, updated_since, snapshot))
            kind = ("extras" if snapshot is not None
                    else "catchup" if updated_since is not None else "live")
            if kind in self.fail:
                raise requests.ConnectionError(f"{kind} walk failed")
            if snapshot is not None:
                rows, deleted = self.extras.get(key, []), []
            elif updated_since is not None:
                rows, deleted = self.catchup.get(key, ([], []))
            else:
                rows, deleted = self.live.get(key, []), []
            total = self.catchup.get("_objects_total", len(rows)) if key == "objects" else len(rows)
            return SyncStream(rows, total, list(deleted))
        return fetch

    def __getattr__(self, name):
        keys = {"fetch_all_objects": "objects", "fetch_all_spectra": "spectra",
                "fetch_all_storage": "storage", "fetch_all_photometry": "photometry",
                "fetch_all_line_fits": "line_fits"}
        if name in keys:
            return self._fetch(keys[name])
        raise AttributeError(name)

    def get_sync_deletions(self, since):
        self.deletion_requests.append(since)
        return self.deleted

    def fetch_tags(self):
        return []


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]


class FakeDownloads:
    def __init__(self, blobs):
        self.blobs = blobs

    def get(self, url, stream=True, timeout=None):
        return FakeResponse(self.blobs[url]) if url in self.blobs else FakeResponse(b"", 404)


@pytest.fixture(autouse=True)
def _stub_export(monkeypatch):
    monkeypatch.setattr("campfire.db.export.export_catalogs", lambda *a, **k: None)


@pytest.fixture
def store(tmp_path):
    s = LocalStore(tmp_path / "meta" / "campfire.db")
    yield s
    s.close()


def _serve(monkeypatch, blobs):
    monkeypatch.setattr("campfire.api.session.create_download_session",
                        lambda *_a, **_k: FakeDownloads(blobs))


def _ids(store, table):
    return sorted(r[0] for r in store._conn.execute(f"SELECT id FROM {table}"))


def test_bootstrap_loads_snapshot_extras_and_catches_up(tmp_path, store, monkeypatch):
    info, blobs = _snapshot_info()
    _serve(monkeypatch, blobs)
    # A mirror row the server no longer has: purged by the bootstrap.
    store.upsert_objects([_obj(99)])

    api = FakeAPI(
        info=info,
        # Extras: object 2 spans a proprietary program (full-scope aggregates +
        # a private list) and a proprietary object 3 with its spectrum.
        extras={"objects": [_obj(2, n_targets=5, lists=["mine"]), _obj(3, programs=["priv"])],
                "spectra": [_spec(3, program_slug="priv")]},
        # Catch-up: object 1 changed after the build began; spectrum 2 revoked.
        catchup={"objects": ([_obj(1, redshift=3.0, updated_at="2026-09-25T06:00:00Z")], []),
                 "spectra": ([], [102]),
                 "_objects_total": 3},
        # Hard-deleted after the build (a photometry supersede): in the
        # snapshot file, invisible to the catch-up, named by the journal.
        deleted={"photometry": [501]},
    )

    result = sync_metadata(api, store, tmp_path / "meta", full=True)

    assert result["snapshot_id"] == 7
    assert result["incremental"] is False
    assert result["needs_full_sync"] is False
    assert _ids(store, "objects") == [1, 2, 3]
    assert _ids(store, "spectra") == [101, 103]
    assert _ids(store, "object_photometry") == []            # journal deletion applied
    assert api.deletion_requests == [STARTED_AT]
    assert len(_ids(store, "storage_objects")) == 2

    by_id = {r["id"]: r for r in store._conn.execute("SELECT id, n_targets, redshift FROM objects")}
    assert by_id[2]["n_targets"] == 5          # extras overrode the snapshot row
    assert by_id[1]["redshift"] == 3.0          # catch-up applied on top

    extras_calls = [c for c in api.calls if c[2] == 7]
    catchup_calls = [c for c in api.calls if c[1] is not None]
    assert {c[0] for c in extras_calls} == {"objects", "spectra", "storage", "photometry", "line_fits"}
    assert all(c[1] is None for c in extras_calls)
    assert {c[0] for c in catchup_calls} == {"objects", "spectra", "storage", "photometry", "line_fits"}
    assert all(c[1] == STARTED_AT and c[2] is None for c in catchup_calls)
    assert not any(c[1] is None and c[2] is None for c in api.calls)   # no live full walk

    assert store.get_meta("snapshot_id") == "7"
    assert store.get_meta("snapshot_started_at") == STARTED_AT
    assert not list((tmp_path / "meta" / "snapshot").glob("*"))        # downloads cleaned up


def _bad_hash(info):
    info["files"][0]["sha256"] = "sha256:" + "0" * 64
    return info


@pytest.mark.parametrize("make_api", [
    pytest.param(lambda info: FakeAPI(info=None), id="none-offered"),
    pytest.param(lambda info: FakeAPI(info_error=requests.ConnectionError("down")), id="endpoint-error"),
    pytest.param(lambda info: FakeAPI(info_error=APIError("500 from /sync/snapshot")), id="endpoint-500"),
    pytest.param(lambda info: FakeAPI(info=info, fail={"extras"}), id="extras-walk-fails"),
    pytest.param(lambda info: FakeAPI(info={**info, "format_version": 2}), id="unknown-format"),
    pytest.param(lambda info: FakeAPI(info=_bad_hash(info)), id="hash-mismatch"),
    pytest.param(lambda info: FakeAPI(info={**info, "files": info["files"][:3]}), id="missing-stream"),
])
def test_unusable_snapshot_falls_back_to_live_walk(tmp_path, store, monkeypatch, make_api):
    info, blobs = _snapshot_info()
    _serve(monkeypatch, blobs)
    api = make_api(info)
    api.live = {"objects": [_obj(1), _obj(4)], "spectra": [_spec(4)]}

    result = sync_metadata(api, store, tmp_path / "meta", full=True)

    assert "snapshot_id" not in result
    assert _ids(store, "objects") == [1, 4]      # live rows, and no snapshot leftovers
    assert _ids(store, "spectra") == [104]
    live_calls = [c for c in api.calls if c[1] is None and c[2] is None]
    assert {c[0] for c in live_calls} == {"objects", "spectra", "storage", "photometry", "line_fits"}
    assert not any(c[1] is not None for c in api.calls)      # no catch-up after a fallback
    assert not store.get_meta("sync_bootstrap")               # nothing left to recover


def test_snapshot_older_than_the_deletion_journal_walks_live(tmp_path, store, monkeypatch):
    info, blobs = _snapshot_info()
    _serve(monkeypatch, blobs)
    api = FakeAPI(info=info, catchup={"_objects_total": 2},
                  live={"objects": [_obj(1)], "spectra": [_spec(1)]})
    api.deleted = None                                       # 410: not journaled that far back
    result = sync_metadata(api, store, tmp_path / "meta", full=True)

    assert "snapshot_id" not in result                       # the live walk's result
    live_calls = [c for c in api.calls if c[1] is None and c[2] is None]
    assert {c[0] for c in live_calls} == {"objects", "spectra", "storage", "photometry", "line_fits"}
    assert _ids(store, "objects") == [1]                     # snapshot-only rows purged
    assert _ids(store, "spectra") == [101]
    assert not store.get_meta("sync_bootstrap")


def test_disk_error_during_download_falls_back(tmp_path, store, monkeypatch):
    info, blobs = _snapshot_info()

    class DiskFull(FakeDownloads):
        def get(self, url, stream=True, timeout=None):
            resp = super().get(url, stream, timeout)
            def boom(chunk_size=1):
                raise OSError(28, "No space left on device")
            resp.iter_content = boom
            return resp

    monkeypatch.setattr("campfire.api.session.create_download_session",
                        lambda *_a, **_k: DiskFull(blobs))
    api = FakeAPI(info=info, live={"objects": [_obj(4)]})
    result = sync_metadata(api, store, tmp_path / "meta", full=True)
    assert "snapshot_id" not in result
    assert _ids(store, "objects") == [4]


def test_interrupted_catchup_resumes_on_next_sync(tmp_path, store, monkeypatch):
    info, blobs = _snapshot_info()
    _serve(monkeypatch, blobs)
    store.upsert_objects([_obj(99)])          # gone from the server

    api = FakeAPI(info=info, fail={"catchup"})
    with pytest.raises(requests.ConnectionError):
        sync_metadata(api, store, tmp_path / "meta", full=True)
    state = json.loads(store.get_meta("sync_bootstrap"))
    assert state["phase"] == "catchup" and state["started_at"] == STARTED_AT

    # Next plain `campfire sync`: resumes the catch-up from the watermark (not
    # from MAX(updated_at)), then runs the bootstrap's purge.
    api2 = FakeAPI(catchup={"objects": ([_obj(2, redshift=4.0)], []), "_objects_total": 2})
    result = sync_metadata(api2, store, tmp_path / "meta")
    assert api2.snapshot_requests == 0
    assert all(c[1] == STARTED_AT for c in api2.calls)
    assert result["snapshot_id"] == 7
    assert _ids(store, "objects") == [1, 2]   # 99 purged after the catch-up
    assert not store.get_meta("sync_bootstrap")


def test_interrupted_load_forces_a_full_sync(tmp_path, store):
    store.upsert_objects([_obj(1)])
    store.set_meta("sync_bootstrap", json.dumps({"phase": "loading"}))
    api = FakeAPI(info=None, live={"objects": [_obj(1), _obj(5)]})
    sync_metadata(api, store, tmp_path / "meta")      # not --full
    assert all(c[1] is None for c in api.calls)
    assert _ids(store, "objects") == [1, 5]
    assert not store.get_meta("sync_bootstrap")


def test_full_resync_keeps_local_state_of_rows_the_catchup_restores(tmp_path, store, monkeypatch):
    # A final created after the snapshot's storage page was read, already
    # downloaded here: missing from the snapshot, returned by the catch-up.
    late = _storage(3)
    store.upsert_storage_objects([late])
    store.mark_object_synced(storage_key=late["storage_key"], local_path="nirspec/obs/late.fits",
                             local_file_hash=late["content_hash"], local_file_size=10)
    info, blobs = _snapshot_info()
    _serve(monkeypatch, blobs)
    api = FakeAPI(info=info, catchup={"storage": ([late], []), "_objects_total": 2})

    result = sync_metadata(api, store, tmp_path / "meta", full=True)

    row = store.get_storage_rows_by_keys([late["storage_key"]])[late["storage_key"]]
    assert row["local_path"] == "nirspec/obs/late.fits"
    assert late["storage_key"] not in [Path(p).name for p in result.get("orphaned_files", [])]
    assert "nirspec/obs/late.fits" not in result.get("orphaned_files", [])


def test_incremental_sync_never_asks_for_a_snapshot(tmp_path, store):
    store.upsert_objects([_obj(1)])
    api = FakeAPI(info=_snapshot_info()[0], catchup={"_objects_total": 1})
    sync_metadata(api, store, tmp_path / "meta")
    assert api.snapshot_requests == 0


def test_use_snapshot_false_walks_live(tmp_path, store):
    api = FakeAPI(info=_snapshot_info()[0], live={"objects": [_obj(1)]})
    sync_metadata(api, store, tmp_path / "meta", full=True, use_snapshot=False)
    assert api.snapshot_requests == 0
    assert _ids(store, "objects") == [1]
