"""Unit tests for the B2 (#218) deploy-side lifecycle plumbing.

Pure-Python: a tiny fake Supabase client captures/serves the PostgREST chain so
we can assert the deploy logic without a DB. The DB-backed behaviour (the RPCs,
RLS, recompute) is covered by ``test_lifecycle_rls.py`` against a local instance.
"""
import types

from campfire.deploy.deploy import _count_published_spectra
from campfire.deploy.supabase import insert_deployment


# ---------------------------------------------------------------------------
# fake client: supports .table().select().in_().eq().execute() and .insert()
# ---------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, rows, captured):
        self._rows = rows
        self._in = None
        self._eq = []
        self._captured = captured

    def select(self, *a, **k):
        return self

    def in_(self, col, vals):
        self._in = (col, set(vals))
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def insert(self, data):
        self._captured.append(data)
        return self

    def execute(self):
        rows = list(self._rows)
        if self._in:
            col, vals = self._in
            rows = [r for r in rows if r.get(col) in vals]
        for col, val in self._eq:
            rows = [r for r in rows if r.get(col) == val]
        # insert() returns a row with an id (mimic deployments insert)
        if self._captured:
            return types.SimpleNamespace(data=[{**self._captured[-1], "id": 42}])
        return types.SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.inserted = []

    def table(self, name):
        return _FakeQuery(self._rows, self.inserted)


# ---------------------------------------------------------------------------
# _count_published_spectra: the --in-prep "would hide live data" guard
# ---------------------------------------------------------------------------

def test_count_published_spectra_counts_only_matching_pairs():
    existing = [
        {"target_id": "t1", "grating": "prism", "deploy_status": "published"},
        {"target_id": "t1", "grating": "g140m", "deploy_status": "published"},
        {"target_id": "t2", "grating": "prism", "deploy_status": "in_prep"},  # not published
    ]
    client = _FakeClient(existing)
    # Deploying t1/prism (live) + t2/prism (draft) + t3/prism (new) -> only t1/prism counts.
    spectra = [
        {"target_id": "t1", "grating": "prism"},
        {"target_id": "t2", "grating": "prism"},
        {"target_id": "t3", "grating": "prism"},
    ]
    assert _count_published_spectra(client, spectra) == 1


def test_count_published_spectra_zero_for_all_new():
    client = _FakeClient([])  # nothing exists yet
    spectra = [{"target_id": "t9", "grating": "prism"}]
    assert _count_published_spectra(client, spectra) == 0


# ---------------------------------------------------------------------------
# insert_deployment: status threading + published_at stamping
# ---------------------------------------------------------------------------

def test_insert_deployment_published_stamps_published_at():
    client = _FakeClient()
    dep_id = insert_deployment(client, observation="obs", deployed_by="u1",
                               status="published")
    assert dep_id == 42
    rec = client.inserted[-1]
    assert rec["status"] == "published"
    assert rec.get("published_at")  # stamped on a published deploy


def test_insert_deployment_in_prep_leaves_published_at_null():
    client = _FakeClient()
    insert_deployment(client, observation="obs", deployed_by="u1", status="in_prep")
    rec = client.inserted[-1]
    assert rec["status"] == "in_prep"
    assert "published_at" not in rec  # draft: no publish stamp until an admin publishes


def test_insert_deployment_defaults_to_published():
    client = _FakeClient()
    insert_deployment(client, observation="obs", deployed_by="u1")
    assert client.inserted[-1]["status"] == "published"
