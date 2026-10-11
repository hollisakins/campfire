"""`campfire dr status` (data releases P1): the report formatter, the CLI wiring,
and the dr_readiness() RPC against the local database.

Pure-Python half: ``format_readiness`` on a hand-built payload (version table
sorted by spectra, leases, null science-hash counts rendered as n/a) and the
CLI invoked with a fake client. DB half: ``sql/dr_readiness.sql`` (admin runs
it, non-admin raises, anon cannot execute, fixture rows are detected).
"""
import json
import types
from pathlib import Path

from click.testing import CliRunner

import campfire.cli as main_cli
from campfire.dr import fetch_readiness, format_readiness, versions_sorted

from ._localdb import requires_local_db, run_sql

SQL_FILE = Path(__file__).parent / "sql" / "dr_readiness.sql"
PASS_MARKER = "DR READINESS HARNESS: ALL ASSERTIONS PASSED"


SAMPLE = {
    "generated_at": "2026-10-11T03:18:30.623434+00:00",
    "spectra_by_version": {
        "0.4.1": {"spectra": 300, "observations": 3},
        "0.5.0+dirty": {"spectra": 12, "observations": 1},
        "0.6.0": {"spectra": 70000, "observations": 200},
    },
    "published": {"spectra": 70312, "observations": 204, "programs": 35, "objects": 42452},
    "drafts": {"spectra": 0, "nircam_images": 280, "deployments": 2},
    "revoked": {"spectra": 0, "deployments": 0},
    "leases": [{
        "scope_type": "observation", "scope_key": "ember_uds_p4", "version": 3,
        "in_progress_by": "1111", "in_progress_by_username": "hollis",
        "in_progress_host": "candide", "in_progress_since": "2026-10-11T02:00:00+00:00",
        "heartbeat_at": "2026-10-11T03:15:00+00:00", "stale": False,
    }, {
        "scope_type": "field", "scope_key": "cosmos", "version": 9,
        "in_progress_by": None, "in_progress_by_username": None,
        "in_progress_host": "laptop", "in_progress_since": "2026-10-10T20:00:00+00:00",
        "heartbeat_at": "2026-10-10T20:05:00+00:00", "stale": True,
    }],
    "redrafted_nircam_fields": [{"field": "cosmos", "draft_images": 280,
                                 "published_images": 1804, "published_deployments": 4}],
    "dangling_registry_rows": {"count": 280, "by_observation": {"old_obs": 200, "older_obs": 80}},
    "etag_hash_rows": {"nircam_exposure_full": 4000, "nircam_exposure_preview": 317},
    "line_fits": {"total": 15916, "stale_redshift": 1, "stale_spectrum": 0},
    "needs_review": 1339,
    "implicit_pins": 15443,
    "science_hash_missing": {"spectra": None, "storage_objects": None},
}


def test_versions_sorted_by_spectra_desc():
    assert [v for v, _, _ in versions_sorted(SAMPLE)] == ["0.6.0", "0.4.1", "0.5.0+dirty"]


def test_format_readiness_renders_every_section():
    text = format_readiness(SAMPLE)
    lines = text.splitlines()
    assert lines[0].startswith("Data-release readiness  (generated 2026-10-11 03:18 UTC)")
    assert "Published: 70,312 spectra · 204 observations · 35 programs · 42,452 objects" in text
    # version table: sorted by spectra, right-aligned counts
    i6 = text.index("0.6.0"); i4 = text.index("0.4.1"); i5 = text.index("0.5.0+dirty")
    assert i6 < i4 < i5
    assert "70,000" in text and "3 version string(s)" in text
    assert "Drafts:  0 spectra · 280 nircam_images · 2 deployments" in text
    assert "Deploy leases: 2" in text
    assert "observation:ember_uds_p4" in text and "hollis" in text and "candide" in text
    assert "service-role" in text and "STALE" in text and "live" in text
    assert "Re-drafted NIRCam fields: 1" in text and "cosmos" in text
    assert "Dangling nirspec_spec registry rows (no spectra row): 280" in text
    assert "old_obs" in text
    assert "Provisional etag: hashes (active rows): 4,317" in text
    assert "Line fits: 15,916 total · 1 stale redshift · 0 stale spectrum" in text
    assert "Objects: 1,339 need review · 15,443 implicit pins" in text
    assert "Science hash missing: spectra n/a (column not deployed yet)" in text


def test_format_readiness_empty_report():
    text = format_readiness({"generated_at": None, "spectra_by_version": {}, "leases": [],
                             "science_hash_missing": {"spectra": 12, "storage_objects": 0}})
    assert "(none)" in text and "(no deploy in progress)" in text
    assert "Science hash missing: spectra 12 · storage_objects 0" in text


# ---------------------------------------------------------------------------
# CLI wiring with a fake client
# ---------------------------------------------------------------------------

class _FakeRPC:
    def __init__(self, data):
        self._data = data

    def execute(self):
        if isinstance(self._data, Exception):
            raise self._data
        return types.SimpleNamespace(data=self._data)


class _FakeClient:
    def __init__(self, data):
        self._data = data
        self.calls = []

    def rpc(self, name, params=None):
        self.calls.append(name)
        return _FakeRPC(self._data)


def test_fetch_readiness_calls_rpc():
    client = _FakeClient(SAMPLE)
    assert fetch_readiness(client) == SAMPLE
    assert client.calls == ["dr_readiness"]


def _patch_client(monkeypatch, client):
    import campfire.deploy.config as cfg
    import campfire.deploy.supabase as sb
    monkeypatch.setattr(cfg, "load_config", lambda *a, **k: {"supabase": {}})
    monkeypatch.setattr(sb, "get_supabase_client", lambda config: client)


def test_dr_group_registered_and_help_renders():
    assert "dr" in main_cli.cli.commands
    res = CliRunner().invoke(main_cli.cli, ["dr", "status", "--help"])
    assert res.exit_code == 0, res.output
    assert "--json" in res.output


def test_dr_status_json_and_table(monkeypatch):
    _patch_client(monkeypatch, _FakeClient(SAMPLE))
    res = CliRunner().invoke(main_cli.cli, ["dr", "status", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["needs_review"] == 1339
    res = CliRunner().invoke(main_cli.cli, ["dr", "status"])
    assert res.exit_code == 0, res.output
    assert "Deploy leases: 2" in res.output


def test_dr_status_non_admin_is_one_line(monkeypatch):
    err = Exception("Access denied: Admin privileges required")
    _patch_client(monkeypatch, _FakeClient(err))
    res = CliRunner().invoke(main_cli.cli, ["dr", "status"])
    assert res.exit_code == 1
    assert "admin-only" in res.output


# ---------------------------------------------------------------------------
# DB-backed
# ---------------------------------------------------------------------------

@requires_local_db
def test_dr_readiness_sql_harness():
    result = run_sql(SQL_FILE.read_text(), timeout=120)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"dr_readiness harness FAILED (psql exit {result.returncode}).\nOutput:\n{combined}")
    assert PASS_MARKER in result.stdout, (
        f"dr_readiness harness did not reach its PASS marker.\nOutput:\n{combined}")
