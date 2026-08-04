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
        self._names = None

    def select(self, _cols):
        return self

    def in_(self, _col, names):
        self._names = list(names)
        return self

    def upsert(self, row, on_conflict=None):
        self.store["upserts"].append((self.name, on_conflict, row))
        return self

    def execute(self):
        rows = self.store.get("tables", {}).get(self.name, [])
        if self._names is not None:
            pk = "slug" if self.name == "programs" else "name"
            rows = [r for r in rows if r.get(pk) in self._names]
        return types.SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, tables=None):
        self.store = {"upserts": [], "rpcs": [], "tables": tables or {}}

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
