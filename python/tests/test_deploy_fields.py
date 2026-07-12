"""Tests for the NIRCam page-redesign deploy layer:

- the `fields.toml -> fields` sync (issue #303): section parsing, deploy-time
  upsert (config + area + deployment), and the deployed-only scoping of
  `sync_fields` — all with a fake Supabase client;
- discovery of the three new deployable artifacts (expmap PNG, field layout PNG,
  mosaic thumbnails) with canonical keys.

Pure/local — no network, no real Supabase.
"""

import json
import textwrap
import types

import pytest

from campfire.deploy import fields as F
from campfire.deploy import nircam as nc
from campfire.deploy.r2 import UploadTask


# --- fake Supabase client ---------------------------------------------------

class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def select(self, _cols):
        return self

    def upsert(self, row, on_conflict=None):
        self.store["upserts"].append((self.name, on_conflict, row))
        return self

    def execute(self):
        if self.name == "deployments":
            return types.SimpleNamespace(data=self.store.get("deployments", []))
        return types.SimpleNamespace(data=[])


class _FakeClient:
    def __init__(self, deployments=None):
        self.store = {"upserts": [], "deployments": deployments or []}

    def table(self, name):
        return _FakeTable(self.store, name)


_FIELDS_TOML = """
    [cosmos]
    filters = ["f115w", "f444w"]
    files = ["jw01727*", "jw05893*"]
    tangent_point = [150.1163, 2.2009]
    fiducial_tiles = ["A1", "A2"]

    [cosmos.A1]
    "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }
    [cosmos.A2]
    rotation = 20
    "30mas" = { crpix = [3000, 1000], naxis = [2000, 2000] }
    [cosmos.epochs.CW]
    files = ["jw05893*"]

    [uds]
    filters = ["f444w"]
    files = ["jw01837*"]
    tangent_point = [34.4, -5.2]
"""


def _write_root(tmp_path, monkeypatch, toml=_FIELDS_TOML):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fields.toml").write_text(textwrap.dedent(toml))
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    return tmp_path


# --- field_config_row -------------------------------------------------------

def test_field_config_row_lifts_and_is_lossless():
    section = F.load_fields_toml  # noqa: F841 (ensure import wired)
    import tomllib
    cfg = tomllib.loads(textwrap.dedent(_FIELDS_TOML))["cosmos"]
    row = F.field_config_row("cosmos", cfg)
    assert row["name"] == "cosmos"
    assert row["filters"] == ["f115w", "f444w"]
    assert row["tiles"] == ["A1", "A2"]               # dict sub-tables, minus epochs
    assert row["fiducial_tiles"] == ["A1", "A2"]
    assert row["epochs"] == ["CW"]
    assert row["jwst_program_ids"] == [1727, 5893]    # from the files globs
    assert row["file_globs"] == ["jw01727*", "jw05893*"]
    assert row["center_ra"] == pytest.approx(150.1163)
    assert row["center_dec"] == pytest.approx(2.2009)
    assert row["programs"] == []                       # slug resolution deferred
    assert row["display_name"] is None                 # RPC derives upper()
    # config is the whole section, verbatim (lossless).
    assert row["config"] == cfg


def test_field_config_row_coerces_string_scalars():
    row = F.field_config_row("x", {"filters": "f444w", "fiducial_tiles": "A1",
                                    "files": "jw01727*"})
    assert row["filters"] == ["f444w"]
    assert row["fiducial_tiles"] == ["A1"]
    assert row["jwst_program_ids"] == [1727]
    assert row["center_ra"] is None and row["center_dec"] is None


def test_field_config_row_excludes_step_override_tables():
    # Step-override sub-tables (jhat/align/…) are dicts but carry no WCS, so they
    # must NOT be counted as tiles. Only NE (a `<N>mas` WCS table) is a tile.
    section = {
        "filters": "f444w", "files": "jw01345*",
        "jhat": {"refcat": "egs_ref.ecsv"},
        "align": {"enabled": True, "refcat": "egs_ref.ecsv"},
        "NE": {"rotation": -49.7, "30mas": {"crpix": [1, 2], "naxis": [3, 4]}},
        "epochs": {"spam": {"files": ["jw08559*"]}},
    }
    row = F.field_config_row("egs", section)
    assert row["tiles"] == ["NE"]
    assert row["epochs"] == ["spam"]


# --- declared_tiles_and_epochs ----------------------------------------------

