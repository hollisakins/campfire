"""storage_objects program-scope leak gate (epic #210).

DB-backed integration test: runs ``sql/storage_objects_scope_leak.sql`` against
the local Supabase Postgres via psql (Docker container or native stack, see ``_localdb.py``) and asserts the harness
reaches its PASS marker. The SQL flips seed rows to draft/revoked and moves one
into a private program (inside a rolled-back transaction), then asserts a
non-admin sees ZERO of those storage objects through RLS and the service-role
RPCs (get_storage_objects_for_sync / filter_accessible_storage_keys), while an
admin does — the exit criterion for opening the registry to program-scoped reads.

Skips automatically when no local Supabase Postgres is reachable.
"""
from pathlib import Path

import pytest

SQL_FILE = Path(__file__).parent / "sql" / "storage_objects_scope_leak.sql"
PASS_MARKER = "ALL ASSERTIONS PASSED"


from ._localdb import requires_local_db, run_sql  # noqa: E402


@requires_local_db
def test_storage_objects_scope_no_leak_to_non_admin():
    """Non-admin gets zero draft/revoked/out-of-program storage objects; admin sees them."""
    sql = SQL_FILE.read_text()
    result = run_sql(sql)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"storage_objects scope leak harness FAILED (psql exit {result.returncode}).\n"
        f"A non-admin saw a draft/revoked/out-of-program storage object, or a "
        f"published control row regressed. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"storage_objects scope leak harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
