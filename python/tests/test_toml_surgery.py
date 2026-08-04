"""Tests for the comment-preserving TOML section surgery behind
`campfire config pull`. The contract: replacing one section must not disturb
one byte of prose or formatting outside it, and the result must parse back to
exactly the section that was written."""

import textwrap
import tomllib

from campfire.deploy.toml_surgery import set_section


def _write(tmp_path, text):
    p = tmp_path / "fields.toml"
    p.write_text(textwrap.dedent(text))
    return p


_DOC = """
    # CAMPFIRE fields — hand-maintained, comments matter!

    [cosmos]
    # our main field
    filters = ["f115w", "f444w"]
    tangent_point = [150.1, 2.2]

    [cosmos.tile1]
    rotation = 0.0

    # smaller test field — do not touch
    [testfield]
    filters = ["f200w"]
    tangent_point = [10.0, -5.0]
"""


def test_replaced_section_round_trips_exactly(tmp_path):
    p = _write(tmp_path, _DOC)
    new = {
        "filters": ["f115w", "f277w"],
        "tangent_point": [150.1, 2.2],
        "tile1": {"rotation": 5.0, "30mas": {"crpix": [1, 2], "naxis": [3, 4]}},
        "epochs": {"e1": {"date_range": ["2026-01-01", "2026-02-01"]}},
        "wcs_shift": [{"files": ["jw01727*"], "delta_ra": 0.1},
                      {"files": ["jw01810*"], "delta_ra": -0.2}],
    }
    set_section(p, "cosmos", new)
    assert tomllib.loads(p.read_text())["cosmos"] == new


def test_untouched_sections_and_comments_survive(tmp_path):
    p = _write(tmp_path, _DOC)
    set_section(p, "cosmos", {"filters": ["f277w"], "tangent_point": [1.0, 2.0]})
    out = p.read_text()
    assert "# CAMPFIRE fields — hand-maintained, comments matter!" in out
    assert "# smaller test field — do not touch" in out
    # In-section comments survive too: the keys they annotate still exist.
    assert "# our main field" in out
    parsed = tomllib.loads(out)
    assert parsed["testfield"] == {"filters": ["f200w"],
                                   "tangent_point": [10.0, -5.0]}


def test_removed_keys_are_deleted(tmp_path):
    p = _write(tmp_path, _DOC)
    set_section(p, "cosmos", {"filters": ["f444w"], "tangent_point": [1.0, 2.0]})
    parsed = tomllib.loads(p.read_text())
    assert "tile1" not in parsed["cosmos"]


def test_new_section_appends(tmp_path):
    p = _write(tmp_path, _DOC)
    set_section(p, "uds", {"filters": ["f090w"], "tangent_point": [34.4, -5.2]})
    parsed = tomllib.loads(p.read_text())
    assert parsed["uds"]["filters"] == ["f090w"]
    assert set(parsed) == {"cosmos", "testfield", "uds"}


def test_creates_file_and_parent_dir(tmp_path):
    p = tmp_path / "config" / "programs.toml"
    set_section(p, "capers", {"program_name": "CAPERS", "cycle": 3})
    assert tomllib.loads(p.read_text()) == {
        "capers": {"program_name": "CAPERS", "cycle": 3}}


def test_dicts_render_as_real_tables_not_inline(tmp_path):
    p = tmp_path / "fields.toml"
    set_section(p, "cosmos", {"filters": ["f444w"],
                              "tile1": {"30mas": {"crpix": [1, 2],
                                                  "naxis": [3, 4]}}})
    out = p.read_text()
    assert "[cosmos.tile1]" in out or "[cosmos.tile1.30mas]" in out
    assert "tile1 = {" not in out


def test_lists_of_dicts_render_as_arrays_of_tables(tmp_path):
    p = tmp_path / "fields.toml"
    set_section(p, "cosmos", {
        "filters": ["f444w"],
        "wcs_shift": [{"files": ["a*"], "delta_ra": 0.1},
                      {"files": ["b*"], "delta_ra": 0.2}]})
    assert "[[cosmos.wcs_shift]]" in p.read_text()
