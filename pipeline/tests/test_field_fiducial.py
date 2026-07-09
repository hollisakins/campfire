"""Tests for the field-level ``fiducial_tiles`` declaration (epic #337, Phase 2).

Covers ``Field.load`` parsing/validation of ``fiducial_tiles`` in ``fields.toml``
and ``Field.fiducial_tile_set()`` (field-level list, per-tile ``fiducial = true``
fallback, and the co-grid WCS-consistency guard). Pure config parsing — no
CRDS/jwst.
"""

import textwrap

import pytest

from campfire_pipeline.nircam.field import Field


def _write_fields_toml(tmp_path, body):
    """Write a fields.toml with the given [cosmos]-section body, return its path."""
    path = tmp_path / "fields.toml"
    path.write_text(textwrap.dedent(body))
    return str(path)


# A co-gridded pair: both tiles inherit the field tangent point (no per-tile
# `tangent_point`), share rotation 0, and declare a 30mas WCS subsection — so
# they pass the co-grid check. A third tile sits on a different tangent point.
_COGRID_FIELD = """
    [cosmos]
    filters = ["f200w", "f444w"]
    files = ["jw01727*"]
    tangent_point = [150.1, 2.1]
    fiducial_tiles = ["A1", "B2"]

    [cosmos.A1]
    "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }

    [cosmos.B2]
    "30mas" = { crpix = [3000, 1000], naxis = [2000, 2000] }

    [cosmos.PRIMER]
    tangent_point = [151.5, 2.9]
    "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }
"""


def test_fiducial_tiles_parsed(tmp_path):
    ff = _write_fields_toml(tmp_path, _COGRID_FIELD)
    field = Field.load("cosmos", fields_file=ff)
    assert field.fiducial_tiles == ["A1", "B2"]


def test_fiducial_tiles_string_is_coerced(tmp_path):
    ff = _write_fields_toml(tmp_path, """
        [cosmos]
        filters = ["f200w"]
        files = ["jw01727*"]
        tangent_point = [150.1, 2.1]
        fiducial_tiles = "A1"

        [cosmos.A1]
        "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }
    """)
    field = Field.load("cosmos", fields_file=ff)
    assert field.fiducial_tiles == ["A1"]


def test_fiducial_tiles_unknown_raises(tmp_path):
    ff = _write_fields_toml(tmp_path, """
        [cosmos]
        filters = ["f200w"]
        files = ["jw01727*"]
        tangent_point = [150.1, 2.1]
        fiducial_tiles = ["A1", "NOPE"]

        [cosmos.A1]
        "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }
    """)
    with pytest.raises(ValueError, match="undeclared tile"):
        Field.load("cosmos", fields_file=ff)


def test_fiducial_tiles_bad_type_raises(tmp_path):
    ff = _write_fields_toml(tmp_path, """
        [cosmos]
        filters = ["f200w"]
        files = ["jw01727*"]
        tangent_point = [150.1, 2.1]
        fiducial_tiles = [1, 2]

        [cosmos.A1]
        "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }
    """)
    with pytest.raises(ValueError, match="list of tile-name"):
        Field.load("cosmos", fields_file=ff)


def test_fiducial_tile_set_returns_declared_order(tmp_path):
    ff = _write_fields_toml(tmp_path, _COGRID_FIELD)
    field = Field.load("cosmos", fields_file=ff)
    assert field.fiducial_tile_set() == ["A1", "B2"]


def test_fiducial_tile_set_per_tile_fallback(tmp_path):
    """No field-level list → tiles flagged `fiducial = true` are collected."""
    ff = _write_fields_toml(tmp_path, """
        [cosmos]
        filters = ["f200w"]
        files = ["jw01727*"]
        tangent_point = [150.1, 2.1]

        [cosmos.A1]
        fiducial = true
        "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }

        [cosmos.B2]
        fiducial = true
        "30mas" = { crpix = [3000, 1000], naxis = [2000, 2000] }

        [cosmos.C3]
        "30mas" = { crpix = [5000, 1000], naxis = [2000, 2000] }
    """)
    field = Field.load("cosmos", fields_file=ff)
    assert field.fiducial_tiles == []  # nothing at the field level
    assert set(field.fiducial_tile_set()) == {"A1", "B2"}  # per-tile flags


def test_fiducial_tile_set_empty_when_undeclared(tmp_path):
    ff = _write_fields_toml(tmp_path, """
        [cosmos]
        filters = ["f200w"]
        files = ["jw01727*"]
        tangent_point = [150.1, 2.1]

        [cosmos.A1]
        "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }
    """)
    field = Field.load("cosmos", fields_file=ff)
    assert field.fiducial_tile_set() == []


def test_fiducial_tile_set_wcs_mismatch_raises(tmp_path):
    """An off-grid tile (different tangent point) breaks the co-grid guard."""
    ff = _write_fields_toml(tmp_path, """
        [cosmos]
        filters = ["f200w"]
        files = ["jw01727*"]
        tangent_point = [150.1, 2.1]
        fiducial_tiles = ["A1", "PRIMER"]

        [cosmos.A1]
        "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }

        [cosmos.PRIMER]
        tangent_point = [151.5, 2.9]
        "30mas" = { crpix = [1000, 1000], naxis = [2000, 2000] }
    """)
    field = Field.load("cosmos", fields_file=ff)
    with pytest.raises(ValueError, match="share a tangent"):
        field.fiducial_tile_set()
