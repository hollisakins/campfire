"""Tests for the pure region-text <-> polygon helpers used by the mask editor.

The Qt/matplotlib UI itself is not exercised here (would require a display
and a GUI event loop) — only the deterministic serialization layer.
"""

from __future__ import annotations

from campfire_pipeline.nirspec import masks
from campfire_pipeline.nirspec.mask_regions import (
    polygons_to_reg_text,
    reg_to_polygons,
)


class TestRegToPolygons:
    def test_simple(self):
        reg = "image\npolygon(10,20,30,20,30,40,10,40)"
        polys = reg_to_polygons(reg)
        # DS9 -> editor: subtract 1 from every coord (1-indexed -> 0-indexed).
        assert polys == [[(9.0, 19.0), (29.0, 19.0), (29.0, 39.0), (9.0, 39.0)]]

    def test_multiple_and_comments(self):
        reg = (
            "# DS9 region file format\n"
            "image\n"
            "polygon(1,1,2,1,2,2,1,2)\n"
            "# trailing comment\n"
            "polygon(10,10,20,10,20,20,10,20)\n"
        )
        polys = reg_to_polygons(reg)
        assert len(polys) == 2
        assert polys[0][0] == (0.0, 0.0)
        assert polys[1][0] == (9.0, 9.0)

    def test_empty_returns_empty(self):
        assert reg_to_polygons(None) == []
        assert reg_to_polygons("") == []
        assert reg_to_polygons("image\n# nothing else\n") == []

    def test_skips_malformed(self):
        # First polygon has 5 < 6 coords; only the second should survive.
        reg = "image\npolygon(1,2,3)\npolygon(10,20,30,20,30,40,10,40)"
        polys = reg_to_polygons(reg)
        assert len(polys) == 1


class TestPolygonsToRegText:
    def test_round_trip(self):
        original = "image\npolygon(10,20,30,20,30,40,10,40)"
        polys = reg_to_polygons(original)
        out = polygons_to_reg_text(polys)
        assert out == original

    def test_empty_returns_empty(self):
        assert polygons_to_reg_text([]) == ""
        # Polygons with <3 vertices are silently dropped.
        assert polygons_to_reg_text([[(0, 0), (1, 1)]]) == ""

    def test_handles_floats(self):
        polys = [[(0.5, 1.5), (10.5, 1.5), (10.5, 11.5)]]
        out = polygons_to_reg_text(polys)
        # +1 offset back to DS9 1-indexed; %g formatting strips trailing zeros.
        assert out == "image\npolygon(1.5,2.5,11.5,2.5,11.5,12.5)"


class TestRoundTripThroughMasksRasterizer:
    """Sanity-check that polygons drawn in the editor rasterize to the same
    pixels when fed through ``masks.parse_regions_to_mask``. Catches off-by-one
    drift between the +1 offset and astropy.regions's 1-indexed handling."""

    def test_square_masks_expected_pixels(self):
        # A square polygon in editor coords (0-indexed pixel centers).
        editor_polys = [[(2, 2), (7, 2), (7, 7), (2, 7)]]
        reg_text = polygons_to_reg_text(editor_polys)
        mask = masks.parse_regions_to_mask(reg_text, (10, 10))
        # Center pixel (5, 5) should be inside the square.
        assert mask[5, 5]
        # Origin should not be masked.
        assert not mask[0, 0]
        # Pixels well outside should not be masked.
        assert not mask[9, 9]
