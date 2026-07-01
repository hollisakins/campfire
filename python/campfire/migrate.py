"""Client-side hook for the one-time #212 layout migration.

Detects when the local ``$CAMPFIRE_ROOT`` still uses the pre-#212 flat layout
(``products/<obs>/`` instead of ``products/nirspec/<obs>/``) and, on ``campfire
sync``, offers to migrate it in place. The heavy lifting is the shared,
zero-dependency ``campfire_layout.migrate`` core — the same code the operator
CLI (``pipeline/scripts/migrate_layout_212.py``) runs.

"Full tree when possible": the products (and NIRCam-shared) migration always
runs; the ``raw/`` + reference relocations additionally run when an
``observations.toml`` is present under the root (reducers), and are skipped for
download-only clients that have neither the file nor a ``raw/`` tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

# ``_meta`` marker recording that this client already migrated (or was born on)
# the #212 layout. Purely an optimization: detection is filesystem-authoritative
# and idempotent, so a lost marker (e.g. after a schema-bump DB rebuild) just
# costs one cheap re-scan.
LAYOUT_MARKER_KEY = "layout_migration"
LAYOUT_MARKER_VALUE = "212"


def load_obs_cfg(root: Path) -> Optional[dict]:
    """Load ``<root>/config/observations.toml`` if present, else None.

    None signals the shared core to skip the ``raw/`` migration entirely (a
    download-only client has no observations table and no raw tree).
    """
    path = Path(root) / "config" / "observations.toml"
    if not path.exists():
        return None
    try:
        import toml
        return toml.load(path)
    except Exception:
        # A malformed/unreadable observations.toml shouldn't block a sync; fall
        # back to products-only migration rather than erroring out.
        return None


def plan(root: Path) -> dict:
    """Dry-run the migration for ``root``; return the shared-core plan dict
    ({pending, counts, warnings})."""
    from campfire_layout.migrate import plan_migration
    return plan_migration(root, obs_cfg=load_obs_cfg(root))


def apply(root: Path, echo: Callable = print) -> dict:
    """Execute the migration for ``root``; return the shared-core result dict."""
    from campfire_layout.migrate import apply_migration
    return apply_migration(root, obs_cfg=load_obs_cfg(root), echo=echo)
