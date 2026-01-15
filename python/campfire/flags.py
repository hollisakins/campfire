"""
CAMPFIRE flag definitions and query builders.

Provides numpy-style operators for filtering objects by flags:

    from campfire.flags import ObjectFlags, DQFlags, SpectralFeatures

    # Has LRD OR LAE, but NOT broad line
    cf.query_objects(
        object_flags=(ObjectFlags.LRD | ObjectFlags.LAE) & ~ObjectFlags.BROAD_LINE
    )

    # Simple string-based query (like web app)
    cf.query_objects(object_flags=['LRD', 'LAE'])

Operators:
    |  : OR  (match any of these flags)
    &  : AND (must have all of these flags)
    ~  : NOT (must not have this flag)
"""

from enum import IntFlag
from dataclasses import dataclass
from typing import Union, List, Dict, Optional, Type


@dataclass
class FlagQuery:
    """
    Represents a flag query with include/exclude semantics.

    Attributes
    ----------
    include_any : int
        Bitmask of flags where at least one must be present.
        SQL: (flags & include_any) != 0
    include_all : int
        Bitmask of flags that must all be present.
        SQL: (flags & include_all) = include_all
    exclude : int
        Bitmask of flags that must not be present.
        SQL: (flags & exclude) = 0
    """

    include_any: int = 0
    include_all: int = 0
    exclude: int = 0

    def __and__(self, other: "FlagQuery") -> "FlagQuery":
        """Combine conditions with AND."""
        if isinstance(other, FlagQuery):
            return FlagQuery(
                include_any=self.include_any | other.include_any,
                include_all=self.include_all | other.include_all,
                exclude=self.exclude | other.exclude,
            )
        return NotImplemented

    def __or__(self, other: "FlagQuery") -> "FlagQuery":
        """Combine include_any masks with OR."""
        if isinstance(other, FlagQuery):
            return FlagQuery(
                include_any=self.include_any | other.include_any,
                include_all=self.include_all,
                exclude=self.exclude,
            )
        return NotImplemented

    def __repr__(self) -> str:
        parts = []
        if self.include_any:
            parts.append(f"include_any={self.include_any}")
        if self.include_all:
            parts.append(f"include_all={self.include_all}")
        if self.exclude:
            parts.append(f"exclude={self.exclude}")
        return f"FlagQuery({', '.join(parts)})"

    def to_params(self, prefix: str = "") -> Dict[str, int]:
        """
        Convert to API query parameters.

        Parameters
        ----------
        prefix : str
            Parameter name prefix (e.g., 'object_flags')

        Returns
        -------
        dict
            Dictionary of query parameters
        """
        params = {}
        sep = "_" if prefix else ""
        if self.include_any:
            params[f"{prefix}{sep}include_any"] = self.include_any
        if self.include_all:
            params[f"{prefix}{sep}include_all"] = self.include_all
        if self.exclude:
            params[f"{prefix}{sep}exclude"] = self.exclude
        return params


def queryable(cls):
    """
    Decorator that adds query operators to a Flag class.

    Must be applied after class creation to override the operators
    that Python's enum metaclass copies from the base Flag class.

    Example
    -------
    @queryable
    class ObjectFlags(QueryableFlag):
        LRD = 1
        BROAD_LINE = 2
    """

    def _or(self, other):
        """Flag | Flag = match any of these (OR)."""
        if isinstance(other, FlagQuery):
            return FlagQuery(
                include_any=int(self) | other.include_any,
                include_all=other.include_all,
                exclude=other.exclude,
            )
        elif isinstance(other, int):
            return FlagQuery(include_any=int(self) | int(other))
        return NotImplemented

    def _and(self, other):
        """Flag & Flag = must have all (AND)."""
        if isinstance(other, FlagQuery):
            return FlagQuery(
                include_any=other.include_any,
                include_all=int(self) | other.include_all,
                exclude=other.exclude,
            )
        elif isinstance(other, int):
            return FlagQuery(include_all=int(self) | int(other))
        return NotImplemented

    def _invert(self):
        """~Flag = exclude this flag (NOT)."""
        return FlagQuery(exclude=int(self))

    def _ror(self, other):
        """Support: other | Flag."""
        return _or(self, other)

    def _rand(self, other):
        """Support: other & Flag."""
        return _and(self, other)

    cls.__or__ = _or
    cls.__and__ = _and
    cls.__invert__ = _invert
    cls.__ror__ = _ror
    cls.__rand__ = _rand
    return cls


