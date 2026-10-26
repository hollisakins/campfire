"""Two-session check that the is_public maintenance serializes with program
privacy changes (Catalog Read Model phase 1, #533; Codex finding on #651).

objects / targets / spectra.is_public is computed by a BEFORE trigger from
``programs.is_public`` and rewritten by an AFTER trigger on programs when a
program flips. Read in isolation, the two paths can interleave: a row inserted
while a program is being made private would be computed from the old value,
the propagation trigger cannot see the uncommitted row, and the row would
commit public for good. The row-side triggers therefore read programs
``FOR SHARE``, which conflicts with the row lock ``UPDATE programs`` takes, so
one side waits for the other. This test drives two psql sessions through both
orderings and asserts the second waits (pg_stat_activity shows a Lock wait)
and the committed flag is the private one.

Skips when no local Supabase Postgres is reachable (see ``_localdb.py``).
"""
from __future__ import annotations

import queue
import subprocess
import threading
import time
import uuid

import pytest

from ._localdb import _psql_cmd, requires_local_db, run_sql

PROGRAM = "zzz_race_" + uuid.uuid4().hex[:8]
OBJECT_A = "TEST-RACE-A-" + uuid.uuid4().hex[:6]
OBJECT_B = "TEST-RACE-B-" + uuid.uuid4().hex[:6]


class Session:
    """An interactive psql session: send SQL, wait for an echoed marker."""

    def __init__(self, name: str):
        cmd = _psql_cmd()
        assert cmd is not None
        self.name = name
        self.proc = subprocess.Popen(
            [*cmd, "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        # tag the backend so pg_stat_activity can find it
        self.send(f"SET application_name = 'race_{name}';")

    def _pump(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def send(self, sql: str):
        assert self.proc.stdin is not None
        self.proc.stdin.write(sql + "\n")
        self.proc.stdin.flush()

    def send_and_wait(self, sql: str, marker: str, timeout: float = 20.0) -> list[str]:
        """Send ``sql`` then ``\\echo marker``; return the lines seen before the marker."""
        self.send(sql)
        self.send(f"\\echo {marker}")
        return self.wait_for(marker, timeout)

    def wait_for(self, marker: str, timeout: float = 20.0) -> list[str]:
        seen: list[str] = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"session {self.name}: no '{marker}' within {timeout}s; saw {seen}")
            try:
                line = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line == marker:
                return seen
            seen.append(line)

    def close(self):
        try:
            self.send("ROLLBACK;")
            self.send("\\q")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _scalar(sql: str) -> str:
    r = run_sql(sql)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip().splitlines()[-1].strip() if r.stdout.strip() else ""


def _waiting_on_lock(app_name: str) -> bool:
    out = _scalar(
        "\\t on\n"
        f"SELECT count(*) FROM pg_stat_activity WHERE application_name = '{app_name}' "
        "AND wait_event_type = 'Lock';"
    )
    return out == "1"


def _wait_until_blocked(app_name: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _waiting_on_lock(app_name):
            return
        time.sleep(0.2)
    raise AssertionError(f"{app_name} never blocked on a lock: the FOR SHARE serialization is missing")


@pytest.fixture
def fixtures():
    r = run_sql(
        f"INSERT INTO programs (slug, program_name, is_public) VALUES ('{PROGRAM}', 'Race test', true);"
    )
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        yield
    finally:
        run_sql(
            f"DELETE FROM objects WHERE object_id IN ('{OBJECT_A}', '{OBJECT_B}');\n"
            f"DELETE FROM programs WHERE slug = '{PROGRAM}';"
        )


INSERT_OBJECT = (
    "INSERT INTO objects (object_id, field, ra, dec, programs, observations, has_published_spectrum) "
    "VALUES ('{oid}', 'zzz_race_field', 153.0, 2.0, '{{{program}}}', '{{}}', true);"
)


@requires_local_db
def test_row_insert_then_program_flip_serializes(fixtures):
    """A: insert (uncommitted, holds FOR SHARE on P). B: make P private -> must wait.
    After A commits, B's propagation rewrites A's row: the committed flag is false."""
    a, b = Session("a"), Session("b")
    try:
        a.send_and_wait("BEGIN;", "A_BEGUN")
        a.send_and_wait(INSERT_OBJECT.format(oid=OBJECT_A, program=PROGRAM), "A_INSERTED")
        # the uncommitted row carries the public flag it computed
        got = a.send_and_wait(f"SELECT is_public FROM objects WHERE object_id = '{OBJECT_A}';", "A_READ")
        assert got[-1] == "t", got

        b.send(f"UPDATE programs SET is_public = false WHERE slug = '{PROGRAM}';")
        b.send("\\echo B_DONE")
        _wait_until_blocked("race_b")

        a.send_and_wait("COMMIT;", "A_COMMITTED")
        b.wait_for("B_DONE", timeout=20)

        flag = _scalar(f"\\t on\nSELECT is_public FROM objects WHERE object_id = '{OBJECT_A}';")
        assert flag == "f", f"row inserted during a privacy flip committed public (is_public={flag!r})"
    finally:
        a.close(); b.close()


@requires_local_db
def test_program_flip_then_row_insert_serializes(fixtures):
    """B: make P private (uncommitted, holds the programs row). A: insert -> its
    FOR SHARE must wait. After B commits, A computes the flag from the new value."""
    a, b = Session("a"), Session("b")
    try:
        b.send_and_wait("BEGIN;", "B_BEGUN")
        b.send_and_wait(f"UPDATE programs SET is_public = false WHERE slug = '{PROGRAM}';", "B_UPDATED")

        a.send("BEGIN;")
        a.send(INSERT_OBJECT.format(oid=OBJECT_B, program=PROGRAM))
        a.send("\\echo A_INSERTED")
        _wait_until_blocked("race_a")

        b.send_and_wait("COMMIT;", "B_COMMITTED")
        a.wait_for("A_INSERTED", timeout=20)
        a.send_and_wait("COMMIT;", "A_COMMITTED")

        flag = _scalar(f"\\t on\nSELECT is_public FROM objects WHERE object_id = '{OBJECT_B}';")
        assert flag == "f", f"row inserted after a privacy flip began committed public (is_public={flag!r})"
    finally:
        a.close(); b.close()
