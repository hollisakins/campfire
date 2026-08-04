"""Tests for the `campfire config` CLI group (issue #303): the three-way
pull/merge verdicts, and push/pull/diff wired end-to-end against a fake
Supabase client with a tmp $CAMPFIRE_ROOT. Auth is stubbed out — `_client`
is monkeypatched, so no credentials and no network."""

import textwrap
import tomllib
import types

from click.testing import CliRunner

from campfire.deploy import config_cli
from campfire.deploy import config_sync as cs
from campfire.deploy.config_cli import _pull_decision, config_group


# --- fake Supabase client (same shape as test_config_sync) -------------------

class _FakeQuery:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._filter = None
        self._update = None

    def select(self, _cols):
        return self

    def in_(self, col, values):
        self._filter = (col, list(values))
        return self

    def eq(self, col, value):
        self._filter = (col, [value])
        return self

    def upsert(self, row, on_conflict=None):
        self.store["upserts"].append((self.name, on_conflict, row))
        return self

    def update(self, patch):
        self._update = patch
        return self

    def execute(self):
        rows = self.store.get("tables", {}).get(self.name, [])
        if self._filter is not None:
            col, values = self._filter
            rows = [r for r in rows if r.get(col) in values]
        if self._update is not None:
            for r in rows:
                r.update(self._update)
            self.store["updates"].append((self.name, self._filter, self._update))
        return types.SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, tables=None):
        self.store = {"upserts": [], "updates": [], "rpcs": [],
                      "tables": tables or {}}

    def table(self, name):
        return _FakeQuery(self.store, name)

    def rpc(self, name, params=None):
        self.store["rpcs"].append((name, params))
        return types.SimpleNamespace(execute=lambda: types.SimpleNamespace(data=None))


def _wire(monkeypatch, tmp_path, client):
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    monkeypatch.setattr(config_cli, "_client",
                        lambda *_a, **_k: ({"supabase": {}}, client))
    return CliRunner()


def _write_toml(tmp_path, kind, text):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / f"{kind}.toml").write_text(textwrap.dedent(text))


def _obs_row(name, section, **extra):
    return {"name": name, "config": section,
            "config_hash": cs.config_hash(section),
            "config_updated_at": "2026-08-01T00:00:00Z",
            "retired_at": None, **extra}


# --- three-way verdicts ------------------------------------------------------

def test_pull_decision_matrix():
    l, c, b = "sha256:l", "sha256:c", "sha256:b"
    assert _pull_decision(c, c, b) == "in-sync"          # hashes equal
    assert _pull_decision(None, c, None) == "new"        # nothing local
    assert _pull_decision(b, c, b) == "fast-forward"     # only cloud moved
    assert _pull_decision(l, c, c) == "local-ahead"      # only local moved
    assert _pull_decision(l, c, b) == "conflict"         # both moved
    assert _pull_decision(l, c, None) == "conflict"      # never synced


# --- pull --------------------------------------------------------------------

_CLOUD_OBS = {"program": "capers", "field": "egs", "files": ["jw06368*"],
              "stage2": {"skip_nsclean": True}}


