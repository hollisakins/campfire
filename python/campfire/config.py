"""CAMPFIRE CLI configuration management.

Manages ~/.campfire/config.toml for API URL and data directory.

Data directory resolution order:
  1. Explicit ``data_dir`` in config.toml
  2. ``$CAMPFIRE_ROOT`` environment variable
  3. ``~/campfire`` (visible default)

Layout within the data directory:
  - ``products/<obs>/``  — FITS files (mirrors pipeline structure)
  - ``meta/``            — campfire.db, objects.csv, spectra.csv
"""

import os
import sys
from pathlib import Path
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
import tomli_w

CAMPFIRE_DIR = Path.home() / ".campfire"
CONFIG_FILE = CAMPFIRE_DIR / "config.toml"


def _default_data_dir() -> Path:
    """Resolve the default data directory.

    Checks ``$CAMPFIRE_ROOT`` first, then falls back to ``~/campfire``.
    """
    campfire_root = os.environ.get("CAMPFIRE_ROOT")
    if campfire_root:
        return Path(campfire_root)
    return Path.home() / "campfire"


class Config:
    """Manages ~/.campfire/config.toml for client settings."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or CONFIG_FILE
        self._data = self._load()

    def _load(self) -> dict:
        if not self.config_path.exists():
            return {"settings": {}}
        with open(self.config_path, "rb") as f:
            data = tomllib.load(f)
        # Drop legacy [observations] section if present
        data.pop("observations", None)
        return data

    def _save(self) -> None:
        """Atomic write: temp file + rename."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".tmp")
        try:
            with open(tmp, "wb") as f:
                tomli_w.dump(self._data, f)
            tmp.rename(self.config_path)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise

    @property
    def base_url(self) -> Optional[str]:
        return self._data.get("settings", {}).get("base_url")

    @base_url.setter
    def base_url(self, url: str) -> None:
        self._data.setdefault("settings", {})["base_url"] = url
        self._save()

    @property
    def data_dir(self) -> Path:
        raw = self._data.get("settings", {}).get("data_dir")
        if raw:
            return Path(raw).expanduser()
        return _default_data_dir()

    @data_dir.setter
    def data_dir(self, path: Path) -> None:
        self._data.setdefault("settings", {})["data_dir"] = str(path)
        self._save()

    @property
    def products_dir(self) -> Path:
        """Directory for FITS files: data_dir/products/."""
        return self.data_dir / "products"

    @property
    def meta_dir(self) -> Path:
        """Directory for SQLite + CSVs: data_dir/meta/."""
        return self.data_dir / "meta"

    def exists(self) -> bool:
        return self.config_path.exists()

    def ensure_data_dir(self) -> Path:
        """Create data directory with products/ and meta/ subdirectories."""
        d = self.data_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / "products").mkdir(exist_ok=True)
        (d / "meta").mkdir(exist_ok=True)
        return d
