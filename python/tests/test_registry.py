"""Unit tests for the storage_objects registry helpers (#214).

Pure functions only — no DB, no bucket. The DB-backed behavior (RLS, the
get_storage_budget gate, seed coverage) is exercised by `supabase db reset` +
the `campfire deploy registry` CLI against a local instance.
"""

from pathlib import Path

import pytest

from campfire.deploy import registry as reg
from campfire.deploy.r2 import UploadTask


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------

def test_hash_file_sha256_prefixed_and_sized(tmp_path):
    p = tmp_path / "x.fits"
    p.write_bytes(b"hello world" * 1000)
    content_hash, size = reg.hash_file(p)
    assert content_hash.startswith("sha256:")
    assert len(content_hash) == len("sha256:") + 64  # hex sha256
    assert size == 11000


def test_normalize_etag_strips_quotes_and_prefixes():
    assert reg.normalize_etag('"abc123"') == "etag:abc123"
    assert reg.normalize_etag("abc-2") == "etag:abc-2"
    assert reg.normalize_etag(None) is None
    assert reg.normalize_etag('""') is None


# ---------------------------------------------------------------------------
# row_for_key — key → row mapping via the campfire_layout contract
# ---------------------------------------------------------------------------

OBS = "ember_egs_p1"
SPEC_KEY = f"spectra/{OBS}/{OBS}_prism_clear_12345_spec.fits"
JSON_KEY = f"spectra/{OBS}/{OBS}_prism_clear_12345_spec.json"
ZFIT_KEY = f"spectra/{OBS}/{OBS}_prism_clear_12345_zfit.json"
RGB_KEY = f"rgb/{OBS}/12345_rgb.png"
SED_KEY = f"sed/{OBS}/12345_sed.pdf"
NIRCAM_PREVIEW_KEY = "nircam/exposures/cosmos/f444w/jw1234_nrcalong_preview.png"
PHOT_KEY = "photometry/cosmos/12345_pz.json"
TILE_KEY = "cosmos/f444w/5/10/12.png"


def _row(key, **kw):
    kw.setdefault("backend", "r2")
    kw.setdefault("content_hash", "sha256:" + "a" * 64)
    kw.setdefault("size_bytes", 10)
    kw.setdefault("content_type", "application/octet-stream")
    return reg.row_for_key(key, **kw)


def test_row_for_nirspec_spec():
    row = _row(SPEC_KEY)
    assert row["product_type"] == "nirspec_spec"
    assert row["bucket"] == "data"
    assert row["instrument"] == "nirspec"
    assert row["observation"] == OBS
    assert row["field"] is None
    assert row["spectrum_id"] == f"{OBS}_prism_clear_12345"
    assert row["status"] == "active"
    assert row["exposure_ref"] is None  # reserved for intermediates


def test_row_spectrum_id_for_json_but_not_zfit_rgb():
    assert _row(JSON_KEY)["spectrum_id"] == f"{OBS}_prism_clear_12345"
    assert _row(ZFIT_KEY)["spectrum_id"] is None
    assert _row(RGB_KEY)["spectrum_id"] is None
    assert _row(SED_KEY)["spectrum_id"] is None


def test_row_for_nircam_preview_has_field_not_obs():
    row = _row(NIRCAM_PREVIEW_KEY)
    assert row["product_type"] == "nircam_exposure_preview"
    assert row["instrument"] == "nircam"
    assert row["field"] == "cosmos"
    assert row["observation"] is None


def test_row_for_photometry_pz_instrument_null():
    row = _row(PHOT_KEY)
    assert row["product_type"] == "photometry_pz"
    assert row["instrument"] is None
    assert row["field"] == "cosmos"


def test_tiles_are_not_registered():
    # Decision F1-B: tiles are aggregated in map_layers, never per-object rows.
    assert reg.row_for_key(TILE_KEY, backend="r2", content_hash="etag:x",
                           size_bytes=1, content_type="image/png", bucket="tiles") is None


# ---------------------------------------------------------------------------
# build_registry_rows — hashing + succeeded_keys filtering
# ---------------------------------------------------------------------------

def test_build_registry_rows_hashes_and_filters(tmp_path):
    spec = tmp_path / "a_spec.fits"
    spec.write_bytes(b"\x00" * 2048)
    rgb = tmp_path / "b_rgb.png"
    rgb.write_bytes(b"\x01" * 512)
    tasks = [
        UploadTask(spec, SPEC_KEY, "application/fits"),
        UploadTask(rgb, RGB_KEY, "image/png"),
    ]

    # Only the spec key "succeeded" → only it is registered.
    rows = reg.build_registry_rows(
        tasks, backend="r2", deployment_id=7, uploaded_by="u1",
        cfpipe_version="1.2.3", succeeded_keys={SPEC_KEY},
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["storage_key"] == SPEC_KEY
    assert r["size_bytes"] == 2048
    assert r["content_hash"].startswith("sha256:")
    assert r["deployment_id"] == 7
    assert r["uploaded_by"] == "u1"
    assert r["cfpipe_version"] == "1.2.3"


def test_build_registry_rows_skips_missing_local_files(tmp_path):
    missing = tmp_path / "gone_spec.fits"  # never created
    tasks = [UploadTask(missing, SPEC_KEY, "application/fits")]
    rows = reg.build_registry_rows(tasks, backend="r2", succeeded_keys={SPEC_KEY})
    assert rows == []


def test_build_registry_rows_none_succeeded_includes_all(tmp_path):
    spec = tmp_path / "a_spec.fits"
    spec.write_bytes(b"z" * 100)
    rows = reg.build_registry_rows(
        [UploadTask(spec, SPEC_KEY, "application/fits")], backend="r2",
    )
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# compute_reconcile — the coverage gate set logic
# ---------------------------------------------------------------------------

def test_reconcile_coverage_gap_when_pointer_unregistered():
    rep = reg.compute_reconcile(["a", "b", "c"], ["a", "b"])
    assert rep.missing == {"c"}
    assert not rep.covered


def test_reconcile_full_coverage():
    rep = reg.compute_reconcile(["a", "b"], ["a", "b", "extra"])
    assert rep.missing == set()
    assert rep.covered


def test_reconcile_dangling_and_orphans_with_bucket():
    live = [SPEC_KEY]
    registry = [SPEC_KEY, "spectra/o/ghost_spec.fits"]  # ghost has no bucket object
    bucket = [SPEC_KEY, RGB_KEY, "evil/../x"]            # rgb orphan + an unknown key
    rep = reg.compute_reconcile(live, registry, bucket)
    assert rep.covered                                   # live pointer is registered
    assert "spectra/o/ghost_spec.fits" in rep.dangling
    assert RGB_KEY in rep.orphans
    assert RGB_KEY in rep.adoptable                      # parses to a known product
    assert "evil/../x" in rep.orphans
    assert "evil/../x" not in rep.adoptable              # not a known key


def test_reconcile_no_bucket_skips_dangling_orphan():
    rep = reg.compute_reconcile(["a"], ["a"], None)
    assert rep.dangling == set()
    assert rep.orphans == set()
    assert rep.covered
