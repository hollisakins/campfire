"""Tests for the generic, product-type-agnostic download engine (epic #210).

The engine plans locally from the storage_objects mirror, presigns the
to-download set, fetches + verifies, and records local state — the one path for
finals, intermediates, and (later) NIRCam.
"""

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from campfire.db.store import (
    LocalStore,
    FINAL_PRODUCT_TYPES,
    INTERMEDIATE_PRODUCT_TYPES,
)
from campfire.sync import download_objects

FINAL_KEY = "spectra/test_obs/test_obs_prism_100_spec.fits"
EXP_KEY = "data/products/nirspec/test_obs/jw01_00001_nrs1_100.fits"
CONTENT = b"FAKE-FITS-BYTES" * 64
CONTENT_SHA = "sha256:" + hashlib.sha256(CONTENT).hexdigest()


@pytest.fixture
def store(tmp_path):
    s = LocalStore(tmp_path / "campfire.db")
    yield s
    s.close()


def _so(key, product_type, content_hash, spectrum_id=None, exposure_ref=None):
    return {
        "storage_key": key, "backend": "r2", "bucket": "data",
        "content_hash": content_hash, "size_bytes": len(CONTENT),
        "content_type": "application/fits", "product_type": product_type,
        "instrument": "nirspec", "status": "active", "observation": "test_obs",
        "field": "cosmos", "spectrum_id": spectrum_id, "exposure_ref": exposure_ref,
        "deployment_id": 1,
    }


class _FakeAPI:
    """Presigns only authorized keys (the rest are silently denied)."""
    def __init__(self, authorized):
        self._authorized = set(authorized)

    def presign_keys(self, keys):
        return {k: f"https://signed.example/{k}" for k in keys if k in self._authorized}


def _fake_session(content=CONTENT):
    resp = MagicMock()
    resp.raise_for_status = Mock()
    resp.iter_content = Mock(return_value=[content])
    sess = MagicMock()
    sess.get = Mock(return_value=resp)
    return sess


def test_downloads_final(store, tmp_path):
    store.upsert_storage_objects([_so(FINAL_KEY, "nirspec_spec", CONTENT_SHA, "test_obs_prism_100")])
    products = tmp_path / "products"

    stats = download_objects(
        _FakeAPI([FINAL_KEY]), ["test_obs"], list(FINAL_PRODUCT_TYPES),
        store, products, download_session=_fake_session(),
    )

    assert stats["downloaded"] == 1
    assert stats["failed"] == 0
    placed = products / "nirspec" / "test_obs" / "test_obs_prism_100_spec.fits"
    assert placed.exists()
    assert store.find_local_path("test_obs_prism_100") == "nirspec/test_obs/test_obs_prism_100_spec.fits"


def test_intermediate_only_skips_finals(store, tmp_path):
    store.upsert_storage_objects([
        _so(FINAL_KEY, "nirspec_spec", CONTENT_SHA, "test_obs_prism_100"),
        _so(EXP_KEY, "nirspec_spectrum_exposure", CONTENT_SHA, exposure_ref="jw01_00001_nrs1_100"),
    ])
    products = tmp_path / "products"

    stats = download_objects(
        _FakeAPI([FINAL_KEY, EXP_KEY]), ["test_obs"], list(INTERMEDIATE_PRODUCT_TYPES),
        store, products, download_session=_fake_session(),
    )

    # Only the exposure was in the requested product-type set.
    assert stats["downloaded"] == 1
    assert (products / "nirspec" / "test_obs" / "jw01_00001_nrs1_100.fits").exists()
    assert not (products / "nirspec" / "test_obs" / "test_obs_prism_100_spec.fits").exists()


def test_unauthorized_keys_are_skipped(store, tmp_path):
    store.upsert_storage_objects([_so(FINAL_KEY, "nirspec_spec", CONTENT_SHA, "test_obs_prism_100")])
    products = tmp_path / "products"

    # API authorizes nothing -> presign returns {} -> nothing fetched.
    stats = download_objects(
        _FakeAPI([]), ["test_obs"], list(FINAL_PRODUCT_TYPES),
        store, products, download_session=_fake_session(),
    )

    assert stats["downloaded"] == 0
    assert stats["unauthorized"] == 1
    assert not (products / "nirspec" / "test_obs" / "test_obs_prism_100_spec.fits").exists()


def test_hash_mismatch_fails(store, tmp_path):
    # content_hash claims a different sha256 than the bytes we serve.
    store.upsert_storage_objects([_so(FINAL_KEY, "nirspec_spec", "sha256:deadbeef", "test_obs_prism_100")])
    products = tmp_path / "products"

    stats = download_objects(
        _FakeAPI([FINAL_KEY]), ["test_obs"], list(FINAL_PRODUCT_TYPES),
        store, products, download_session=_fake_session(),
    )

    assert stats["downloaded"] == 0
    assert stats["failed"] == 1
    assert store.find_local_path("test_obs_prism_100") is None


def test_dry_run_plans_without_fetching(store, tmp_path):
    store.upsert_storage_objects([_so(FINAL_KEY, "nirspec_spec", CONTENT_SHA, "test_obs_prism_100")])
    products = tmp_path / "products"
    sess = _fake_session()

    stats = download_objects(
        _FakeAPI([FINAL_KEY]), ["test_obs"], list(FINAL_PRODUCT_TYPES),
        store, products, dry_run=True, download_session=sess,
    )

    assert stats["to_download"] == 1
    assert stats["downloaded"] == 0
    sess.get.assert_not_called()


def test_skips_already_local(store, tmp_path):
    store.upsert_storage_objects([_so(FINAL_KEY, "nirspec_spec", CONTENT_SHA, "test_obs_prism_100")])
    # Mark it already present with a matching hash -> not pending.
    store.mark_object_synced(
        storage_key=FINAL_KEY,
        local_path="nirspec/test_obs/test_obs_prism_100_spec.fits",
        local_file_hash=CONTENT_SHA,
        local_file_size=len(CONTENT),
    )
    products = tmp_path / "products"

    stats = download_objects(
        _FakeAPI([FINAL_KEY]), ["test_obs"], list(FINAL_PRODUCT_TYPES),
        store, products, download_session=_fake_session(),
    )

    assert stats["to_download"] == 0
    assert stats["downloaded"] == 0
