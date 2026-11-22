"""The NIRSpec stuck-shutter / background-override lists on the config plane.

Covers `campfire.deploy.nirspec_flags`: the entry model, the three-way rule as pure
functions, and pull / push / diff end to end against the in-memory Supabase fake with a
tmp ``$CAMPFIRE_ROOT``. ``nirspec_review_apply`` (the guarded write) is imitated here by
``_apply_handler``; the real function is guarded by
``supabase/tests/check_nirspec_review_flags.sql``.

Tests that read or write the stuck TOML go through the pipeline's reader/writer and are
skipped where ``campfire_pipeline`` is not installed (the ``pipeline`` fixture).
"""
import copy

import pytest
import toml
from click.testing import CliRunner

from campfire.deploy import config_cli
from campfire.deploy import config_sync as cs
from campfire.deploy import nirspec_flags as nf
from campfire.deploy.config_cli import _resolve_scope, config_group
from tests.fakes import FakePostgrest

OBS = "ember_uds_p4"
ROOT_A = "jw06368001001_03101"
ROOT_B = "jw06368001001_05101"
TABLE = "nirspec_source_review"


@pytest.fixture
def pipeline():
    return pytest.importorskip("campfire_pipeline")


# ---------------------------------------------------------------------------
# Fixtures: a fake cloud that implements the guarded write, and a tmp tree
# ---------------------------------------------------------------------------

def _apply_handler(client):
    """nirspec_review_apply in Python: apply a change only while the entry still
    holds `expect`; report the rest under `refused`."""
    def handler(params):
        obs = params["p_observation"]
        rows = client.rows(TABLE)
        applied = unchanged = 0
        refused = []
        for c in params["p_changes"]:
            channel, root, sid = c["channel"], c["exposure_root"], c["source_id"]
            row = next((r for r in rows if r["observation"] == obs
                        and r["exposure_root"] == root and r["source_id"] == sid), None)
            col = "stuck_shutters" if channel == "stuck" else "bkg_overrides"
            cur_value = row.get(col) if row else None
            cur_source = (row.get("stuck_source") if row and channel == "stuck"
                          and cur_value is not None else None)
            exp, new = c.get("expect") or {}, c.get("set") or {}
            new_value = new.get("value")
            new_source = new.get("source") if channel == "stuck" and new_value is not None else None
            exp_source = exp.get("source") if channel == "stuck" else None
            if cur_value == new_value and cur_source == new_source:
                unchanged += 1
                continue
            if cur_value != exp.get("value") or cur_source != exp_source:
                refused.append({"channel": channel, "exposure_root": root, "source_id": sid,
                                "current": None if cur_value is None else {"value": cur_value}})
                continue
            if row is None:
                row = {"id": max([r["id"] for r in rows], default=0) + 1, "observation": obs,
                       "exposure_root": root, "source_id": sid, "stuck_shutters": None,
                       "stuck_source": None, "bkg_overrides": None}
                rows.append(row)
            row[col] = copy.deepcopy(new_value)
            if channel == "stuck":
                row["stuck_source"] = new_source
            applied += 1
        return {"applied": applied, "unchanged": unchanged, "refused": refused}
    return handler


def _cloud(rows=()):
    client = FakePostgrest(tables={TABLE: [dict(r) for r in rows]})
    client.rpc_handlers["nirspec_review_apply"] = _apply_handler(client)
    return client


def _row(i, root, sid, stuck=None, source=None, bkg=None, obs=OBS):
    return {"id": i, "observation": obs, "exposure_root": root, "source_id": sid,
            "stuck_shutters": stuck, "stuck_source": source, "bkg_overrides": bkg}


def _cloud_view(client, obs=OBS):
    """{channel: {(root, sid): value}} of what the cloud holds (raw values)."""
    out = {"stuck": {}, "bkg": {}}
    for r in client.rows(TABLE):
        if r["observation"] != obs:
            continue
        key = (r["exposure_root"], str(r["source_id"]))
        if r.get("stuck_shutters") is not None:
            out["stuck"][key] = (r["stuck_shutters"], r.get("stuck_source"))
        if r.get("bkg_overrides") is not None:
            out["bkg"][key] = r["bkg_overrides"]
    return out


def _write(root, name, text, obs=OBS):
    d = root / "reference" / "nirspec" / obs
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)
    return d / name


def _stuck_text(*lines):
    return "# vetted stuck closed shutter list\n\n" + "\n".join(lines) + "\n"


def _local(root, obs=OBS):
    return nf.read_local(obs, root)


def _S(shutters, source):
    return {"shutters": shutters, "source": source}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMPFIRE_ROOT", str(tmp_path))
    return tmp_path


def _pull(client, state, root, **kw):
    lines = []
    counts = nf.sync_pull(client, [OBS], state, root=root, out=lines.append, **kw)
    return counts, "\n".join(lines)


def _push(client, state, root, **kw):
    lines = []
    counts = nf.sync_push(client, [OBS], state, root=root, out=lines.append, **kw)
    return counts, "\n".join(lines)


# ---------------------------------------------------------------------------
# The entry model
# ---------------------------------------------------------------------------

def test_stuck_entry_is_sorted_unique_and_keeps_an_empty_list():
    assert nf.stuck_entry([3, 1, 3], "hand") == _S([1, 3], "hand")
    # `12345 = []` is an entry ("vetted, none stuck"), not the absence of one.
    assert nf.stuck_entry([], "web") == _S([], "web")
    with pytest.raises(nf.FlagsError):
        nf.stuck_entry([1], "robot")
    with pytest.raises(nf.FlagsError):
        nf.stuck_entry(["1"], "hand")


def test_bkg_entry_keeps_the_exclude_nod_marker_and_drops_an_empty_map():
    # `3 = []` means "exclude this nod" (CFP_BKG='excluded:override'): kept.
    assert nf.bkg_entry({"3": [], 10: [2, 1]}) == {"3": [], "10": [1, 2]}
    assert nf.bkg_entry({}) is None       # no override at all: no entry
    with pytest.raises(nf.FlagsError):
        nf.bkg_entry([1])


def test_serialize_bkg_overrides_shape_and_roundtrip():
    out = nf.serialize_bkg_overrides({(ROOT_A, "12345"): {"3": [1]}}, OBS, generated_at="t")
    assert f"[{ROOT_A}]" in out
    assert "12345 = {3 = [1]}" in out
    # toml.load reads it back into the exact shape Observation.bkg_overrides expects
    assert toml.loads(out)[ROOT_A]["12345"]["3"] == [1]


