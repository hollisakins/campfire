"""The field products of every imaging instrument.

Exposure maps, the layout plot and the sky coverage were NIRCam's alone; each
imaging instrument of a field has its own set now (``cfpipe <instrument>
expmap``), and the map dataset is built from every instrument's mosaics of the
field's tiles. These tests cover the client's side: the uploads carry the
instrument's layout kinds, the coverage is recorded per instrument without
touching NIRCam's, and the map build picks up HST bands in wavelength order.
The database's side is supabase/tests/check_field_coverage.sql.
"""

import json
import textwrap
from pathlib import Path

import click
import pytest

from campfire.db.store import INTERMEDIATE_PRODUCT_TYPES
from campfire.deploy import fields as F
from campfire.deploy import nircam as nc
from campfire.deploy import registry as reg
from campfire.fitsgl import build as fb
from campfire_layout import parse_key
from tests.fakes import FakePostgrest


_FIELDS_TOML = """
    [cosmos]
    filters = ["f115w", "f444w"]
    files = ["jw01727*"]
    tangent_point = [150.1163, 2.2009]
    fiducial_tiles = ["A1"]

    [cosmos.A1]
    "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }

    [cosmos.acswfc]
    filters = ["f814w"]
    files = ["j8pu*"]

    [hstonly]
    tangent_point = [34.4, -5.2]
    fiducial_tiles = ["B1"]

    [hstonly.B1]
    "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }

    [hstonly.wfc3ir]
    filters = ["f160w"]
    files = ["iczg*"]
"""


