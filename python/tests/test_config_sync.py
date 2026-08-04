"""Tests for the config-sync core (issue #303): canonical hashing, the
section->row mappers, the push path with its divergence guard, and the local
sync-state file. Pure/local — no network, no real Supabase."""

import datetime
import json
import textwrap
import types

import pytest

from campfire.deploy import config_sync as cs


# --- fake Supabase client ---------------------------------------------------

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


# --- canonical hash ----------------------------------------------------------

def test_config_hash_is_order_independent_and_prefixed():
    h1 = cs.config_hash({"a": 1, "b": {"y": 2, "x": [1, 2]}})
    h2 = cs.config_hash({"b": {"x": [1, 2], "y": 2}, "a": 1})
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert h1 != cs.config_hash({"a": 1})


def test_config_hash_survives_jsonb_round_trip():
    # cloud rows come back through json — the hash must match the TOML parse.
    section = {"files": ["jw01727*"], "n": 3, "f": 1.5, "flag": True,
               "nested": {"list": [1, 2]}}
    round_tripped = json.loads(json.dumps(section))
    assert cs.config_hash(section) == cs.config_hash(round_tripped)


def test_find_unjsonable_reports_paths():
    section = {"ok": "2026-01-01",
               "epochs": {"e1": {"date_range": [datetime.date(2026, 1, 1)]}}}
    assert cs.find_unjsonable(section) == ["epochs.e1.date_range[0]"]
    assert cs.find_unjsonable({"a": 1, "b": [1, "x"]}) == []


# --- row mappers -------------------------------------------------------------

def test_program_config_row_normalizes_and_is_lossless():
    section = {"program_name": "CAPERS", "pi_name": "", "cycle": 3}
    row = cs.program_config_row("capers", section)
    assert row["slug"] == "capers"
    assert row["program_name"] == "CAPERS"
    assert row["pi_name"] is None            # '' never propagates (pre-#303 wart)
    assert row["description"] is None
    assert row["is_public"] is False
    assert row["cycle"] == 3
    assert row["config"] == section
    assert row["config_hash"] == cs.config_hash(section)
    assert row["config_updated_at"]


def test_program_config_row_defaults_name_to_slug():
    assert cs.program_config_row("x", {})["program_name"] == "x"


def test_observation_config_row_lifts_and_is_lossless():
    section = {"program": "capers", "field": "egs", "data_subdir": "capers_egs",
               "files": ["jw06368*"], "gratings": ["prism"],
               "config_groups": [["a", "b"]], "stage2": {"skip_nsclean": True}}
    row = cs.observation_config_row("capers-egs-p1", section)
    assert row["program_slug"] == "capers"
    assert row["field"] == "egs"
    assert row["jwst_program_id"] == 6368      # parsed from the glob
    assert row["file_globs"] == ["jw06368*"]
    assert row["gratings"] == ["prism"]
    assert row["data_subdir"] == "capers_egs"
    # THE point of #303: stage overrides + config_groups survive in config.
    assert row["config"]["stage2"] == {"skip_nsclean": True}
    assert row["config"]["config_groups"] == [["a", "b"]]
    assert row["config_hash"] == cs.config_hash(section)


def test_observation_config_row_explicit_program_id_wins():
    row = cs.observation_config_row(
        "o", {"program": "p", "field": "f", "program_id": 1810,
              "files": ["jw99999*"]})
    assert row["jwst_program_id"] == 1810


def test_observation_config_row_requires_program_field_pid():
    with pytest.raises(ValueError, match="missing program"):
        cs.observation_config_row("o", {"field": "f", "files": ["jw01727*"]})
    with pytest.raises(ValueError, match="program_id/files"):
        cs.observation_config_row("o", {"program": "p", "field": "f"})


# --- push_kind ---------------------------------------------------------------

def _write_toml(tmp_path, monkeypatch, kind, text):
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / f"{kind}.toml").write_text(textwrap.dedent(text))
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))