def test_serialize_bkg_overrides_empty_list_is_preserved():
    out = nf.serialize_bkg_overrides({("root_a", "999"): {"3": []}}, "obs", generated_at="t")
    assert "999 = {3 = []}" in out
    assert toml.loads(out)["root_a"]["999"]["3"] == []


def test_serialize_bkg_overrides_multi_nod_sorted():
    out = nf.serialize_bkg_overrides({("r", "1"): {"10": [2], "2": [1, 3]}}, "obs", generated_at="t")
    assert "1 = {2 = [1, 3], 10 = [2]}" in out      # nod keys sorted numerically
    assert toml.loads(out)["r"]["1"] == {"2": [1, 3], "10": [2]}


def test_read_bkg_file_matches_the_pipeline_reader_shape(tmp_path):
    p = tmp_path / "bkg.toml"
    p.write_text("[r]\n  7 = {3 = [1], 4 = []}\n  8 = {}\n")
    assert nf.read_bkg_file(p) == {("r", "7"): {"3": [1], "4": []}}   # 8 = {}: no entry
    assert nf.read_bkg_file(tmp_path / "missing.toml") is None        # absent != empty
    p.write_text("[r]\n  7 = {3 = [1]\n")
    with pytest.raises(nf.FlagsError):
        nf.read_bkg_file(p)


# ---------------------------------------------------------------------------
# The three-way rule (pure)
# ---------------------------------------------------------------------------

def test_decide_matrix():
    a, b, c = _S([1], "hand"), _S([2], "hand"), _S([3], "hand")
    assert nf.decide(a, a, None) == nf.IN_SYNC
    assert nf.decide(None, a, None) == nf.CLOUD_NEW          # cloud added
    assert nf.decide(a, b, a) == nf.CLOUD_CHANGED            # only the cloud moved
    assert nf.decide(a, None, a) == nf.CLOUD_REMOVED
    assert nf.decide(a, None, None) == nf.LOCAL_NEW          # this machine added
    assert nf.decide(b, a, a) == nf.LOCAL_CHANGED            # only this machine moved
    assert nf.decide(None, a, a) == nf.LOCAL_REMOVED
    assert nf.decide(b, c, a) == nf.CONFLICT                 # both moved
    assert nf.decide(a, b, None) == nf.CONFLICT              # never synced, both differ
    assert nf.decide(None, b, a) == nf.CONFLICT              # removed here, changed there


def _cloud_stuck(shutters, source):
    return {"value": nf.stuck_entry(shutters, source), "raw": {"value": shutters, "source": source}}


def _verdicts(items):
    return {it.key: it.verdict for it in items}


def test_plan_missing_file_ignores_the_base():
    # No file on disk says nothing about any entry: every cloud entry is new, and
    # the base (which would otherwise read as "you removed these") is ignored.
    k = ("r", "1")
    items = nf.plan_channel("stuck", None, {k: _cloud_stuck([1], "hand")}, {k: _S([1], "hand")})
    assert _verdicts(items) == {k: nf.CLOUD_NEW}
    # ...whereas an existing, empty file with the same base is a local removal.
    items = nf.plan_channel("stuck", {}, {k: _cloud_stuck([1], "hand")}, {k: _S([1], "hand")})
    assert _verdicts(items) == {k: nf.LOCAL_REMOVED}


def test_plan_human_entry_beats_a_detection():
    k = ("r", "1")
    # never synced: this machine detected [9], the cloud has a reviewer's [2]
    items = nf.plan_channel("stuck", {k: _S([9], "auto")}, {k: _cloud_stuck([2], "web")}, {})
    assert (items[0].verdict, items[0].note) == (nf.CLOUD_CHANGED, "human-over-auto")
    # the other way round: a hand entry here, a detection in the cloud
    items = nf.plan_channel("stuck", {k: _S([5], "hand")}, {k: _cloud_stuck([2], "auto")}, {})
    assert (items[0].verdict, items[0].note) == (nf.LOCAL_CHANGED, "human-over-auto")
    # two people: a real conflict
    items = nf.plan_channel("stuck", {k: _S([5], "hand")}, {k: _cloud_stuck([2], "web")}, {})
    assert items[0].verdict == nf.CONFLICT
    # two detections that disagree: a real conflict too
    items = nf.plan_channel("stuck", {k: _S([5], "auto")}, {k: _cloud_stuck([2], "auto")}, {})
    assert items[0].verdict == nf.CONFLICT


def test_plan_a_detection_never_replaces_a_persons_entry_whatever_the_base_says():
    # The regenerated-file case: this machine synced a person's [1, 2], lost the file,
    # and a re-reduction wrote a detection in its place. Three-way alone reads that as
    # "this machine edited the entry" and would send the detection over the person's.
    k = ("r", "1")
    for tag in ("hand", "web"):
        base = {k: _S([1, 2], tag)}
        items = nf.plan_channel("stuck", {k: _S([3], "auto")}, {k: _cloud_stuck([1, 2], tag)}, base)
        assert (items[0].verdict, items[0].note) == (nf.CLOUD_CHANGED, "human-over-auto")
    # a portal veto ([] web) is a person's entry too
    items = nf.plan_channel("stuck", {k: _S([2], "auto")}, {k: _cloud_stuck([], "web")},
                            {k: _S([], "web")})
    assert items[0].verdict == nf.CLOUD_CHANGED
    # and the mirror image: a detection in the cloud never stands over a person's entry here
    items = nf.plan_channel("stuck", {k: _S([1, 2], "hand")}, {k: _cloud_stuck([3], "auto")},
                            {k: _S([1, 2], "hand")})
    assert (items[0].verdict, items[0].note) == (nf.LOCAL_CHANGED, "human-over-auto")
    # vetting a detection (retagging it hand) is an ordinary local edit
    items = nf.plan_channel("stuck", {k: _S([3], "hand")}, {k: _cloud_stuck([1, 2], "web")},
                            {k: _S([1, 2], "web")})
    assert (items[0].verdict, items[0].note) == (nf.LOCAL_CHANGED, "")


