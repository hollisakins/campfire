"""NIRCam exposure admin-only leak gate (epic #261, N1).

DB-backed integration test: runs ``sql/nircam_exposure_admin_only.sql`` against
the local Supabase Postgres via psql (Docker container or native stack, see ``_localdb.py``) and asserts the harness
reaches its PASS marker. The SQL inserts a canonical NIRCam exposure storage
object (spectrum_id=NULL, deployment_id=NULL) plus a nircam_exposures triage row
(inside a rolled-back transaction) and asserts a non-admin sees ZERO of them
through RLS, get_storage_objects_for_sync, and filter_accessible_storage_keys —
while an admin does. This is the exit criterion for deploying NIRCam canonical
exposures admin-only (no public/draft tier for exposures; that lives at the
mosaic level, N3).

Skips automatically when no local Supabase Postgres is reachable.
"""
from pathlib import Path

import pytest

SQL_FILE = Path(__file__).parent / "sql" / "nircam_exposure_admin_only.sql"
PASS_MARKER = "ALL ASSERTIONS PASSED"


from ._localdb import requires_local_db, run_sql  # noqa: E402


@requires_local_db
def test_nircam_exposures_admin_only_no_leak():
    """Non-admin gets zero NIRCam exposure objects/rows; admin sees them."""
    sql = SQL_FILE.read_text()
    result = run_sql(sql)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"NIRCam exposure admin-only harness FAILED (psql exit {result.returncode}).\n"
        f"A non-admin saw a NIRCam exposure storage object or triage row. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"NIRCam exposure admin-only harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
