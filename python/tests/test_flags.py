"""Tests for flag query operators."""

import pytest
from campfire.flags import ObjectFlags, SpectralFeatures, DQFlags, FlagQuery


class TestFlagOperators:
    """Test numpy-style flag operators."""

    def test_or_returns_flagquery(self):
        """Test that | operator returns FlagQuery with include_any."""
        result = ObjectFlags.LRD | ObjectFlags.BROAD_LINE
        assert isinstance(result, FlagQuery)
        assert result.include_any == 3  # 1 | 2
        assert result.include_all == 0
        assert result.exclude == 0

    def test_and_returns_flagquery(self):
        """Test that & operator returns FlagQuery with include_all."""
        result = ObjectFlags.LRD & ObjectFlags.BROAD_LINE
        assert isinstance(result, FlagQuery)
        assert result.include_all == 3  # 1 | 2
        assert result.include_any == 0
        assert result.exclude == 0

    def test_invert_returns_flagquery(self):
        """Test that ~ operator returns FlagQuery with exclude."""
        result = ~ObjectFlags.LRD
        assert isinstance(result, FlagQuery)
        assert result.exclude == 1
        assert result.include_any == 0
        assert result.include_all == 0

    def test_complex_or_and_not(self):
        """Test (LRD | LAE) & ~BROAD_LINE expression."""
        result = (ObjectFlags.LRD | ObjectFlags.LYA_EMITTER) & ~ObjectFlags.BROAD_LINE
        assert isinstance(result, FlagQuery)
        assert result.include_any == 5  # 1 | 4
        assert result.exclude == 2  # BROAD_LINE

    def test_chained_or(self):
        """Test multiple OR operations."""
        result = ObjectFlags.LRD | ObjectFlags.BROAD_LINE | ObjectFlags.LYA_EMITTER
        assert isinstance(result, FlagQuery)
        assert result.include_any == 7  # 1 | 2 | 4

    def test_chained_and(self):
        """Test multiple AND operations."""
        result = ObjectFlags.LRD & ObjectFlags.BROAD_LINE & ObjectFlags.LYA_EMITTER
        assert isinstance(result, FlagQuery)
        assert result.include_all == 7  # 1 | 2 | 4

    def test_or_with_flagquery(self):
        """Test Flag | FlagQuery."""
        query = FlagQuery(include_any=2)
        result = ObjectFlags.LRD | query
        assert isinstance(result, FlagQuery)
        assert result.include_any == 3  # 1 | 2

    def test_and_with_flagquery(self):
        """Test Flag & FlagQuery."""
        query = FlagQuery(include_all=2, exclude=4)
        result = ObjectFlags.LRD & query
        assert isinstance(result, FlagQuery)
        assert result.include_all == 3  # 1 | 2
        assert result.exclude == 4  # preserved from query

    def test_all_flag_classes_have_operators(self):
        """Test that all flag classes support operators."""
        for cls in [ObjectFlags, SpectralFeatures, DQFlags]:
            flag = list(cls)[0]
            # OR
            result = flag | flag
            assert isinstance(result, FlagQuery), f"{cls.__name__} | failed"
            # AND
            result = flag & flag
            assert isinstance(result, FlagQuery), f"{cls.__name__} & failed"
            # NOT
            result = ~flag
            assert isinstance(result, FlagQuery), f"{cls.__name__} ~ failed"

    def test_spectral_features_operators(self):
        """Test SpectralFeatures specific operators."""
        result = SpectralFeatures.LYMAN_BREAK | SpectralFeatures.MULTI_EMISSION
        assert isinstance(result, FlagQuery)
        assert result.include_any == 34  # 2 | 32

    def test_dq_flags_operators(self):
        """Test DQFlags specific operators."""
        result = ~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR
        assert isinstance(result, FlagQuery)
        assert result.exclude == 34  # 2 | 32


class TestFlagQueryCombination:
    """Test FlagQuery combination operations."""

    def test_flagquery_and_flagquery(self):
        """Test FlagQuery & FlagQuery."""
        q1 = FlagQuery(include_any=1, exclude=2)
        q2 = FlagQuery(include_all=4, exclude=8)
        result = q1 & q2
        assert result.include_any == 1
        assert result.include_all == 4
        assert result.exclude == 10  # 2 | 8

    def test_flagquery_or_flagquery(self):
        """Test FlagQuery | FlagQuery."""
        q1 = FlagQuery(include_any=1)
        q2 = FlagQuery(include_any=4)
        result = q1 | q2
        assert result.include_any == 5  # 1 | 4

    def test_flagquery_to_params(self):
        """Test FlagQuery.to_params() method."""
        query = FlagQuery(include_any=1, include_all=2, exclude=4)
        params = query.to_params("object_flags")
        assert params == {
            "object_flags_include_any": 1,
            "object_flags_include_all": 2,
            "object_flags_exclude": 4,
        }

    def test_flagquery_to_params_omits_zeros(self):
        """Test that to_params omits zero values."""
        query = FlagQuery(include_any=1)
        params = query.to_params("object_flags")
        assert params == {"object_flags_include_any": 1}
        assert "object_flags_include_all" not in params
        assert "object_flags_exclude" not in params