def test_plan_unknown_cloud_source():
    k = ("r", "1")
    # same shutters here under `auto`: in sync, and this machine's tag is sent up
    items = nf.plan_channel("stuck", {k: _S([1, 2], "auto")}, {k: _cloud_stuck([1, 2], None)}, {})
    assert (items[0].verdict, items[0].tag_upgrade) == (nf.IN_SYNC, True)
    # same shutters here under `hand` (the default reading): in sync, nothing to send
    items = nf.plan_channel("stuck", {k: _S([1, 2], "hand")}, {k: _cloud_stuck([1, 2], None)}, {})
    assert (items[0].verdict, items[0].tag_upgrade) == (nf.IN_SYNC, False)
    # nothing here: pulled as `hand`
    items = nf.plan_channel("stuck", {}, {k: _cloud_stuck([1, 2], None)}, {})
    assert items[0].verdict == nf.CLOUD_NEW and items[0].cloud == _S([1, 2], "hand")
    # different shutters under a detection here, never synced: nobody has agreed the
    # untagged entry is a person's, so it does NOT win. A plain conflict: local kept.
    items = nf.plan_channel("stuck", {k: _S([7], "auto")}, {k: _cloud_stuck([1, 2], None)}, {})
    assert (items[0].verdict, items[0].note) == (nf.CONFLICT, "")
    # once this machine pulled it (base holds it as hand), it is a person's entry here
    items = nf.plan_channel("stuck", {k: _S([7], "auto")}, {k: _cloud_stuck([1, 2], None)},
                            {k: _S([1, 2], "hand")})
    assert (items[0].verdict, items[0].note) == (nf.CLOUD_CHANGED, "human-over-auto")


def test_plan_unknown_cloud_source_takes_the_base_tag():
    # The cloud holds [1, 2] untagged; this machine agreed on it as `auto` (base) but
    # has not pushed the tag. A refreshed detection [1, 2, 3] is an ordinary local
    # edit -- not a conflict with a "hand" entry the cloud never held.
    k = ("r", "1")
    items = nf.plan_channel("stuck", {k: _S([1, 2, 3], "auto")}, {k: _cloud_stuck([1, 2], None)},
                            {k: _S([1, 2], "auto")})
    assert (items[0].verdict, items[0].note) == (nf.LOCAL_CHANGED, "")
    assert nf._change(items[0])["expect"] == {"value": [1, 2], "source": None}


def test_plan_empty_cloud_bkg_map_is_no_entry_but_keeps_its_raw_value():
    k = ("r", "1")
    cloud = nf.cloud_entries([_row(1, "r", 1, bkg={})])
    assert cloud["bkg"][k] == {"value": None, "raw": {"value": {}}}
    assert nf.plan_channel("bkg", {}, cloud["bkg"], {}) == []
    # a local override over it is a plain addition whose guard expects the stored {}
    items = nf.plan_channel("bkg", {k: {"3": [1]}}, cloud["bkg"], {})
    assert items[0].verdict == nf.LOCAL_NEW
    assert nf._change(items[0])["expect"] == {"value": {}}


def test_cloud_entries_rejects_a_malformed_row():
    with pytest.raises(nf.FlagsError):
        nf.cloud_entries([_row(1, "r", 1, stuck="1,2")])
    with pytest.raises(nf.FlagsError):
        nf.cloud_entries([_row(1, "r", 1, stuck={})])     # {} is not the veto []
    with pytest.raises(nf.FlagsError):
        nf.cloud_entries([_row(1, "r", 1, stuck=[1], source="robot")])


# ---------------------------------------------------------------------------
# can_sync: zero rows from a non-admin session must never read as "empty cloud"
# ---------------------------------------------------------------------------

def test_can_sync_needs_positive_evidence():
    assert nf.can_sync({"supabase": {"_auth_mode": "local"}}, None)
    assert nf.can_sync({"supabase": {"_auth_mode": "service_role"}}, None)
    assert nf.can_sync({"supabase": {"_auth_mode": "login"}},
                       FakePostgrest(rpc_handlers={"is_admin": True}))
    assert not nf.can_sync({"supabase": {"_auth_mode": "login"}},
                           FakePostgrest(rpc_handlers={"is_admin": False}))
    assert not nf.can_sync({"supabase": {}}, FakePostgrest())      # no answer: no
    broken = FakePostgrest()
    broken.inject_error(RuntimeError("network"), rpc="is_admin")
    assert not nf.can_sync({"supabase": {"_auth_mode": "login"}}, broken)


# ---------------------------------------------------------------------------
# Round trip: TOML -> cloud rows -> TOML on a clean machine
# ---------------------------------------------------------------------------

def test_round_trip_preserves_tags_vetoes_and_inline_tables(pipeline, tmp_path):
    a, b = tmp_path / "machine_a", tmp_path / "machine_b"
    _write(a, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]",
        "    101 = [2]  # hand",
        "    102 = [1, 3]  # web",
        "    103 = [4]  # auto",
        "    104 = []  # hand",          # veto: vetted, none stuck
        "    105 = [5]",                 # untagged reads as hand
        f"[{ROOT_B}]",
        "    -7 = [1]  # auto",          # negative (virtual) source ids are real
    ))
    _write(a, nf.BKG_FILE,
           f"# overrides\n[{ROOT_A}]\n    101 = {{3=[1]}}\n    102 = {{3 = [], 4 = [2, 3]}}\n")
    client = _cloud()

    counts, _ = _push(client, {}, a)
    assert counts["pushed"] == 8 and counts["pending"] == 0
    cloud = _cloud_view(client)
    assert cloud["stuck"][(ROOT_A, "104")] == ([], "hand")        # the veto is stored
    assert cloud["stuck"][(ROOT_A, "103")] == ([4], "auto")       # with its tag
    assert cloud["stuck"][(ROOT_B, "-7")] == ([1], "auto")
    assert cloud["bkg"][(ROOT_A, "102")] == {"3": [], "4": [2, 3]}

    counts, _ = _pull(client, {}, b)                               # a clean machine
    assert counts["applied"] == 8
    assert _local(b) == _local(a)

    # Byte-compatible with the pipeline's writer: tags as trailing comments...
    stuck_text = (b / "reference" / "nirspec" / OBS / nf.STUCK_FILE).read_text()
    assert "    103 = [4]  # auto" in stuck_text
    assert "    104 = []  # hand" in stuck_text
    assert "    105 = [5]  # hand" in stuck_text
    assert "    -7 = [1]  # auto" in stuck_text
    # ...and the override file in the inline-table form the pipeline reads back.
    bkg_text = (b / "reference" / "nirspec" / OBS / nf.BKG_FILE).read_text()
    assert "    102 = {3 = [], 4 = [2, 3]}" in bkg_text
    assert toml.loads(bkg_text)[ROOT_A]["102"] == {"3": [], "4": [2, 3]}
    assert toml.loads(stuck_text)[ROOT_A]["104"] == []


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------

