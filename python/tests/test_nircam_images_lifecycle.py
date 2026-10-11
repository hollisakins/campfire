"""NIRCam mosaic (nircam_images) lifecycle gate (epic #261, N2).

DB-backed integration test: runs ``sql/nircam_images_lifecycle.sql`` against the
local Supabase Postgres and asserts the harness reaches its PASS marker. Proves a
draft mosaic is admin-only, a published one is public to everyone, a revoked one
is hidden again, and that set_deployment_status on a NIRCam field deployment flips
its mosaics' deploy_status.

Skips automatically when no local Supabase Postgres is reachable.
"""
from pathlib import Path

import pytest

SQL_FILE = Path(__file__).parent / "sql" / "nircam_images_lifecycle.sql"
PASS_MARKER = "ALL ASSERTIONS PASSED"


from ._localdb import requires_local_db, run_sql  # noqa: E402


@requires_local_db
def test_nircam_images_lifecycle():
    """Draft mosaic admin-only; published public; revoked hidden; RPC flips it."""
    sql = SQL_FILE.read_text()
    result = run_sql(sql)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"NIRCam images lifecycle harness FAILED (psql exit {result.returncode}).\n"
        f"A draft/revoked mosaic leaked to a non-admin, or publish didn't make it "
        f"public. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"NIRCam images lifecycle harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
