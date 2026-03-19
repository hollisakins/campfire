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


def meta_dir(data_dir: Path = None) -> Path:
    """Meta directory for SQLite + CSVs."""
    return (data_dir or resolve_data_dir()) / "meta"


def ensure_data_dir(data_dir: Path = None) -> Path:
    """Create data directory with products/ and meta/ subdirectories."""
    d = data_dir or resolve_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "products").mkdir(exist_ok=True)
    (d / "meta").mkdir(exist_ok=True)
    return d
