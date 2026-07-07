"""P7: stuck-shutter provenance tags round-trip through the TOML writer/reader.

Guards the design §4.3 contract that `# hand` / `# web` / `# auto` tags survive a
`write_stuck_shutters_toml` -> `load_stuck_shutters_tagged` cycle (and an auto-detect
rewrite that carries them), so the `campfire deploy nirspec pull-stuck-shutters`
authority merge (hand > web > auto) still tells web from hand on a re-pull. Pure
TOML text — no jwst — guarded by importorskip so it skips where the package isn't
installed.
"""
import pytest

pytest.importorskip("campfire_pipeline")

from campfire_pipeline.nirspec.stuck_shutters import (  # noqa: E402
    write_stuck_shutters_toml,
    load_stuck_shutters_tagged,
    merge_stuck_shutters,
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
