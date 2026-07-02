"""Tests for cone-search ``--target`` normalization in the download tool.

Regression coverage for the MAST HTTP 500 raised when a comma-separated
coordinate pair (``"215.0,52.9"``) was forwarded verbatim to the JWST search
API's ``target`` field, which only accepts an object name or a *space*-
separated ``"RA Dec"`` pair.
"""

from campfire_pipeline.common.query import _looks_like_float, _normalize_target


class TestNormalizeTarget:
    def test_comma_coords_folded_to_space(self):
        assert _normalize_target("215.0,52.9") == "215.0 52.9"

    def test_comma_with_whitespace(self):
        assert _normalize_target("215.0, 52.9") == "215.0 52.9"
        assert _normalize_target("  -5.1 , +2.2  ") == "-5.1 +2.2"

    def test_scientific_notation(self):
        assert _normalize_target("1e2,-3.5") == "1e2 -3.5"

    def test_space_coords_unchanged(self):
        assert _normalize_target("150.1163 2.2070") == "150.1163 2.2070"

    def test_object_names_unchanged(self):
        assert _normalize_target("M1") == "M1"
        assert _normalize_target("NGC 1300") == "NGC 1300"

    def test_two_names_not_coords_unchanged(self):
        # Two comma-separated names are not a coordinate pair — leave as-is.
        assert _normalize_target("M1, M2") == "M1, M2"

    def test_three_numbers_unchanged(self):
        # Only an exact two-token pair is a coordinate; anything else is left
        # untouched rather than silently mangled.
        assert _normalize_target("1,2,3") == "1,2,3"


class TestLooksLikeFloat:
    def test_accepts_numbers(self):
        assert _looks_like_float("215.0")
        assert _looks_like_float("-3.5")
        assert _looks_like_float("+2.2")
        assert _looks_like_float("1e2")

    def test_rejects_non_numbers(self):
        assert not _looks_like_float("M1")
        assert not _looks_like_float("")
        assert not _looks_like_float(None)
