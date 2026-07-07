"""Vocabulary for the CAMPFIRE layout contract.

This module is the bottom of the dependency graph: pure enums + the ``Scope``
identity record + the package error type. No other ``campfire_layout`` module is
imported here, so it is safe for everything else to import from it.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum


class LayoutError(ValueError):
    """Raised when a product/scope/key/path cannot be resolved by the contract.

    A subclass of ``ValueError`` so existing ``except ValueError`` call sites keep
    working, while callers that care can catch the specific type.
    """


class Instrument(str, Enum):
    NIRSPEC = "nirspec"
    NIRCAM = "nircam"


class LifecycleClass(str, Enum):
    """Per-tree lifecycle, driving ``download --intermediate`` / ``delete-local``.

    Mirrors the table in design-intermediate-products.md §3 PR-2.
    """

    CLOUD_PRODUCT = "cloud-backed-product"   # products/, tiles/ — re-fetchable from cloud
    USER_STATE = "user-state"                # reducer decisions (masks, overrides, catalogs)
    SHARED_CALIBRATION = "shared-calibration"  # reference/nircam/shared/{flats,wisps}
    EXTERNAL_MAST = "external-MAST"          # raw/ — re-fetchable from MAST
    REGENERABLE = "regenerable"              # cache/ — recomputable
    CLI_LOCAL = "cli-local"                  # meta/, cutouts/ — exist only on the client


class KeyScheme(str, Enum):
    """Which storage-key vocabulary to emit.

    ``LEGACY`` is the scheme live today (and at F0, before the OSN re-key): bare
    ``spectra/<obs>/…`` / ``rgb/<obs>/…`` keys that diverge from the disk tree.
    ``CANONICAL`` is the post-re-key scheme where a data-bucket key is just
    ``data/`` + the local relpath (near-identity). The cutover flips the default;
    until then ``CANONICAL`` is implemented and tested but inert.
    """

    LEGACY = "legacy"
    CANONICAL = "canonical"


@dataclass(frozen=True)
class Scope:
    """Identifying coordinates of a product, instrument-agnostic.

    Only the fields a given ``product_type`` declares as required are consulted;
    the rest stay ``None``. Frozen + hashable so scopes can key caches/sets.
    """

    obs: str | None = None              # NIRSpec observation name (products/raw/reference unit)
    field: str | None = None            # NIRCam field name (products/reference unit)
    filt: str | None = None             # filter, e.g. 'f444w'
    pid: str | None = None              # JWST program id (raw/nircam partition)
    data_subdir: str | None = None      # raw/nirspec partition (obs-declared)
    source_id: str | None = None        # NIRSpec source / slit id
    detector: str | None = None         # 'nrs1','nrs2','nrcalong',...
    exposure: str | None = None         # exposure rootname
    object_id: str | None = None        # cross-program object / target id (rgb/sed/photometry)
    catalog_id: str | None = None       # photometry catalog id
    tile: str | None = None             # NIRCam mosaic/RGB tile name
    pixel_scale: str | None = None      # e.g. '30mas'
    zoom: int | None = None             # map tile z
    x: int | None = None                # map tile x
    y: int | None = None                # map tile y

    def require(self, *names: str) -> None:
        """Raise ``LayoutError`` unless every named field is set (non-None)."""
        missing = [n for n in names if getattr(self, n) is None]
        if missing:
            raise LayoutError(
                f"Scope missing required field(s) {missing} "
                f"(have: { {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not None} })"
            )

    @classmethod
    def from_dict(cls, d: dict) -> "Scope":
        """Build a Scope from a plain dict, ignoring unknown keys.

        Used by the shared golden fixture (which carries scopes as JSON objects)
        and by web → python parity checks.
        """
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})
