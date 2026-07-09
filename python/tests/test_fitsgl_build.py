"""Tests for `campfire fitsgl build`'s pure config-generation layer (epic #337).

Exercises the fitsgl-free functions in ``campfire.fitsgl.build`` — mosaic
selection, band grouping, RGB-viewer derivation, and fitsgl.toml assembly — so CI
covers the logic without the FitsGL producer installed or any real NIRCam data.
The producer-import contract lives in ``test_fitsgl_packaging.py``.
"""

import json

import toml

from campfire.fitsgl.build import (
    build_fitsgl_toml,
    derive_viewer,
    group_bands,
    select_mosaics,
)


# --- build_fitsgl_toml ------------------------------------------------------

def test_build_fitsgl_toml_composite_inputs_are_lists(tmp_path):
    a, b, c = tmp_path / "a.fits", tmp_path / "b.fits", tmp_path / "c.fits"
    for p in (a, b, c):
        p.write_bytes(b"\x00")
    bands = {"f150w": [a, b], "f444w": [c]}
    viewer = {"default": "single", "band": "f444w", "stretch": "asinh"}

    d = build_fitsgl_toml("cosmos", bands=bands, viewer=viewer,
                          pixel_scale="30mas", single_tile=False)

    assert d["dataset"]["name"] == "cosmos"
    assert d["build"]["shared_grid"] is True
    assert d["build"]["quantize_level"] == 8
    by_name = {b["name"]: b for b in d["dataset"]["bands"]}
    assert isinstance(by_name["f150w"]["input"], list) and len(by_name["f150w"]["input"]) == 2
    assert isinstance(by_name["f444w"]["input"], list) and len(by_name["f444w"]["input"]) == 1
    assert by_name["f150w"]["label"] == "F150W"
    # paths are absolutized so the toml can live anywhere
    assert all(p.startswith("/") for p in by_name["f150w"]["input"])


def test_build_fitsgl_toml_single_tile_input_is_str(tmp_path):
    x = tmp_path / "x.fits"
    x.write_bytes(b"\x00")
    d = build_fitsgl_toml("cosmos", bands={"f444w": x}, viewer={"default": "single"},
                          pixel_scale="30mas", single_tile=True, tile="PRIMER")
    assert d["dataset"]["name"] == "cosmos__PRIMER"
    assert isinstance(d["dataset"]["bands"][0]["input"], str)


def test_build_fitsgl_toml_serializes_as_array_of_tables(tmp_path):
    a = tmp_path / "a.fits"
    a.write_bytes(b"\x00")
    d = build_fitsgl_toml("cosmos", bands={"f444w": [a]},
                          viewer={"default": "single", "band": "f444w"},
                          pixel_scale="30mas", single_tile=False)
    # round-trips through the toml serializer FitsGL will parse
    reparsed = toml.loads(toml.dumps(d))
    assert reparsed["dataset"]["bands"][0]["name"] == "f444w"
    assert reparsed["build"]["shared_grid"] is True


# --- derive_viewer ----------------------------------------------------------

def test_derive_viewer_rgb_from_color_weights():
    rgb = {"f444w": (1.0, 0.0, 0.0), "f277w": (0.0, 1.0, 0.0), "f150w": (0.0, 0.0, 1.0)}
    v = derive_viewer(rgb, ["f150w", "f277w", "f444w"])
    assert v["default"] == "rgb"
    assert v["r"] == "f444w" and v["g"] == "f277w" and v["b"] == "f150w"
    assert v["stretch"] == "trilogy"


def test_derive_viewer_single_band_fallback_without_rgb():
    v = derive_viewer(None, ["f150w", "f444w"])
    assert v["default"] == "single"
    assert v["band"] == "f444w"  # reddest present
    assert v["stretch"] == "asinh"


def test_derive_viewer_single_when_fewer_than_three_rgb_filters():
    rgb = {"f444w": (1.0, 0.0, 0.0), "f150w": (0.0, 0.0, 1.0)}
    v = derive_viewer(rgb, ["f150w", "f444w"])
    assert v["default"] == "single"


def test_derive_viewer_empty_bands():
    v = derive_viewer(None, [])
    assert v["default"] == "single" and "band" not in v


# --- select_mosaics / group_bands (real discover_mosaics on a fake tree) -----

def _write_mosaic(products, field, filt, tile, scale, exts):
    """Create a version-free mosaic slot (manifest + given extension files)."""
    fdir = products / filt
    fdir.mkdir(parents=True, exist_ok=True)
    base = f"mosaic_nircam_{filt}_{field}_{scale}_{tile}"
    (fdir / f"{base}_manifest.json").write_text(json.dumps({
        "mosaic_name": base, "field": field, "filter": filt,
        "tile": tile, "pixel_scale": scale,
    }))
    for suffix in exts:
        (fdir / f"{base}{suffix}").write_bytes(b"\x00")


def _fake_field(tmp_path):
    """A cosmos products tree: A1/B2 at 30mas (fiducial), PRIMER + a 60mas slot."""
    products = tmp_path / "products" / "nircam" / "cosmos"
    # A1: sci + i2d present (extension preference should pick sci)
    _write_mosaic(products, "cosmos", "f444w", "A1", "30mas", ("_sci.fits", "_i2d.fits"))
    _write_mosaic(products, "cosmos", "f150w", "A1", "30mas", ("_sci.fits",))
    # B2: only i2d present (fallback)
    _write_mosaic(products, "cosmos", "f444w", "B2", "30mas", ("_i2d.fits",))
    _write_mosaic(products, "cosmos", "f150w", "B2", "30mas", ("_sci.fits",))
    # PRIMER: off-grid, not in the fiducial set
    _write_mosaic(products, "cosmos", "f444w", "PRIMER", "30mas", ("_i2d.fits",))
    # a coarser scale that must be filtered out
    _write_mosaic(products, "cosmos", "f444w", "A1", "60mas", ("_i2d.fits",))
    return {"products": products}


def test_select_mosaics_filters_scale_and_tiles(tmp_path):
    dirs = _fake_field(tmp_path)
    picked = select_mosaics(dirs, "cosmos", ["f150w", "f444w"],
                            pixel_scale="30mas", tiles={"A1", "B2"})
    # PRIMER and the 60mas slot are excluded
    assert {(m["filter"], m["tile"]) for m in picked} == {
        ("f444w", "A1"), ("f444w", "B2"), ("f150w", "A1"), ("f150w", "B2"),
    }
    # extension preference: sci wins for A1/f444w (both present); i2d for B2/f444w
    ext = {(m["filter"], m["tile"]): m["extension"] for m in picked}
    assert ext[("f444w", "A1")] == "sci"
    assert ext[("f444w", "B2")] == "i2d"


def test_group_bands_composite_lists_per_filter(tmp_path):
    dirs = _fake_field(tmp_path)
    picked = select_mosaics(dirs, "cosmos", ["f150w", "f444w"],
                            pixel_scale="30mas", tiles={"A1", "B2"})
    bands = group_bands(picked, single_tile=False)
    assert set(bands) == {"f150w", "f444w"}
    assert all(isinstance(v, list) and len(v) == 2 for v in bands.values())
    # blue→red band ordering
    assert list(bands) == ["f150w", "f444w"]


def test_group_bands_single_tile_scalar_path(tmp_path):
    dirs = _fake_field(tmp_path)
    picked = select_mosaics(dirs, "cosmos", ["f444w"], pixel_scale="30mas", tiles={"A1"})
    bands = group_bands(picked, single_tile=True)
    assert set(bands) == {"f444w"}
    assert not isinstance(bands["f444w"], list)  # single path, not a list
