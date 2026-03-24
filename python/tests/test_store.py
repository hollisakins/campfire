"""Tests for LocalStore database layer."""

import pytest
from pathlib import Path

from campfire.db.store import LocalStore, SCHEMA_VERSION


@pytest.fixture
def store(tmp_path):
    """Create a LocalStore in a temp directory."""
    db_path = tmp_path / "meta" / "campfire.db"
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

        meta_dir = tmp_path / "meta"
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


class TestStaleness:
    """Test staleness detection with split hash columns."""

    def test_no_stale_when_hashes_match(self, store, sample_objects):
        """No stale files when server and local hashes match."""
        # Set server hash via upsert
        sample_objects[0]["spectra"][0]["file_hash"] = "sha256:serverhash"
        store.upsert_objects(sample_objects)

        # Download with matching hash
        store.mark_synced(
            10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
            "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
            "sha256:serverhash", 1000,
        )

        stale = store.get_stale_files()
        assert len(stale) == 0

    def test_stale_when_server_hash_changes(self, store, sample_objects):
        """Stale file detected when server hash differs from local."""
        # Initial state: server hash = abc
        sample_objects[0]["spectra"][0]["file_hash"] = "sha256:abc"
        store.upsert_objects(sample_objects)

        # Download with hash abc
        store.mark_synced(
            10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
            "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
            "sha256:abc", 1000,
        )

        # Server reprocesses — hash changes to xyz
        sample_objects[0]["spectra"][0]["file_hash"] = "sha256:xyz"
        store.upsert_objects(sample_objects)

        stale = store.get_stale_files()
        assert len(stale) == 1
        assert stale[0]["spectra_id"] == 10
        assert stale[0]["server_hash"] == "sha256:xyz"
        assert stale[0]["local_file_hash"] == "sha256:abc"

    def test_not_stale_if_not_downloaded(self, store, sample_objects):
        """Files not downloaded are not reported as stale."""
        sample_objects[0]["spectra"][0]["file_hash"] = "sha256:abc"
        store.upsert_objects(sample_objects)
        # Don't download — no local_path

        stale = store.get_stale_files()
        assert len(stale) == 0

    def test_mark_synced_writes_local_file_hash(self, store, sample_objects):
        """mark_synced stores hash in local_file_hash, not file_hash."""
        sample_objects[0]["spectra"][0]["file_hash"] = "sha256:server"
        store.upsert_objects(sample_objects)

        store.mark_synced(
            10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
            "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
            "sha256:local", 1000,
        )

        # file_hash should still be the server hash
        row = store._conn.execute(
            "SELECT file_hash, local_file_hash FROM spectra WHERE spectra_id = 10"
        ).fetchone()
        assert row["file_hash"] == "sha256:server"
        assert row["local_file_hash"] == "sha256:local"


