"""B2 (#218) intermediate-product lifecycle gate.

DB-backed integration test: runs ``sql/b2_lifecycle.sql`` against the local
Supabase Postgres via ``docker exec ... psql`` and asserts the harness reaches
its PASS marker. The SQL drives the seed's published control object through a
full revoke -> recover cycle using the B2 lifecycle RPCs (inside a rolled-back
transaction) and proves end to end that:

  * ``get_lifecycle_status().enabled`` is true (the ``--in-prep`` capability gate),
  * ``set_spectra_deploy_status`` flips deploy_status, recomputes
    targets/objects.has_published_spectrum, and writes a deploy_events audit row,
  * a non-admin sees ZERO revoked rows and ZERO admin-only-table rows through RLS,
  * a non-admin is denied the lifecycle RPC, while service_role / admin are not.

Skips automatically when the local Supabase DB container is not reachable (no-op
without ``supabase start``). In CI it runs after ``supabase db reset``. Companion
to ``test_deploy_status_rls.py`` (B1): B1 proves the readers hide unpublished
data; this proves the RPCs that produce and reverse that state are correct.
"""
import subprocess
from pathlib import Path

import pytest

CONTAINER = "supabase_db_campfire"
SQL_FILE = Path(__file__).parent / "sql" / "b2_lifecycle.sql"
PASS_MARKER = "B2 LIFECYCLE HARNESS: ALL ASSERTIONS PASSED"


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
def test_lifecycle_publish_revoke_no_leak():
    """The full revoke/recover lifecycle keeps unpublished data hidden + audited."""
    sql = SQL_FILE.read_text()
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER,
         "psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=120,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"B2 lifecycle harness FAILED (psql exit {result.returncode}).\n"
        f"A lifecycle RPC mis-transitioned status, skipped a has_published_spectrum "
        f"recompute, missed an audit row, or leaked an admin-only row to a non-admin. "
        f"Output:\n{combined}"
    )
    assert PASS_MARKER in result.stdout, (
        f"B2 lifecycle harness did not reach its PASS marker.\nOutput:\n{combined}"
    )
