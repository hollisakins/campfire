"""Tests for LocalStore database layer."""

import pytest
from pathlib import Path

from campfire.db.store import LocalStore


@pytest.fixture
def store(tmp_path):
    """Create a LocalStore in a temp directory."""
    db_path = tmp_path / ".campfire_meta" / "campfire.db"
    s = LocalStore(db_path)
    yield s
    s.close()


@pytest.fixture
def sample_objects():
    """Sample API response objects for upsert testing."""
    return [
        {
            "id": 1,
            "object_id": "ember_uds_p4_100",
            "program_slug": "ember-uds",
            "program_name": "EMBER-UDS",
            "field": "UDS",
            "observation": "ember_uds_p4",
            "ra": 34.123,
            "dec": -5.678,
            "redshift": 2.54,
            "redshift_auto": 2.54,
            "redshift_inspected": 2.54,
            "redshift_quality": 3,
            "spectral_features": 0,
            "object_flags": 1,  # LRD
            "dq_flags": 0,
            "max_snr": 15.5,
            "max_exposure_time": 3600.0,
            "last_inspected_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "spectra": [
                {
                    "id": 10,
                    "object_id": "ember_uds_p4_100",
                    "grating": "PRISM",
                    "fits_path": "spectra/ember_uds_p4/ember_uds_p4_PRISM_100_spec.fits",
                    "signal_to_noise": 15.5,
                    "exposure_time": 3600.0,
                },
                {
                    "id": 11,
                    "object_id": "ember_uds_p4_100",
                    "grating": "G395M",
                    "fits_path": "spectra/ember_uds_p4/ember_uds_p4_G395M_100_spec.fits",
                    "signal_to_noise": 8.2,
                    "exposure_time": 7200.0,
                },
            ],
        },
        {
            "id": 2,
            "object_id": "ember_uds_p4_200",
            "program_slug": "ember-uds",
            "program_name": "EMBER-UDS",
            "field": "UDS",
            "observation": "ember_uds_p4",
            "ra": 34.200,
            "dec": -5.700,
            "redshift": 0.8,
            "redshift_auto": 0.8,
            "redshift_inspected": None,
            "redshift_quality": 0,
            "spectral_features": 32,  # MULTI_EMISSION
            "object_flags": 32,  # HA_EMITTER
            "dq_flags": 2,  # CONTAMINATION
            "max_snr": 5.0,
            "max_exposure_time": 3600.0,
            "spectra": [
                {
                    "id": 20,
                    "object_id": "ember_uds_p4_200",
                    "grating": "PRISM",
                    "fits_path": "spectra/ember_uds_p4/ember_uds_p4_PRISM_200_spec.fits",
                    "signal_to_noise": 5.0,
                },
            ],
        },
    ]


class TestLocalStoreInit:
    """Test store creation and migration."""

    def test_creates_database(self, tmp_path):
        """Store creates database file on init."""
        db_path = tmp_path / "meta" / "campfire.db"
        store = LocalStore(db_path)
        assert db_path.exists()
        store.close()

    def test_context_manager(self, tmp_path):
        """Store works as context manager."""
        db_path = tmp_path / "campfire.db"
        with LocalStore(db_path) as store:
            assert db_path.exists()

    def test_migrates_from_old_db(self, tmp_path):
        """Store migrates from sync_state.db if it exists."""
        import sqlite3

        meta_dir = tmp_path / ".campfire_meta"
        meta_dir.mkdir()
        old_path = meta_dir / "sync_state.db"
        new_path = meta_dir / "campfire.db"

        # Create old-format database
        conn = sqlite3.connect(str(old_path))
        conn.executescript("""
            CREATE TABLE synced_files (
                spectra_id INTEGER PRIMARY KEY,
                object_id TEXT NOT NULL,
                observation TEXT NOT NULL,
                grating TEXT NOT NULL,
                fits_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                file_hash TEXT,
                file_size INTEGER,
                synced_at TEXT NOT NULL
            );
            INSERT INTO synced_files VALUES (
                10, 'obj_100', 'obs1', 'PRISM',
                'spectra/obs1/file.fits', 'obs1/file.fits',
                'sha256:abc123', 1000, '2026-01-01T00:00:00Z'
            );
            CREATE TABLE sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                files_downloaded INTEGER DEFAULT 0,
                files_skipped INTEGER DEFAULT 0,
                bytes_downloaded INTEGER DEFAULT 0,
                status TEXT DEFAULT 'in_progress'
            );
        """)
        conn.close()

        # Open with LocalStore — should migrate
        store = LocalStore(new_path)

        # Old file should have been renamed
        assert not old_path.exists()
        assert new_path.exists()

        # Migrated data should be in new spectra table
        spectra = store.get_spectra_for_object("obj_100")
        assert len(spectra) == 1
        assert spectra[0]["local_path"] == "obs1/file.fits"
        assert spectra[0]["file_hash"] == "sha256:abc123"

        store.close()


