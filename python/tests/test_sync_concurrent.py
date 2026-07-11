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
    DEFAULT_STORAGE_SYNC_PAGE_SIZE,
    DEFAULT_SYNC_PAGE_SIZE,
    _resolve_storage_sync_page_size,
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


# ---------------------------------------------------------------------------
# Storage page-size config (storage_objects paginates with a larger page)
# ---------------------------------------------------------------------------
def test_resolve_storage_page_size_default(monkeypatch):
    monkeypatch.delenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", raising=False)
    monkeypatch.delenv("CAMPFIRE_SYNC_PAGE_SIZE", raising=False)
    assert _resolve_storage_sync_page_size() == DEFAULT_STORAGE_SYNC_PAGE_SIZE
    # Storage default is deliberately larger than the shared default.
    assert DEFAULT_STORAGE_SYNC_PAGE_SIZE > DEFAULT_SYNC_PAGE_SIZE


def test_resolve_storage_page_size_explicit_override(monkeypatch):
    monkeypatch.setenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", "8000")
    # The storage-specific var wins, even against the shared one.
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "2000")
    assert _resolve_storage_sync_page_size() == 8000


def test_resolve_storage_page_size_tracks_shared_when_higher(monkeypatch):
    # No storage-specific var: storage tracks the shared page size once it rises
    # above the storage floor.
    monkeypatch.delenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", raising=False)
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", str(DEFAULT_STORAGE_SYNC_PAGE_SIZE + 3000))
    assert _resolve_storage_sync_page_size() == DEFAULT_STORAGE_SYNC_PAGE_SIZE + 3000


def test_resolve_storage_page_size_floored_by_default(monkeypatch):
    # A shared page size below the storage floor does not shrink storage.
    monkeypatch.delenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", raising=False)
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "500")
    assert _resolve_storage_sync_page_size() == DEFAULT_STORAGE_SYNC_PAGE_SIZE


def test_resolve_storage_page_size_clamps_large(monkeypatch):
    monkeypatch.setenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", "999999")
    assert _resolve_storage_sync_page_size() == 50000


@pytest.mark.parametrize("bad", ["notanint", "0", "-5"])
def test_resolve_storage_page_size_invalid_falls_back_to_floor(monkeypatch, bad):
    monkeypatch.delenv("CAMPFIRE_SYNC_PAGE_SIZE", raising=False)
    monkeypatch.setenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", bad)
    assert _resolve_storage_sync_page_size() == DEFAULT_STORAGE_SYNC_PAGE_SIZE


def test_api_client_reads_storage_page_size(monkeypatch):
    monkeypatch.setenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", "7777")
    client = APIClient(session=MagicMock())
    assert client._storage_page_size == 7777


def test_fetch_all_storage_requests_storage_page_size(monkeypatch):
    """/sync/storage paginates with the storage page size, not the shared one."""
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "1000")
    monkeypatch.setenv("CAMPFIRE_SYNC_STORAGE_PAGE_SIZE", "6000")

    session = MagicMock()
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "data": [],
        "pagination": {"total": 0},
        "total_accessible_count": 0,
    }
    session.get.return_value = response

    client = APIClient(session=session)
    client.fetch_all_storage()

    # The first (and only) page is requested with the storage limit.
    _path, kwargs = session.get.call_args
    assert kwargs["params"]["limit"] == 6000


# ---------------------------------------------------------------------------
# Keyset pagination (#103)
# ---------------------------------------------------------------------------
def _canned_session(pages):
    """A MagicMock session whose .get() returns ``pages`` in order and records
    the params of each request in the returned ``calls`` list."""
    calls = []

    def fake_get(path, params=None, timeout=None):
        calls.append(params)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = pages[len(calls) - 1]
        return resp

    session = MagicMock()
    session.get.side_effect = fake_get
    return session, calls


def test_paginate_sync_endpoint_keyset_cursor_propagation(monkeypatch):
    """The paginator seeks with ?after=<previous page's last cursor>, never
    offset, and stops on a short page without an extra round-trip (#103)."""
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "3")
    pages = [
        {  # page 1: full — carries the counts
            "data": [{"object_id": "A"}, {"object_id": "B"}, {"object_id": "C"}],
            "pagination": {"total": 7},
            "total_accessible_count": 42,
        },
        {  # page 2: full
            "data": [{"object_id": "D"}, {"object_id": "E"}, {"object_id": "F"}],
            "pagination": {"total": 0},
            "total_accessible_count": 0,
        },
        {  # page 3: short → stop
            "data": [{"object_id": "G"}],
            "pagination": {"total": 0},
            "total_accessible_count": 0,
        },
    ]
    session, calls = _canned_session(pages)
    client = APIClient(session=session)

    items, accessible = client.fetch_all_objects()

    # Every row, in page order; accessible count taken from the first page only.
    assert [i["object_id"] for i in items] == ["A", "B", "C", "D", "E", "F", "G"]
    assert accessible == 42
    # Short final page stops the walk — three requests, no trailing empty fetch.
    assert len(calls) == 3
    # Keyset, not offset: no offset param anywhere.
    assert all("offset" not in p for p in calls)
    # First page: no cursor, counts requested at the honored page size.
    assert "after" not in calls[0]
    assert calls[0]["include_counts"] == "true"
    assert calls[0]["limit"] == 3
    # Later pages: cursor = previous page's last object_id; counts gated off.
    assert calls[1]["after"] == "C" and calls[1]["include_counts"] == "false"
    assert calls[2]["after"] == "F" and calls[2]["include_counts"] == "false"


def test_paginate_sync_endpoint_stops_on_empty_after_exact_multiple(monkeypatch):
    """An exactly-full final page falls through to one empty page, which stops
    the walk (no infinite loop, no missed rows)."""
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "2")
    pages = [
        {
            "data": [{"object_id": "A"}, {"object_id": "B"}],
            "pagination": {"total": 2},
            "total_accessible_count": 2,
        },
        {"data": [], "pagination": {"total": 0}, "total_accessible_count": 0},
    ]
    session, calls = _canned_session(pages)
    client = APIClient(session=session)

    items, _ = client.fetch_all_objects()

    assert [i["object_id"] for i in items] == ["A", "B"]
    assert len(calls) == 2            # exact-full page, then the empty terminator
    assert calls[1]["after"] == "B"   # cursor advanced past the last row


def test_fetch_all_photometry_uses_integer_id_cursor(monkeypatch):
    """Photometry now folds into the shared keyset paginator, cursoring on the
    integer ``id`` field (#103)."""
    monkeypatch.setenv("CAMPFIRE_SYNC_PAGE_SIZE", "2")
    pages = [
        {"data": [{"id": 10}, {"id": 20}], "pagination": {"total": 3}},
        {"data": [{"id": 30}], "pagination": {"total": 0}},
    ]
    session, calls = _canned_session(pages)
    client = APIClient(session=session)

    items, total = client.fetch_all_photometry()

    assert [i["id"] for i in items] == [10, 20, 30]
    # Photometry carries no total_accessible_count field; the paginator falls
    # back to pagination.total instead of reporting a false 0 (Codex review,
    # PR #372).
    assert total == 3
    assert calls[0].get("after") is None
    assert calls[1]["after"] == 20    # last id of the previous page
