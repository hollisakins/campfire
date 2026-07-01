"""NIRCam exposure admin-only leak gate (epic #261, N1).

DB-backed integration test: runs ``sql/nircam_exposure_admin_only.sql`` against
the local Supabase Postgres via ``docker exec ... psql`` and asserts the harness
reaches its PASS marker. The SQL inserts a canonical NIRCam exposure storage
object (spectrum_id=NULL, deployment_id=NULL) plus a nircam_exposures triage row
(inside a rolled-back transaction) and asserts a non-admin sees ZERO of them
through RLS, get_storage_objects_for_sync, and filter_accessible_storage_keys —
while an admin does. This is the exit criterion for deploying NIRCam canonical
exposures admin-only (no public/draft tier for exposures; that lives at the
mosaic level, N3).

Skips automatically when the local Supabase DB container is not reachable.
"""
import subprocess
from pathlib import Path

import pytest

CONTAINER = "supabase_db_campfire"
SQL_FILE = Path(__file__).parent / "sql" / "nircam_exposure_admin_only.sql"
PASS_MARKER = "ALL ASSERTIONS PASSED"


def _docker_psql_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "exec", "-i", CONTAINER,
             "psql", "-U", "postgres", "-d", "postgres", "-tA", "-c", "SELECT 1"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0 and r.stdout.strip().startswith("1")
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


requires_local_db = pytest.mark.skipif(
    not _docker_psql_available(),
    reason=f"local Supabase container '{CONTAINER}' not reachable (run `supabase start` + `supabase db reset`)",
)


@requires_local_db
def test_nircam_exposures_admin_only_no_leak():
    """Non-admin gets zero NIRCam exposure objects/rows; admin sees them."""
    sql = SQL_FILE.read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"NIRCam exposure admin-only harness FAILED (psql exit {result.returncode}).\n"
        f"A non-admin saw a NIRCam exposure storage object or triage row. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"NIRCam exposure admin-only harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
