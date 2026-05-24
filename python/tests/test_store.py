"""Tests for the object/spectra LocalStore."""

import pytest

from campfire.db.store import LocalStore, SchemaMismatchError, SCHEMA_VERSION


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "meta" / "campfire.db"
    s = LocalStore(db_path)
    yield s
    s.close()


@pytest.fixture
def sample_objects():
    return [
        {
            "id": 1,
            "object_id": "CAMPFIRE-J0001+0001",
            "field": "uds",
            "ra": 34.123,
            "dec": -5.678,
            "redshift": 2.54,
            "redshift_auto": 2.54,
            "redshift_inspected": 2.54,
            "redshift_quality": 3,
            "n_targets": 1,
            "n_spectra": 2,
            "programs": ["ember-uds"],
            "gratings": ["PRISM", "G395M"],
            "observations": ["ember_uds_p4"],
            "member_target_ids": ["ember_uds_p4_100"],
            "max_snr": 15.5,
            "max_exposure_time": 3600.0,
            "has_photometry": False,
            "is_active": True,
            "lists": ["lrd"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": 2,
            "object_id": "CAMPFIRE-J0002+0002",
            "field": "cosmos",
            "ra": 150.2,
            "dec": 2.7,
            "redshift": 0.8,
            "redshift_auto": 0.8,
            "redshift_inspected": None,
            "redshift_quality": 0,
            "n_targets": 1,
            "n_spectra": 1,
            "programs": ["ember-cosmos"],
            "gratings": ["PRISM"],
            "observations": ["ember_cosmos_p1"],
            "member_target_ids": ["ember_cosmos_p1_200"],
            "max_snr": 5.0,
            "max_exposure_time": 3600.0,
            "has_photometry": False,
            "is_active": True,
            "lists": ["blagn", "lae"],
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]


@pytest.fixture
def sample_spectra():
    return [
        {
            "id": 10,
            "spectrum_id": "ember_uds_p4_prism_clear_100",
            "target_id": "ember_uds_p4_100",
            "object_id": "CAMPFIRE-J0001+0001",
            "grating": "PRISM",
            "fits_path": "ember_uds_p4/ember_uds_p4_PRISM_CLEAR_100_spec.fits",
            "file_hash": "sha256:aaa",
            "file_size": 1024,
            "signal_to_noise": 15.5,
            "exposure_time": 3600.0,
            "reduction_version": "v1.0",
            "redshift_auto": 2.54,
            "dq_flags": 0,
            "program_slug": "ember-uds",
            "observation": "ember_uds_p4",
            "field": "uds",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": 11,
            "spectrum_id": "ember_uds_p4_g395m_f290lp_100",
            "target_id": "ember_uds_p4_100",
            "object_id": "CAMPFIRE-J0001+0001",
            "grating": "G395M",
            "fits_path": "ember_uds_p4/ember_uds_p4_G395M_F290LP_100_spec.fits",
            "file_hash": "sha256:bbb",
            "file_size": 2048,
            "signal_to_noise": 8.2,
            "exposure_time": 7200.0,
            "redshift_auto": 2.54,
            "dq_flags": 0,
            "program_slug": "ember-uds",
            "observation": "ember_uds_p4",
            "field": "uds",
            "updated_at": "2026-01-01T00:00:00Z",
        },
        {
            "id": 20,
            "spectrum_id": "ember_cosmos_p1_prism_clear_200",
            "target_id": "ember_cosmos_p1_200",
            "object_id": "CAMPFIRE-J0002+0002",
            "grating": "PRISM",
            "fits_path": "ember_cosmos_p1/ember_cosmos_p1_PRISM_CLEAR_200_spec.fits",
            "file_hash": "sha256:ccc",
            "file_size": 512,
            "signal_to_noise": 5.0,
            "dq_flags": 2,
            "program_slug": "ember-cosmos",
            "observation": "ember_cosmos_p1",
            "field": "cosmos",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    ]


class TestSchema:
    def test_schema_version(self, store):
        assert store._get_schema_version() == SCHEMA_VERSION

    def test_schema_mismatch_raises(self, tmp_path):
        db_path = tmp_path / "meta" / "campfire.db"
        s = LocalStore(db_path)
        s._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', '1')"
        )
        s._conn.commit()
        s.close()

        with pytest.raises(SchemaMismatchError):
            LocalStore(db_path)


class TestUpsertObjects:
    def test_upsert_new(self, store, sample_objects):
        count = store.upsert_objects(sample_objects)
        assert count == 2

    def test_upsert_idempotent(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        store.upsert_objects(sample_objects)
        assert len(store.query_objects()) == 2

    def test_get_object(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        obj = store.get_object("CAMPFIRE-J0001+0001")
        assert obj["object_id"] == "CAMPFIRE-J0001+0001"
        assert obj["redshift_quality"] == 3
        assert len(obj["spectra"]) == 2

    def test_upsert_renamed_object_id(self, store, sample_objects):
        """Server reconcile can rewrite object_id while keeping the same id —
        e.g. sub-arcsec ra/dec shifts changing the IAU name's last digit.
        The upsert must overwrite the stale row, not raise UNIQUE failures."""
        store.upsert_objects(sample_objects)
        renamed = dict(sample_objects[0])
        renamed["object_id"] = "CAMPFIRE-J0001+0002"  # same id=1, new name
        store.upsert_objects([renamed])
        rows = store.query_objects()
        assert len(rows) == 2
        ids = {r["object_id"] for r in rows}
        assert "CAMPFIRE-J0001+0001" not in ids
        assert "CAMPFIRE-J0001+0002" in ids


class TestQueryObjects:
    def test_query_all(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects()
        assert len(results) == 2

    def test_field_filter(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(fields=["uds"])
        assert len(results) == 1
        assert results[0]["object_id"] == "CAMPFIRE-J0001+0001"

    def test_program_filter(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(programs=["ember-uds"])
        assert len(results) == 1

    def test_redshift_range(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(redshift_range=(2.0, 3.0))
        assert len(results) == 1

    def test_quality_filter(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(redshift_quality=[3])
        assert len(results) == 1

    def test_inspected_only(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(inspected_only=True)
        assert len(results) == 1

    def test_sort(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(sort="redshift", sort_dir="desc")
        assert results[0]["redshift"] >= results[1]["redshift"]

    def test_list_fields_deserialized(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        obj = store.query_objects(fields=["uds"])[0]
        assert obj["programs"] == ["ember-uds"]
        assert "ember_uds_p4" in obj["observations"]

    def test_dq_flags_filter(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        from campfire.flags import DQFlags
        from campfire.flags import parse_flag_input
        q = parse_flag_input(~DQFlags.CONTAMINATION, DQFlags)
        results = store.query_objects(dq_flags={
            "include_any": q.include_any,
            "include_all": q.include_all,
            "exclude": q.exclude,
        })
        object_ids = {o["object_id"] for o in results}
        assert "CAMPFIRE-J0002+0002" not in object_ids


class TestTagFilters:
    def test_tag_filter(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(tags=["lrd"])
        assert len(results) == 1
        assert results[0]["object_id"] == "CAMPFIRE-J0001+0001"

        results = store.query_objects(tags=["blagn"])
        assert len(results) == 1
        assert results[0]["object_id"] == "CAMPFIRE-J0002+0002"

    def test_tag_union(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        results = store.query_objects(tags=["blagn", "lae"])
        assert len(results) == 1
        assert results[0]["object_id"] == "CAMPFIRE-J0002+0002"

    def test_tag_no_match(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        assert store.query_objects(tags=["nonexistent"]) == []


class TestUpsertSpectra:
    def test_upsert_new(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        count = store.upsert_spectra(sample_spectra)
        assert count == 3

    def test_preserves_local_fields(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        store.mark_synced(
            spectrum_id="ember_uds_p4_prism_clear_100",
            local_path="ember_uds_p4/test.fits",
            file_hash="sha256:local",
            file_size=1024,
        )
        # Re-upsert should not clobber local_path
        store.upsert_spectra(sample_spectra)
        row = store.get_spectrum("ember_uds_p4_prism_clear_100")
        assert row["local_path"] == "ember_uds_p4/test.fits"
        assert row["local_file_hash"] == "sha256:local"


class TestQuerySpectra:
    def test_query_all(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        rows = store.query_spectra()
        assert len(rows) == 3

    def test_grating_filter(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        rows = store.query_spectra(gratings=["PRISM"])
        assert len(rows) == 2
        assert all(r["grating"] == "PRISM" for r in rows)

    def test_observation_filter(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        rows = store.query_spectra(observations=["ember_uds_p4"])
        assert len(rows) == 2

    def test_dq_flags_exclude(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        rows = store.query_spectra(dq_flags={"exclude": 2})
        assert all(r["dq_flags"] & 2 == 0 for r in rows)

    def test_redshift_filter_through_object(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        rows = store.query_spectra(redshift_range=(2.0, 3.0))
        assert all(r["object_id"] == "CAMPFIRE-J0001+0001" for r in rows)

    def test_get_spectrum(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        row = store.get_spectrum("ember_uds_p4_prism_clear_100")
        assert row is not None
        assert row["grating"] == "PRISM"


class TestDownloadTracking:
    def test_mark_synced(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        store.mark_synced(
            spectrum_id="ember_uds_p4_prism_clear_100",
            local_path="ember_uds_p4/test.fits",
            file_hash="sha256:local",
            file_size=1024,
        )
        row = store.get_spectrum("ember_uds_p4_prism_clear_100")
        assert row["local_path"] == "ember_uds_p4/test.fits"

    def test_find_local_path(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        store.mark_synced(
            spectrum_id="ember_uds_p4_prism_clear_100",
            local_path="ember_uds_p4/test.fits",
            file_hash="sha256:local",
            file_size=1024,
        )
        path = store.find_local_path("ember_uds_p4_prism_clear_100")
        assert path == "ember_uds_p4/test.fits"
        assert store.find_local_path("ember_uds_p4_g395m_f290lp_100") is None

    def test_get_stale_files(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        store.mark_synced(
            spectrum_id="ember_uds_p4_prism_clear_100",
            local_path="ember_uds_p4/test.fits",
            file_hash="sha256:OUTDATED",
            file_size=1024,
        )
        stale = store.get_stale_files()
        assert len(stale) == 1
        assert stale[0]["spectrum_id"] == "ember_uds_p4_prism_clear_100"

    def test_get_pending_downloads(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        pending = store.get_pending_downloads()
        total = sum(len(v) for v in pending.values())
        assert total == 3

    def test_get_pending_after_sync(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        store.mark_synced(
            spectrum_id="ember_uds_p4_prism_clear_100",
            local_path="ember_uds_p4/test.fits",
            file_hash="sha256:aaa",  # matches server
            file_size=1024,
        )
        pending = store.get_pending_downloads()
        total = sum(len(v) for v in pending.values())
        assert total == 2


class TestPurge:
    def test_purge_stale_objects(self, store, sample_objects):
        store.upsert_objects(sample_objects)
        # Force one object to appear stale
        store._conn.execute(
            "UPDATE objects SET _synced_at = '2026-01-01T00:00:00Z' WHERE object_id = 'CAMPFIRE-J0002+0002'"
        )
        store._conn.commit()
        purged = store.purge_stale_objects("2026-01-02T00:00:00Z")
        assert purged >= 1

    def test_purge_stale_spectra(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        store._conn.execute(
            "UPDATE spectra SET _synced_at = '2026-01-01T00:00:00Z' WHERE spectrum_id = 'ember_cosmos_p1_prism_clear_200'"
        )
        store._conn.commit()
        result = store.purge_stale_spectra("2026-01-02T00:00:00Z")
        assert result["purged_spectra"] >= 1


class TestObservationQueries:
    def test_get_synced_observations(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        obs = store.get_synced_observations()
        assert "ember_uds_p4" in obs
        assert "ember_cosmos_p1" in obs

    def test_get_observation_summary(self, store, sample_objects, sample_spectra):
        store.upsert_objects(sample_objects)
        store.upsert_spectra(sample_spectra)
        summary = store.get_observation_summary()
        assert len(summary) >= 1