_PROGRAMS_TOML = """
    [capers]
    program_name = "CAPERS"
    cycle = 3
    [cosmos3d]
    program_name = "COSMOS-3D"
    is_public = true
"""


def test_push_kind_pushes_all_local_sections(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, "programs", _PROGRAMS_TOML)
    client = _FakeClient()
    sections = cs.load_sections("programs")
    n, pushed = cs.push_kind(client, "programs", sections)
    assert n == 2
    assert set(pushed) == {"capers", "cosmos3d"}
    tables = [t for (t, _c, _r) in client.store["upserts"]]
    assert tables == ["programs", "programs"]


def test_push_kind_skips_in_sync_sections(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, "programs", _PROGRAMS_TOML)
    sections = cs.load_sections("programs")
    same_hash = cs.config_hash(sections["capers"])
    client = _FakeClient(tables={"programs": [
        {"slug": "capers", "config_hash": same_hash}]})
    n, pushed = cs.push_kind(client, "programs", sections, ["capers"])
    assert n == 0                       # nothing written…
    assert pushed == {"capers": same_hash}   # …but the base is still recorded
    assert client.store["upserts"] == []


def test_push_kind_divergence_guard_refuses_then_force_wins(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, "programs", _PROGRAMS_TOML)
    sections = cs.load_sections("programs")
    client = _FakeClient(tables={"programs": [
        {"slug": "capers", "config_hash": "sha256:someone-elses-push"}]})
    base = {"capers": "sha256:what-i-synced-last"}   # cloud moved since
    n, pushed = cs.push_kind(client, "programs", sections, ["capers"], base=base)
    assert n == 0 and pushed == {} and client.store["upserts"] == []
    n, pushed = cs.push_kind(client, "programs", sections, ["capers"],
                             base=base, force=True)
    assert n == 1 and "capers" in pushed


def test_push_kind_skips_retired_rows(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, "programs", _PROGRAMS_TOML)
    sections = cs.load_sections("programs")
    client = _FakeClient(tables={"programs": [
        {"slug": "capers", "retired_at": "2026-08-01T00:00:00Z"}]})
    n, _ = cs.push_kind(client, "programs", sections, ["capers"])
    assert n == 0 and client.store["upserts"] == []


def test_push_kind_validates_observation_program_slug(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, "observations", """
        [good]
        program = "capers"
        field = "egs"
        files = ["jw06368*"]
        [bad]
        program = "CAPERS Name Not Slug"
        field = "egs"
        files = ["jw06368*"]
    """)
    client = _FakeClient()
    sections = cs.load_sections("observations")
    n, pushed = cs.push_kind(client, "observations", sections,
                             programs_config={"capers": {}})
    assert n == 1 and list(pushed) == ["good"]


def test_push_kind_rejects_bare_toml_datetimes(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, "observations", """
        [o]
        program = "capers"
        field = "egs"
        files = ["jw06368*"]
        taken = 2026-01-01
    """)
    client = _FakeClient()
    n, _ = cs.push_kind(client, "observations", cs.load_sections("observations"))
    assert n == 0 and client.store["upserts"] == []


def test_push_kind_dry_run_writes_nothing(tmp_path, monkeypatch):
    _write_toml(tmp_path, monkeypatch, "programs", _PROGRAMS_TOML)
    n, pushed = cs.push_kind(_FakeClient(), "programs",
                             cs.load_sections("programs"), dry_run=True)
    assert n == 0 and pushed == {}


# --- state file --------------------------------------------------------------

def test_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    state = cs.load_state()
    assert state["base"] == {k: {} for k in cs.KINDS}
    cs.record_base(state, "observations", {"o1": "sha256:aa"})
    cs.save_state(state)
    assert (tmp_path / "meta" / "config_sync_state.json").exists()
    again = cs.load_state()
    assert again["base"]["observations"] == {"o1": "sha256:aa"}


def test_state_survives_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "config_sync_state.json").write_text("{not json")
    assert cs.load_state()["base"] == {k: {} for k in cs.KINDS}