def test_pull_on_a_clean_machine_writes_the_files_and_records_the_base(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand", bkg={"3": [1]}),
                     _row(2, ROOT_A, 102, stuck=[], source="web")])
    state = {}
    counts, out = _pull(client, state, tree)
    assert counts == {"applied": 3, "kept": 0, "observations": 1, "errors": 0}
    assert _local(tree) == {
        "stuck": {(ROOT_A, "101"): _S([2], "hand"), (ROOT_A, "102"): _S([], "web")},
        "bkg": {(ROOT_A, "101"): {"3": [1]}},
    }
    assert state[nf.STATE_KEY][OBS]["stuck"][f"{ROOT_A}/101"] == _S([2], "hand")
    # a second pull has nothing to do and prints nothing
    counts, out = _pull(client, state, tree)
    assert counts["applied"] == 0 and out == ""


def test_first_pull_never_overwrites_what_is_already_on_disk(pipeline, tree):
    # No base yet (first sync on this machine). The local file holds a hand entry the
    # cloud lacks, one the cloud disagrees with, and lacks one the cloud has.
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2]  # hand", "    102 = [5]  # hand"))
    _write(tree, nf.BKG_FILE, f"[{ROOT_A}]\n    101 = {{3 = [1]}}\n")
    client = _cloud([_row(1, ROOT_A, 102, stuck=[6], source="web"),
                     _row(2, ROOT_A, 103, stuck=[1], source="web", bkg={"4": [2]}),
                     _row(3, ROOT_A, 101, bkg={"3": [2]})])
    state = {}
    counts, out = _pull(client, state, tree)
    local = _local(tree)
    assert local["stuck"] == {
        (ROOT_A, "101"): _S([2], "hand"),      # local-only: kept
        (ROOT_A, "102"): _S([5], "hand"),      # conflict: local kept
        (ROOT_A, "103"): _S([1], "web"),       # cloud-only: added
    }
    assert local["bkg"] == {(ROOT_A, "101"): {"3": [1]},      # conflict: local kept
                            (ROOT_A, "103"): {"4": [2]}}
    assert counts["applied"] == 2 and counts["kept"] == 3
    assert f"{ROOT_A} 102: local [5] (hand), cloud [6] (web)" in out
    assert "--theirs" in out
    # The conflicting entries have no base, so they stay conflicts until settled.
    assert f"{ROOT_A}/102" not in state[nf.STATE_KEY][OBS]["stuck"]


def test_pull_takes_a_cloud_change_to_an_untouched_entry(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="auto")])
    state = {}
    _pull(client, state, tree)
    client.rows(TABLE)[0].update(stuck_shutters=[2, 3], stuck_source="web")   # the portal
    counts, _ = _pull(client, state, tree)
    assert counts["applied"] == 1
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2, 3], "web")}
    prev = tree / "reference" / "nirspec" / OBS / (nf.STUCK_FILE + ".prev")
    assert "101 = [2]  # auto" in prev.read_text()       # the previous file is kept


def test_pull_keeps_a_local_edit_the_cloud_did_not_touch(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="auto")])
    state = {}
    _pull(client, state, tree)
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2, 4]  # hand"))
    counts, out = _pull(client, state, tree)
    assert counts == {"applied": 0, "kept": 1, "observations": 1, "errors": 0}
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2, 4], "hand")}
    assert "campfire config push" in out


def test_pull_conflict_needs_theirs(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand")])
    state = {}
    _pull(client, state, tree)
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [4]  # hand"))
    client.rows(TABLE)[0].update(stuck_shutters=[9], stuck_source="web")
    _pull(client, state, tree)
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([4], "hand")}     # kept
    counts, _ = _pull(client, state, tree, theirs=True)
    assert counts["applied"] == 1
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([9], "web")}


def test_pull_does_not_apply_a_cloud_removal_without_a_yes(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand", bkg={"3": [1]})])
    state = {}
    _pull(client, state, tree)
    client.rows(TABLE)[0].update(stuck_shutters=None, stuck_source=None, bkg_overrides=None)
    counts, out = _pull(client, state, tree)
    assert counts["applied"] == 0 and counts["kept"] == 2
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand")}
    assert "removed in the cloud" in out
    # --theirs settles conflicts; it does not delete anything
    counts, _ = _pull(client, state, tree, theirs=True)
    assert counts["applied"] == 0 and len(_local(tree)["stuck"]) == 1
    # still pending (the base stands), applied with --prune
    counts, _ = _pull(client, state, tree, prune=True)
    assert counts["applied"] == 2
    assert _local(tree) == {"stuck": {}, "bkg": {}}


def test_pull_restores_everything_when_the_file_is_gone(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand"),
                     _row(2, ROOT_A, 102, stuck=[3], source="auto")])
    state = {}
    _pull(client, state, tree)
    (tree / "reference" / "nirspec" / OBS / nf.STUCK_FILE).unlink()      # tree wiped
    counts, _ = _pull(client, state, tree)
    assert counts["applied"] == 2
    assert set(_local(tree)["stuck"]) == {(ROOT_A, "101"), (ROOT_A, "102")}


def test_a_regenerated_file_never_costs_a_persons_entry(pipeline, tree, monkeypatch):
    # The EMBER failure mode, end to end. The lists were synced; the reference dir was
    # lost while the sync state survived; a re-reduction recreated the files and its
    # detector wrote what it found. The file now lacks a hand entry (101), holds a
    # detection where a reviewer's veto was (102) and where a hand list was (103),
    # lacks an old detection (104), and the override file is blank.
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand", bkg={"3": [1]}),
                     _row(2, ROOT_A, 102, stuck=[], source="web"),
                     _row(3, ROOT_A, 103, stuck=[1, 2], source="hand"),
                     _row(4, ROOT_A, 104, stuck=[4], source="auto")])
    before = _cloud_view(client)
    config = {"supabase": {"url": "https://api.example.org"}}
    nf.pull_on_pull(client, OBS, config, out=lambda _l: None)
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    102 = [2]  # auto", "    103 = [3]  # auto", "    105 = [1]  # auto"))
    _write(tree, nf.BKG_FILE, "# nodded background overrides\n")

    # The deploy of that re-reduction sends the new detection only: no person's entry
    # is replaced, nothing is removed.
    res = nf.push_on_deploy(client, OBS, config)
    assert res["pushed"] == 1 and res["pending"] == 5
    after = _cloud_view(client)
    assert after["stuck"].pop((ROOT_A, "105")) == ([1], "auto")
    assert after == before
    assert any("differ from a person's entry in the cloud" in line for line in res["lines"])
    # even an explicit, forced, non-interactive push leaves them (removal needs --prune,
    # and a detection is never sent over a person's entry)
    state = cs.load_state()
    _push(client, nf.backend_state(state, config), tree, force=True)
    assert _cloud_view(client)["stuck"] == {**before["stuck"], (ROOT_A, "105"): ([1], "auto")}

    # A plain `campfire pull spec --obs` puts the person's entries back.
    lines = []
    nf.pull_on_pull(client, OBS, config, out=lines.append)
    local = _local(tree)
    assert local["stuck"] == {
        (ROOT_A, "101"): _S([2], "hand"),        # restored
        (ROOT_A, "102"): _S([], "web"),          # the veto, over the detection
        (ROOT_A, "103"): _S([1, 2], "hand"),     # the hand list, over the detection
        (ROOT_A, "105"): _S([1], "auto"),
    }                                            # 104 (a detection) is not restored
    assert local["bkg"] == {(ROOT_A, "101"): {"3": [1]}}
    assert any("restored" in line for line in lines)
    # the old detection the file lacks stays a pending decision: --theirs restores it
    state = cs.load_state()
    _pull(client, nf.backend_state(state, config), tree, theirs=True)
    assert _local(tree)["stuck"][(ROOT_A, "104")] == _S([4], "auto")


