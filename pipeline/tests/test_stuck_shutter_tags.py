"""Stuck-shutter provenance tags round-trip through the TOML writer/reader.

Guards the contract that `# hand` / `# web` / `# auto` tags survive a
`write_stuck_shutters_toml` -> `load_stuck_shutters_tagged` cycle (and an auto-detect
rewrite that carries them): the tags travel with the list on `campfire config push /
pull`, and `detect-stuck --overwrite` (`refresh_auto_entries`) replaces only what the
detector wrote. Pure TOML text — no jwst — guarded by importorskip so it skips where
the package isn't installed.
"""
import pytest

pytest.importorskip("campfire_pipeline")

from campfire_pipeline.nirspec.stuck_shutters import (  # noqa: E402
    write_stuck_shutters_toml,
    load_stuck_shutters_tagged,
    merge_stuck_shutters,
    read_stuck_shutters,
    refresh_auto_entries,
)


def test_load_missing_file_returns_empty(tmp_path):
    assert load_stuck_shutters_tagged(str(tmp_path / "nope.toml")) == {}


def test_write_and_load_tags_roundtrip(tmp_path):
    path = str(tmp_path / "stuck_closed_shutters.toml")
    data = {"jw06368001001_03101": {"1": [5], "2": [3], "3": [1, 2]}}
    provenance = {
        ("jw06368001001_03101", "1"): "hand",
        ("jw06368001001_03101", "2"): "web",
        ("jw06368001001_03101", "3"): "auto",
    }
    write_stuck_shutters_toml(data, path, "obs", provenance=provenance)

    tagged = load_stuck_shutters_tagged(path)
    assert tagged[("jw06368001001_03101", "1")] == ([5], "hand")
    assert tagged[("jw06368001001_03101", "2")] == ([3], "web")
    assert tagged[("jw06368001001_03101", "3")] == ([1, 2], "auto")


def test_legacy_untagged_and_auto_detected_classification(tmp_path):
    # The pre-P7 writer emitted `# auto-detected` on detected entries and left manual
    # entries untagged. The loader must map untagged -> hand and `auto-detected` -> auto.
    path = tmp_path / "stuck_closed_shutters.toml"
    path.write_text(
        "# header\n\n[root_a]\n    100 = [1]\n    200 = [2]  # auto-detected\n"
    )
    tagged = load_stuck_shutters_tagged(str(path))
    assert tagged[("root_a", "100")] == ([1], "hand")
    assert tagged[("root_a", "200")] == ([2], "auto")


def test_auto_detect_rewrite_preserves_prior_tags(tmp_path):
    path = str(tmp_path / "stuck_closed_shutters.toml")
    write_stuck_shutters_toml(
        {"r": {"1": [5], "2": [3]}}, path, "obs",
        provenance={("r", "1"): "hand", ("r", "2"): "web"},
    )
    tagged = load_stuck_shutters_tagged(path)

    # Simulate an auto-detect pass adding a new entry; prior tags must survive.
    existing_plain = {"r": {sid: sh for (root, sid), (sh, _t) in tagged.items()}}
    merged, updated = merge_stuck_shutters(existing_plain, {"r": {4: [2]}})
    prov = {k: t for k, (_sh, t) in tagged.items()}
    for (root, sid) in updated:
        prov[(root, str(sid))] = "auto"
    write_stuck_shutters_toml(merged, path, "obs", provenance=prov)

    tagged2 = load_stuck_shutters_tagged(path)
    assert tagged2[("r", "1")][1] == "hand"   # not collapsed
    assert tagged2[("r", "2")][1] == "web"    # not collapsed
    assert tagged2[("r", "4")] == ([2], "auto")


# ---------------------------------------------------------------------------
# read_stuck_shutters: toml.load values + line-parser tags
# ---------------------------------------------------------------------------

def test_read_stuck_shutters_missing_file(tmp_path):
    assert read_stuck_shutters(str(tmp_path / "nope.toml")) == {}


def test_read_stuck_shutters_sees_what_toml_load_sees(tmp_path):
    # A multi-line list is valid TOML the line parser cannot see. The pipeline uses
    # it (toml.load), so the reader must return it too -- as `hand` -- or a rewrite
    # that starts from this reader would drop an entry the reduction relies on.
    path = tmp_path / "stuck_closed_shutters.toml"
    path.write_text(
        "[r]\n"
        "    1 = [5]  # auto\n"
        "    2 = [\n        3,\n        4,\n    ]\n"
        "    3 = []\n"
    )
    assert load_stuck_shutters_tagged(str(path)).get(("r", "2")) is None
    entries = read_stuck_shutters(str(path))
    assert entries == {
        ("r", "1"): ([5], "auto"),
        ("r", "2"): ([3, 4], "hand"),
        ("r", "3"): ([], "hand"),      # a veto is an entry
    }


def test_empty_list_round_trips_through_the_writer(tmp_path):
    path = str(tmp_path / "stuck_closed_shutters.toml")
    write_stuck_shutters_toml({"r": {"7": []}}, path, "obs", provenance={("r", "7"): "web"})
    assert read_stuck_shutters(path) == {("r", "7"): ([], "web")}


# ---------------------------------------------------------------------------
# refresh_auto_entries: `detect-stuck --overwrite`
# ---------------------------------------------------------------------------

def _apply(existing, detected, analysed):
    data, prov, stats = refresh_auto_entries(existing, detected, analysed)
    return {(r, s): (sh, prov[(r, s)]) for r, t in data.items() for s, sh in t.items()}, stats


def test_refresh_keeps_hand_and_web_entries():
    existing = {("r", "1"): ([5], "hand"), ("r", "2"): ([3], "web"), ("r", "3"): ([1], "auto")}
    out, stats = _apply(existing, {"r": {3: [2], 4: [1]}}, {("r", 1), ("r", 2), ("r", 3), ("r", 4)})
    assert out == {
        ("r", "1"): ([5], "hand"),
        ("r", "2"): ([3], "web"),
        ("r", "3"): ([2], "auto"),     # refreshed
        ("r", "4"): ([1], "auto"),     # new
    }
    assert stats["kept"] == 2 and stats["auto"] == 2 and stats["auto_dropped"] == 0


def test_refresh_drops_an_auto_entry_no_longer_detected():
    existing = {("r", "1"): ([5], "auto"), ("r", "2"): ([3], "hand")}
    out, stats = _apply(existing, {}, {("r", 1), ("r", 2)})
    assert out == {("r", "2"): ([3], "hand")}
    assert stats["auto_dropped"] == 1


def test_refresh_leaves_sources_it_did_not_analyse():
    # A run scoped with --source-ids says nothing about the other sources.
    existing = {("r", "1"): ([5], "auto"), ("r", "9"): ([2], "auto")}
    out, stats = _apply(existing, {"r": {1: [4]}}, {("r", 1)})
    assert out == {("r", "1"): ([4], "auto"), ("r", "9"): ([2], "auto")}
    assert stats["auto_kept"] == 1


def test_refresh_veto_blocks_a_detection():
    # `12345 = []  # hand` means "vetted, none stuck": the detector must not re-add it.
    existing = {("r", "1"): ([], "hand")}
    out, stats = _apply(existing, {"r": {1: [2]}}, {("r", 1)})
    assert out == {("r", "1"): ([], "hand")}
    assert stats["blocked"] == 1 and stats["auto"] == 0
