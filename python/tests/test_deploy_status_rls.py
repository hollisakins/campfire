"""B1 (#217) deploy_status lifecycle-enforcement leak gate.

DB-backed integration test: runs ``sql/b1_deploy_status_leak.sql`` against the
local Supabase Postgres via ``docker exec ... psql`` and asserts the harness
reaches its PASS marker. The SQL marks seed rows draft/revoked inside a
transaction (rolled back) and asserts a non-admin sees ZERO of them through RLS
and the service-role RPC predicate, while an admin does — the B1 exit criterion.

Skips automatically when the local Supabase DB container is not reachable (so it
is a no-op in environments without ``supabase start``). In CI it runs after
``supabase db reset``. The unit-level RLS guarantees themselves do not depend on
this test running, but it is the regression gate that proves the predicate
threading actually hides unpublished science data.
"""
import subprocess
from pathlib import Path

import pytest

CONTAINER = "supabase_db_campfire"
SQL_FILE = Path(__file__).parent / "sql" / "b1_deploy_status_leak.sql"
PASS_MARKER = "ALL ASSERTIONS PASSED"


def _docker_psql_available() -> bool:
    """True iff the local Supabase Postgres container answers a trivial query."""
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
def test_deploy_status_no_leak_to_non_admin():
    """Non-admin gets zero draft/revoked rows (RLS + RPC); admin sees them."""
    sql = SQL_FILE.read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=120,
    )
    combined = result.stdout + result.stderr
    # A leak/regression RAISEs inside a DO block -> ON_ERROR_STOP -> non-zero exit.
    assert result.returncode == 0, (
        f"B1 leak harness FAILED (psql exit {result.returncode}).\n"
        f"This means a reader exposed an draft/revoked row to a non-admin, or a "
        f"published control row regressed. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"B1 leak harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