def test_declared_tiles_and_epochs():
    section = {
        "jhat": {"refcat": "x"},                          # step table -> not a tile
        "A1": {"30mas": {"crpix": [1, 1], "naxis": [2, 2]}},   # WCS tile
        "legacy": {"corners": [[0, 0], [1, 1]]},               # explicit-corners tile
        "epochs": {"cy1": {"files": ["jw01*"]}, "spam": {"files": ["jw08*"]}},
    }
    tiles, epochs = F.declared_tiles_and_epochs(section)
    assert tiles == {"A1", "legacy"}
    assert epochs == {"cy1", "spam"}


# --- scope_mosaics_to_fields_toml -------------------------------------------

def _mos(tile, epoch=""):
    return {"tile": tile, "epoch": epoch, "storage_key": f"k/{tile}/{epoch}"}


def test_scope_mosaics_keeps_declared_drops_stray(tmp_path, monkeypatch, capsys):
    _write_root(tmp_path, monkeypatch)   # cosmos declares tiles A1, A2 + epoch CW
    mosaics = [
        _mos("A1"), _mos("A2"),          # declared full-field tiles -> kept
        _mos("A1", "CW"),                # declared tile + declared epoch -> kept
        _mos("full"),                    # stray tile -> dropped
        _mos("A2", "bogus"),             # declared tile, undeclared epoch -> dropped
    ]
    kept = nc.scope_mosaics_to_fields_toml(mosaics, "cosmos")
    assert sorted((m["tile"], m["epoch"]) for m in kept) == [
        ("A1", ""), ("A1", "CW"), ("A2", "")]
    out = capsys.readouterr().out
    assert "tile 'full'" in out and "tile not declared" in out
    assert "epoch 'bogus'" in out


def test_scope_mosaics_raises_when_field_absent(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="no \\[ghost\\] section in fields.toml"):
        nc.scope_mosaics_to_fields_toml([_mos("t1")], "ghost")


def test_scope_mosaics_empty_is_noop_without_fields_toml(tmp_path, monkeypatch):
    # No mosaics -> nothing to scope, so a missing section never bites.
    _write_root(tmp_path, monkeypatch)
    assert nc.scope_mosaics_to_fields_toml([], "ghost") == []


# --- read_layout_coverage ---------------------------------------------------

def test_read_layout_coverage(tmp_path):
    (tmp_path / "cosmos_layout.json").write_text(
        json.dumps({"coverage_area_arcmin2": 1944.0, "coverage_area_deg2": 0.54}))
    cov = F.read_layout_coverage(tmp_path, "cosmos")
    assert cov["coverage_area_arcmin2"] == 1944.0
    assert F.read_layout_coverage(tmp_path, "absent") is None


# --- upsert_field_on_deploy -------------------------------------------------

