"""Tests for `campfire fitsgl deploy`'s pure layer (epic #337, Phase 3).

Exercises the fitsgl-free, network-free functions in ``campfire.fitsgl.deploy`` —
prefix/name derivation, DeployConfig-kwargs assembly (the public_url == base/prefix
invariant), source-hash nesting, and the fitsgl_datasets row — so CI covers the logic
without the FitsGL producer installed, a live bucket, or Supabase.
"""

import os

import pytest

from campfire.fitsgl.deploy import (
    _hash_count,
    build_deploy_config,
    compute_source_hashes,
    dataset_name,
    dataset_prefix,
    dataset_row,
    fitsgl_json_url,
)


# --- prefix / name ----------------------------------------------------------

def test_dataset_prefix_composite_and_tile():
    assert dataset_prefix("cosmos") == "fitsgl/cosmos/composite"
    assert dataset_prefix("cosmos", tile="PRIMER") == "fitsgl/cosmos/tile/PRIMER"


def test_prefixes_are_disjoint_from_png_tile_keyspace():
    # PNG tiles are `<field>/<filter>/<z>/<x>/<y>.png`; FitsGL lives under `fitsgl/`,
    # and composite is never an ancestor of a tile prefix (sibling paths).
    comp = dataset_prefix("cosmos")
    tile = dataset_prefix("cosmos", tile="A1")
    assert comp.startswith("fitsgl/") and tile.startswith("fitsgl/")
    assert not tile.startswith(comp + "/") and not comp.startswith(tile + "/")


def test_dataset_name_matches_build_naming():
    assert dataset_name("cosmos") == "cosmos"
    assert dataset_name("cosmos", tile="PRIMER") == "cosmos__PRIMER"


# --- build_deploy_config ----------------------------------------------------

def test_build_deploy_config_public_url_is_base_slash_prefix():
    creds = {"bucket": "campfire-tiles", "endpoint": "https://r2.example.com",
             "public_url_base": "https://tiles.campfire.example/"}
    dc = build_deploy_config(creds, "fitsgl/cosmos/composite")
    # trailing slash on base is normalized; prefix appended
    assert dc["public_url"] == "https://tiles.campfire.example/fitsgl/cosmos/composite"
    assert dc["prefix"] == "fitsgl/cosmos/composite"
    assert dc["bucket"] == "campfire-tiles"
    assert dc["viewer_origin"] == "*"


def test_build_deploy_config_requires_public_url_base():
    with pytest.raises(ValueError, match="public_url_base"):
        build_deploy_config({"bucket": "b", "endpoint": "e"}, "fitsgl/cosmos/composite")


def test_build_deploy_config_zone_id_from_creds_then_env(monkeypatch):
    base = {"bucket": "b", "endpoint": "e", "public_url_base": "https://t/"}
    monkeypatch.delenv("CLOUDFLARE_ZONE_ID", raising=False)
    # creds win
    dc = build_deploy_config({**base, "cf_zone_id": "zone-from-creds"}, "p")
    assert dc["zone_id"] == "zone-from-creds"
    # env fallback
    monkeypatch.setenv("CLOUDFLARE_ZONE_ID", "zone-from-env")
    assert build_deploy_config(base, "p")["zone_id"] == "zone-from-env"
    # neither → None (purge skipped downstream)
    monkeypatch.delenv("CLOUDFLARE_ZONE_ID", raising=False)
    assert build_deploy_config(base, "p")["zone_id"] is None


def test_fitsgl_json_url_appends_manifest():
    assert fitsgl_json_url("https://t/fitsgl/cosmos/composite") == \
        "https://t/fitsgl/cosmos/composite/fitsgl.json"


# --- compute_source_hashes --------------------------------------------------

def test_compute_source_hashes_nests_by_tile_then_filter():
    mosaics = [
        {"tile": "A1", "filter": "f444w", "storage_key": "k1"},
        {"tile": "A1", "filter": "f150w", "storage_key": "k2"},
        {"tile": "B2", "filter": "f444w", "storage_key": "k3"},
    ]
    by_key = {"k1": "sha256:aaa", "k2": "sha256:bbb", "k3": "sha256:ccc"}
    out = compute_source_hashes(mosaics, by_key)
    assert out == {
        "A1": {"f444w": "sha256:aaa", "f150w": "sha256:bbb"},
        "B2": {"f444w": "sha256:ccc"},
    }


def test_compute_source_hashes_skips_unknown_keys():
    mosaics = [{"tile": "A1", "filter": "f444w", "storage_key": "missing"}]
    assert compute_source_hashes(mosaics, {}) == {}


def test_hash_count_counts_leaf_hashes():
    # the staleness hook uses this to detect an incomplete (registry-only) resolve
    assert _hash_count({}) == 0
    assert _hash_count({"A1": {"f444w": "sha256:x"}}) == 1
    assert _hash_count({"A1": {"f444w": "sha256:x", "f150w": "sha256:y"},
                        "B2": {"f444w": "sha256:z"}}) == 3


# --- dataset_row ------------------------------------------------------------

def test_dataset_row_composite_is_default_field_kind():
    row = dataset_row(
        field="cosmos", tile=None, prefix="fitsgl/cosmos/composite",
        pixel_scale="30mas", fitsgl_json="https://t/.../fitsgl.json",
        bands=["f150w", "f444w"], tiles=["B2", "A1"],
        source_hashes={"A1": {"f444w": "sha256:x"}}, is_default=True,
    )
    assert row["kind"] == "field"
    assert row["tile"] is None
    assert row["is_default"] is True
    assert row["tiles"] == ["A1", "B2"]  # sorted
    assert row["bands"] == ["f150w", "f444w"]
    assert row["schema_version"] == 1
    assert row["prefix"] == "fitsgl/cosmos/composite"


def test_dataset_row_single_tile_is_tile_kind_not_default():
    row = dataset_row(
        field="cosmos", tile="PRIMER", prefix="fitsgl/cosmos/tile/PRIMER",
        pixel_scale="30mas", fitsgl_json="https://t/.../fitsgl.json",
        bands=["f444w"], tiles=["PRIMER"], source_hashes={}, is_default=False,
    )
    assert row["kind"] == "tile"
    assert row["tile"] == "PRIMER"
    assert row["is_default"] is False


# --- import contract --------------------------------------------------------

def test_pure_layer_imports_without_fitsgl():
    # The module must import (and its pure helpers run) without the FitsGL producer.
    import importlib
    mod = importlib.import_module("campfire.fitsgl.deploy")
    assert hasattr(mod, "run_deploy") and hasattr(mod, "suggest_fitsgl_rebuild")
