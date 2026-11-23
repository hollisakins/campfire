"""The local storage mirror never sinks a push (#575).

`meta/campfire.db` is best-effort: it only supplies the next run's stat fast path.
A mirror write that raises (on CANDIDE, two hosts wrote the SQLite file over NFS and
left it "not a database") must leave the server registration in place, warn once,
and let the remaining batches register.
"""
import sqlite3
from types import SimpleNamespace

from campfire.deploy import push, registry
from campfire.deploy.r2 import UploadTask


class BrokenStore:
    def __init__(self):
        self.calls = 0

    def upsert_storage_objects(self, rows):
        self.calls += 1
        raise sqlite3.DatabaseError("file is not a database")


def _task(i, tmp_path):
    path = tmp_path / f"f{i}.json"
    path.write_text("{}")
    return UploadTask(local_path=str(path), r2_key=f"spectra/x/f{i}.json",
                      content_type="application/json")


def test_a_failed_mirror_write_does_not_abort_registration(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    registered = []
    monkeypatch.setattr(registry, "build_registry_rows",
                        lambda batch, **kw: [{"storage_key": t.r2_key} for t in batch])
    monkeypatch.setattr(registry, "upsert_storage_objects",
                        lambda client, rows: registered.extend(rows))
    plan = SimpleNamespace(identities={}, wcs_hashes={}, whole_file={})
    store = BrokenStore()

    flusher = push.registration_flusher(object(), store, plan, backend="osn", batch_size=2)
    for i in range(5):
        flusher.add(_task(i, tmp_path))
    flusher.flush()

    assert [r["storage_key"] for r in registered] == [f"spectra/x/f{i}.json" for i in range(5)]
    assert store.calls == 1                       # dropped after the first failure
    out = capsys.readouterr().out
    assert out.count("local storage mirror write failed") == 1
    assert "file is not a database" in out
