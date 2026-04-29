"""Smoke tests for the flat spectra query surface (local store)."""

import pytest

from campfire.db.store import LocalStore
from campfire.flags import DQFlags, parse_flag_input


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "meta" / "campfire.db"
    s = LocalStore(db_path)

    s.upsert_objects([
        {
            "id": 1,
            "object_id": "OBJ-A",
            "field": "cosmos",
            "ra": 150.0,
            "dec": 2.0,
            "redshift": 2.5,
            "redshift_quality": 3,
            "n_targets": 1,
            "n_spectra": 2,
            "programs": ["prog-a"],
            "gratings": ["PRISM", "G395M"],
            "observations": ["obs1"],
            "member_target_ids": ["t_a"],
            "has_photometry": True,
            "is_active": True,
        },
        {
            "id": 2,
            "object_id": "OBJ-B",
            "field": "uds",
            "ra": 34.0,
            "dec": -5.0,
            "redshift": 0.5,
            "redshift_quality": 0,
            "n_targets": 1,
            "n_spectra": 1,
            "programs": ["prog-b"],
            "gratings": ["PRISM"],
            "observations": ["obs2"],
            "member_target_ids": ["t_b"],
            "has_photometry": False,
            "is_active": True,
        },
    ])

    s.upsert_spectra([
        {
            "id": 1,
            "spectrum_id": "obs1_prism_a",
            "target_id": "t_a",
            "object_id": "OBJ-A",
            "grating": "PRISM",
            "fits_path": "obs1/obs1_prism_a_spec.fits",
            "dq_flags": 0,
            "program_slug": "prog-a",
            "observation": "obs1",
            "field": "cosmos",
            "signal_to_noise": 20.0,
        },
        {
            "id": 2,
            "spectrum_id": "obs1_g395m_a",
            "target_id": "t_a",
            "object_id": "OBJ-A",
            "grating": "G395M",
            "fits_path": "obs1/obs1_g395m_a_spec.fits",
            "dq_flags": 2,  # CONTAMINATION
            "program_slug": "prog-a",
            "observation": "obs1",
            "field": "cosmos",
            "signal_to_noise": 8.0,
        },
        {
            "id": 3,
            "spectrum_id": "obs2_prism_b",
            "target_id": "t_b",
            "object_id": "OBJ-B",
            "grating": "PRISM",
            "fits_path": "obs2/obs2_prism_b_spec.fits",
            "dq_flags": 0,
            "program_slug": "prog-b",
            "observation": "obs2",
            "field": "uds",
            "signal_to_noise": 5.0,
        },
    ])

    yield s
    s.close()


def test_query_spectra_all(store):
    rows = store.query_spectra()
    assert len(rows) == 3
    ids = {r["spectrum_id"] for r in rows}
    assert "obs1_prism_a" in ids
    assert "obs1_g395m_a" in ids
    assert "obs2_prism_b" in ids


def test_query_spectra_field_filter(store):
    rows = store.query_spectra(fields=["cosmos"])
    assert all(r["field"] == "cosmos" for r in rows)
    assert len(rows) == 2


def test_query_spectra_dq_flags_exclude(store):
    q = parse_flag_input(~DQFlags.CONTAMINATION, DQFlags)
    rows = store.query_spectra(dq_flags={
        "include_any": q.include_any,
        "include_all": q.include_all,
        "exclude": q.exclude,
    })
    ids = {r["spectrum_id"] for r in rows}
    assert "obs1_g395m_a" not in ids
    assert "obs1_prism_a" in ids


def test_query_spectra_inspected_only_joins_object(store):
    rows = store.query_spectra(inspected_only=True)
    # Only OBJ-A's spectra have redshift_quality=3
    assert {r["object_id"] for r in rows} == {"OBJ-A"}


def test_query_spectra_cone_search(store):
    # Cone search near OBJ-A; OBJ-B is at 34/-5 so excluded at small radius
    rows = store.query_spectra(cone_search=(150.0, 2.0, 2.0))
    object_ids = {r["object_id"] for r in rows}
    assert "OBJ-A" in object_ids
    assert "OBJ-B" not in object_ids


def test_get_spectrum_single(store):
    row = store.get_spectrum("obs1_prism_a")
    assert row["grating"] == "PRISM"
    assert row["object_id"] == "OBJ-A"
