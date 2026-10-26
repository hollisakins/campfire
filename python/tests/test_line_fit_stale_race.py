"""Two-session check that a line fit's stored staleness serializes with an
inspection of its object (Catalog Read Model phase 2a; Codex finding on #652).

spectrum_line_fits.stale_redshift is computed by a BEFORE trigger from the
object's redshift / quality / version, and recomputed by a statement trigger
when the object is inspected. In isolation the two can interleave: a fit
inserted while the object is being inspected computes "fresh" from the old
redshift, the inspection's recompute cannot see the uncommitted fit, and the
fit commits fresh for good. The trigger therefore reads the object FOR SHARE,
which conflicts with the row lock the inspection's UPDATE holds, so one side
waits for the other. This drives both orderings through two psql sessions and
asserts the second waits and the committed flag is stale.

Skips when no local Supabase Postgres is reachable (see ``_localdb.py``).
"""
from __future__ import annotations

import uuid

import pytest

from ._localdb import requires_local_db, run_sql
from .test_is_public_race import Session, _scalar, _wait_until_blocked

SUFFIX = uuid.uuid4().hex[:8]
PROGRAM = "zzz_lfr_" + SUFFIX
OBS = "lfr_obs_" + SUFFIX
OBJECT = "TEST-LFR-" + SUFFIX
OBJECT2 = "TEST-LFR2-" + SUFFIX
TARGET = "test-lfr-" + SUFFIX
FITS = f"/tmp/lfr_{SUFFIX}.fits"


@pytest.fixture
def fixtures():
    r = run_sql(
        f"""
        INSERT INTO programs (slug, program_name, is_public) VALUES ('{PROGRAM}', 'Stale race', true);
        INSERT INTO observations (name, program_slug, jwst_program_id, field) VALUES ('{OBS}', '{PROGRAM}', 9987, 'zzz_lfr_field');
        INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum, redshift_inspected, redshift_quality)
          VALUES ('{OBJECT}', 'zzz_lfr_field', 155.0, 2.0, '{{{PROGRAM}}}', '{{{OBS}}}', true, 5.0, 3),
                 ('{OBJECT2}', 'zzz_lfr_field', 155.01, 2.0, '{{{PROGRAM}}}', '{{{OBS}}}', true, 2.0, 3);
        INSERT INTO targets (target_id, field, ra, dec, program_slug, observation, object_id)
          VALUES ('{TARGET}', 'zzz_lfr_field', 155.0, 2.0, '{PROGRAM}', '{OBS}', (SELECT id FROM objects WHERE object_id = '{OBJECT}'));
        INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status)
          VALUES ('{TARGET}', 'PRISM', '{FITS}', 10.0, 1000.0, 'published');
        """
    )
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        yield
    finally:
        run_sql(
            f"""
            DELETE FROM spectrum_line_fits WHERE target_id = '{TARGET}';
            DELETE FROM spectra WHERE fits_path = '{FITS}';
            DELETE FROM targets WHERE target_id = '{TARGET}';
            DELETE FROM objects WHERE object_id IN ('{OBJECT}', '{OBJECT2}');
            DELETE FROM observations WHERE name = '{OBS}';
            DELETE FROM programs WHERE slug = '{PROGRAM}';
            """
        )


def _insert_fit_sql() -> str:
    # a fit at the object's CURRENT redshift and version: fresh if nothing moves
    return (
        "INSERT INTO spectrum_line_fits (spectrum_id, target_id, grating, program_slug, observation, "
        "z_used, z_source, z_quality, object_id, object_version, n_lines, n_detected, lines, fit_version) "
        f"SELECT s.id, '{TARGET}', 'PRISM', '{PROGRAM}', '{OBS}', 5.0, 'inspected', 3, '{OBJECT}', o.version, 1, 1, "
        "jsonb_build_object('CIII1908', jsonb_build_object('component','doublet','wave_rest',1907.7,'flux',1e-18,'flux_err',1e-19,'snr',10.0)), 'test' "
        f"FROM spectra s, objects o WHERE s.fits_path = '{FITS}' AND o.object_id = '{OBJECT}';"
    )


def _flag() -> str:
    return _scalar(f"\\t on\nSELECT f.stale_redshift FROM spectrum_line_fits f WHERE f.target_id = '{TARGET}';")


