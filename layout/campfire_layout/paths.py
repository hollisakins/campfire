"""Local filesystem path resolution under ``$CAMPFIRE_ROOT``.

Every path the pipeline writes/globs and the client mirrors on download resolves
here. Results are absolute ``Path`` objects (joined onto the resolved root) unless
a ``*_relpath`` helper is used, which returns the POSIX-relative form the storage
keys and the conformance fixture compare against.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional

from . import products
from .scope import Instrument, LayoutError, Scope


def campfire_root(env: Optional[Mapping[str, str]] = None) -> Path:
    """Resolve ``$CAMPFIRE_ROOT`` (default ``~/campfire``).

    Accepts an explicit env mapping for testability; defaults to ``os.environ``.
    """
    e = os.environ if env is None else env
    return Path(e.get("CAMPFIRE_ROOT") or (Path.home() / "campfire"))


# Top-level tree segments, keyed by the name the registry uses.
_TREES = ("raw", "products", "reference", "cache", "tiles", "meta", "cutouts")


class Roots:
    """The resolved top-level directories under one ``$CAMPFIRE_ROOT``."""

    __slots__ = ("campfire_root", *_TREES)

    def __init__(self, root: Path):
        self.campfire_root = root
        for tree in _TREES:
            setattr(self, tree, root / tree)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Roots({self.campfire_root})"


def roots(root: Optional[Path] = None, *, env: Optional[Mapping[str, str]] = None) -> Roots:
    """Return the :class:`Roots` for ``root`` (or the resolved CAMPFIRE_ROOT)."""
    return Roots(Path(root) if root is not None else campfire_root(env))


def _instrument_value(instrument) -> str:
    return instrument.value if isinstance(instrument, Instrument) else str(instrument)


# ---------------------------------------------------------------------------
# Product paths
# ---------------------------------------------------------------------------

def local_relpath(product_type: str, scope: Scope, filename: Optional[str] = None) -> str:
    """POSIX path of a product relative to ``$CAMPFIRE_ROOT``.

    Raises :class:`LayoutError` for key-only products (``mirrored=False``).
    """
    spec = products.get(product_type)
    if not spec.mirrored:
        raise LayoutError(
            f"product '{product_type}' has no local path (it is key-only: "
            f"generated at deploy and stored only in the bucket)"
        )
    fname = spec.resolve_filename(scope, filename)
    return f"{spec.rel_dir(scope)}/{fname}"


def dir_for(product_type: str, scope: Scope, *, root: Optional[Path] = None) -> Path:
    """Absolute directory holding a product type for the given scope."""
    spec = products.get(product_type)
    if not spec.mirrored:
        raise LayoutError(f"product '{product_type}' has no local directory (key-only)")
    return roots(root).campfire_root / PurePosixPath(spec.rel_dir(scope))


def local_path(product_type: str, scope: Scope, filename: Optional[str] = None,
               *, root: Optional[Path] = None) -> Path:
    """Absolute path of a single product file."""
    return roots(root).campfire_root / PurePosixPath(local_relpath(product_type, scope, filename))


def nircam_work_dir(scope: Scope, *, root: Optional[Path] = None) -> Path:
    """Local combine working-copy dir: ``products/nircam_work/<field>/<filt>/``.

    The combine phase (bad-pixel, outlier, resample) mutates disposable working
    copies of the per-exposure FITS here so the canonical ``nircam_exposure``
    under ``products/nircam/<field>/<filt>/`` stays frozen as the process-phase
    output.

    Deliberately *not* a ``PRODUCTS`` entry, for the same reason ``cache_path`` /
    ``raw_dir`` are plain helpers: it is a regenerable, local-only tree that
    carries no storage key, is never deployed (deploy globs
    ``products/nircam/…``, not this sibling ``nircam_work``), and is never
    reverse-dispatched (:func:`~campfire_layout.bijection.parse_relpath`). The
    ``<filt>`` leaf mirrors the canonical tree's path tail so the pipeline's
    ``split('/')[-2]`` filter parse keeps working on a work copy.
    """
    scope.require("field", "filt")
    return roots(root).campfire_root / "products" / "nircam_work" / scope.field / scope.filt


# ---------------------------------------------------------------------------
# Tree-level helpers (span products, used by Observation/Field setup)
# ---------------------------------------------------------------------------

def reference_dir(instrument, scope: Scope, *, root: Optional[Path] = None) -> Path:
    """Per-scope reducer-decision reference dir (``reference/<inst>/<scope>``)."""
    inst = _instrument_value(instrument)
    if inst == Instrument.NIRSPEC.value:
        scope.require("obs")
        rel = f"reference/nirspec/{scope.obs}"
    elif inst == Instrument.NIRCAM.value:
        scope.require("field")
        rel = f"reference/nircam/{scope.field}"
    else:
        raise LayoutError(f"unknown instrument '{inst}'")
    return roots(root).campfire_root / PurePosixPath(rel)


def shared_reference_dir(instrument, *, root: Optional[Path] = None) -> Path:
    """Shared (de-fielded) calibration-reference dir (``reference/<inst>/shared``)."""
    inst = _instrument_value(instrument)
    if inst != Instrument.NIRCAM.value:
        raise LayoutError(f"no shared reference tree for instrument '{inst}'")
    return roots(root).campfire_root / "reference" / "nircam" / "shared"


def raw_dir(instrument=None, scope: Optional[Scope] = None, *,
            root: Optional[Path] = None) -> Path:
    """Raw (MAST) directory.

    With no instrument: the ``raw/`` root. With an instrument + scope: the
    instrument-partitioned raw dir the pipeline globs and the downloader writes
    (NIRSpec by ``data_subdir``, NIRCam by ``(pid, filt)``).
    """
    base = roots(root).campfire_root / "raw"
    if instrument is None:
        return base
    inst = _instrument_value(instrument)
    if inst == Instrument.NIRSPEC.value:
        scope = scope or Scope()
        if scope.data_subdir is None:
            return base / "nirspec"
        return base / "nirspec" / scope.data_subdir
    if inst == Instrument.NIRCAM.value:
        scope = scope or Scope()
        if scope.pid is None:
            return base / "nircam"
        sub = base / "nircam" / scope.pid
        return sub / scope.filt if scope.filt is not None else sub
    raise LayoutError(f"unknown instrument '{inst}'")


def raw_path(instrument, scope: Scope, filename: str, *, root: Optional[Path] = None) -> Path:
    """Absolute path of a single raw file (where download writes / pipeline reads)."""
    return raw_dir(instrument, scope, root=root) / filename


# Cache sub-kinds the contract governs under cache/. (Other regenerable caches
# with idiosyncratic flat names — e.g. empirical wavecorr / extended-wavelength
# asdf — are intentionally left to their call sites; they are not part of the
# layout contract and relocating them would change behavior for no benefit.)
_CACHE_KINDS = {
    "crds": "crds",
    "templates": "templates",
    "wisps": "wisps",
}


def cache_path(kind: str, filename: Optional[str] = None, *,
               root: Optional[Path] = None) -> Path:
    """Absolute path under ``cache/`` for a regenerable artifact."""
    if kind not in _CACHE_KINDS:
        raise LayoutError(f"unknown cache kind '{kind}'. Known: {sorted(_CACHE_KINDS)}")
    base = roots(root).campfire_root / "cache" / _CACHE_KINDS[kind]
    return base / filename if filename else base


def glob_pattern(product_type: str, scope: Scope, suffix: str, *,
                 root: Optional[Path] = None) -> str:
    """Absolute glob pattern for files of a product type in a scope's dir.

    ``suffix`` is appended to the product directory (caller supplies the wildcard,
    e.g. ``'*_uncal.fits'``).
    """
    return str(dir_for(product_type, scope, root=root) / suffix)
