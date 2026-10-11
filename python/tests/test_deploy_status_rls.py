"""B1 (#217) deploy_status lifecycle-enforcement leak gate.

DB-backed integration test: runs ``sql/b1_deploy_status_leak.sql`` against the
local Supabase Postgres via psql (Docker container or native stack, see ``_localdb.py``) and asserts the harness
reaches its PASS marker. The SQL marks seed rows draft/revoked inside a
transaction (rolled back) and asserts a non-admin sees ZERO of them through RLS
and the service-role RPC predicate, while an admin does — the B1 exit criterion.

Skips automatically when the local Supabase DB container is not reachable (so it
is a no-op in environments without ``supabase start``). In CI it runs after
``supabase db reset``. The unit-level RLS guarantees themselves do not depend on
this test running, but it is the regression gate that proves the predicate
threading actually hides unpublished science data.
"""
from pathlib import Path

import pytest

SQL_FILE = Path(__file__).parent / "sql" / "b1_deploy_status_leak.sql"
PASS_MARKER = "ALL ASSERTIONS PASSED"


from ._localdb import requires_local_db, run_sql  # noqa: E402


@requires_local_db
def test_deploy_status_no_leak_to_non_admin():
    """Non-admin gets zero draft/revoked rows (RLS + RPC); admin sees them."""
    sql = SQL_FILE.read_text()
    result = run_sql(sql)
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
