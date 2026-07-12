"""Tests for `campfire fitsgl deploy`'s pure layer (epic #337, Phase 3).

Exercises the fitsgl-free, network-free functions in ``campfire.fitsgl.deploy`` —
prefix/name derivation, DeployConfig-kwargs assembly (the public_url == base/prefix
invariant), source-hash nesting, and the fitsgl_datasets row — so CI covers the logic
without the FitsGL producer installed, a live bucket, or Supabase.
"""

import os

import click
import pytest

from campfire.fitsgl.deploy import (
    _hash_count,
    build_deploy_config,
    compute_source_hashes,
    dataset_name,
    dataset_prefix,
    dataset_row,
    fetch_tiles_credentials,
    fitsgl_json_url,
    resolve_tiles_creds,
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


# --- server-error surfacing (fetch_tiles_credentials) -----------------------
#
# The login-mode credential fetch must turn a failed web-API response into a clean,
# actionable message instead of a bare `raise_for_status` traceback — this is the bug
# behind the opaque "500 Server Error: Internal Server Error" the deployer hit.

class _FakeResp:
    def __init__(self, status_code, *, json_body=None, text=''):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json = json_body
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def _patch_login(monkeypatch, resp):
    """Make fetch_tiles_credentials believe it's a logged-in admin and return `resp`."""
    import requests

    import campfire.api.session as session
    import campfire.auth.tokens as tokens

    monkeypatch.setattr(session, "resolve_base_url", lambda *a, **k: "https://x/api/v1")

    class _TM:
        def __init__(self, *a, **k):
            pass

        def is_oauth(self):
            return True

        def get_valid_token(self):
            return "tok"

    monkeypatch.setattr(tokens, "TokenManager", _TM)
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)


def test_fetch_tiles_credentials_returns_json_on_success(monkeypatch):
    creds = {"bucket": "campfire-tiles", "endpoint": "https://e", "access_key_id": "k",
             "secret_access_key": "s", "public_url_base": "https://cdn"}
    _patch_login(monkeypatch, _FakeResp(200, json_body=creds))
    assert fetch_tiles_credentials({}) == creds


def test_fetch_tiles_credentials_500_surfaces_server_detail_and_fallback(monkeypatch):
    _patch_login(monkeypatch, _FakeResp(
        500, json_body={"error": "server_error",
                        "error_description": "Tiles storage not configured on the server: "
                                             "Storage \"tiles\" credentials not configured"}))
    with pytest.raises(click.ClickException) as exc:
        fetch_tiles_credentials({})
    msg = exc.value.message
    assert "500" in msg
    assert "Tiles storage not configured on the server" in msg   # server detail surfaced
    assert "--service-role" in msg                                # actionable fallback
    assert "raise_for_status" not in msg


def test_fetch_tiles_credentials_500_without_json_body_still_actionable(monkeypatch):
    # A proxy 502/HTML body (no JSON) must not crash the detail extractor.
    _patch_login(monkeypatch, _FakeResp(502, text="<html>Bad Gateway</html>"))
    with pytest.raises(click.ClickException) as exc:
        fetch_tiles_credentials({})
    assert "502" in exc.value.message
    assert "--service-role" in exc.value.message


def test_fetch_tiles_credentials_403_is_admin_message(monkeypatch):
    _patch_login(monkeypatch, _FakeResp(403, json_body={"error": "forbidden"}))
    with pytest.raises(click.ClickException) as exc:
        fetch_tiles_credentials({})
    assert "admin" in exc.value.message.lower()


def test_fetch_tiles_credentials_requires_login(monkeypatch):
    import campfire.api.session as session
    import campfire.auth.tokens as tokens

    monkeypatch.setattr(session, "resolve_base_url", lambda *a, **k: "https://x/api/v1")

    class _TM:
        def __init__(self, *a, **k):
            pass

        def is_oauth(self):
            return False

    monkeypatch.setattr(tokens, "TokenManager", _TM)
    with pytest.raises(click.ClickException) as exc:
        fetch_tiles_credentials({})
    assert "campfire login" in exc.value.message


