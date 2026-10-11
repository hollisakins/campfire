"""B4 (#220) multi-reducer concurrency gate.

DB-backed integration test: runs ``sql/b4_multireducer.sql`` against the local
Supabase Postgres via psql (Docker container or native stack, see ``_localdb.py``) and asserts the harness reaches
its PASS marker. The SQL proves claim_deploy_scope's optimistic compare-and-set
detects a concurrent same-scope deploy (the "not silently clobbered" gate) and
denies non-admins. Skips when the local DB container is unreachable.
"""
from pathlib import Path

import pytest

SQL_FILE = Path(__file__).parent / "sql" / "b4_multireducer.sql"
PASS_MARKER = "B4 MULTI-REDUCER HARNESS: ALL ASSERTIONS PASSED"


from ._localdb import requires_local_db, run_sql  # noqa: E402


@requires_local_db
def test_concurrent_deploy_detected():
    """A stale compare-and-set is detected as a conflict, not silently applied."""
    sql = SQL_FILE.read_text()
    result = run_sql(sql, timeout=60)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"B4 multi-reducer harness FAILED (psql exit {result.returncode}).\n"
        f"claim_deploy_scope did not detect a stale compare-and-set, or the admin "
        f"gate failed. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"B4 multi-reducer harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
