"""Shared data layer for CAMPFIRE.

Provides SQLite-based local storage for object/spectra metadata and
sync state, plus CSV export for human-readable catalog artifacts.
"""

from .store import LocalStore
from .export import export_catalogs

__all__ = [
    "LocalStore",
    "export_catalogs",
]
