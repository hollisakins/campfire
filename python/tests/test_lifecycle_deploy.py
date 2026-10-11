"""Unit tests for the B2 (#218) deploy-side lifecycle plumbing.

Pure-Python: a tiny fake Supabase client captures/serves the PostgREST chain so
we can assert the deploy logic without a DB. The DB-backed behaviour (the RPCs,
RLS, recompute) is covered by ``test_lifecycle_rls.py`` against a local instance.
"""
from campfire.deploy.deploy import _count_published_spectra
from campfire.deploy.supabase import insert_deployment
from tests.fakes import FakePostgrest


# ---------------------------------------------------------------------------
# _count_published_spectra: the --in-prep "would hide live data" guard
# ---------------------------------------------------------------------------

def test_count_published_spectra_counts_only_matching_pairs():
    existing = [
        {"target_id": "t1", "grating": "prism", "deploy_status": "published"},
        {"target_id": "t1", "grating": "g140m", "deploy_status": "published"},
        {"target_id": "t2", "grating": "prism", "deploy_status": "draft"},  # not published
    ]
    client = FakePostgrest(tables={'spectra': existing})
    # Deploying t1/prism (live) + t2/prism (draft) + t3/prism (new) -> only t1/prism counts.
    spectra = [
        {"target_id": "t1", "grating": "prism"},
        {"target_id": "t2", "grating": "prism"},
        {"target_id": "t3", "grating": "prism"},
    ]
    assert _count_published_spectra(client, spectra) == 1


def test_count_published_spectra_zero_for_all_new():
    client = FakePostgrest()  # nothing exists yet
    spectra = [{"target_id": "t9", "grating": "prism"}]
    assert _count_published_spectra(client, spectra) == 0


# ---------------------------------------------------------------------------
# insert_deployment: status threading + published_at stamping
# ---------------------------------------------------------------------------

def test_insert_deployment_published_stamps_published_at():
    client = FakePostgrest(next_id=42)  # the deployments sequence hands back 42
    dep_id = insert_deployment(client, observation="obs", deployed_by="u1",
                               status="published")
    assert dep_id == 42
    rec = client.inserts[-1].data
    assert rec["status"] == "published"
    assert rec.get("published_at")  # stamped on a published deploy


def test_insert_deployment_draft_leaves_published_at_null():
    client = FakePostgrest()
    insert_deployment(client, observation="obs", deployed_by="u1", status="draft")
    rec = client.inserts[-1].data
    assert rec["status"] == "draft"
    assert "published_at" not in rec  # draft: no publish stamp until an admin publishes


def test_insert_deployment_defaults_to_published():
    client = FakePostgrest()
    insert_deployment(client, observation="obs", deployed_by="u1")
    assert client.inserts[-1].data["status"] == "published"
