"""Shared data layer for CAMPFIRE.

Provides SQLite-based local storage for object/spectra metadata and
sync state, plus CSV export for human-readable catalog artifacts.
"""

from .store import LocalStore, SchemaMismatchError
from .export import export_catalogs

__all__ = [
    "LocalStore",
    "SchemaMismatchError",
    "export_catalogs",
]
