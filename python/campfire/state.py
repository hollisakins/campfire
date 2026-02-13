"""Local sync state tracking via SQLite.

Maintains a record of which spectra files have been downloaded and their
integrity hashes, enabling incremental sync.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional


class SyncState:
    """SQLite wrapper for tracking sync state at {data_dir}/.campfire_meta/sync_state.db."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS synced_files (
                spectra_id INTEGER PRIMARY KEY,
                object_id TEXT NOT NULL,
                observation TEXT NOT NULL,
                grating TEXT NOT NULL,
                fits_path TEXT NOT NULL,
                local_path TEXT NOT NULL,
                file_hash TEXT,
                file_size INTEGER,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_synced_observation ON synced_files(observation);
            CREATE INDEX IF NOT EXISTS idx_synced_object ON synced_files(object_id);

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                files_downloaded INTEGER DEFAULT 0,
                files_skipped INTEGER DEFAULT 0,
                bytes_downloaded INTEGER DEFAULT 0,
                status TEXT DEFAULT 'in_progress'
            );
        """)
        self._conn.commit()

    def get_synced_files(self, observation: str) -> Dict[int, dict]:
        """Return {spectra_id: row_dict} for an observation."""
        rows = self._conn.execute(
            "SELECT * FROM synced_files WHERE observation = ?", (observation,)
        ).fetchall()
        return {row["spectra_id"]: dict(row) for row in rows}

    def mark_synced(
        self,
        spectra_id: int,
        object_id: str,
        observation: str,
        grating: str,
        fits_path: str,
        local_path: str,
        file_hash: Optional[str],
        file_size: Optional[int],
    ) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO synced_files
            (spectra_id, object_id, observation, grating, fits_path, local_path, file_hash, file_size, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                spectra_id, object_id, observation, grating,
                fits_path, local_path, file_hash, file_size,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def remove_observation(self, observation: str) -> int:
        cursor = self._conn.execute(
            "DELETE FROM synced_files WHERE observation = ?", (observation,)
        )
        self._conn.commit()
        return cursor.rowcount

    def get_observation_stats(self, observation: str) -> dict:
        row = self._conn.execute(
            """SELECT COUNT(*) as synced_count, COALESCE(SUM(file_size), 0) as total_bytes
            FROM synced_files WHERE observation = ?""",
            (observation,),
        ).fetchone()
        return dict(row) if row else {"synced_count": 0, "total_bytes": 0}

    def get_last_sync(self, observation: str) -> Optional[str]:
        row = self._conn.execute(
            """SELECT completed_at FROM sync_log
            WHERE observation = ? AND status = 'completed'
            ORDER BY completed_at DESC LIMIT 1""",
            (observation,),
        ).fetchone()
        return row["completed_at"] if row else None

    def log_sync_start(self, observation: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO sync_log (observation, started_at) VALUES (?, ?)",
            (observation, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return cursor.lastrowid

    def log_sync_complete(
        self, log_id: int, files_downloaded: int, files_skipped: int, bytes_downloaded: int
    ) -> None:
        self._conn.execute(
            """UPDATE sync_log SET completed_at = ?, files_downloaded = ?,
            files_skipped = ?, bytes_downloaded = ?, status = 'completed'
            WHERE id = ?""",
            (
                datetime.now(timezone.utc).isoformat(),
                files_downloaded, files_skipped, bytes_downloaded, log_id,
            ),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
