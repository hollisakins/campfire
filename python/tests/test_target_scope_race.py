"""Two-session check that the spectra program_slug / observation copies serialize
with a change of their target's scope (Catalog Read Model phase 2b).

spectra.program_slug and spectra.observation are copies of the parent target's
columns: sync_spectra_target_scope (BEFORE INSERT / UPDATE) sets them from the
target, and propagate_target_scope_to_spectra (AFTER UPDATE on targets)
rewrites existing spectra when the target moves. In isolation the two can
interleave: a spectrum inserted while its target is being re-scoped copies the
OLD values, the propagation cannot see the uncommitted spectrum, and the copy
commits wrong for good. Phase 2b made the BEFORE trigger read the target
FOR SHARE, which conflicts with the row lock the UPDATE holds. This drives
both orderings through two psql sessions and asserts the second waits and the
committed spectrum carries the new scope.

Skips when no local Supabase Postgres is reachable (see ``_localdb.py``).
"""
from __future__ import annotations

import uuid

import pytest

from ._localdb import requires_local_db, run_sql
from .test_is_public_race import Session, _scalar, _wait_until_blocked

SUFFIX = uuid.uuid4().hex[:8]
PROGRAM = "zzz_tsr_" + SUFFIX
OBS_OLD = "tsr_old_" + SUFFIX
OBS_NEW = "tsr_new_" + SUFFIX
TARGET = "test-tsr-" + SUFFIX
FITS = f"/tmp/tsr_{SUFFIX}.fits"


@pytest.fixture
def fixtures():
    r = run_sql(
        f"""
        INSERT INTO programs (slug, program_name, is_public) VALUES ('{PROGRAM}', 'Scope race', true);
        INSERT INTO observations (name, program_slug, jwst_program_id, field)
          VALUES ('{OBS_OLD}', '{PROGRAM}', 9986, 'zzz_tsr_field'),
                 ('{OBS_NEW}', '{PROGRAM}', 9986, 'zzz_tsr_field');
        INSERT INTO targets (target_id, field, ra, dec, program_slug, observation)
          VALUES ('{TARGET}', 'zzz_tsr_field', 156.0, 2.0, '{PROGRAM}', '{OBS_OLD}');
        """
    )
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        yield
    finally:
        run_sql(
            f"""
            DELETE FROM spectra WHERE fits_path = '{FITS}';
            DELETE FROM targets WHERE target_id = '{TARGET}';
            DELETE FROM observations WHERE name IN ('{OBS_OLD}', '{OBS_NEW}');
            DELETE FROM programs WHERE slug = '{PROGRAM}';
            """
        )


def _insert_spectrum_sql() -> str:
    return (
        "INSERT INTO spectra (target_id, grating, fits_path, signal_to_noise, exposure_time, deploy_status) "
        f"VALUES ('{TARGET}', 'PRISM', '{FITS}', 10.0, 1000.0, 'published');"
    )


def _move_target_sql() -> str:
    return f"UPDATE targets SET observation = '{OBS_NEW}' WHERE target_id = '{TARGET}';"


def _spectrum_observation() -> str:
    return _scalar(f"\\t on\nSELECT observation FROM spectra WHERE fits_path = '{FITS}';")


@requires_local_db
def test_spectrum_insert_then_target_move_serializes(fixtures):
    """A inserts the spectrum (copies OBS_OLD, uncommitted, FOR SHARE on the target).
    B moves the target -> must wait. After A commits, B's propagation sees the
    spectrum and rewrites it: OBS_NEW."""
    a, b = Session("a"), Session("b")
    try:
        a.send_and_wait("BEGIN;", "A_BEGUN")
        a.send_and_wait(_insert_spectrum_sql(), "A_INSERTED")
        b.send(_move_target_sql())
        b.send("\\echo B_DONE")
        _wait_until_blocked("race_b")
        a.send_and_wait("COMMIT;", "A_COMMITTED")
        b.wait_for("B_DONE", timeout=20)
        assert _spectrum_observation() == OBS_NEW, "spectrum inserted during a target move kept the old scope"
    finally:
        a.close(); b.close()


@requires_local_db
def test_target_move_then_spectrum_insert_serializes(fixtures):
    """B moves the target (uncommitted, holds the targets row). A inserts the
    spectrum -> its FOR SHARE must wait. After B commits, A copies OBS_NEW."""
    a, b = Session("a"), Session("b")
    try:
        b.send_and_wait("BEGIN;", "B_BEGUN")
        b.send_and_wait(_move_target_sql(), "B_UPDATED")
        a.send("BEGIN;")
        a.send(_insert_spectrum_sql())
        a.send("\\echo A_INSERTED")
        _wait_until_blocked("race_a")
        b.send_and_wait("COMMIT;", "B_COMMITTED")
        a.wait_for("A_INSERTED", timeout=20)
        a.send_and_wait("COMMIT;", "A_COMMITTED")
        assert _spectrum_observation() == OBS_NEW, "spectrum inserted after a target move began kept the old scope"
    finally:
        a.close(); b.close()
