"""Tests for flag query operators."""

import re

from campfire.flags import DQFlags, FlagQuery, RedshiftQuality
from campfire.models import Object


class TestFlagOperators:
    def test_or_returns_flagquery(self):
        result = DQFlags.CHIP_GAP | DQFlags.CONTAMINATION
        assert isinstance(result, FlagQuery)
        assert result.include_any == 3  # 1 | 2
        assert result.include_all == 0
        assert result.exclude == 0

    def test_and_returns_flagquery(self):
        result = DQFlags.CHIP_GAP & DQFlags.CONTAMINATION
        assert isinstance(result, FlagQuery)
        assert result.include_all == 3
        assert result.include_any == 0
        assert result.exclude == 0

    def test_invert_returns_flagquery(self):
        result = ~DQFlags.CHIP_GAP
        assert isinstance(result, FlagQuery)
        assert result.exclude == 1
        assert result.include_any == 0
        assert result.include_all == 0

    def test_complex_or_and_not(self):
        result = (DQFlags.CHIP_GAP | DQFlags.MULTIPLE_SOURCES) & ~DQFlags.CONTAMINATION
        assert isinstance(result, FlagQuery)
        assert result.include_any == 9  # 1 | 8
        assert result.exclude == 2  # CONTAMINATION

    def test_chained_or(self):
        result = DQFlags.CHIP_GAP | DQFlags.CONTAMINATION | DQFlags.STUCK_SHUTTER
        assert isinstance(result, FlagQuery)
        assert result.include_any == 7  # 1 | 2 | 4

    def test_chained_and(self):
        result = DQFlags.CHIP_GAP & DQFlags.CONTAMINATION & DQFlags.STUCK_SHUTTER
        assert isinstance(result, FlagQuery)
        assert result.include_all == 7

    def test_or_with_flagquery(self):
        query = FlagQuery(include_any=2)
        result = DQFlags.CHIP_GAP | query
        assert isinstance(result, FlagQuery)
        assert result.include_any == 3

    def test_and_with_flagquery(self):
        query = FlagQuery(include_all=2, exclude=4)
        result = DQFlags.CHIP_GAP & query
        assert isinstance(result, FlagQuery)
        assert result.include_all == 3
        assert result.exclude == 4

    def test_dq_flags_operators(self):
        result = ~DQFlags.CONTAMINATION & ~DQFlags.LOW_SNR
        assert isinstance(result, FlagQuery)
        assert result.exclude == 34  # 2 | 32


class TestFlagQueryCombination:
    def test_flagquery_and_flagquery(self):
        q1 = FlagQuery(include_any=1, exclude=2)
        q2 = FlagQuery(include_all=4, exclude=8)
        result = q1 & q2
        assert result.include_any == 1
        assert result.include_all == 4
        assert result.exclude == 10

    def test_flagquery_or_flagquery(self):
        q1 = FlagQuery(include_any=1)
        q2 = FlagQuery(include_any=4)
        result = q1 | q2
        assert result.include_any == 5

    def test_flagquery_to_params(self):
        query = FlagQuery(include_any=1, include_all=2, exclude=4)
        params = query.to_params("dq_flags")
        assert params == {
            "dq_flags_include_any": 1,
            "dq_flags_include_all": 2,
            "dq_flags_exclude": 4,
        }

    def test_flagquery_to_params_omits_zeros(self):
        query = FlagQuery(include_any=1)
        params = query.to_params("dq_flags")
        assert params == {"dq_flags_include_any": 1}
        assert "dq_flags_include_all" not in params
        assert "dq_flags_exclude" not in params


class TestRedshiftQualityDocstring:
    """Guard against the Object docstring drifting from the canonical enum.

    The integer semantics of ``redshift_quality`` are restated in prose in the
    ``Object`` dataclass docstring. This previously inverted the codes
    (claiming ``1=tentative`` when 1 is Impossible — see issue #198). Assert the
    documented ``N=label`` pairs match :class:`RedshiftQuality` so any future
    inversion fails CI.
    """

    def _documented_pairs(self):
        # Extract "<int>=<label>" pairs from the Object docstring, e.g. "1=impossible".
        return {
            int(value): label.strip()
            for value, label in re.findall(r"(\d+)=([a-z ]+)", Object.__doc__)
        }

    def test_docstring_documents_every_enum_member(self):
        documented = self._documented_pairs()
        assert set(documented) == {member.value for member in RedshiftQuality}

    def test_docstring_labels_match_enum(self):
        documented = self._documented_pairs()
        for member in RedshiftQuality:
            expected_label = member.name.lower().replace("_", " ")
            assert documented[member.value] == expected_label, (
                f"redshift_quality={member.value} documented as "
                f"{documented[member.value]!r}, expected {expected_label!r}"
            )