class QueryableFlag(IntFlag):
    """
    IntFlag subclass marker for queryable flags.

    Note: Operators are applied via @queryable decorator, not inheritance,
    because Python's enum metaclass overwrites inherited operators.

    Supports:
        - Flag | Flag : match any (OR)
        - Flag & Flag : must have all (AND)
        - ~Flag : exclude (NOT)
    """

    pass


@queryable
class SpectralFeatures(QueryableFlag):
    """
    Spectral features used for redshift determination.

    These flags indicate which spectral features were used to
    constrain or determine the redshift of an object.
    """

    CONTINUUM_BREAK = 1
    """Redshift constrained by overall continuum shape."""

    LYMAN_BREAK = 2
    """Clear Lyman break detected."""

    BALMER_BREAK = 4
    """Clear Balmer break detected."""

    ABSORPTION_FEATURES = 8
    """Absorption lines/features identified."""

    SINGLE_EMISSION = 16
    """Single emission line detected."""

    MULTI_EMISSION = 32
    """Multiple emission lines detected."""


@queryable
class ObjectFlags(QueryableFlag):
    """
    Object properties and classifications.

    These flags indicate notable properties or classifications
    assigned during visual inspection.
    """

    LRD = 1
    """Little Red Dot - compact red source."""

    BROAD_LINE = 2
    """Broad emission line detected (AGN indicator)."""

    LYA_EMITTER = 4
    """Strong Lyman-alpha emission."""

    BALMER_BREAK_GALAXY = 8
    """Strong Balmer break indicating evolved stellar population."""

    OIII_EMITTER = 16
    """Strong [OIII] 4959,5007 emission."""

    HA_EMITTER = 32
    """Strong H-alpha emission."""

    PASSIVE = 64
    """Quiescent galaxy with little star formation."""

    DUSTY = 128
    """Significant dust attenuation."""

    STAR = 256
    """Stellar spectrum (not a galaxy)."""


@queryable
class DQFlags(QueryableFlag):
    """
    Data quality flags.

    These flags indicate potential issues with the spectral data
    that may affect scientific analysis.
    """

    CHIP_GAP = 1
    """Spectrum affected by detector chip gap."""

    CONTAMINATION = 2
    """Contamination from nearby source or open shutter."""

    STUCK_SHUTTER = 4
    """Possible stuck closed shutter."""

    MULTIPLE_SOURCES = 8
    """Multiple sources in slitlet."""

    NO_DETECTION = 16
    """No source detected in spectrum."""

    LOW_SNR = 32
    """Low signal-to-noise ratio."""

    SPECTRAL_OVERLAP = 64
    """Spectral overlap in grating spectrum."""

    PRISM_CORRUPTED = 128
    """PRISM data corrupted or unusable."""

    GRATING_CORRUPTED = 256
    """Grating data corrupted or unusable."""


# Registry of flag classes by parameter name
_FLAG_REGISTRY: Dict[str, Type[QueryableFlag]] = {
    "spectral_features": SpectralFeatures,
    "object_flags": ObjectFlags,
    "dq_flags": DQFlags,
}


def list_flags(flag_type: Optional[str] = None) -> None:
    """
    Print available flags and their values.

    Parameters
    ----------
    flag_type : str, optional
        One of 'spectral_features', 'object_flags', 'dq_flags'.
        If None, prints all flag types.

    Examples
    --------
    >>> list_flags('object_flags')
    ObjectFlags:
      LRD                    = 1
      BROAD_LINE             = 2
      LYA_EMITTER            = 4
      ...
    """
    if flag_type is not None:
        if flag_type not in _FLAG_REGISTRY:
            raise ValueError(
                f"Unknown flag type: {flag_type}. "
                f"Must be one of: {list(_FLAG_REGISTRY.keys())}"
            )
        classes = [_FLAG_REGISTRY[flag_type]]
    else:
        classes = list(_FLAG_REGISTRY.values())

    for cls in classes:
        print(f"\n{cls.__name__}:")
        for flag in cls:
            doc = flag.__doc__ or ""
            if doc:
                print(f"  {flag.name:22} = {flag.value:<4}  # {doc}")
            else:
                print(f"  {flag.name:22} = {flag.value}")