def test_pull_skips_an_unreadable_file_and_writes_nothing(pipeline, tree):
    path = _write(tree, nf.STUCK_FILE, "[r\n  1 = [2\n")
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand")])
    state = {}
    counts, out = _pull(client, state, tree)
    assert counts["errors"] == 1 and counts["applied"] == 0
    assert "skipped, nothing written" in out
    assert path.read_text() == "[r\n  1 = [2\n"
    assert state == {}


def test_pull_dry_run_writes_nothing(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand")])
    state = {}
    counts, out = _pull(client, state, tree, dry_run=True)
    assert counts["applied"] == 1 and "(dry run)" in out
    assert not (tree / "reference").exists() and state == {}


def test_bare_pull_visits_observations_whose_cloud_entries_are_gone(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand")])
    state = {}
    nf.sync_pull(client, None, state, root=tree, out=lambda _l: None)
    client.rows(TABLE)[0].update(stuck_shutters=None, stuck_source=None)
    lines = []
    nf.sync_pull(client, None, state, root=tree, out=lines.append)
    assert any("removed in the cloud" in line for line in lines)


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def test_push_sends_additions_and_edits_then_is_a_no_op(pipeline, tree):
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2]  # hand", "    102 = [3]  # auto"))
    client = _cloud([_row(1, ROOT_A, 102, stuck=[3], source="auto")])
    state = {}
    counts, _ = _push(client, state, tree)
    assert counts["pushed"] == 1
    assert _cloud_view(client)["stuck"] == {(ROOT_A, "101"): ([2], "hand"),
                                            (ROOT_A, "102"): ([3], "auto")}
    n_calls = len(client.rpcs)
    counts, out = _push(client, state, tree)
    assert counts["pushed"] == 0 and out == "" and len(client.rpcs) == n_calls
    # an edit is sent as a change guarded by the value this machine read
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2, 5]  # hand", "    102 = [3]  # auto"))
    _push(client, state, tree)
    change = client.rpcs[-1].params["p_changes"][0]
    assert change["expect"] == {"value": [2], "source": "hand"}
    assert change["set"] == {"value": [2, 5], "source": "hand"}


def test_push_refuses_an_entry_the_cloud_also_changed(pipeline, tree):
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2]  # hand"))
    client = _cloud()
    state = {}
    _push(client, state, tree)
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [4]  # hand"))
    client.rows(TABLE)[0].update(stuck_shutters=[9], stuck_source="web")
    counts, out = _push(client, state, tree)
    assert counts["pushed"] == 0 and counts["pending"] == 1
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([9], "web")
    assert "--force" in out
    counts, _ = _push(client, state, tree, force=True)
    assert counts["pushed"] == 1
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([4], "hand")


def test_push_never_removes_without_a_yes(pipeline, tree):
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2]  # hand", "    102 = [3]  # auto"))
    _write(tree, nf.BKG_FILE, f"[{ROOT_A}]\n    101 = {{3 = [1]}}\n")
    client = _cloud()
    state = {}
    _push(client, state, tree)
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    102 = [3]  # auto"))
    _write(tree, nf.BKG_FILE, "# no overrides\n")

    counts, out = _push(client, state, tree)                       # not a tty, no --force
    assert counts["pushed"] == 0 and counts["pending"] == 2
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([2], "hand")
    assert _cloud_view(client)["bkg"][(ROOT_A, "101")] == {"3": [1]}
    assert "not removed" in out

    counts, _ = _push(client, state, tree, force=True)             # --force is not a yes
    assert counts["pushed"] == 0 and _cloud_view(client)["bkg"] != {}

    counts, out = _push(client, state, tree, prune=True)           # the explicit yes
    assert counts["pushed"] == 2
    assert f"{ROOT_A} 101: [2] (hand)" in out                      # what was removed is listed
    assert _cloud_view(client) == {"stuck": {(ROOT_A, "102"): ([3], "auto")}, "bkg": {}}
    # the row itself stays (rows are never deleted), with both entries cleared
    assert any(r["source_id"] == 101 and r["stuck_shutters"] is None for r in client.rows(TABLE))


def test_push_with_no_file_sends_nothing(pipeline, tree):
    # No file is not an empty list: nothing is removed, and the cloud's entries are
    # reported as newer.
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand")])
    state = {nf.STATE_KEY: {OBS: {"stuck": {f"{ROOT_A}/101": _S([2], "hand")}}}}
    counts, out = _push(client, state, tree, force=True, prune=True)
    assert counts["pushed"] == 0 and client.rpcs == []
    assert "newer in the cloud" in out
    assert state[nf.STATE_KEY][OBS]["stuck"] == {f"{ROOT_A}/101": _S([2], "hand")}


def test_push_is_refused_when_the_cloud_changes_between_read_and_write(pipeline, tree):
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2]  # hand"))
    client = _cloud()
    state = {}
    _push(client, state, tree)
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [4]  # hand"))

    real = client.rpc_handlers["nirspec_review_apply"]

    def reviewer_clicks_first(params):
        client.rows(TABLE)[0].update(stuck_shutters=[9], stuck_source="web")
        return real(params)

    client.rpc_handlers["nirspec_review_apply"] = reviewer_clicks_first
    counts, out = _push(client, state, tree)
    assert counts["pushed"] == 0 and counts["pending"] == 1
    assert "changed in the cloud while pushing" in out
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([9], "web")    # not overwritten
    # the base did not move, so the entry is a conflict for the next pull
    client.rpc_handlers["nirspec_review_apply"] = real
    _, out = _pull(client, state, tree)
    assert "changed here AND in the cloud" in out