def test_upsert_field_on_deploy_writes_config_area_and_deployment(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    products = tmp_path / "products"
    products.mkdir()
    (products / "cosmos_layout.json").write_text(
        json.dumps({"coverage_area_arcmin2": 1944.0, "coverage_area_deg2": 0.54}))
    client = _FakeClient()
    assert F.upsert_field_on_deploy(client, products, "cosmos", 42) is True
    (name, on_conflict, row), = client.store["upserts"]
    assert name == "fields" and on_conflict == "name"
    assert row["latest_deployment_id"] == 42
    assert row["coverage_area_arcmin2"] == 1944.0
    assert row["coverage_area_deg2"] == 0.54
    assert row["filters"] == ["f115w", "f444w"]


def test_upsert_field_on_deploy_skips_unknown_field(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    client = _FakeClient()
    assert F.upsert_field_on_deploy(client, tmp_path, "not_a_field", 1) is False
    assert client.store["upserts"] == []


# --- sync_fields scoping ----------------------------------------------------

def test_sync_fields_scopes_to_deployed_fields(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    # Only cosmos has a deployment; uds is defined in fields.toml but not deployed.
    client = _FakeClient(deployments=[{"field": "cosmos"}, {"field": None},
                                      {"field": "cosmos"}])
    n = F.sync_fields(client)  # field_names=None -> deployed only
    assert n == 1
    names = [row["name"] for (_t, _c, row) in client.store["upserts"]]
    assert names == ["cosmos"]
    # config-only sync never sends the deploy-owned columns.
    (_t, _c, row), = client.store["upserts"]
    assert "coverage_area_arcmin2" not in row
    assert "latest_deployment_id" not in row


def test_sync_fields_single_field(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    client = _FakeClient()
    n = F.sync_fields(client, ["uds"])
    assert n == 1
    assert client.store["upserts"][0][2]["name"] == "uds"


def test_sync_fields_dry_run_writes_nothing(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    client = _FakeClient(deployments=[{"field": "cosmos"}])
    n = F.sync_fields(client, dry_run=True)
    assert n == 0
    assert client.store["upserts"] == []


# --- new artifact discovery -------------------------------------------------

def test_discover_expmap_tasks_includes_png(tmp_path):
    products = tmp_path / "products" / "nircam" / "cosmos"
    (products / "f444w").mkdir(parents=True)
    fdir = products / "f444w"
    (fdir / "expmap_cosmos_f444w.fits").write_bytes(b"\x00")
    (fdir / "expmap_cosmos_f444w.png").write_bytes(b"\x00")
    (fdir / "expmap_cosmos_f444w_uncal.png").write_bytes(b"\x00")  # excluded
    tasks = nc.discover_expmap_tasks({"products": products}, "cosmos", ["f444w"])
    keys = sorted(t.r2_key for t in tasks)
    assert keys == [
        "data/products/nircam/cosmos/f444w/expmap_cosmos_f444w.fits",
        "data/products/nircam/cosmos/f444w/expmap_cosmos_f444w.png",
    ]
    png = next(t for t in tasks if t.r2_key.endswith(".png"))
    assert png.content_type == "image/png"


def test_discover_layout_tasks(tmp_path):
    products = tmp_path / "products" / "nircam" / "cosmos"
    products.mkdir(parents=True)
    assert nc.discover_layout_tasks({"products": products}, "cosmos") == []
    (products / "cosmos_layout.png").write_bytes(b"\x00")
    (products / "cosmos_layout_uncal.png").write_bytes(b"\x00")  # not deployed
    tasks = nc.discover_layout_tasks({"products": products}, "cosmos")
    assert [t.r2_key for t in tasks] == [
        "data/products/nircam/cosmos/cosmos_layout.png"]
    assert tasks[0].content_type == "image/png"


def test_discover_mosaic_thumbnail_tasks(tmp_path):
    fdir = tmp_path / "products" / "nircam" / "cosmos" / "f444w"
    fdir.mkdir(parents=True)
    base = "mosaic_nircam_f444w_cosmos_30mas_A1"
    (fdir / f"{base}_i2d.fits").write_bytes(b"\x00")
    (fdir / f"{base}_thumb.png").write_bytes(b"\x00")
    (fdir / f"{base}_quicklook.png").write_bytes(b"\x00")
    # two extensions of the same mosaic base -> still ONE task per rendition
    mosaics = [
        {"path": fdir / f"{base}_i2d.fits", "mosaic_name": base},
        {"path": fdir / f"{base}_sci.fits", "mosaic_name": base},
    ]
    tasks = nc.discover_mosaic_thumbnail_tasks(mosaics, "cosmos")
    assert [t.r2_key for t in tasks] == [
        f"data/products/nircam/cosmos/f444w/{base}_thumb.png",
        f"data/products/nircam/cosmos/f444w/{base}_quicklook.png"]
    assert all(t.content_type == "image/png" for t in tasks)


def test_discover_mosaic_thumbnail_tasks_thumb_only(tmp_path):
    # A field reduced before the quicklook existed ships just the thumbnail.
    fdir = tmp_path / "products" / "nircam" / "cosmos" / "f444w"
    fdir.mkdir(parents=True)
    base = "mosaic_nircam_f444w_cosmos_30mas_A1"
    (fdir / f"{base}_i2d.fits").write_bytes(b"\x00")
    (fdir / f"{base}_thumb.png").write_bytes(b"\x00")
    mosaics = [{"path": fdir / f"{base}_i2d.fits", "mosaic_name": base}]
    tasks = nc.discover_mosaic_thumbnail_tasks(mosaics, "cosmos")
    assert [t.r2_key for t in tasks] == [
        f"data/products/nircam/cosmos/f444w/{base}_thumb.png"]


def test_discover_mosaic_thumbnail_tasks_empty_when_absent(tmp_path):
    fdir = tmp_path / "products" / "nircam" / "cosmos" / "f444w"
    fdir.mkdir(parents=True)
    base = "mosaic_nircam_f444w_cosmos_30mas_A1"
    (fdir / f"{base}_i2d.fits").write_bytes(b"\x00")  # no _thumb.png
    mosaics = [{"path": fdir / f"{base}_i2d.fits", "mosaic_name": base}]
    assert nc.discover_mosaic_thumbnail_tasks(mosaics, "cosmos") == []