@requires_local_db
def test_fit_insert_then_inspection_serializes(fixtures):
    """A inserts the fit (fresh, uncommitted, FOR SHARE on the object). B inspects the
    object -> must wait. After A commits, B's recompute sees the fit: stale."""
    a, b = Session("a"), Session("b")
    try:
        a.send_and_wait("BEGIN;", "A_BEGUN")
        a.send_and_wait(_insert_fit_sql(), "A_INSERTED")
        b.send(f"UPDATE objects SET redshift_inspected = 5.1 WHERE object_id = '{OBJECT}';")
        b.send("\\echo B_DONE")
        _wait_until_blocked("race_b")
        a.send_and_wait("COMMIT;", "A_COMMITTED")
        b.wait_for("B_DONE", timeout=20)
        assert _flag() == "t", "fit inserted during an inspection committed fresh"
    finally:
        a.close(); b.close()


@requires_local_db
def test_inspection_then_fit_insert_serializes(fixtures):
    """B inspects (uncommitted, holds the objects row). A inserts the fit -> its
    FOR SHARE must wait. After B commits, A computes from the new redshift: stale."""
    a, b = Session("a"), Session("b")
    try:
        b.send_and_wait("BEGIN;", "B_BEGUN")
        b.send_and_wait(f"UPDATE objects SET redshift_inspected = 5.1 WHERE object_id = '{OBJECT}';", "B_UPDATED")
        a.send("BEGIN;")
        a.send(_insert_fit_sql())
        a.send("\\echo A_INSERTED")
        _wait_until_blocked("race_a")
        b.send_and_wait("COMMIT;", "B_COMMITTED")
        a.wait_for("A_INSERTED", timeout=20)
        a.send_and_wait("COMMIT;", "A_COMMITTED")
        assert _flag() == "t", "fit inserted after an inspection began committed fresh"
    finally:
        a.close(); b.close()


def _line_object() -> str:
    return _scalar(
        "\\t on\n"
        f"SELECT o.object_id FROM spectrum_lines l JOIN spectra s ON s.id = l.spectrum_id "
        f"JOIN objects o ON o.id = l.object_id WHERE s.fits_path = '{FITS}' LIMIT 1;"
    )


@requires_local_db
def test_fit_insert_then_target_regroup_serializes(fixtures):
    """A inserts the fit and its lines (object link = OBJECT, uncommitted, FOR SHARE on
    the target). B regroups the target to OBJECT2 -> must wait. After A commits, B's
    relink sees the lines: they point at OBJECT2."""
    a, b = Session("a"), Session("b")
    try:
        a.send_and_wait("BEGIN;", "A_BEGUN")
        a.send_and_wait(_insert_fit_sql(), "A_INSERTED")
        b.send(f"UPDATE targets SET object_id = (SELECT id FROM objects WHERE object_id = '{OBJECT2}') WHERE target_id = '{TARGET}';")
        b.send("\\echo B_DONE")
        _wait_until_blocked("race_b")
        a.send_and_wait("COMMIT;", "A_COMMITTED")
        b.wait_for("B_DONE", timeout=20)
        assert _line_object() == OBJECT2, "lines written during a target regroup kept the old object"
    finally:
        a.close(); b.close()


@requires_local_db
def test_target_regroup_then_fit_insert_serializes(fixtures):
    """B regroups the target (uncommitted, holds the targets row). A inserts the fit ->
    its FOR SHARE on the target must wait. After B commits, A links to OBJECT2."""
    a, b = Session("a"), Session("b")
    try:
        b.send_and_wait("BEGIN;", "B_BEGUN")
        b.send_and_wait(f"UPDATE targets SET object_id = (SELECT id FROM objects WHERE object_id = '{OBJECT2}') WHERE target_id = '{TARGET}';", "B_UPDATED")
        a.send("BEGIN;")
        a.send(_insert_fit_sql())
        a.send("\\echo A_INSERTED")
        _wait_until_blocked("race_a")
        b.send_and_wait("COMMIT;", "B_COMMITTED")
        a.wait_for("A_INSERTED", timeout=20)
        a.send_and_wait("COMMIT;", "A_COMMITTED")
        assert _line_object() == OBJECT2, "lines written after a target regroup began kept the old object"
    finally:
        a.close(); b.close()