def test_push_sends_the_tag_of_an_entry_the_cloud_holds_untagged(pipeline, tree):
    # A backfilled deployment snapshot carries no tags. A machine whose file holds the
    # same shutters as `auto` keeps its tag and records it in the cloud.
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2]  # auto", "    102 = [3]  # hand"))
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2]), _row(2, ROOT_A, 102, stuck=[3])])
    state = {}
    counts, out = _push(client, state, tree)
    assert _cloud_view(client)["stuck"] == {(ROOT_A, "101"): ([2], "auto"),
                                            (ROOT_A, "102"): ([3], None)}
    assert "tag" in out
    counts, out = _push(client, state, tree)
    assert out == ""


def test_untagged_cloud_entries_pull_as_hand(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2])])
    state = {}
    _pull(client, state, tree)
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand")}
    _, out = _push(client, state, tree)       # hand is the default reading: nothing to send
    assert out == "" and client.rpcs == []


def test_detection_yields_to_a_reviewer_on_first_sync(pipeline, tree):
    # A fresh reduction detected [9] for a source a reviewer had flagged [2] on the web.
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [9]  # auto", "    102 = [1]  # auto"))
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="web")])
    state = {}
    counts, out = _push(client, state, tree)
    assert counts["pushed"] == 1                                        # 102 only
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([2], "web")
    _pull(client, state, tree)
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "web"),
                                     (ROOT_A, "102"): _S([1], "auto")}


def test_a_veto_survives_the_trip(pipeline, tree):
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = []"))
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="auto")])
    state = {}
    counts, _ = _push(client, state, tree)       # a person's [] over a detection
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([], "hand")


# ---------------------------------------------------------------------------
# The questions an interactive session is asked
# ---------------------------------------------------------------------------

def _answer(monkeypatch, answer):
    asked = []

    def confirm(question, default=False):
        asked.append(question)
        return answer
    import click
    monkeypatch.setattr(click, "confirm", confirm)
    return asked


def test_interactive_push_asks_before_removing(pipeline, tree, monkeypatch):
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2]  # hand"))
    client = _cloud()
    state = {}
    _push(client, state, tree)
    _write(tree, nf.STUCK_FILE, _stuck_text())

    asked = _answer(monkeypatch, False)
    counts, out = _push(client, state, tree, interactive=True)
    assert asked == ["    Remove them from the cloud?"]
    assert f"{ROOT_A} 101: [2] (hand)" in out               # shown before the question
    assert counts["pushed"] == 0
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([2], "hand")

    _answer(monkeypatch, True)
    counts, _ = _push(client, state, tree, interactive=True)
    assert counts["pushed"] == 1 and _cloud_view(client)["stuck"] == {}
    # a dry run never asks
    asked = _answer(monkeypatch, True)
    _push(client, state, tree, interactive=True, dry_run=True)
    assert asked == []


def test_interactive_pull_asks_before_removing_and_before_taking_a_conflict(pipeline, tree, monkeypatch):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand"),
                     _row(2, ROOT_A, 102, stuck=[3], source="hand")])
    state = {}
    _pull(client, state, tree)
    client.rows(TABLE)[0].update(stuck_shutters=None, stuck_source=None)        # removed there
    client.rows(TABLE)[1].update(stuck_shutters=[9], stuck_source="web")        # changed there
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2]  # hand", "    102 = [4]  # hand"))       # and here

    asked = _answer(monkeypatch, False)
    counts, _ = _pull(client, state, tree, interactive=True)
    assert asked == ["    Take the cloud's version of these?", "    Remove them here too?"]
    assert counts["applied"] == 0
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand"),
                                     (ROOT_A, "102"): _S([4], "hand")}

    _answer(monkeypatch, True)
    counts, _ = _pull(client, state, tree, interactive=True)
    assert counts["applied"] == 2
    assert _local(tree)["stuck"] == {(ROOT_A, "102"): _S([9], "web")}


def test_a_removal_inside_a_conflict_still_needs_its_own_yes(pipeline, tree):
    # Removed on one side, edited on the other is a conflict whose resolution deletes
    # an entry. --force / --theirs pick the side; they are not consent to delete.
    def synced():
        client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand"),
                         _row(2, ROOT_A, 102, stuck=[3], source="hand")])
        state = {}
        _pull(client, state, tree)
        return client, state

    # push: 101 removed here (the file still exists), edited in the cloud meanwhile
    client, state = synced()
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    102 = [3]  # hand"))
    client.rows(TABLE)[0].update(stuck_shutters=[2, 5], stuck_source="auto")
    counts, out = _push(client, state, tree, force=True)
    assert counts["pushed"] == 0 and client.rpcs == []
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([2, 5], "auto")
    assert "not removed" in out
    counts, _ = _push(client, state, tree, prune=True)          # a yes without --force
    assert counts["pushed"] == 0                                 # ... it is still a conflict
    counts, out = _push(client, state, tree, force=True, prune=True)
    assert counts["pushed"] == 1 and (ROOT_A, "101") not in _cloud_view(client)["stuck"]
    assert f"{ROOT_A} 101: [2, 5] (auto)" in out                 # what was removed is listed

    # pull: 101 edited here, removed in the cloud meanwhile
    client, state = synced()
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2, 7]  # hand", "    102 = [3]  # hand"))
    client.rows(TABLE)[0].update(stuck_shutters=None, stuck_source=None)
    for kw in ({"theirs": True}, {"prune": True}):
        counts, _ = _pull(client, state, tree, **kw)
        assert counts["applied"] == 0
        assert _local(tree)["stuck"][(ROOT_A, "101")] == _S([2, 7], "hand")
    counts, _ = _pull(client, state, tree, theirs=True, prune=True)
    assert counts["applied"] == 1 and (ROOT_A, "101") not in _local(tree)["stuck"]


def test_a_lost_persons_entry_is_restored_even_if_the_cloud_copy_changed(pipeline, tree):
    # The file was regenerated without a hand entry AND a reviewer edited that entry
    # in the cloud since the last sync. Three-way calls that a conflict; the file is
    # still missing a person's entry, and stage 2 reads the file.
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand", bkg={"3": [1]}),
                     _row(2, ROOT_A, 102, stuck=[4], source="auto")])
    config = {"supabase": {}}
    nf.pull_on_pull(client, OBS, config, out=lambda _l: None)
    _write(tree, nf.STUCK_FILE, _stuck_text())
    _write(tree, nf.BKG_FILE, "# nodded background overrides\n")
    client.rows(TABLE)[0].update(stuck_shutters=[2, 3], stuck_source="web", bkg_overrides={"3": [2]})
    client.rows(TABLE)[1].update(stuck_shutters=[4, 5], stuck_source="auto")

    lines = []
    nf.pull_on_pull(client, OBS, config, out=lines.append)     # the non-interactive pull
    local = _local(tree)
    assert local["stuck"] == {(ROOT_A, "101"): _S([2, 3], "web")}     # the cloud's current value
    assert local["bkg"] == {(ROOT_A, "101"): {"3": [2]}}
    assert (ROOT_A, "102") not in local["stuck"]                      # a detection waits for --theirs
    assert any("restored" in line for line in lines)
    # and the deploy never clears the one still missing
    res = nf.push_on_deploy(client, OBS, config)
    assert res["pushed"] == 0
    assert _cloud_view(client)["stuck"][(ROOT_A, "102")] == ([4, 5], "auto")


