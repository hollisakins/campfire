"""CAMPFIRE CLI configuration management.

Manages ~/.campfire/config (TOML format) for data directory and tracked observations.
"""

import sys
from pathlib import Path
from typing import List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
import tomli_w

CAMPFIRE_DIR = Path.home() / ".campfire"
CONFIG_FILE = CAMPFIRE_DIR / "config.toml"
DEFAULT_DATA_DIR = CAMPFIRE_DIR / "data"


class Config:
    """Manages ~/.campfire/config for sync settings and tracked observations."""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or CONFIG_FILE
        self._data = self._load()

    def _load(self) -> dict:
        if not self.config_path.exists():
            return {"settings": {}, "observations": {"tracked": []}}
        with open(self.config_path, "rb") as f:
            return tomllib.load(f)

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
        raw = self._data.get("settings", {}).get("data_dir", str(DEFAULT_DATA_DIR))
        return Path(raw).expanduser()

    @data_dir.setter
    def data_dir(self, path: Path) -> None:
        self._data.setdefault("settings", {})["data_dir"] = str(path)
        self._save()

    @property
    def tracked_observations(self) -> List[str]:
        return list(self._data.get("observations", {}).get("tracked", []))

    def add_observation(self, obs_name: str) -> None:
        tracked = self.tracked_observations
        if obs_name not in tracked:
            tracked.append(obs_name)
            self._data.setdefault("observations", {})["tracked"] = tracked
            self._save()

    def remove_observation(self, obs_name: str) -> bool:
        tracked = self.tracked_observations
        if obs_name in tracked:
            tracked.remove(obs_name)
            self._data.setdefault("observations", {})["tracked"] = tracked
            self._save()
            return True
        return False

    def exists(self) -> bool:
        return self.config_path.exists()

    def ensure_data_dir(self) -> Path:
        """Create data directory and .campfire_meta subdirectory."""
        d = self.data_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / ".campfire_meta").mkdir(exist_ok=True)
        return d
