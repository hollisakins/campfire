"""CAMPFIRE data directory resolution.

Data directory resolution order:
  1. ``$CAMPFIRE_ROOT`` environment variable
  2. ``~/campfire``

Layout within the data directory:
  - ``products/<obs>/``  — FITS files (mirrors pipeline structure)
  - ``meta/``            — campfire.db, objects.csv, spectra.csv

Credentials are stored separately in ``~/.campfire/credentials``.
"""

import os
from pathlib import Path

from campfire_layout import key_to_relpath


CAMPFIRE_DIR = Path.home() / ".campfire"


def resolve_data_dir() -> Path:
    """Resolve the root data directory.

    Returns ``$CAMPFIRE_ROOT`` if set, otherwise ``~/campfire``.
    """
    campfire_root = os.environ.get("CAMPFIRE_ROOT")
    if campfire_root:
        return Path(campfire_root)
    return Path.home() / "campfire"


def products_dir(data_dir: Path = None) -> Path:
    """Products directory for FITS files."""
    return (data_dir or resolve_data_dir()) / "products"


def products_relpath(storage_key: str) -> str:
    """Local path of a downloaded object relative to the products dir.

    Maps a storage key (e.g. ``spectra/<obs>/x_spec.fits``) to its place under
    ``products/`` (e.g. ``nirspec/<obs>/x_spec.fits``) via the shared layout
    contract. This is the single source of the download-destination layout —
    routing through it fixes the prior client/pipeline drift where the client
    wrote to ``products/<obs>/`` (missing the PR-4 ``nirspec/`` segment).
    """
    rel = key_to_relpath(storage_key)
    prefix = "products/"
    return rel[len(prefix):] if rel.startswith(prefix) else rel


def meta_dir(data_dir: Path = None) -> Path:
    """Meta directory for SQLite + CSVs."""
    return (data_dir or resolve_data_dir()) / "meta"


def cutouts_dir(data_dir: Path = None) -> Path:
    """Cutouts cache directory for PNG thumbnails."""
    return (data_dir or resolve_data_dir()) / "cutouts"


def ensure_data_dir(data_dir: Path = None) -> Path:
    """Create data directory with products/, meta/, and cutouts/ subdirectories."""
    d = data_dir or resolve_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "products").mkdir(exist_ok=True)
    (d / "meta").mkdir(exist_ok=True)
    (d / "cutouts").mkdir(exist_ok=True)
    return d