def test_apply_changes_does_not_trust_an_answer_that_does_not_add_up(pipeline, tree):
    # If the function's answer does not account for every change, nothing is recorded
    # as agreed (a base for a write that did not land would lose the edit on a later pull).
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2]  # hand"))
    for answer in (None, {}, {"applied": 0, "unchanged": 0, "refused": []}):
        client = _cloud()
        client.rpc_handlers["nirspec_review_apply"] = answer
        state = {}
        counts, out = _push(client, state, tree)
        assert counts["errors"] == 1 and "nothing sent" in out
        assert state == {}


def test_the_base_is_kept_per_backend():
    # What was agreed with a local test stack says nothing about production.
    state = {}
    local = nf.backend_state(state, {"supabase": {"url": "http://127.0.0.1:54321"}})
    prod = nf.backend_state(state, {"supabase": {"url": "https://api.example.org"}})
    nf._set_base(local, OBS, "stuck", {(ROOT_A, "101"): _S([2], "hand")})
    assert nf._base_of(prod, OBS, "stuck") == {}
    assert nf._base_of(local, OBS, "stuck") == {(ROOT_A, "101"): _S([2], "hand")}
    assert set(state[nf.STATE_KEY]) == {"127.0.0.1:54321", "api.example.org"}


# ---------------------------------------------------------------------------
# The non-interactive callers
# ---------------------------------------------------------------------------

def test_push_on_deploy_sends_edits_and_reports_the_rest(pipeline, tree):
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]", "    101 = [2]  # hand", "    102 = [3]  # auto"))
    client = _cloud()
    config = {"supabase": {"url": "https://api.example.org"}}
    assert nf.push_on_deploy(client, OBS, config)["pushed"] == 2
    # the deployer's own push is recorded as the base ...
    assert (cs.load_state()[nf.STATE_KEY]["api.example.org"][OBS]["stuck"][f"{ROOT_A}/101"]
            == _S([2], "hand"))
    # ... so a later local removal is seen as one, and a deploy does not send it
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    102 = [3]  # auto"))
    res = nf.push_on_deploy(client, OBS, config)
    assert res["pushed"] == 0 and res["pending"] == 1
    assert any("not removed" in line for line in res["lines"])
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([2], "hand")


def test_pull_on_pull_adds_but_never_removes_or_overrides(pipeline, tree):
    client = _cloud([_row(1, ROOT_A, 101, stuck=[2], source="hand")])
    config = {"supabase": {}}
    nf.pull_on_pull(client, OBS, config, out=lambda _l: None)
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand")}
    client.rows(TABLE)[0].update(stuck_shutters=None, stuck_source=None)
    counts = nf.pull_on_pull(client, OBS, config, out=lambda _l: None)
    assert counts["kept"] == 1
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand")}


def test_deployment_snapshot_shapes(pipeline, tmp_path):
    d = tmp_path / "ref"
    d.mkdir()
    assert nf.deployment_snapshot(d) == {
        "stuck_shutters": None, "stuck_shutter_sources": None, "bkg_overrides": None}
    (d / nf.STUCK_FILE).write_text(_stuck_text(
        f"[{ROOT_A}]", "    101 = [2]  # hand", "    102 = []  # web", "    103 = [3]  # auto"))
    (d / nf.BKG_FILE).write_text(f"[{ROOT_A}]\n    101 = {{3 = [1]}}\n")
    snap = nf.deployment_snapshot(d)
    # stuck_shutters keeps the shape every earlier deployment row has
    assert snap["stuck_shutters"] == {ROOT_A: {"101": [2], "102": [], "103": [3]}}
    assert snap["stuck_shutter_sources"] == {ROOT_A: {"101": "hand", "102": "web", "103": "auto"}}
    assert snap["bkg_overrides"] == {ROOT_A: {"101": {"3": [1]}}}


# ---------------------------------------------------------------------------
# Seeding the cloud from deployment snapshots (the backfill script's planner)
# ---------------------------------------------------------------------------

def test_snapshot_entries_reads_the_historical_shape_and_skips_what_it_cannot():
    snapshot = {ROOT_A: {"101": [2, 1], "102": [], "bad": [1], "103": "x"}, "junk": 3}
    entries, bad = nf.snapshot_entries(snapshot)
    # no tags were recorded before this change: provenance unknown (None), not "hand"
    assert entries == {(ROOT_A, "101"): _S([1, 2], None), (ROOT_A, "102"): _S([], None)}
    assert len(bad) == 3
    # a deploy that recorded tags
    entries, _ = nf.snapshot_entries({ROOT_A: {"101": [2]}}, {ROOT_A: {"101": "auto"}})
    assert entries == {(ROOT_A, "101"): _S([2], "auto")}
    assert nf.snapshot_entries(None) == ({}, [])
    entries, _ = nf.snapshot_entries({ROOT_A: {"101": {"3": [1]}, "102": {}}}, channel="bkg")
    assert entries == {(ROOT_A, "101"): {"3": [1]}}


def test_backfill_inserts_only_where_the_cloud_has_no_entry():
    snapshot, _ = nf.snapshot_entries({ROOT_A: {"101": [1], "102": [2], "103": [3], "104": [4]}})
    cloud = nf.cloud_entries([
        _row(1, ROOT_A, 101, stuck=[1], source="web"),     # same shutters: left alone
        _row(2, ROOT_A, 102, stuck=[9], source="web"),     # differs: the review table wins
        _row(3, ROOT_A, 103, bkg={"3": [1]}),              # row exists, no stuck entry
    ])
    changes, stats = nf.plan_snapshot_backfill("stuck", snapshot, cloud["stuck"])
    assert stats == {"insert": 2, "same": 1, "differ": 1}
    assert [(c["source_id"], c["expect"], c["set"]) for c in changes] == [
        (103, None, {"value": [3], "source": None}),
        (104, None, {"value": [4], "source": None}),
    ]
    # applied through the guarded write, it changes nothing that was there
    client = _cloud([_row(1, ROOT_A, 101, stuck=[1], source="web"),
                     _row(2, ROOT_A, 102, stuck=[9], source="web"),
                     _row(3, ROOT_A, 103, bkg={"3": [1]})])
    result = nf.apply_changes(client, OBS, changes)
    assert result["applied"] == 2 and result["refused"] == []
    assert _cloud_view(client)["stuck"] == {
        (ROOT_A, "101"): ([1], "web"), (ROOT_A, "102"): ([9], "web"),
        (ROOT_A, "103"): ([3], None), (ROOT_A, "104"): ([4], None)}
    assert _cloud_view(client)["bkg"] == {(ROOT_A, "103"): {"3": [1]}}


