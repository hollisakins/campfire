"""NIRCam field-deployment visibility gate (epic #261, N1).

DB-backed integration test: runs ``sql/nircam_field_deploy_gate.sql`` against the
local Supabase Postgres and asserts the harness reaches its PASS marker. Proves
that a *draft* field-scoped deployment is admin-only and a *published* one is
public to everyone (NIRCam fields span multiple programs, so there is no
per-program scope), enforced across the storage_objects RLS +
filter_accessible_storage_keys + get_storage_objects_for_sync, and that
set_deployment_status flips a field deployment without the observation-NULL raise.

Skips automatically when no local Supabase Postgres is reachable.
"""
from pathlib import Path

import pytest

SQL_FILE = Path(__file__).parent / "sql" / "nircam_field_deploy_gate.sql"
PASS_MARKER = "ALL ASSERTIONS PASSED"


from ._localdb import requires_local_db, run_sql  # noqa: E402


@requires_local_db
def test_nircam_field_deploy_gate():
    """Draft field deploy admin-only; published field deploy public to everyone."""
    sql = SQL_FILE.read_text()
    result = run_sql(sql)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"NIRCam field-deploy gate harness FAILED (psql exit {result.returncode}).\n"
        f"A draft field deploy leaked to a non-admin, or a published one wasn't public. "
        f"Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"NIRCam field-deploy gate harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