def test_pull_writes_new_section_and_records_base(tmp_path, monkeypatch):
    client = _FakeClient(tables={"observations": [_obs_row("capers-egs-p1", _CLOUD_OBS)]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads((tmp_path / "config" / "observations.toml").read_text())
    assert parsed["capers-egs-p1"] == _CLOUD_OBS
    state = cs.load_state()
    assert state["base"]["observations"]["capers-egs-p1"] == cs.config_hash(_CLOUD_OBS)


def test_pull_fast_forwards_cloud_change(tmp_path, monkeypatch):
    old = {"program": "capers", "field": "egs", "files": ["jw06368*"]}
    _write_toml(tmp_path, "observations", """
        # precious comment
        [capers-egs-p1]
        program = "capers"
        field = "egs"
        files = ["jw06368*"]
    """)
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    state = cs.load_state()
    cs.record_base(state, "observations", {"capers-egs-p1": cs.config_hash(old)})
    cs.save_state(state)

    client = _FakeClient(tables={"observations": [_obs_row("capers-egs-p1", _CLOUD_OBS)]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert result.exit_code == 0, result.output
    text = (tmp_path / "config" / "observations.toml").read_text()
    assert "# precious comment" in text
    assert tomllib.loads(text)["capers-egs-p1"] == _CLOUD_OBS


def test_pull_keeps_local_ahead_edits(tmp_path, monkeypatch):
    _write_toml(tmp_path, "observations", """
        [capers-egs-p1]
        program = "capers"
        field = "egs"
        files = ["jw06368*"]
        gratings = ["prism"]
    """)
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    # base == cloud: the local gratings edit is ahead, pull must not clobber it.
    state = cs.load_state()
    cs.record_base(state, "observations", {"capers-egs-p1": cs.config_hash(_CLOUD_OBS)})
    cs.save_state(state)

    client = _FakeClient(tables={"observations": [_obs_row("capers-egs-p1", _CLOUD_OBS)]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert result.exit_code == 0, result.output
    assert "local edits are ahead" in result.output
    parsed = tomllib.loads((tmp_path / "config" / "observations.toml").read_text())
    assert parsed["capers-egs-p1"]["gratings"] == ["prism"]


def test_pull_conflict_needs_theirs(tmp_path, monkeypatch):
    _write_toml(tmp_path, "observations", """
        [capers-egs-p1]
        program = "capers"
        field = "egs"
        files = ["jw99999*"]
    """)
    client = _FakeClient(tables={"observations": [_obs_row("capers-egs-p1", _CLOUD_OBS)]})
    runner = _wire(monkeypatch, tmp_path, client)

    # No base, both differ, non-interactive: conflict -> keep local.
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads((tmp_path / "config" / "observations.toml").read_text())
    assert parsed["capers-egs-p1"]["files"] == ["jw99999*"]

    # --theirs takes the cloud version.
    result = runner.invoke(config_group, ["pull", "--observations", "--theirs"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads((tmp_path / "config" / "observations.toml").read_text())
    assert parsed["capers-egs-p1"] == _CLOUD_OBS


def test_pull_skips_retired_and_unmirrored_rows(tmp_path, monkeypatch):
    client = _FakeClient(tables={"observations": [
        _obs_row("gone", _CLOUD_OBS, retired_at="2026-08-01T00:00:00Z"),
        {"name": "old-client", "config": None, "config_hash": None,
         "config_updated_at": None, "retired_at": None},
    ]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert result.exit_code == 0, result.output
    assert "retired in cloud" in result.output
    assert "no mirrored config" in result.output
    assert not (tmp_path / "config" / "observations.toml").exists()


def test_pull_dry_run_writes_nothing(tmp_path, monkeypatch):
    client = _FakeClient(tables={"observations": [_obs_row("o1", _CLOUD_OBS)]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["pull", "--observations", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would add o1" in result.output
    assert not (tmp_path / "config" / "observations.toml").exists()


# --- push --------------------------------------------------------------------

def test_push_local_flag_pushes_and_records_state(tmp_path, monkeypatch):
    _write_toml(tmp_path, "programs", """
        [capers]
        program_name = "CAPERS"
    """)
    client = _FakeClient()
    runner = _wire(monkeypatch, tmp_path, client)
    # --local skips the admin gate (parity with the deploy group).
    result = runner.invoke(config_group, ["push", "--programs", "--local"])
    assert result.exit_code == 0, result.output
    (table, on_conflict, row), = client.store["upserts"]
    assert table == "programs" and on_conflict == "slug"
    assert row["config"] == {"program_name": "CAPERS"}
    assert cs.load_state()["base"]["programs"]["capers"] == row["config_hash"]
    # programs push refreshes the overview matview + writes the audit event.
    rpc_names = [n for (n, _p) in client.store["rpcs"]]
    assert "refresh_programs_overview" in rpc_names
    assert "log_deploy_event" in rpc_names


def test_push_dry_run_no_writes_no_state(tmp_path, monkeypatch):
    _write_toml(tmp_path, "programs", """
        [capers]
        program_name = "CAPERS"
    """)
    client = _FakeClient()
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["push", "--programs", "--local", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert client.store["upserts"] == []
    assert not (tmp_path / "meta" / "config_sync_state.json").exists()


def test_push_fields_default_scope_empty_intersection_pushes_nothing(tmp_path, monkeypatch):
    """Local fields.toml has sections but nothing is deployed: the default
    deployed-data scope must push zero fields, not all of them."""
    _write_toml(tmp_path, "fields", """
        [cosmos]
        filters = ["f444w"]
        files = ["jw01727*"]
        tangent_point = [150.1, 2.2]
    """)
    client = _FakeClient()   # empty deployments table
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["push", "--fields", "--local"])
    assert result.exit_code == 0, result.output
    assert "no deployed fields overlap" in result.output
    assert client.store["upserts"] == []
    # Explicit --field overrides the deployed-data scope.
    result = runner.invoke(config_group, ["push", "--field", "cosmos", "--local"])
    assert result.exit_code == 0, result.output
    assert [t for (t, _c, _r) in client.store["upserts"]] == ["fields"]


# --- diff --------------------------------------------------------------------

def test_diff_reports_all_states(tmp_path, monkeypatch):
    _write_toml(tmp_path, "observations", """
        [local-only-obs]
        program = "capers"
        field = "egs"
        files = ["jw06368*"]

        [diverged-obs]
        program = "capers"
        field = "egs"
        files = ["jw11111*"]
    """)
    client = _FakeClient(tables={"observations": [
        _obs_row("cloud-only-obs", _CLOUD_OBS),
        _obs_row("diverged-obs", _CLOUD_OBS),
    ]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["diff", "--observations"])
    assert result.exit_code == 0, result.output
    assert "> local-only-obs: local-only" in result.output
    assert "< cloud-only" in result.output
    assert "! diverged" in result.output


# --- bare-date local sections must not crash pull/diff -----------------------

_BARE_DATE_TOML = """
    [capers-egs-p1]
    program = "capers"
    field = "egs"
    files = ["jw06368*"]
    taken = 2026-01-01
"""


def test_pull_skips_unjsonable_local_section(tmp_path, monkeypatch):
    """A bare TOML date in a local section can't be hashed, so pull can't tell
    local-ahead from stale — it must keep the local section, not crash and
    not overwrite."""
    _write_toml(tmp_path, "observations", _BARE_DATE_TOML)
    client = _FakeClient(tables={"observations": [_obs_row("capers-egs-p1", _CLOUD_OBS)]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert result.exit_code == 0, result.output
    assert "bare TOML datetime" in result.output
    parsed = tomllib.loads((tmp_path / "config" / "observations.toml").read_text())
    assert "taken" in parsed["capers-egs-p1"]   # local kept verbatim


def test_diff_reports_unjsonable_local_section(tmp_path, monkeypatch):
    _write_toml(tmp_path, "observations", _BARE_DATE_TOML)
    client = _FakeClient(tables={"observations": [_obs_row("capers-egs-p1", _CLOUD_OBS)]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["diff", "--observations"])
    assert result.exit_code == 0, result.output
    assert "bare TOML datetime" in result.output
    assert "taken" in result.output   # the offending key path is named


# --- retire (issue #454) -----------------------------------------------------

def test_retire_sets_retired_at_and_audits(tmp_path, monkeypatch):
    client = _FakeClient(tables={"observations": [
        {"name": "old-obs", "retired_at": None},
        {"name": "keep-obs", "retired_at": None},
    ]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group,
                           ["retire", "observations", "old-obs", "--local"])
    assert result.exit_code == 0, result.output
    assert "old-obs retired" in result.output
    rows = {r["name"]: r for r in client.store["tables"]["observations"]}
    assert rows["old-obs"]["retired_at"] is not None
    assert rows["keep-obs"]["retired_at"] is None
    # Audited via the deploy_events RPC.
    rpc_names = [n for (n, _p) in client.store["rpcs"]]
    assert "log_deploy_event" in rpc_names
    (_n, params), = [(n, p) for (n, p) in client.store["rpcs"]
                     if n == "log_deploy_event"]
    assert params["p_metadata"]["scope"] == "config_retire"
    assert params["p_metadata"]["names"] == ["old-obs"]


def test_retire_undo_reactivates(tmp_path, monkeypatch):
    client = _FakeClient(tables={"fields": [
        {"name": "oldfield", "retired_at": "2026-08-01T00:00:00Z"}]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group,
                           ["retire", "fields", "oldfield", "--undo", "--local"])
    assert result.exit_code == 0, result.output
    assert "re-activated" in result.output
    assert client.store["tables"]["fields"][0]["retired_at"] is None


def test_retire_reports_missing_rows(tmp_path, monkeypatch):
    client = _FakeClient()
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group,
                           ["retire", "programs", "ghost", "--local"])
    assert result.exit_code == 0, result.output
    assert "not found in cloud programs" in result.output
    assert client.store["rpcs"] == []   # nothing retired -> no audit row


def test_retired_section_round_trip_with_pull(tmp_path, monkeypatch):
    """retire -> pull skips it; --undo -> pull applies it again."""
    row = _obs_row("capers-egs-p1", _CLOUD_OBS)
    client = _FakeClient(tables={"observations": [row]})
    runner = _wire(monkeypatch, tmp_path, client)
    runner.invoke(config_group,
                  ["retire", "observations", "capers-egs-p1", "--local"])
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert "retired in cloud" in result.output
    assert not (tmp_path / "config" / "observations.toml").exists()
    runner.invoke(config_group,
                  ["retire", "observations", "capers-egs-p1", "--undo", "--local"])
    result = runner.invoke(config_group, ["pull", "--observations"])
    assert result.exit_code == 0, result.output
    parsed = tomllib.loads((tmp_path / "config" / "observations.toml").read_text())
    assert parsed["capers-egs-p1"] == _CLOUD_OBS


# --- fields.programs backfill (issue #454) -----------------------------------

def test_push_fields_resolves_program_slugs(tmp_path, monkeypatch):
    _write_toml(tmp_path, "fields", """
        [cosmos]
        filters = ["f444w"]
        files = ["jw01727*", "jw05893*", "jw09999*"]
        tangent_point = [150.1, 2.2]
    """)
    client = _FakeClient(tables={"observations": [
        {"name": "o1", "jwst_program_id": 1727, "program_slug": "cweb"},
        {"name": "o2", "jwst_program_id": 5893, "program_slug": "cosmos3d"},
        {"name": "o3", "jwst_program_id": 5893, "program_slug": "cosmos3d"},
        # 9999 has no observation row -> stays unresolved
    ]})
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["push", "--field", "cosmos", "--local"])
    assert result.exit_code == 0, result.output
    (_t, _c, row), = [u for u in client.store["upserts"] if u[0] == "fields"]
    assert row["programs"] == ["cosmos3d", "cweb"]
    assert row["jwst_program_ids"] == [1727, 5893, 9999]


def test_push_fields_unresolved_omits_programs_key(tmp_path, monkeypatch):
    """No matching observation rows: the programs key must be absent so the
    upsert keeps whatever the cloud column already holds."""
    _write_toml(tmp_path, "fields", """
        [cosmos]
        filters = ["f444w"]
        files = ["jw09999*"]
        tangent_point = [150.1, 2.2]
    """)
    client = _FakeClient()
    runner = _wire(monkeypatch, tmp_path, client)
    result = runner.invoke(config_group, ["push", "--field", "cosmos", "--local"])
    assert result.exit_code == 0, result.output
    (_t, _c, row), = [u for u in client.store["upserts"] if u[0] == "fields"]
    assert "programs" not in row