def _write_root(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fields.toml").write_text(textwrap.dedent(_FIELDS_TOML))
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    return tmp_path


def _mosaic_on_disk(root, instrument, field, filt, tile, scale="30mas"):
    fdir = root / "products" / instrument / field / filt
    fdir.mkdir(parents=True, exist_ok=True)
    base = f"mosaic_{instrument}_{filt}_{field}_{scale}_{tile}"
    (fdir / f"{base}_manifest.json").write_text(json.dumps({
        "mosaic_name": base, "field": field, "filter": filt, "tile": tile,
        "pixel_scale": scale}))
    (fdir / f"{base}_sci.fits").write_bytes(b"\x00")
    return fdir / f"{base}_sci.fits"


# ---------------------------------------------------------------------------
# uploads: exposure maps and the layout plot, per instrument
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("instrument, filt", [("acswfc", "f814w"), ("wfc3ir", "f160w")])
def test_exposure_maps_and_layout_upload_under_the_instruments_kinds(tmp_path, instrument, filt):
    products = tmp_path / "products" / instrument / "cosmos"
    fdir = products / filt
    fdir.mkdir(parents=True)
    for name in (f"expmap_cosmos_{filt}.fits", f"expmap_cosmos_{filt}.png",
                 f"expmap_cosmos_{filt}.pdf", f"expmap_cosmos_{filt}_uncal.fits"):
        (fdir / name).write_bytes(b"x")
    (products / "cosmos_layout.png").write_bytes(b"\x89PNG")
    (products / "cosmos_layout.json").write_text("{}")
    dirs = {"products": products}
    profile = nc.imaging_profile(instrument)

    keys = sorted(t.r2_key for t in nc.discover_expmap_tasks(dirs, "cosmos", [filt], profile))
    # the map and its dark plot; the light PDF and a stage-decorated map stay local
    assert keys == [
        f"data/products/{instrument}/cosmos/{filt}/expmap_cosmos_{filt}.fits",
        f"data/products/{instrument}/cosmos/{filt}/expmap_cosmos_{filt}.png",
    ]
    assert [parse_key(k).product_type for k in keys] == [
        f"{instrument}_expmap", f"{instrument}_expmap_plot"]

    (layout,) = nc.discover_layout_tasks(dirs, "cosmos", profile)
    assert layout.r2_key == f"data/products/{instrument}/cosmos/cosmos_layout.png"
    row = reg.row_for_key(layout.r2_key, backend="osn", content_hash="sha256:" + "a" * 64,
                          size_bytes=1, content_type="image/png")
    assert row["product_type"] == f"{instrument}_layout"
    assert row["instrument"] == instrument and row["field"] == "cosmos"

    # the exposure map is an intermediate a user can pull, as NIRCam's is
    assert f"{instrument}_expmap" in INTERMEDIATE_PRODUCT_TYPES


def test_nircam_field_products_keep_their_kinds(tmp_path):
    products = tmp_path / "products" / "nircam" / "cosmos"
    (products / "f444w").mkdir(parents=True)
    (products / "f444w" / "expmap_cosmos_f444w.fits").write_bytes(b"x")
    (products / "cosmos_layout.png").write_bytes(b"\x89PNG")
    dirs = {"products": products}
    (task,) = nc.discover_expmap_tasks(dirs, "cosmos", ["f444w"])
    assert parse_key(task.r2_key).product_type == "nircam_expmap"
    (layout,) = nc.discover_layout_tasks(dirs, "cosmos")
    assert parse_key(layout.r2_key).product_type == "nircam_layout"


# ---------------------------------------------------------------------------
# coverage: one entry per instrument
# ---------------------------------------------------------------------------

def test_an_hst_deploy_records_its_coverage_under_its_own_key(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    products = tmp_path / "products" / "acswfc" / "cosmos"
    products.mkdir(parents=True)
    (products / "cosmos_layout.json").write_text(json.dumps({
        "instrument": "acswfc", "coverage_area_arcmin2": 58.2,
        "coverage_area_deg2": 0.0161667, "pixel_scale_arcsec": 0.5}))
    client = FakePostgrest()
    assert F.upsert_field_on_deploy(client, products, "cosmos", 77, instrument="acswfc") is True

    # the config upsert carries nothing of NIRCam's deploy-owned columns
    (name, _conflict, row), = client.upserts
    assert name == "fields"
    for column in ("latest_deployment_id", "coverage_area_arcmin2",
                   "coverage_area_deg2", "expmap_pixel_scale_arcsec",
                   "instrument_coverage"):
        assert column not in row
    # the coverage goes through the merging RPC, one instrument's entry
    (rpc,) = [r for r in client.rpcs if r.name == "set_field_coverage"]
    assert rpc.params == {
        "p_field": "cosmos", "p_instrument": "acswfc",
        "p_coverage": {"area_arcmin2": 58.2, "area_deg2": 0.0161667,
                       "pixel_scale_arcsec": 0.5, "deployment_id": 77}}


def test_an_hst_deploy_without_a_layout_file_says_so_and_records_nothing(tmp_path, monkeypatch, capsys):
    _write_root(tmp_path, monkeypatch)
    products = tmp_path / "products" / "wfc3ir" / "hstonly"
    products.mkdir(parents=True)
    client = FakePostgrest()
    assert F.upsert_field_on_deploy(client, products, "hstonly", 5, instrument="wfc3ir") is True
    assert "set_field_coverage" not in client.rpc_names()
    assert "cfpipe wfc3ir expmap --field hstonly" in capsys.readouterr().out
    # the field row still exists for a field with no NIRCam at all
    (_name, _conflict, row), = client.upserts
    assert row["name"] == "hstonly" and row["filters"] == []


def test_a_nircam_deploy_writes_the_typed_columns_as_before(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    products = tmp_path / "products" / "nircam" / "cosmos"
    products.mkdir(parents=True)
    (products / "cosmos_layout.json").write_text(json.dumps({
        "coverage_area_arcmin2": 1944.0, "coverage_area_deg2": 0.54,
        "pixel_scale_arcsec": 0.5}))
    client = FakePostgrest()
    assert F.upsert_field_on_deploy(client, products, "cosmos", 42, instrument="nircam") is True
    (_name, _conflict, row), = client.upserts
    assert row["latest_deployment_id"] == 42
    assert row["coverage_area_arcmin2"] == 1944.0
    assert row["expmap_pixel_scale_arcsec"] == 0.5
    assert "set_field_coverage" not in client.rpc_names()


def test_coverage_payload():
    assert F.instrument_coverage_payload(None, 1) is None
    assert F.instrument_coverage_payload({"field": "x"}, 1) is None
    assert F.instrument_coverage_payload({"pixel_scale_arcsec": 0.5}, None) == {
        "pixel_scale_arcsec": 0.5}


# ---------------------------------------------------------------------------
# the map dataset: every instrument's mosaics of the field's tiles
# ---------------------------------------------------------------------------

def test_the_map_build_takes_hst_bands_in_wavelength_order(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    _mosaic_on_disk(tmp_path, "nircam", "cosmos", "f444w", "A1")
    _mosaic_on_disk(tmp_path, "nircam", "cosmos", "f115w", "A1")
    acs = _mosaic_on_disk(tmp_path, "acswfc", "cosmos", "f814w", "A1")
    _mosaic_on_disk(tmp_path, "wfc3ir", "cosmos", "f160w", "A1")
    _mosaic_on_disk(tmp_path, "wfc3ir", "cosmos", "f160w", "A9")       # not a fiducial tile

    mosaics = fb.select_field_mosaics("cosmos", pixel_scale="30mas", tiles=["A1"])
    assert {(m["instrument"], m["filter"]) for m in mosaics} == {
        ("nircam", "f115w"), ("nircam", "f444w"), ("acswfc", "f814w"), ("wfc3ir", "f160w")}
    # ACS F814W is 0.81 micron (its number is in nm), WFC3/IR F160W 1.6 micron
    bands = fb.group_bands(mosaics, single_tile=False)
    assert list(bands) == ["f814w", "f115w", "f160w", "f444w"]
    assert bands["f814w"] == [acs]
    # the storage key the staleness hook hashes is the instrument's own
    key = next(m["storage_key"] for m in mosaics if m["instrument"] == "acswfc")
    assert key.startswith("data/products/acswfc/cosmos/f814w/")


def test_the_default_single_band_is_the_reddest_across_instruments():
    instruments = {"f814w": "acswfc", "f160w": "wfc3ir", "f115w": "nircam"}
    v = fb.derive_viewer(None, ["f814w", "f160w", "f115w"], instruments=instruments)
    assert v["band"] == "f160w"
    # read as a NIRCam name, f814w would be 8.14 micron and win
    assert fb._filter_wavelength("f814w", "acswfc") < fb._filter_wavelength("f115w", "nircam")
    assert fb._filter_wavelength("f850lp", "acswfc")[1] == 850


def test_a_field_with_hst_only_builds_from_its_own_tiles(tmp_path, monkeypatch):
    pytest.importorskip("campfire_pipeline")
    _write_root(tmp_path, monkeypatch)
    _mosaic_on_disk(tmp_path, "wfc3ir", "hstonly", "f160w", "B1")
    # no top-level filters / files: the tiles are still the field's
    assert fb.resolve_fiducial_tiles("hstonly") == ["B1"]
    mosaics = fb.select_field_mosaics("hstonly", pixel_scale="30mas", tiles=["B1"])
    assert [(m["instrument"], m["filter"]) for m in mosaics] == [("wfc3ir", "f160w")]


def test_two_instruments_with_one_filter_name_are_refused(tmp_path, monkeypatch):
    _write_root(tmp_path, monkeypatch)
    _mosaic_on_disk(tmp_path, "nircam", "cosmos", "f814w", "A1")
    _mosaic_on_disk(tmp_path, "acswfc", "cosmos", "f814w", "A1")
    with pytest.raises(click.ClickException, match="from both nircam and acswfc"):
        fb.select_field_mosaics("cosmos", pixel_scale="30mas", tiles=["A1"])