class TestV3Migration:
    """Test schema v2 to v3 migration."""

    def test_migrates_v2_to_v3(self, tmp_path):
        """V2 database gets local_file_hash column added."""
        import sqlite3

        db_path = tmp_path / "campfire.db"

        # Create a v2 database (no local_file_hash column)
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO _meta VALUES ('schema_version', '2');

            CREATE TABLE objects (
                id INTEGER PRIMARY KEY,
                object_id TEXT UNIQUE NOT NULL,
                program_slug TEXT, program_name TEXT,
                field TEXT, observation TEXT,
                ra REAL, dec REAL,
                redshift REAL, redshift_auto REAL, redshift_inspected REAL,
                redshift_quality INTEGER DEFAULT 0,
                spectral_features INTEGER DEFAULT 0,
                object_flags INTEGER DEFAULT 0,
                dq_flags INTEGER DEFAULT 0,
                max_snr REAL, max_exposure_time REAL,
                last_inspected_at TEXT, created_at TEXT, updated_at TEXT,
                _synced_at TEXT
            );

            CREATE TABLE spectra (
                spectra_id INTEGER PRIMARY KEY,
                object_id TEXT NOT NULL,
                grating TEXT NOT NULL,
                fits_path TEXT, file_hash TEXT, file_size INTEGER,
                signal_to_noise REAL, exposure_time REAL,
                reduction_version TEXT,
                local_path TEXT, synced_at TEXT, _synced_at TEXT
            );

            -- Simulate a downloaded file (v2 style: file_hash = download hash)
            INSERT INTO objects (id, object_id, observation) VALUES (1, 'obj1', 'obs1');
            INSERT INTO spectra (spectra_id, object_id, grating, fits_path,
                                 file_hash, local_path, synced_at)
            VALUES (10, 'obj1', 'PRISM', 'spectra/obs1/file.fits',
                    'sha256:downloadhash', 'obs1/file.fits', '2026-01-01');

            CREATE TABLE sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation TEXT NOT NULL, started_at TEXT NOT NULL,
                completed_at TEXT, files_downloaded INTEGER DEFAULT 0,
                files_skipped INTEGER DEFAULT 0, bytes_downloaded INTEGER DEFAULT 0,
                status TEXT DEFAULT 'in_progress'
            );
        """)
        conn.close()

        # Open with LocalStore — should auto-migrate
        store = LocalStore(db_path)

        # Verify local_file_hash was populated from old file_hash
        row = store._conn.execute(
            "SELECT file_hash, local_file_hash FROM spectra WHERE spectra_id = 10"
        ).fetchone()
        assert row["local_file_hash"] == "sha256:downloadhash"

        # Verify schema version is current (cascading migration)
        version = store._get_schema_version()
        assert version == SCHEMA_VERSION

        store.close()


class TestV5Migration:
    """Test schema v4 → v5 migration (local_file_mtime/local_file_size columns)."""

    def test_migrates_v4_to_v5(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "meta" / "campfire.db"
        db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row

        # Create a v4-like schema (without the new columns)
        conn.executescript("""
            CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO _meta (key, value) VALUES ('schema_version', '4');

            CREATE TABLE objects (
                id INTEGER PRIMARY KEY, object_id TEXT UNIQUE NOT NULL,
                program_slug TEXT, program_name TEXT, field TEXT, observation TEXT,
                ra REAL, dec REAL, redshift REAL, redshift_auto REAL,
                redshift_inspected REAL, redshift_quality INTEGER DEFAULT 0,
                spectral_features INTEGER DEFAULT 0, object_flags INTEGER DEFAULT 0,
                dq_flags INTEGER DEFAULT 0, max_snr REAL, max_exposure_time REAL,
                last_inspected_at TEXT, last_inspected_by TEXT,
                created_at TEXT, updated_at TEXT, _synced_at TEXT
            );

            CREATE TABLE spectra (
                spectra_id INTEGER PRIMARY KEY, object_id TEXT NOT NULL,
                grating TEXT NOT NULL, fits_path TEXT, file_hash TEXT,
                file_size INTEGER, signal_to_noise REAL, exposure_time REAL,
                reduction_version TEXT, local_path TEXT, local_file_hash TEXT,
                synced_at TEXT, _synced_at TEXT
            );

            INSERT INTO spectra (spectra_id, object_id, grating, fits_path,
                    file_hash, local_file_hash, local_path, synced_at)
            VALUES (10, 'obj1', 'PRISM', 'spectra/obs1/file.fits',
                    'sha256:server', 'sha256:local', 'obs1/file.fits', '2026-01-01');

            CREATE TABLE sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation TEXT NOT NULL, started_at TEXT NOT NULL,
                completed_at TEXT, files_downloaded INTEGER DEFAULT 0,
                files_skipped INTEGER DEFAULT 0, bytes_downloaded INTEGER DEFAULT 0,
                status TEXT DEFAULT 'in_progress'
            );
        """)
        conn.close()

        store = LocalStore(db_path)

        # Verify new columns exist
        row = store._conn.execute(
            "SELECT local_file_mtime, local_file_size FROM spectra WHERE spectra_id = 10"
        ).fetchone()
        assert row["local_file_mtime"] is None
        assert row["local_file_size"] is None

        # Verify schema version
        assert store._get_schema_version() == SCHEMA_VERSION

        store.close()


class TestVerifyLocalFiles:
    """Test verify_local_files with mtime/size tracking and real hashing."""

    def test_clears_missing_files(self, store, sample_objects, tmp_path):
        store.upsert_objects(sample_objects)
        products_dir = tmp_path / "products"
        products_dir.mkdir()

        # Mark as synced but don't create the file
        store.mark_synced(10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
                          "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
                          "sha256:abc", 1000, local_file_mtime=1000.0, local_file_size=1000)

        result = store.verify_local_files(products_dir)
        assert result["cleared"] == 1

        # local_path should be cleared
        row = store._conn.execute(
            "SELECT local_path, local_file_hash, local_file_mtime, local_file_size "
            "FROM spectra WHERE spectra_id = 10"
        ).fetchone()
        assert row["local_path"] is None
        assert row["local_file_hash"] is None
        assert row["local_file_mtime"] is None
        assert row["local_file_size"] is None

    def test_discovers_files_with_real_hash(self, store, sample_objects, tmp_path):
        store.upsert_objects(sample_objects)
        products_dir = tmp_path / "products"

        # Create a file on disk that matches expected path
        obs_dir = products_dir / "ember_uds_p4"
        obs_dir.mkdir(parents=True)
        file_path = obs_dir / "ember_uds_p4_PRISM_100_spec.fits"
        file_path.write_bytes(b"fake fits content")

        result = store.verify_local_files(products_dir)
        assert result["discovered"] == 1

        # Should have computed real hash, not copied server hash
        from campfire.sync import compute_file_hash
        expected_hash = compute_file_hash(file_path)

        row = store._conn.execute(
            "SELECT local_path, local_file_hash, local_file_mtime, local_file_size "
            "FROM spectra WHERE spectra_id = 10"
        ).fetchone()
        assert row["local_path"] == "ember_uds_p4/ember_uds_p4_PRISM_100_spec.fits"
        assert row["local_file_hash"] == expected_hash
        assert row["local_file_mtime"] is not None
        assert row["local_file_size"] == len(b"fake fits content")

    def test_rehashes_modified_files(self, store, sample_objects, tmp_path):
        store.upsert_objects(sample_objects)
        products_dir = tmp_path / "products"
        obs_dir = products_dir / "ember_uds_p4"
        obs_dir.mkdir(parents=True)

        file_path = obs_dir / "file.fits"
        file_path.write_bytes(b"original content")
        st = file_path.stat()

        # Mark as synced with current stat
        store.mark_synced(10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
                          "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
                          "sha256:originalhash", 1000,
                          local_file_mtime=st.st_mtime, local_file_size=st.st_size)

        # Modify the file
        file_path.write_bytes(b"modified content that is different")

        result = store.verify_local_files(products_dir)
        assert result["rehashed"] == 1

        # Hash should be updated
        from campfire.sync import compute_file_hash
        expected_hash = compute_file_hash(file_path)

        row = store._conn.execute(
            "SELECT local_file_hash, local_file_mtime, local_file_size "
            "FROM spectra WHERE spectra_id = 10"
        ).fetchone()
        assert row["local_file_hash"] == expected_hash

    def test_skips_unchanged_files(self, store, sample_objects, tmp_path):
        store.upsert_objects(sample_objects)
        products_dir = tmp_path / "products"
        obs_dir = products_dir / "ember_uds_p4"
        obs_dir.mkdir(parents=True)

        file_path = obs_dir / "file.fits"
        file_path.write_bytes(b"content")
        st = file_path.stat()

        store.mark_synced(10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
                          "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
                          "sha256:somehash", 1000,
                          local_file_mtime=st.st_mtime, local_file_size=st.st_size)

        result = store.verify_local_files(products_dir)
        assert result["rehashed"] == 0
        assert result["cleared"] == 0

        # Hash should remain unchanged
        row = store._conn.execute(
            "SELECT local_file_hash FROM spectra WHERE spectra_id = 10"
        ).fetchone()
        assert row["local_file_hash"] == "sha256:somehash"


class TestPurgeStaleRows:
    """Test purge_stale_rows for cleaning up server-deleted records."""

    def test_purges_deleted_objects(self, store, sample_objects):
        store.upsert_objects(sample_objects)

        # Simulate a full sync that only returns one of the two objects
        store._conn.execute(
            "UPDATE objects SET _synced_at = '2026-01-02T00:00:00Z' WHERE object_id = 'ember_uds_p4_100'"
        )
        store._conn.execute(
            "UPDATE objects SET _synced_at = '2026-01-01T00:00:00Z' WHERE object_id = 'ember_uds_p4_200'"
        )
        store._conn.execute(
            "UPDATE spectra SET _synced_at = '2026-01-02T00:00:00Z' WHERE spectra_id IN (10, 11)"
        )
        store._conn.execute(
            "UPDATE spectra SET _synced_at = '2026-01-01T00:00:00Z' WHERE spectra_id = 20"
        )
        store._conn.commit()

        result = store.purge_stale_rows("2026-01-02T00:00:00Z")
        assert result["purged_objects"] == 1
        assert result["purged_spectra"] == 1

        # Only the first object should remain
        rows = store._conn.execute("SELECT object_id FROM objects").fetchall()
        assert len(rows) == 1
        assert rows[0]["object_id"] == "ember_uds_p4_100"

    def test_reports_orphaned_files(self, store, sample_objects):
        store.upsert_objects(sample_objects)

        # Mark one spectrum as downloaded
        store.mark_synced(20, "ember_uds_p4_200", "ember_uds_p4", "PRISM",
                          "spectra/ember_uds_p4/file200.fits", "ember_uds_p4/file200.fits",
                          "sha256:abc", 1000)

        # Set timestamps so object 200's spectrum gets purged
        store._conn.execute(
            "UPDATE objects SET _synced_at = '2026-01-02T00:00:00Z' WHERE object_id = 'ember_uds_p4_100'"
        )
        store._conn.execute(
            "UPDATE objects SET _synced_at = '2026-01-01T00:00:00Z' WHERE object_id = 'ember_uds_p4_200'"
        )
        store._conn.execute("UPDATE spectra SET _synced_at = '2026-01-02T00:00:00Z' WHERE spectra_id IN (10, 11)")
        store._conn.execute("UPDATE spectra SET _synced_at = '2026-01-01T00:00:00Z' WHERE spectra_id = 20")
        store._conn.commit()

        result = store.purge_stale_rows("2026-01-02T00:00:00Z")
        assert "ember_uds_p4/file200.fits" in result["orphaned_files"]

    def test_no_purge_when_all_current(self, store, sample_objects):
        store.upsert_objects(sample_objects)

        result = store.purge_stale_rows("2026-01-01T00:00:00Z")
        assert result["purged_objects"] == 0
        assert result["purged_spectra"] == 0
        assert result["orphaned_files"] == []


class TestPendingDownloads:
    """Test get_pending_downloads for local download planning."""

    def test_new_files_detected(self, store, sample_objects):
        """Spectra never downloaded should be returned as 'new'."""
        store.upsert_objects(sample_objects)

        pending = store.get_pending_downloads()
        all_pending = [s for specs in pending.values() for s in specs]
        assert len(all_pending) == 3  # 2 for obj1 + 1 for obj2
        assert all(s["status"] == "new" for s in all_pending)

    def test_updated_files_detected(self, store, sample_objects):
        """Spectra with stale local hash should be returned as 'updated'."""
        for obj in sample_objects:
            for spec in obj.get("spectra", []):
                spec["file_hash"] = f"sha256:server_{spec['id']}"
        store.upsert_objects(sample_objects)

        # Mark one as downloaded with a different hash
        store.mark_synced(10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
                          "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
                          "sha256:old_hash", 1000)

        pending = store.get_pending_downloads()
        all_pending = [s for specs in pending.values() for s in specs]

        updated = [s for s in all_pending if s["status"] == "updated"]
        assert len(updated) == 1
        assert updated[0]["spectra_id"] == 10

    def test_up_to_date_excluded(self, store, sample_objects):
        """Spectra with matching hashes should not be returned."""
        for obj in sample_objects:
            for spec in obj.get("spectra", []):
                spec["file_hash"] = f"sha256:hash_{spec['id']}"
        store.upsert_objects(sample_objects)

        # Mark one as downloaded with matching hash
        store.mark_synced(10, "ember_uds_p4_100", "ember_uds_p4", "PRISM",
                          "spectra/ember_uds_p4/file.fits", "ember_uds_p4/file.fits",
                          "sha256:hash_10", 1000)

        pending = store.get_pending_downloads()
        all_pending = [s for specs in pending.values() for s in specs]

        ids = [s["spectra_id"] for s in all_pending]
        assert 10 not in ids
        assert 11 in ids  # Still pending (never downloaded)

    def test_observation_filter(self, store):
        """get_pending_downloads filters by observation."""
        objects = [
            {
                "id": 1, "object_id": "obs1_100", "program_slug": "prog",
                "field": "F", "observation": "obs1",
                "spectra": [{"id": 10, "object_id": "obs1_100", "grating": "PRISM",
                             "fits_path": "spectra/obs1/f.fits"}],
            },
            {
                "id": 2, "object_id": "obs2_200", "program_slug": "prog",
                "field": "F", "observation": "obs2",
                "spectra": [{"id": 20, "object_id": "obs2_200", "grating": "PRISM",
                             "fits_path": "spectra/obs2/f.fits"}],
            },
        ]
        store.upsert_objects(objects)

        pending = store.get_pending_downloads(observations=["obs1"])
        assert "obs1" in pending
        assert "obs2" not in pending

    def test_grating_filter(self, store, sample_objects):
        """get_pending_downloads filters by grating."""
        store.upsert_objects(sample_objects)

        pending = store.get_pending_downloads(gratings=["PRISM"])
        all_pending = [s for specs in pending.values() for s in specs]
        assert all(s["grating"] == "PRISM" for s in all_pending)