def decode_flags(value: int, flag_type: str) -> List[str]:
    """
    Decode a bitmask integer to a list of flag names.

    Parameters
    ----------
    value : int
        Bitmask integer value.
    flag_type : str
        One of 'spectral_features', 'object_flags', 'dq_flags'.

    Returns
    -------
    list of str
        List of flag names that are set in the bitmask.

    Examples
    --------
    >>> decode_flags(5, 'object_flags')
    ['LRD', 'LYA_EMITTER']
    """
    if flag_type not in _FLAG_REGISTRY:
        raise ValueError(
            f"Unknown flag type: {flag_type}. "
            f"Must be one of: {list(_FLAG_REGISTRY.keys())}"
        )

    flag_class = _FLAG_REGISTRY[flag_type]
    return [f.name for f in flag_class if value & f.value]


def encode_flags(names: List[str], flag_type: str) -> int:
    """
    Encode a list of flag names to a bitmask integer.

    Parameters
    ----------
    names : list of str
        List of flag names (case-insensitive).
    flag_type : str
        One of 'spectral_features', 'object_flags', 'dq_flags'.

    Returns
    -------
    int
        Bitmask integer with the specified flags set.

    Examples
    --------
    >>> encode_flags(['LRD', 'LYA_EMITTER'], 'object_flags')
    5
    """
    if flag_type not in _FLAG_REGISTRY:
        raise ValueError(
            f"Unknown flag type: {flag_type}. "
            f"Must be one of: {list(_FLAG_REGISTRY.keys())}"
        )

    flag_class = _FLAG_REGISTRY[flag_type]
    mask = 0
    for name in names:
        try:
            flag = flag_class[name.upper()]
            mask |= flag.value
        except KeyError:
            valid_names = [f.name for f in flag_class]
            raise ValueError(
                f"Unknown flag name: {name}. " f"Valid names for {flag_type}: {valid_names}"
            )
    return mask


def parse_flag_input(
    value: Union[None, int, str, List[str], QueryableFlag, FlagQuery],
    flag_class: Type[QueryableFlag],
) -> Optional[FlagQuery]:
    """
    Convert various input types to a FlagQuery.

    Parameters
    ----------
    value : various
        Can be:
        - None: Returns None
        - int: Treated as include_any (legacy behavior)
        - str: Single flag name, treated as include_all
        - list of str: Multiple flag names, treated as include_any
        - QueryableFlag: Single flag, treated as include_all
        - FlagQuery: Returned as-is
    flag_class : type
        The flag enum class to use for parsing.

    Returns
    -------
    FlagQuery or None
        The normalized flag query, or None if input was None.
    """
    if value is None:
        return None

    if isinstance(value, FlagQuery):
        return value

    if isinstance(value, flag_class):
        # Single flag = must have this flag (include_all)
        return FlagQuery(include_all=int(value))

    if isinstance(value, int):
        # Raw integer = legacy behavior (include_any)
        return FlagQuery(include_any=value)

    if isinstance(value, str):
        # Single string = single flag (include_all)
        try:
            flag = flag_class[value.upper()]
            return FlagQuery(include_all=int(flag))
        except KeyError:
            valid_names = [f.name for f in flag_class]
            raise ValueError(
                f"Unknown flag name: {value}. " f"Valid names: {valid_names}"
            )

    if isinstance(value, list):
        # List of strings = include_any (match any, like web app)
        mask = 0
        for name in value:
            try:
                flag = flag_class[name.upper()]
                mask |= flag.value
            except KeyError:
                valid_names = [f.name for f in flag_class]
                raise ValueError(
                    f"Unknown flag name: {name}. " f"Valid names: {valid_names}"
                )
        return FlagQuery(include_any=mask)

    raise TypeError(
        f"Invalid flag parameter type: {type(value).__name__}. "
        f"Expected int, str, list, {flag_class.__name__}, or FlagQuery."
    )
