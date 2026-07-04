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


def _so(key, product_type, content_hash, spectrum_id=None, exposure_ref=None,
        observation="test_obs", field="cosmos", filter=None, instrument="nirspec"):
    return {
        "storage_key": key, "backend": "r2", "bucket": "data",
        "content_hash": content_hash, "size_bytes": len(CONTENT),
        "content_type": "application/fits", "product_type": product_type,
        "instrument": instrument, "status": "active", "observation": observation,
        "field": field, "filter": filter, "spectrum_id": spectrum_id,
        "exposure_ref": exposure_ref, "deployment_id": 1,
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


def _nircam_mosaic(filt):
    """A NIRCam field mosaic row (observation NULL, field-scoped, per-filter)."""
    key = f"data/products/nircam/egs/{filt}/mosaic_nircam_{filt}_egs_30mas_sci.fits"
    return _so(key, "nircam_mosaic", CONTENT_SHA, observation=None,
               field="egs", filter=filt, instrument="nircam")


def test_get_pending_filters_narrows_nircam(store):
    store.upsert_storage_objects([
        _nircam_mosaic("f277w"), _nircam_mosaic("f356w"), _nircam_mosaic("f444w"),
    ])
    pending = store.get_pending_objects(
        product_types=["nircam_mosaic"], fields=["egs"], filters=["f277w", "f444w"],
    )
    got = sorted(r["filter"] for rows in pending.values() for r in rows)
    assert got == ["f277w", "f444w"]  # f356w excluded


def test_get_pending_filters_case_insensitive(store):
    store.upsert_storage_objects([_nircam_mosaic("f444w")])
    pending = store.get_pending_objects(
        product_types=["nircam_mosaic"], fields=["egs"], filters=["F444W"],
    )
    assert [r["filter"] for rows in pending.values() for r in rows] == ["f444w"]


def test_get_pending_filters_keeps_null_filter_rows(store):
    # A filter-less row (NIRSpec final) is never excluded by --filters, mirroring
    # the grating rule for attribute-less rows.
    store.upsert_storage_objects([
        _so(FINAL_KEY, "nirspec_spec", CONTENT_SHA, "test_obs_prism_100"),
        _nircam_mosaic("f444w"),
    ])
    pending = store.get_pending_objects(
        product_types=["nirspec_spec", "nircam_mosaic"],
        observations=["test_obs"], fields=["egs"], filters=["f150w"],
    )
    keys = {r["storage_key"] for rows in pending.values() for r in rows}
    assert FINAL_KEY in keys          # NULL-filter NIRSpec row survives
    assert all("f444w" not in k for k in keys)  # non-matching NIRCam filter dropped


def test_download_objects_filters_scope(store, tmp_path):
    store.upsert_storage_objects([_nircam_mosaic("f277w"), _nircam_mosaic("f444w")])
    products = tmp_path / "products"
    keys = [_nircam_mosaic(f)["storage_key"] for f in ("f277w", "f444w")]

    stats = download_objects(
        _FakeAPI(keys), [], ["nircam_mosaic"], store, products,
        fields=["egs"], filters=["f444w"], download_session=_fake_session(),
    )

    assert stats["downloaded"] == 1
    assert (products / "nircam" / "egs" / "f444w" /
            "mosaic_nircam_f444w_egs_30mas_sci.fits").exists()
    assert not (products / "nircam" / "egs" / "f277w").exists()


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
