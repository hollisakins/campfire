"""Lifecycle classification of a tree / path / key.

Resolves via the registry first (so a product is classified by *what it is*, not
merely which top-level dir it sits in — e.g. NIRSpec manual masks live under
``products/`` but are USER_STATE, which ``delete-local`` must never treat as a
regenerable product). Falls back to a top-level-dir heuristic for paths the
registry doesn't recognize.
"""

from __future__ import annotations

from . import bijection, products
from .scope import LayoutError, LifecycleClass

# Top-level-dir fallback for unrecognized paths/keys.
_TREE_FALLBACK = {
    "products": LifecycleClass.CLOUD_PRODUCT,
    "tiles": LifecycleClass.CLOUD_PRODUCT,
    "reference": LifecycleClass.USER_STATE,
    "raw": LifecycleClass.EXTERNAL_MAST,
    "cache": LifecycleClass.REGENERABLE,
    "meta": LifecycleClass.CLI_LOCAL,
    "cutouts": LifecycleClass.CLI_LOCAL,
}


def tree_class(path_or_key: str) -> LifecycleClass:
    """Lifecycle class for a relpath or a storage key."""
    # Registry-first: try relpath, then key.
    for parse in (bijection.parse_relpath, bijection.parse_key):
        try:
            pk = parse(path_or_key)
        except LayoutError:
            continue
        return products.get(pk.product_type).lifecycle

    # Fallback: classify by the top-level tree segment.
    top = path_or_key.split("/", 1)[0]
    if top in _TREE_FALLBACK:
        return _TREE_FALLBACK[top]
    raise LayoutError(f"cannot classify lifecycle for '{path_or_key}'")