class TestUpsertObjects:
    """Test object/spectra upsert."""

    def test_upsert_inserts(self, store, sample_objects):
        """upsert_objects inserts new objects and spectra."""
        obj_count, spec_count = store.upsert_objects(sample_objects)
        assert obj_count == 2
        assert spec_count == 3

    def test_upsert_updates(self, store, sample_objects):
        """upsert_objects updates existing objects."""
        store.upsert_objects(sample_objects)

        # Update redshift
        sample_objects[0]["redshift"] = 3.0
        store.upsert_objects(sample_objects)

        obj = store.get_object("ember_uds_p4_100")
        assert obj["redshift"] == 3.0

    def test_upsert_preserves_local_path(self, store, sample_objects):
        """upsert_objects doesn't overwrite local_path on spectra."""
        store.upsert_objects(sample_objects)

        # Mark a file as synced
        store.mark_synced(
            10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
            "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
            "sha256:abc", 1000,
        )

        # Re-upsert — local_path should be preserved
        store.upsert_objects(sample_objects)

        spectra = store.get_spectra_for_object("ember_uds_p4_100", "PRISM")
        assert spectra[0]["local_path"] == "ember_uds_p4/file.fits"


class TestQueryObjects:
    """Test local query filtering."""

    def test_query_all(self, store, sample_objects):
        """query_objects returns all objects when no filters."""
        store.upsert_objects(sample_objects)
        results = store.query_objects()
        assert len(results) == 2

    def test_query_by_field(self, store, sample_objects):
        """query_objects filters by field."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(fields=["UDS"])
        assert len(results) == 2
        results = store.query_objects(fields=["COSMOS"])
        assert len(results) == 0

    def test_query_by_redshift_range(self, store, sample_objects):
        """query_objects filters by redshift range."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(redshift_range=(2.0, 3.0))
        assert len(results) == 1
        assert results[0]["object_id"] == "ember_uds_p4_100"

    def test_query_by_redshift_quality(self, store, sample_objects):
        """query_objects filters by redshift quality."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(redshift_quality=[3])
        assert len(results) == 1

    def test_query_inspected_only(self, store, sample_objects):
        """query_objects filters for inspected objects."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(inspected_only=True)
        assert len(results) == 1
        assert results[0]["redshift_quality"] == 3

    def test_query_by_object_flags(self, store, sample_objects):
        """query_objects filters by object flags (include_any)."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(
            object_flags={"include_any": 1}  # LRD
        )
        assert len(results) == 1
        assert results[0]["object_id"] == "ember_uds_p4_100"

    def test_query_exclude_dq_flags(self, store, sample_objects):
        """query_objects excludes by dq_flags."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(
            dq_flags={"exclude": 2}  # CONTAMINATION
        )
        assert len(results) == 1
        assert results[0]["object_id"] == "ember_uds_p4_100"

    def test_query_by_search(self, store, sample_objects):
        """query_objects does text search on object_id."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(search="100")
        assert len(results) == 1

    def test_query_with_limit_offset(self, store, sample_objects):
        """query_objects respects limit and offset."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(limit=1, sort="object_id")
        assert len(results) == 1
        results2 = store.query_objects(limit=1, offset=1, sort="object_id")
        assert len(results2) == 1
        assert results[0]["object_id"] != results2[0]["object_id"]

    def test_query_with_sorting(self, store, sample_objects):
        """query_objects sorts results."""
        store.upsert_objects(sample_objects)
        results = store.query_objects(sort="redshift", sort_dir="desc")
        assert results[0]["redshift"] > results[1]["redshift"]

    def test_query_includes_spectra(self, store, sample_objects):
        """query_objects attaches spectra to each object."""
        store.upsert_objects(sample_objects)
        results = store.query_objects()
        obj1 = next(r for r in results if r["object_id"] == "ember_uds_p4_100")
        assert len(obj1["spectra"]) == 2


class TestSyncState:
    """Test sync state operations."""

    def test_mark_and_find_local(self, store, sample_objects):
        """mark_synced records local path, find_local_path retrieves it."""
        store.upsert_objects(sample_objects)

        store.mark_synced(
            10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
            "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
            "sha256:abc", 1000,
        )

        path = store.find_local_path("ember_uds_p4_100", "PRISM")
        assert path == "ember_uds_p4/file.fits"

        # Non-synced grating returns None
        path2 = store.find_local_path("ember_uds_p4_100", "G395M")
        assert path2 is None

    def test_sync_log(self, store):
        """Sync logging works."""
        log_id = store.log_sync_start("obs1")
        assert log_id > 0

        store.log_sync_complete(log_id, 10, 5, 1000000)

        last = store.get_last_sync("obs1")
        assert last is not None

    def test_observation_stats(self, store, sample_objects):
        """get_observation_stats counts synced files."""
        store.upsert_objects(sample_objects)

        store.mark_synced(
            10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
            "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
            "sha256:abc", 5000,
        )

        stats = store.get_observation_stats("ember_uds_p4")
        assert stats["synced_count"] == 1
        assert stats["total_bytes"] == 5000


class TestDistinctValues:
    """Test metadata queries."""

    def test_get_distinct_fields(self, store, sample_objects):
        """get_distinct_values returns unique fields."""
        store.upsert_objects(sample_objects)
        fields = store.get_distinct_values("field")
        assert fields == ["UDS"]

    def test_get_distinct_gratings(self, store, sample_objects):
        """get_distinct_values returns unique gratings from spectra table."""
        store.upsert_objects(sample_objects)
        gratings = store.get_distinct_values("grating")
        assert set(gratings) == {"G395M", "PRISM"}

    def test_get_synced_observations(self, store, sample_objects):
        """get_synced_observations returns observations in DB."""
        store.upsert_objects(sample_objects)
        obs = store.get_synced_observations()
        assert obs == ["ember_uds_p4"]
