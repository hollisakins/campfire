"""Storage-key construction.

A storage key is the object's address in a bucket. Two schemes coexist (see
:class:`~campfire_layout.scope.KeyScheme`):

* ``LEGACY`` — today's bare keys (``spectra/<obs>/…``) for products that already
  have them; reserved products fall back to the canonical form.
* ``CANONICAL`` — data-bucket key is ``data/`` + the local relpath; tiles keys are
  unchanged (they stay on R2).
"""

from __future__ import annotations

from typing import Optional

from . import products
from .scope import KeyScheme, LayoutError, Scope


def bucket_for(product_type: str) -> str:
    """Return the bucket ('data' | 'tiles') for a cloud-backed product."""
    spec = products.get(product_type)
    if spec.bucket is None:
        raise LayoutError(
            f"product '{product_type}' is not cloud-backed (no storage key)"
        )
    return spec.bucket


def key_prefix(product_type: str, scope: Scope, *,
               scheme: KeyScheme = KeyScheme.LEGACY) -> str:
    """Key prefix (everything before the filename) — for list/delete/clean.

    No trailing slash; callers that prefix-list add one.
    """
    spec = products.get(product_type)
    if spec.bucket is None:
        raise LayoutError(f"product '{product_type}' is not cloud-backed (no storage key)")
    spec.validate(scope)

    # Tiles are scheme-invariant and never gain a 'data/' prefix (they stay on R2).
    if spec.scheme_invariant:
        return spec.legacy_prefix(scope)

    # Legacy scheme reproduces today's key for products that already have one.
    if scheme is KeyScheme.LEGACY and spec.legacy_prefix is not None:
        return spec.legacy_prefix(scope)

    # CANONICAL (or a reserved product, which adopts the canonical form in both
    # schemes since there is no legacy key to preserve).
    if spec.mirrored:
        # data-bucket canonical key = 'data/' + local relative dir.
        return f"data/{spec.rel_dir(scope)}"
    if spec.legacy_prefix is not None:
        # key-only product (no disk relpath to mirror): canonical = 'data/' + legacy prefix.
        return f"data/{spec.legacy_prefix(scope)}"
    raise LayoutError(
        f"product '{product_type}' has neither a local path nor a legacy key — "
        f"cannot form a canonical storage key"
    )


def storage_key(product_type: str, scope: Scope, filename: Optional[str] = None, *,
                scheme: KeyScheme = KeyScheme.LEGACY) -> str:
    """Full storage key for a single object.

    Products with ``compressed_suffixes`` are stored gzipped in the bucket: the
    key gains a ``.gz`` extension for any filename ending in one of those
    suffixes (the local relpath, built from the same *plain* filename, does not
    — the bijection strips ``.gz`` on the way back). See
    :func:`bijection.parse_relpath`.
    """
    spec = products.get(product_type)
    fname = spec.resolve_filename(scope, filename)
    if any(fname.endswith(sfx) for sfx in spec.compressed_suffixes):
        fname = f"{fname}.gz"
    return f"{key_prefix(product_type, scope, scheme=scheme)}/{fname}"
