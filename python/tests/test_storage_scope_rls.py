"""storage_objects program-scope leak gate (epic #210).

DB-backed integration test: runs ``sql/storage_objects_scope_leak.sql`` against
the local Supabase Postgres via ``docker exec ... psql`` and asserts the harness
reaches its PASS marker. The SQL flips seed rows to draft/revoked and moves one
into a private program (inside a rolled-back transaction), then asserts a
non-admin sees ZERO of those storage objects through RLS and the service-role
RPCs (get_storage_objects_for_sync / filter_accessible_storage_keys), while an
admin does — the exit criterion for opening the registry to program-scoped reads.

Skips automatically when the local Supabase DB container is not reachable.
"""
import subprocess
from pathlib import Path

import pytest

CONTAINER = "supabase_db_campfire"
SQL_FILE = Path(__file__).parent / "sql" / "storage_objects_scope_leak.sql"
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
def test_storage_objects_scope_no_leak_to_non_admin():
    """Non-admin gets zero draft/revoked/out-of-program storage objects; admin sees them."""
    sql = SQL_FILE.read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"storage_objects scope leak harness FAILED (psql exit {result.returncode}).\n"
        f"A non-admin saw a draft/revoked/out-of-program storage object, or a "
        f"published control row regressed. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"storage_objects scope leak harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
