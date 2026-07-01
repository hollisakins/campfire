"""Tests for the concurrent /sync/* fetch orchestration and page-size config.

Covers the refactor that fetches the four independent catalogs (objects,
spectra, storage, photometry) concurrently and then applies them to the local
store serially, plus the ``CAMPFIRE_SYNC_PAGE_SIZE`` override.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from campfire.api.client import (
    APIClient,
    DEFAULT_SYNC_PAGE_SIZE,
    _resolve_sync_page_size,
)
from campfire.sync import sync_metadata


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def _make_fake_api(objects, spectra, storage, photometry, tags):
    """A stand-in APIClient whose fetch_all_* return canned data.

    Each fetch invokes its on_page_complete callback once (as the real
    paginators do) so the progress plumbing is exercised even though bars are
    disabled in tests.
    """
    api = MagicMock()

    def _fetcher(rows):
        def fetch(updated_since=None, on_page_complete=None):
            if on_page_complete:
                on_page_complete(len(rows), len(rows))
            return rows, len(rows)
        return fetch

    api.fetch_all_objects.side_effect = _fetcher(objects)
    api.fetch_all_spectra.side_effect = _fetcher(spectra)
    api.fetch_all_storage.side_effect = _fetcher(storage)
    api.fetch_all_photometry.side_effect = _fetcher(photometry)
    api.fetch_tags.return_value = tags
    return api


def _make_fake_store():
    store = MagicMock()
    # Full sync path: no incremental cursors.
    store.get_max_objects_updated_at.return_value = None
    store.get_max_spectra_updated_at.return_value = None
    store.get_max_storage_updated_at.return_value = None
    store.get_max_photometry_updated_at.return_value = None
    store.upsert_objects.side_effect = lambda rows: len(rows)
    store.upsert_spectra.side_effect = lambda rows: len(rows)
    store.upsert_storage_objects.side_effect = lambda rows: len(rows)
    store.upsert_photometry.side_effect = lambda rows: len(rows)
    store.upsert_tags.side_effect = lambda data: len(data)
    store.purge_stale_objects.return_value = 0
    store.purge_stale_spectra.return_value = {"purged_spectra": 0}
    store.purge_stale_storage_objects.return_value = {"purged": 0, "orphaned_files": []}
    store.purge_stale_photometry.return_value = 0
    store.get_stale_objects.return_value = []
    store.get_synced_observations.return_value = []
    return store


@pytest.fixture(autouse=True)
def _stub_export(monkeypatch):
    """CSV export reads the real store; stub it out for these orchestration tests."""
    monkeypatch.setattr(
        "campfire.db.export.export_catalogs", lambda store, meta_dir: (0, 0)
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def test_sync_metadata_fetches_all_streams_and_routes_results():
    objects = [{"object_id": f"O{i}"} for i in range(5)]
    spectra = [{"spectrum_id": f"S{i}"} for i in range(3)]
    storage = [{"storage_key": f"k{i}"} for i in range(7)]
    photometry = [{"id": i} for i in range(2)]
    tags = [{"slug": "t1"}]

    api = _make_fake_api(objects, spectra, storage, photometry, tags)
    store = _make_fake_store()

    result = sync_metadata(api, store, Path("/tmp/meta"), show_progress=False, full=True)

    # Every stream fetched exactly once.
    api.fetch_all_objects.assert_called_once()
    api.fetch_all_spectra.assert_called_once()
    api.fetch_all_storage.assert_called_once()
    api.fetch_all_photometry.assert_called_once()

    # Each stream's rows routed to the matching upsert (no cross-wiring).
    store.upsert_objects.assert_called_once_with(objects)
    store.upsert_spectra.assert_called_once_with(spectra)
    store.upsert_storage_objects.assert_called_once_with(storage)
    store.upsert_photometry.assert_called_once_with(photometry)

    assert result["objects"] == 5
    assert result["spectra"] == 3
    assert result["storage_objects"] == 7
    assert result["photometry"] == 2
    assert result["tags"] == 1
    assert result["incremental"] is False
    assert result["needs_full_sync"] is False


def test_sync_metadata_full_passes_no_cursor_to_fetchers():
    api = _make_fake_api([], [], [], [], [])
    store = _make_fake_store()

    sync_metadata(api, store, Path("/tmp/meta"), show_progress=False, full=True)

    # full=True => updated_since=None for every stream.
    assert api.fetch_all_objects.call_args.kwargs["updated_since"] is None
    assert api.fetch_all_spectra.call_args.kwargs["updated_since"] is None
    assert api.fetch_all_storage.call_args.kwargs["updated_since"] is None
    assert api.fetch_all_photometry.call_args.kwargs["updated_since"] is None


def test_sync_metadata_incremental_threads_cursor_and_skips_purge():
    api = _make_fake_api([{"object_id": "O1"}], [{"spectrum_id": "S1"}], [], [], [])
    store = _make_fake_store()
    store.get_max_objects_updated_at.return_value = "2026-01-01T00:00:00Z"
    store.get_max_spectra_updated_at.return_value = "2026-01-02T00:00:00Z"
    store.get_max_storage_updated_at.return_value = "2026-01-03T00:00:00Z"
    store.get_max_photometry_updated_at.return_value = "2026-01-04T00:00:00Z"
    # server_total != local -> needs_full_sync path exercised
    store._conn.execute.return_value.fetchone.return_value = [0]

    result = sync_metadata(api, store, Path("/tmp/meta"), show_progress=False, full=False)

    assert api.fetch_all_objects.call_args.kwargs["updated_since"] == "2026-01-01T00:00:00Z"
    assert result["incremental"] is True
    # Incremental syncs never purge (the server only sends deltas).
    store.purge_stale_objects.assert_not_called()
    store.purge_stale_spectra.assert_not_called()
    store.purge_stale_storage_objects.assert_not_called()
    store.purge_stale_photometry.assert_not_called()


def test_sync_metadata_propagates_stream_failure():
    api = _make_fake_api([], [], [], [], [])
    api.fetch_all_storage.side_effect = RuntimeError("boom")
    store = _make_fake_store()

    with pytest.raises(RuntimeError, match="boom"):
        sync_metadata(api, store, Path("/tmp/meta"), show_progress=False, full=True)


# ---------------------------------------------------------------------------
# Page-size config
# ---------------------------------------------------------------------------
def test_resolve_sync_page_size_default(monkeypatch):
    monkeypatch.delenv("CAMPFIRE_SYNC_PAGE_SIZE", raising=False)
    assert _resolve_sync_page_size() == DEFAULT_SYNC_PAGE_SIZE


def test_resolve_sync_page_size_override(monkeypatch):
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "2500")
    assert _resolve_sync_page_size() == 2500


@pytest.mark.parametrize("bad", ["notanint", "0", "-5", ""])
def test_resolve_sync_page_size_invalid_falls_back(monkeypatch, bad):
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", bad)
    assert _resolve_sync_page_size() == DEFAULT_SYNC_PAGE_SIZE


def test_resolve_sync_page_size_clamps_large(monkeypatch):
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "999999")
    assert _resolve_sync_page_size() == 50000


def test_api_client_reads_env_page_size(monkeypatch):
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "1234")
    client = APIClient(session=MagicMock())
    assert client._page_size == 1234