# --- public_url_base resolution (resolve_tiles_creds) -----------------------
#
# The tiles CDN origin (public_url_base) is non-secret, so login mode accepts a local
# override as a fallback when the web app didn't supply it; a genuinely-missing value
# must fail with mode-correct guidance.

_TILES_PUBLIC_ENVS = (
    "CAMPFIRE_S3_TILES_PUBLIC_URL_BASE",
    "CAMPFIRE_R2_TILES_PUBLIC_URL_BASE",
    "CAMPFIRE_R2_TILES_PUBLIC_URL",
)


def _clear_local_tiles_public_url(monkeypatch):
    """Drop any ambient CAMPFIRE_*_TILES_PUBLIC_URL* so the local fallback is empty."""
    for v in _TILES_PUBLIC_ENVS:
        monkeypatch.delenv(v, raising=False)


def test_resolve_tiles_creds_login_returns_server_creds(monkeypatch):
    server = {"bucket": "campfire-tiles", "endpoint": "https://e",
              "access_key_id": "k", "secret_access_key": "s",
              "public_url_base": "https://tiles.cdn"}
    monkeypatch.setattr("campfire.fitsgl.deploy.fetch_tiles_credentials",
                        lambda cfg: server)
    assert resolve_tiles_creds({"supabase": {"_auth_mode": "login"}}) == server


def test_resolve_tiles_creds_login_missing_public_url_points_at_server(monkeypatch):
    _clear_local_tiles_public_url(monkeypatch)  # no local fallback -> must raise
    monkeypatch.setattr("campfire.fitsgl.deploy.fetch_tiles_credentials",
                        lambda cfg: {"bucket": "b", "endpoint": "e",
                                     "public_url_base": None})
    with pytest.raises(click.ClickException) as exc:
        resolve_tiles_creds({"supabase": {"_auth_mode": "login"}})
    msg = exc.value.message
    assert "S3_TILES_PUBLIC_URL_BASE" in msg          # server-side var named
    assert "web deployment" in msg
    assert "redeploy" in msg                          # the Vercel gotcha
    assert "override" in msg                          # local fallback offered


def test_resolve_tiles_creds_login_falls_back_to_local_env(monkeypatch):
    # Web app returned public_url_base: null, but the deployer set it locally — use it,
    # so a server env that hasn't propagated (Vercel var added w/o redeploy) is recoverable.
    _clear_local_tiles_public_url(monkeypatch)
    monkeypatch.setenv("CAMPFIRE_S3_TILES_PUBLIC_URL_BASE", "https://tiles.local.cdn")
    monkeypatch.setattr("campfire.fitsgl.deploy.fetch_tiles_credentials",
                        lambda cfg: {"bucket": "b", "endpoint": "e", "public_url_base": None})
    creds = resolve_tiles_creds({"supabase": {"_auth_mode": "login"}})
    assert creds["public_url_base"] == "https://tiles.local.cdn"


def test_resolve_tiles_creds_login_falls_back_to_config_r2_tiles(monkeypatch):
    _clear_local_tiles_public_url(monkeypatch)  # env empty -> config wins
    monkeypatch.setattr("campfire.fitsgl.deploy.fetch_tiles_credentials",
                        lambda cfg: {"bucket": "b", "endpoint": "e", "public_url_base": None})
    cfg = {"supabase": {"_auth_mode": "login"},
           "r2_tiles": {"public_url_base": "https://cfg.cdn"}}
    assert resolve_tiles_creds(cfg)["public_url_base"] == "https://cfg.cdn"


def test_resolve_tiles_creds_service_role_missing_public_url_points_at_local(monkeypatch):
    _clear_local_tiles_public_url(monkeypatch)

    class _B:
        endpoint = "https://e"; region = "auto"; bucket = "b"
        access_key_id = "k"; secret_access_key = "s"
        force_path_style = False; public_url_base = None

    import campfire.deploy.backend as backend
    monkeypatch.setattr(backend, "resolve_backend", lambda cfg, purpose: _B())
    with pytest.raises(click.ClickException) as exc:
        resolve_tiles_creds({"supabase": {"_auth_mode": "service_role"}})
    msg = exc.value.message
    assert "CAMPFIRE_S3_TILES_PUBLIC_URL_BASE" in msg   # local var
    assert "deploy.toml" in msg
