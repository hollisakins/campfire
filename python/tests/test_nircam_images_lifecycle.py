"""NIRCam mosaic (nircam_images) lifecycle gate (epic #261, N2).

DB-backed integration test: runs ``sql/nircam_images_lifecycle.sql`` against the
local Supabase Postgres and asserts the harness reaches its PASS marker. Proves a
draft mosaic is admin-only, a published one is public to everyone, a revoked one
is hidden again, and that set_deployment_status on a NIRCam field deployment flips
its mosaics' deploy_status.

Skips automatically when the local Supabase DB container is not reachable.
"""
import subprocess
from pathlib import Path

import pytest

CONTAINER = "supabase_db_campfire"
SQL_FILE = Path(__file__).parent / "sql" / "nircam_images_lifecycle.sql"
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
def test_nircam_images_lifecycle():
    """Draft mosaic admin-only; published public; revoked hidden; RPC flips it."""
    sql = SQL_FILE.read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"NIRCam images lifecycle harness FAILED (psql exit {result.returncode}).\n"
        f"A draft/revoked mosaic leaked to a non-admin, or publish didn't make it "
        f"public. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"NIRCam images lifecycle harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
