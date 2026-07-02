"""NIRCam field-deployment visibility gate (epic #261, N1).

DB-backed integration test: runs ``sql/nircam_field_deploy_gate.sql`` against the
local Supabase Postgres and asserts the harness reaches its PASS marker. Proves
that a *draft* field-scoped deployment is admin-only and a *published* one is
public to everyone (NIRCam fields span multiple programs, so there is no
per-program scope), enforced across the storage_objects RLS +
filter_accessible_storage_keys + get_storage_objects_for_sync, and that
set_deployment_status flips a field deployment without the observation-NULL raise.

Skips automatically when the local Supabase DB container is not reachable.
"""
import subprocess
from pathlib import Path

import pytest

CONTAINER = "supabase_db_campfire"
SQL_FILE = Path(__file__).parent / "sql" / "nircam_field_deploy_gate.sql"
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
def test_nircam_field_deploy_gate():
    """Draft field deploy admin-only; published field deploy public to everyone."""
    sql = SQL_FILE.read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"NIRCam field-deploy gate harness FAILED (psql exit {result.returncode}).\n"
        f"A draft field deploy leaked to a non-admin, or a published one wasn't public. "
        f"Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"NIRCam field-deploy gate harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
