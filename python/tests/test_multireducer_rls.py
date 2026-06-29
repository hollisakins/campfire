"""B4 (#220) multi-reducer concurrency gate.

DB-backed integration test: runs ``sql/b4_multireducer.sql`` against the local
Supabase Postgres via ``docker exec ... psql`` and asserts the harness reaches
its PASS marker. The SQL proves claim_deploy_scope's optimistic compare-and-set
detects a concurrent same-scope deploy (the "not silently clobbered" gate) and
denies non-admins. Skips when the local DB container is unreachable.
"""
import subprocess
from pathlib import Path

import pytest

CONTAINER = "supabase_db_campfire"
SQL_FILE = Path(__file__).parent / "sql" / "b4_multireducer.sql"
PASS_MARKER = "B4 MULTI-REDUCER HARNESS: ALL ASSERTIONS PASSED"


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
def test_concurrent_deploy_detected():
    """A stale compare-and-set is detected as a conflict, not silently applied."""
    sql = SQL_FILE.read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=60,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"B4 multi-reducer harness FAILED (psql exit {result.returncode}).\n"
        f"claim_deploy_scope did not detect a stale compare-and-set, or the admin "
        f"gate failed. Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"B4 multi-reducer harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
