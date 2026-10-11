"""Shared access to a local Supabase Postgres for the SQL harness tests.

The seven ``test_*_rls.py`` / lifecycle harnesses pipe a ``sql/*.sql`` file into
psql and look for a PASS marker. Historically that meant ``docker exec`` into the
``supabase_db_campfire`` container. The local stack can now also run on the
CLI's native runtime (no container, ports allocated per project), so the
connection is resolved in this order:

1. ``CAMPFIRE_LOCAL_DB_URL`` — a libpq URL; run with a local ``psql`` binary.
   CI sets it from ``supabase status --output-format json`` (``.env.DB_URL``);
   the same command prints it locally (ports are per local project).
2. ``docker exec -i supabase_db_campfire psql`` — the classic Docker stack.

Tests skip (never fail) when neither answers ``SELECT 1``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

import pytest

CONTAINER = "supabase_db_campfire"
ENV_VAR = "CAMPFIRE_LOCAL_DB_URL"


def _psql_cmd() -> list[str] | None:
    """The psql invocation prefix to use, or None when no local DB is reachable."""
    url = os.environ.get(ENV_VAR)
    if url:
        psql = shutil.which("psql")
        if psql is None:
            return None
        return [psql, url]
    if shutil.which("docker") is None:
        return None
    return ["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", "postgres"]


@lru_cache(maxsize=1)
def local_db_available() -> bool:
    """True iff a local Supabase Postgres answers a trivial query."""
    cmd = _psql_cmd()
    if cmd is None:
        return False
    try:
        r = subprocess.run(
            [*cmd, "-tA", "-c", "SELECT 1"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0 and r.stdout.strip().startswith("1")
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


requires_local_db = pytest.mark.skipif(
    not local_db_available(),
    reason=(
        f"no local Supabase Postgres: set {ENV_VAR} (native stack: `supabase status --output-format json`, .env.DB_URL) "
        f"or run the Docker stack so container '{CONTAINER}' answers; then `supabase db reset`"
    ),
)


def run_sql(sql: str, *, timeout: int = 120) -> subprocess.CompletedProcess:
    """Pipe ``sql`` into psql with ON_ERROR_STOP and return the completed process."""
    cmd = _psql_cmd()
    assert cmd is not None, "run_sql called without a reachable local database"
    return subprocess.run(
        [*cmd, "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql, capture_output=True, text=True, timeout=timeout,
    )
