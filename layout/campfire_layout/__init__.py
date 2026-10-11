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
    ROOT_ARCHIVE,
    ROOT_DR,
    ROOT_LIVE,
    ParsedKey,
    derive_sibling,
    is_compressed_key,
    is_known_key,
    is_readable_key,
    is_writable_key,
    key_for_parsed,
    key_to_relpath,
    parse_key,
    parse_relpath,
    relpath_to_key,
)
from .keys import ARCHIVE_ROOT, archive_hex, archive_key, bucket_for, key_prefix, storage_key
from .lifecycle import tree_class
from .migrate import LayoutMigrator, apply_migration, plan_migration
from .paths import (
    Roots,
    cache_path,
    campfire_root,
    dir_for,
    dr_dir,
    dr_relpath,
    glob_pattern,
    is_dr_tag,
    local_path,
    local_relpath,
    nircam_work_dir,
    raw_dir,
    raw_path,
    reference_dir,
    roots,
    shared_reference_dir,
)
from .products import PRODUCTS, ProductSpec, get, is_archivable
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
    "PRODUCTS", "ProductSpec", "get", "is_archivable",
    # paths
    "Roots", "roots", "campfire_root", "dir_for", "local_path", "local_relpath",
    "nircam_work_dir", "dr_dir", "dr_relpath", "is_dr_tag",
    "reference_dir", "shared_reference_dir", "raw_dir", "raw_path", "cache_path",
    "glob_pattern",
    # keys
    "bucket_for", "key_prefix", "storage_key", "archive_key", "archive_hex", "ARCHIVE_ROOT",
    # bijection
    "ParsedKey", "parse_key", "parse_relpath", "key_to_relpath", "relpath_to_key",
    "key_for_parsed", "derive_sibling", "is_known_key", "is_readable_key", "is_writable_key",
    "is_compressed_key", "ROOT_LIVE", "ROOT_ARCHIVE", "ROOT_DR",
    # lifecycle
    "tree_class",
    # migration (#212 one-time layout re-org)
    "LayoutMigrator", "plan_migration", "apply_migration",
]

__version__ = "0.1.0"
