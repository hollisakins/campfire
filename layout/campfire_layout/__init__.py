"""campfire_layout — the single authority for the CAMPFIRE directory/key contract.

One tested, zero-dependency module shared by the pipeline (``campfire_pipeline``),
the client/deploy package (``campfire``), and — mirrored in TypeScript — the web
portal. It owns:

  (i)   the local path of any product under ``$CAMPFIRE_ROOT``;
  (ii)  its storage key (LEGACY today, CANONICAL after the OSN re-key);
  (iii) the bijection key ↔ local-relpath (total + reversible per scheme);
  (iv)  a lifecycle class per top-level tree.

Nobody hand-builds a path or a key: deploy/download/pipeline/web all call here.
"""

from __future__ import annotations

from .bijection import (
    ParsedKey,
    derive_sibling,
    is_known_key,
    key_to_relpath,
    parse_key,
    parse_relpath,
    relpath_to_key,
)
from .keys import bucket_for, key_prefix, storage_key
from .lifecycle import tree_class
from .paths import (
    Roots,
    cache_path,
    campfire_root,
    dir_for,
    glob_pattern,
    local_path,
    local_relpath,
    raw_dir,
    raw_path,
    reference_dir,
    roots,
    shared_reference_dir,
)
from .products import PRODUCTS, ProductSpec, get
from .scope import (
    Instrument,
    KeyScheme,
    LayoutError,
    LifecycleClass,
    Scope,
)

__all__ = [
    # vocabulary
    "Instrument", "KeyScheme", "LayoutError", "LifecycleClass", "Scope",
    # registry
    "PRODUCTS", "ProductSpec", "get",
    # paths
    "Roots", "roots", "campfire_root", "dir_for", "local_path", "local_relpath",
    "reference_dir", "shared_reference_dir", "raw_dir", "raw_path", "cache_path",
    "glob_pattern",
    # keys
    "bucket_for", "key_prefix", "storage_key",
    # bijection
    "ParsedKey", "parse_key", "parse_relpath", "key_to_relpath", "relpath_to_key",
    "derive_sibling", "is_known_key",
    # lifecycle
    "tree_class",
]

__version__ = "0.1.0"
