"""purge_cutout_store (perf T2-D3, #509): best-effort prefix delete of a
field's stored cutouts when its imaging version changes."""

from __future__ import annotations

from campfire.deploy import tiles


class _Paginator:
    def __init__(self, keys):
        self._keys = keys

    def paginate(self, Bucket, Prefix):
        assert Prefix == "cutouts/egs/"
        yield {"Contents": [{"Key": k} for k in self._keys]}


class _Client:
    def __init__(self, keys):
        self.keys = keys
        self.deleted = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return _Paginator(self.keys)

    def delete_objects(self, Bucket, Delete):
        self.deleted.extend(o["Key"] for o in Delete["Objects"])


def test_purge_deletes_the_field_prefix(monkeypatch):
    client = _Client(["cutouts/egs/vaaa/64/5/1_+1.png", "cutouts/egs/vbbb/600/3.2/1_+1.png"])
    monkeypatch.setattr(tiles, "get_r2_tiles_client", lambda config: client)
    monkeypatch.setattr(
        "campfire.deploy.backend.resolve_backend",
        lambda config, purpose: type("B", (), {"bucket": "campfire-tiles"})(),
    )
    n = tiles.purge_cutout_store({"r2_tiles": {}}, "egs")
    assert n == 2
    assert client.deleted == client.keys


def test_purge_is_skipped_without_direct_tiles_credentials(monkeypatch, capsys):
    called = []
    monkeypatch.setattr(tiles, "get_r2_tiles_client", lambda config: called.append(1))
    assert tiles.purge_cutout_store({}, "egs") is None
    assert not called
    assert "lifecycle" in capsys.readouterr().out


def test_purge_never_raises(monkeypatch):
    def boom(config):
        raise RuntimeError("no network")
    monkeypatch.setattr(tiles, "get_r2_tiles_client", boom)
    assert tiles.purge_cutout_store({"r2_tiles": {}}, "egs") is None