def test_first_pull_after_a_backfill_merges_with_restored_local_files(pipeline, tree):
    # The cloud was seeded from a snapshot (no tags). This machine restored some hand
    # entries locally first and re-detected others: nothing local may be overwritten.
    client = _cloud([_row(1, ROOT_A, 101, stuck=[1]), _row(2, ROOT_A, 102, stuck=[2]),
                     _row(3, ROOT_A, 103, stuck=[3])])
    _write(tree, nf.STUCK_FILE, _stuck_text(
        f"[{ROOT_A}]",
        "    101 = [1]  # hand",      # restored by hand, same as the snapshot
        "    102 = [2]  # auto",      # re-detected, same shutters: keeps its auto tag
        "    104 = [7]  # hand"))     # only here
    state = {}
    counts, out = _pull(client, state, tree)
    assert _local(tree)["stuck"] == {
        (ROOT_A, "101"): _S([1], "hand"), (ROOT_A, "102"): _S([2], "auto"),
        (ROOT_A, "103"): _S([3], "hand"),      # unknown provenance reads as hand
        (ROOT_A, "104"): _S([7], "hand")}
    assert counts["applied"] == 1 and "conflict" not in out
    _push(client, state, tree)
    assert _cloud_view(client)["stuck"] == {
        (ROOT_A, "101"): ([1], None), (ROOT_A, "102"): ([2], "auto"),   # the tag went up
        (ROOT_A, "103"): ([3], None), (ROOT_A, "104"): ([7], "hand")}


# ---------------------------------------------------------------------------
# `campfire config push / pull / diff` wiring
# ---------------------------------------------------------------------------

def test_resolve_scope_matrix():
    none = (False, False, False)
    # nothing named: every kind and every observation's lists
    assert _resolve_scope(*none, (), ()) == ({k: None for k in cs.KINDS}, None)
    # --obs: that observation's section and its lists
    assert _resolve_scope(*none, ("a",), ()) == ({"observations": ["a"]}, ["a"])
    assert _resolve_scope(*none, ("a",), (), kind_flags=True) == ({}, ["a"])
    assert _resolve_scope(False, True, False, ("a",), ()) == ({"observations": ["a"]}, False)
    # --flags alone: the lists of every observation, no TOML kind
    assert _resolve_scope(*none, (), (), kind_flags=True) == ({}, None)
    # a TOML kind alone leaves the lists out
    assert _resolve_scope(True, False, False, (), ()) == ({"programs": None}, False)
    assert _resolve_scope(*none, (), ("cosmos",)) == ({"fields": ["cosmos"]}, False)


def _wire(monkeypatch, tree, client, mode="local"):
    monkeypatch.setattr(config_cli, "_client",
                        lambda *_a, **_k: ({"supabase": {"_auth_mode": mode}}, client))
    return CliRunner()


def test_cli_push_pull_diff_carry_the_lists(pipeline, tree, monkeypatch):
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2]  # hand"))
    client = _cloud([_row(1, ROOT_A, 102, stuck=[3], source="web")])
    runner = _wire(monkeypatch, tree, client)

    res = runner.invoke(config_group, ["diff", "--flags"])
    assert res.exit_code == 0, res.output
    assert f"> local-only       {OBS} stuck {ROOT_A} 101: [2] (hand)" in res.output
    assert f"< cloud-only       {OBS} stuck {ROOT_A} 102: [3] (web)" in res.output

    res = runner.invoke(config_group, ["push", "--flags", "--obs", OBS])
    assert res.exit_code == 0, res.output
    assert "Pushed 0 section(s), 1 list entry." in res.output
    assert _cloud_view(client)["stuck"][(ROOT_A, "101")] == ([2], "hand")

    res = runner.invoke(config_group, ["pull", "--flags"])
    assert res.exit_code == 0, res.output
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand"),
                                     (ROOT_A, "102"): _S([3], "web")}
    # the base lives beside the TOML kinds' base, in the same state file
    assert cs.load_state()[nf.STATE_KEY]["default"][OBS]["stuck"][f"{ROOT_A}/102"] == _S([3], "web")

    res = runner.invoke(config_group, ["diff", "--flags"])
    assert "flags: in sync" in res.output


def test_cli_dry_run_leaves_cloud_files_and_state_alone(pipeline, tree, monkeypatch):
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2]  # hand"))
    client = _cloud([_row(1, ROOT_A, 102, stuck=[3], source="web")])
    runner = _wire(monkeypatch, tree, client)
    for verb in ("push", "pull"):
        res = runner.invoke(config_group, [verb, "--flags", "--dry-run"])
        assert res.exit_code == 0, res.output
    assert client.rpcs == []
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand")}
    assert not (tree / "meta" / "config_sync_state.json").exists()


def test_cli_non_admin_session_never_touches_the_lists(pipeline, tree, monkeypatch):
    # Under RLS a non-admin reads zero rows. With a recorded base, "zero rows" would
    # read as "everything was removed in the cloud" -- so the lists are not synced at
    # all unless the session is positively an admin.
    _write(tree, nf.STUCK_FILE, _stuck_text(f"[{ROOT_A}]", "    101 = [2]  # hand"))
    cs.save_state({"version": 1, "base": {k: {} for k in cs.KINDS},
                   nf.STATE_KEY: {"default": {OBS: {"stuck": {f"{ROOT_A}/101": _S([2], "hand")}}}}})
    client = _cloud()
    client.rpc_handlers["is_admin"] = False
    runner = _wire(monkeypatch, tree, client, mode="login")
    res = runner.invoke(config_group, ["pull", "--flags", "--theirs", "--prune"])
    assert res.exit_code == 0, res.output
    assert "admin-only" in res.output
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand")}
    # a bare pull says nothing about them at all
    res = runner.invoke(config_group, ["pull"])
    assert res.exit_code == 0, res.output
    assert "flags" not in res.output
    assert _local(tree)["stuck"] == {(ROOT_A, "101"): _S([2], "hand")}
