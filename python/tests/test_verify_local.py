"""verify_local_objects: bulk-scan reconcile with size-match hash adoption.

The reducer-scale regression (CANDIDE, post-#364): a fresh mirror over a
pipeline-written tree made verify full-hash ~60k files over NFS. Verify is now
an rsync-style quick check — one scandir pass, adopt the server hash when the
size matches, read file contents ONLY under --deep.
"""

import hashlib
import os
from pathlib import Path

import pytest

from campfire.db.store import LocalStore
from campfire.storage.hashing import compute_file_hash
from campfire_layout import KeyScheme, Scope, storage_key


@pytest.fixture()
def store(tmp_path):
    s = LocalStore(tmp_path / "campfire.db")
    yield s
    s.close()


@pytest.fixture()
def products(tmp_path):
    d = tmp_path / "products"
    d.mkdir()
    return d


def _seed_row(store, name="a_spec.fits", obs="obs1", content=b"x" * 100,
              size=None, content_hash=None, product_type='nirspec_spec'):
    """Insert a server-mirrored row for a spectrum final; returns (key, rel)."""
    key = storage_key(product_type, Scope(obs=obs), name, scheme=KeyScheme.CANONICAL)
    h = content_hash or f"sha256:{hashlib.sha256(content).hexdigest()}"
    store.upsert_storage_objects([{
        "storage_key": key, "backend": "osn", "bucket": "data",
        "content_hash": h, "size_bytes": size if size is not None else len(content),
        "product_type": product_type, "status": "active", "observation": obs,
        "updated_at": "2026-07-11T00:00:00+00:00",
    }])
    from campfire.config import products_relpath
    return key, products_relpath(key)


def _materialize(products, rel, content=b"x" * 100):
    p = products / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _row(store, key):
    return store.get_storage_rows_by_keys([key])[key]


# --- adoption (the quick check) ----------------------------------------------

def test_discovery_adopts_server_hash_on_size_match_without_reading(store, products, monkeypatch):
    key, rel = _seed_row(store)
    _materialize(products, rel)

    # No file content may be read on the default path.
    import campfire.db.store as store_mod
    def _boom(*a, **k):
        raise AssertionError("default verify must not hash file contents")
    monkeypatch.setattr("campfire.storage.hashing.hash_file", _boom)

    res = store.verify_local_objects(products)
    assert res["discovered"] == 1 and res["mismatched"] == 0
    row = _row(store, key)
    assert row["local_path"] == rel
    assert row["local_file_hash"] == row["content_hash"]  # adopted, not computed
    assert row["local_file_size"] == 100


def test_size_mismatch_left_pending_not_adopted(store, products):
    key, rel = _seed_row(store, content=b"x" * 100)
    _materialize(products, rel, content=b"x" * 37)  # truncated/partial

    res = store.verify_local_objects(products)
    assert res["discovered"] == 0
    assert res["mismatched"] == 1
    row = _row(store, key)
    assert row["local_path"] is None  # stays pending → pull re-fetches
    # And the pull differ agrees:
    pending = store.get_pending_objects(observations=["obs1"],
                                        product_types=["nirspec_spec"])
    assert any(r["storage_key"] == key for r in pending.get("obs1", []))


def test_etag_row_not_adopted(store, products):
    # Provisional etag: hashes are not authoritative — never presume them.
    key, rel = _seed_row(store, content_hash="etag:abc123")
    _materialize(products, rel)
    res = store.verify_local_objects(products)
    assert res["discovered"] == 0 and res["mismatched"] == 1


# --- tracked-row transitions ---------------------------------------------------

def test_vanished_file_cleared(store, products):
    key, rel = _seed_row(store)
    p = _materialize(products, rel)
    store.verify_local_objects(products)
    p.unlink()

    res = store.verify_local_objects(products)
    assert res["cleared"] == 1
    assert _row(store, key)["local_path"] is None


def test_unchanged_stat_is_noop(store, products):
    key, rel = _seed_row(store)
    _materialize(products, rel)
    store.verify_local_objects(products)
    res = store.verify_local_objects(products)
    assert res == {"cleared": 0, "rehashed": 0, "discovered": 0, "mismatched": 0}


def test_stat_change_same_size_readopts(store, products):
    key, rel = _seed_row(store)
    p = _materialize(products, rel)
    store.verify_local_objects(products)
    os.utime(p, (1e9, 1e9))  # touch: mtime changes, size unchanged

    res = store.verify_local_objects(products)
    assert res["rehashed"] == 1  # refreshed via adoption
    assert abs(_row(store, key)["local_file_mtime"] - 1e9) < 1e-3


def test_stat_change_new_size_clears(store, products):
    key, rel = _seed_row(store, content=b"x" * 100)
    p = _materialize(products, rel)
    store.verify_local_objects(products)
    p.write_bytes(b"y" * 55)  # rewritten with different size

    res = store.verify_local_objects(products)
    assert res["mismatched"] == 1 and res["cleared"] == 1
    assert _row(store, key)["local_path"] is None


# --- deep mode ------------------------------------------------------------------

def test_deep_hashes_real_content(store, products):
    content = b"real-bytes"
    key, rel = _seed_row(store, content=b"x" * len(content))  # server hash differs
    p = _materialize(products, rel, content=content)

    res = store.verify_local_objects(products, deep=True)
    assert res["discovered"] == 1
    row = _row(store, key)
    assert row["local_file_hash"] == compute_file_hash(p)
    assert row["local_file_hash"] != row["content_hash"]  # true state recorded
    # A content mismatch recorded under --deep shows up as stale:
    assert any(s["storage_key"] == key for s in store.get_stale_objects())


# --- scoping ---------------------------------------------------------------------

def test_scoped_verify_only_touches_selection(store, products):
    k1, r1 = _seed_row(store, name="a_spec.fits", obs="obs1")
    k2, r2 = _seed_row(store, name="b_spec.fits", obs="obs2")
    _materialize(products, r1)
    _materialize(products, r2)

    res = store.verify_local_objects(products, observations=["obs1"])
    assert res["discovered"] == 1
    assert _row(store, k1)["local_path"] == r1
    assert _row(store, k2)["local_path"] is None  # out of scope, untouched


def test_legacy_observation_kwarg_still_works(store, products):
    key, rel = _seed_row(store, obs="obs1")
    _materialize(products, rel)
    res = store.verify_local_objects(products, observation="obs1")
    assert res["discovered"] == 1
