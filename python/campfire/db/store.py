"""SQLite-based local store for CAMPFIRE metadata and sync state.

Replaces the old ``SyncState`` class and absorbs catalog storage. The database
stores full target and spectra metadata (populated during sync) and tracks
which FITS files have been downloaded locally.
"""

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union


# Schema version — bump when tables change
SCHEMA_VERSION = 7

# Column lists used by both store and export
TARGET_COLUMNS = [
    "id", "target_id", "program_slug", "program_name", "field", "observation",
    "ra", "dec", "redshift", "redshift_auto", "redshift_inspected",
    "redshift_quality", "spectral_features", "object_flags", "dq_flags",
    "max_snr", "max_exposure_time",
    "last_inspected_at", "last_inspected_by", "created_at", "updated_at",
]

SPECTRA_COLUMNS = [
    "spectra_id", "target_id", "grating", "fits_path", "file_hash",
    "file_size", "signal_to_noise", "exposure_time", "reduction_version",
    "local_path",
]

# Columns exported to targets.csv (subset, user-friendly order)
TARGET_EXPORT_COLUMNS = [
    "target_id", "program_slug", "program_name", "field", "observation",
    "ra", "dec", "redshift", "redshift_auto", "redshift_inspected",
    "redshift_quality", "spectral_features", "object_flags", "dq_flags",
    "max_snr", "max_exposure_time",
    "last_inspected_at", "last_inspected_by", "created_at", "updated_at",
]

SPECTRA_EXPORT_COLUMNS = [
    "spectra_id", "target_id", "grating", "fits_path", "file_hash",
    "file_size", "signal_to_noise", "exposure_time", "reduction_version",
    "local_path",
]

# Columns for the sky-objects table (cross-program groupings)
OBJECT_COLUMNS = [
    "id", "object_id", "field", "ra", "dec",
    "n_targets", "n_spectra",
    "programs", "gratings",
    "max_snr", "max_exposure_time",
    "best_redshift", "best_redshift_quality",
    "member_target_ids",
    "created_at", "updated_at",
]

OBJECT_EXPORT_COLUMNS = [
    "object_id", "field", "ra", "dec",
    "best_redshift", "best_redshift_quality",
    "n_targets", "n_spectra",
    "programs", "gratings",
    "max_snr", "max_exposure_time",
    "member_target_ids",
]


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS targets (
    id INTEGER PRIMARY KEY,
    target_id TEXT UNIQUE NOT NULL,
    program_slug TEXT,
    program_name TEXT,
    field TEXT,
    observation TEXT,
    ra REAL,
    dec REAL,
    redshift REAL,
    redshift_auto REAL,
    redshift_inspected REAL,
    redshift_quality INTEGER DEFAULT 0,
    spectral_features INTEGER DEFAULT 0,
    object_flags INTEGER DEFAULT 0,
    dq_flags INTEGER DEFAULT 0,
    max_snr REAL,
    max_exposure_time REAL,
    last_inspected_at TEXT,
    last_inspected_by TEXT,
    created_at TEXT,
    updated_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_targets_target_id ON targets(target_id);
CREATE INDEX IF NOT EXISTS idx_targets_observation ON targets(observation);
CREATE INDEX IF NOT EXISTS idx_targets_field ON targets(field);
CREATE INDEX IF NOT EXISTS idx_targets_redshift ON targets(redshift);
CREATE INDEX IF NOT EXISTS idx_targets_object_flags ON targets(object_flags);

CREATE TABLE IF NOT EXISTS objects (
    id INTEGER PRIMARY KEY,
    object_id TEXT UNIQUE NOT NULL,
    field TEXT,
    ra REAL,
    dec REAL,
    n_targets INTEGER DEFAULT 0,
    n_spectra INTEGER DEFAULT 0,
    programs TEXT,
    gratings TEXT,
    max_snr REAL,
    max_exposure_time REAL,
    best_redshift REAL,
    best_redshift_quality INTEGER DEFAULT 0,
    member_target_ids TEXT,
    created_at TEXT,
    updated_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_objects_object_id ON objects(object_id);
CREATE INDEX IF NOT EXISTS idx_objects_field ON objects(field);

CREATE TABLE IF NOT EXISTS spectra (
    spectra_id INTEGER PRIMARY KEY,
    target_id TEXT NOT NULL,
    grating TEXT NOT NULL,
    fits_path TEXT,
    file_hash TEXT,
    file_size INTEGER,
    signal_to_noise REAL,
    exposure_time REAL,
    reduction_version TEXT,
    local_path TEXT,
    local_file_hash TEXT,
    local_file_mtime REAL,
    local_file_size INTEGER,
    synced_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_spectra_target_id ON spectra(target_id);
CREATE INDEX IF NOT EXISTS idx_spectra_grating ON spectra(grating);
CREATE INDEX IF NOT EXISTS idx_spectra_target_grating ON spectra(target_id, grating);

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
"""


class LocalStore:
    """SQLite database manager for local CAMPFIRE metadata and sync state.

    Stores full object/spectra metadata from the API and tracks which
    FITS files have been downloaded locally.

    Parameters
    ----------
    db_path : Path
        Path to the SQLite database file. Created if it doesn't exist.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Check for old sync_state.db to migrate from
        self._maybe_migrate_from_old_db()

        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=OFF")

        # Register math functions once for cone-search distance calculations
        self._conn.create_function("COS", 1, math.cos)
        self._conn.create_function("SQRT", 1, math.sqrt)
        self._conn.create_function("RADIANS", 1, math.radians)
        self._conn.create_function("POWER", 2, math.pow)

        self._init_schema()

    def _maybe_migrate_from_old_db(self) -> None:
        """If sync_state.db exists but campfire.db doesn't, migrate."""
        old_path = self.db_path.parent / "sync_state.db"
        if old_path.exists() and not self.db_path.exists():
            # Rename the old database
            old_path.rename(self.db_path)

    def _init_schema(self) -> None:
        """Create tables if they don't exist, and migrate if needed."""
        # Detect existing database state
        has_synced_files = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='synced_files'"
        ).fetchone() is not None
        has_targets = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='targets'"
        ).fetchone() is not None
        has_objects = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='objects'"
        ).fetchone() is not None

        if has_synced_files and not has_targets and not has_objects:
            # Migrate from v1 (sync-only) to full catalog schema
            self._migrate_from_v1()
        elif not has_targets and not has_objects:
            # Fresh install
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        else:
            # Existing schema — run any pending migrations
            version = self._get_schema_version()
            if version < SCHEMA_VERSION:
                if version < 3:
                    self._migrate_from_v2()
                if version < 4:
                    self._migrate_from_v3()
                if version < 5:
                    self._migrate_from_v4()
                if version < 6:
                    self._migrate_from_v5()
                if version < 7:
                    self._migrate_from_v6()

    def _get_schema_version(self) -> int:
        """Get current schema version from _meta table."""
        try:
            row = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row else 1
        except Exception:
            return 1

    def _migrate_from_v1(self) -> None:
        """Migrate from old synced_files-only schema to full catalog schema."""
        # Create the new tables
        self._conn.executescript(_SCHEMA_SQL)

        # Copy synced_files data into the new spectra table
        self._conn.execute("""
            INSERT OR IGNORE INTO spectra
                (spectra_id, target_id, grating, fits_path, file_hash, file_size,
                 local_path, synced_at)
            SELECT
                spectra_id, object_id, grating, fits_path, file_hash, file_size,
                local_path, synced_at
            FROM synced_files
        """)

        # Drop the old table
        self._conn.execute("DROP TABLE IF EXISTS synced_files")

        # Mark schema version
        self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate_from_v2(self) -> None:
        """Migrate from v2 to v3: add local_file_hash column."""
        # Add the new column
        try:
            self._conn.execute(
                "ALTER TABLE spectra ADD COLUMN local_file_hash TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Copy file_hash to local_file_hash for already-downloaded files
        # (in v2, mark_synced wrote the download hash into file_hash)
        self._conn.execute("""
            UPDATE spectra SET local_file_hash = file_hash
            WHERE local_path IS NOT NULL AND file_hash IS NOT NULL
        """)

        self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate_from_v3(self) -> None:
        """Migrate from v3 to v4: add last_inspected_by column.

        At v3 the table is still called 'objects' (renamed in v5→v6).
        """
        table = "objects"
        if not self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='objects'"
        ).fetchone():
            table = "targets"
        try:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN last_inspected_by TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists

        self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate_from_v4(self) -> None:
        """Migrate from v4 to v5: add local_file_mtime and local_file_size to spectra."""
        for col, col_type in [
            ("local_file_mtime", "REAL"),
            ("local_file_size", "INTEGER"),
        ]:
            try:
                self._conn.execute(
                    f"ALTER TABLE spectra ADD COLUMN {col} {col_type}"
                )
            except sqlite3.OperationalError:
                pass  # Column already exists

        self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate_from_v5(self) -> None:
        """Migrate from v5 to v6: rename objects → targets, object_id → target_id."""
        has_objects = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='objects'"
        ).fetchone()

        if has_objects:
            # If targets table also exists (e.g. from _SCHEMA_SQL), drop the empty one
            has_targets = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='targets'"
            ).fetchone()
            if has_targets:
                self._conn.execute("DROP TABLE targets")

            # Rename table and primary column
            self._conn.execute("ALTER TABLE objects RENAME TO targets")
            self._conn.execute(
                "ALTER TABLE targets RENAME COLUMN object_id TO target_id"
            )

            # Recreate indexes with new names
            for idx in ['idx_objects_object_id', 'idx_objects_observation',
                        'idx_objects_field', 'idx_objects_redshift',
                        'idx_objects_object_flags']:
                self._conn.execute(f"DROP INDEX IF EXISTS {idx}")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_targets_target_id ON targets(target_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_targets_observation ON targets(observation)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_targets_field ON targets(field)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_targets_redshift ON targets(redshift)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_targets_object_flags ON targets(object_flags)"
            )

        # Rename spectra.object_id → target_id if the old column exists
        has_object_id_col = self._conn.execute(
            "SELECT 1 FROM pragma_table_info('spectra') WHERE name = 'object_id'"
        ).fetchone()
        if has_object_id_col:
            self._conn.execute(
                "ALTER TABLE spectra RENAME COLUMN object_id TO target_id"
            )
            self._conn.execute("DROP INDEX IF EXISTS idx_spectra_object_id")
            self._conn.execute("DROP INDEX IF EXISTS idx_spectra_object_grating")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_spectra_target_id ON spectra(target_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_spectra_target_grating ON spectra(target_id, grating)"
            )

        # Safety net: ensure last_inspected_by exists on targets
        try:
            self._conn.execute(
                "ALTER TABLE targets ADD COLUMN last_inspected_by TEXT"
            )
        except sqlite3.OperationalError:
            pass  # Already exists

        self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate_from_v6(self) -> None:
        """Migrate from v6 to v7: add objects table for sky-position groupings."""
        # Create objects table
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY,
                object_id TEXT UNIQUE NOT NULL,
                field TEXT,
                ra REAL,
                dec REAL,
                n_targets INTEGER DEFAULT 0,
                n_spectra INTEGER DEFAULT 0,
                programs TEXT,
                gratings TEXT,
                max_snr REAL,
                max_exposure_time REAL,
                best_redshift REAL,
                best_redshift_quality INTEGER DEFAULT 0,
                member_target_ids TEXT,
                created_at TEXT,
                updated_at TEXT,
                _synced_at TEXT
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_object_id ON objects(object_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_objects_field ON objects(field)"
        )

        self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    # -------------------------------------------------------------------------
    # Catalog operations
    # -------------------------------------------------------------------------

    def upsert_targets(self, objects_data: List[dict]) -> Tuple[int, int]:
        """Insert or update targets and their spectra from API response dicts.

        Parameters
        ----------
        objects_data : list of dict
            Targets from the /api/v1/targets endpoint, each with nested
            'spectra' list.

        Returns
        -------
        tuple of (target_count, spectra_count)
        """
        now = datetime.now(timezone.utc).isoformat()
        obj_count = 0
        spec_count = 0

        for obj in objects_data:
            # Upsert the target
            # Accept both "target_id" (new schema) and "object_id" (API compat)
            target_id = obj.get("target_id") or obj.get("object_id")
            self._conn.execute("""
                INSERT INTO targets
                    (id, target_id, program_slug, program_name, field, observation,
                     ra, dec, redshift, redshift_auto, redshift_inspected,
                     redshift_quality, spectral_features, object_flags, dq_flags,
                     max_snr, max_exposure_time,
                     last_inspected_at, last_inspected_by,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_id) DO UPDATE SET
                    program_slug=excluded.program_slug,
                    program_name=excluded.program_name,
                    field=excluded.field,
                    observation=excluded.observation,
                    ra=excluded.ra, dec=excluded.dec,
                    redshift=excluded.redshift,
                    redshift_auto=excluded.redshift_auto,
                    redshift_inspected=excluded.redshift_inspected,
                    redshift_quality=excluded.redshift_quality,
                    spectral_features=excluded.spectral_features,
                    object_flags=excluded.object_flags,
                    dq_flags=excluded.dq_flags,
                    max_snr=excluded.max_snr,
                    max_exposure_time=excluded.max_exposure_time,
                    last_inspected_at=excluded.last_inspected_at,
                    last_inspected_by=excluded.last_inspected_by,
                    updated_at=excluded.updated_at,
                    _synced_at=excluded._synced_at
            """, (
                obj.get("id"),
                target_id,
                obj.get("program_slug"),
                obj.get("program_name"),
                obj.get("field"),
                obj.get("observation"),
                obj.get("ra"),
                obj.get("dec"),
                obj.get("redshift"),
                obj.get("redshift_auto"),
                obj.get("redshift_inspected"),
                obj.get("redshift_quality", 0),
                obj.get("spectral_features", 0),
                obj.get("object_flags", 0),
                obj.get("dq_flags", 0),
                obj.get("max_snr"),
                obj.get("max_exposure_time"),
                obj.get("last_inspected_at"),
                obj.get("last_inspected_by"),
                obj.get("created_at"),
                obj.get("updated_at"),
                now,
            ))
            obj_count += 1

            # Upsert spectra — preserve local_path and synced_at if already set
            obs = obj.get("observation", "")
            for spec in obj.get("spectra", []):
                spec_id = spec.get("id")
                if spec_id is None:
                    continue

                filename = Path(spec.get("fits_path", "")).name
                inferred_local_path = f"{obs}/{filename}" if obs else filename

                spec_target_id = spec.get("target_id") or spec.get("object_id") or target_id

                self._conn.execute("""
                    INSERT INTO spectra
                        (spectra_id, target_id, grating, fits_path, file_hash,
                         file_size, signal_to_noise, exposure_time,
                         reduction_version, _synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(spectra_id) DO UPDATE SET
                        target_id=excluded.target_id,
                        grating=excluded.grating,
                        fits_path=excluded.fits_path,
                        file_hash=excluded.file_hash,
                        file_size=COALESCE(excluded.file_size, spectra.file_size),
                        signal_to_noise=excluded.signal_to_noise,
                        exposure_time=excluded.exposure_time,
                        reduction_version=excluded.reduction_version,
                        _synced_at=excluded._synced_at
                """, (
                    spec_id,
                    spec_target_id,
                    spec.get("grating"),
                    spec.get("fits_path"),
                    spec.get("file_hash"),
                    spec.get("file_size"),
                    spec.get("signal_to_noise"),
                    spec.get("exposure_time"),
                    spec.get("reduction_version"),
                    now,
                ))
                spec_count += 1

        self._conn.commit()
        return obj_count, spec_count

    def query_targets(
        self,
        programs: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[int]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        spectral_features: Optional[dict] = None,
        object_flags: Optional[dict] = None,
        dq_flags: Optional[dict] = None,
        inspected_only: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        sort: str = "target_id",
        sort_dir: str = "asc",
        limit: Optional[int] = None,
        offset: int = 0,
        **kwargs,
    ) -> List[dict]:
        """Query targets from local SQLite store.

        Supports the same filter parameters as the remote API.

        Parameters
        ----------
        programs : list of str, optional
            Program slugs to filter by.
        fields, observations : list of str, optional
            Filter by field or observation name.
        redshift_range : tuple of (min, max), optional
        redshift_quality : list of int, optional
        max_snr_range : tuple of (min, max), optional
        spectral_features, object_flags, dq_flags : dict, optional
            Flag filter dicts with keys 'include_any', 'include_all', 'exclude'.
        inspected_only : bool, optional
        search : str, optional
            Text search on target_id (LIKE match).
        cone_search : tuple of (ra, dec, radius_arcsec), optional
        sort : str
            Column to sort by.
        sort_dir : str
            'asc' or 'desc'.
        limit, offset : int

        Returns
        -------
        list of dict
            Target records matching the filters.
        """
        where_clauses = []
        params = []

        if programs:
            placeholders = ",".join("?" * len(programs))
            where_clauses.append(f"o.program_slug IN ({placeholders})")
            params.extend(programs)

        if fields:
            placeholders = ",".join("?" * len(fields))
            where_clauses.append(f"o.field IN ({placeholders})")
            params.extend(fields)

        if observations:
            placeholders = ",".join("?" * len(observations))
            where_clauses.append(f"o.observation IN ({placeholders})")
            params.extend(observations)

        if redshift_range:
            where_clauses.append("o.redshift >= ? AND o.redshift <= ?")
            params.extend(redshift_range)

        if redshift_quality:
            placeholders = ",".join("?" * len(redshift_quality))
            where_clauses.append(f"o.redshift_quality IN ({placeholders})")
            params.extend(redshift_quality)

        if max_snr_range:
            where_clauses.append("o.max_snr >= ? AND o.max_snr <= ?")
            params.extend(max_snr_range)

        if inspected_only:
            where_clauses.append("o.redshift_quality > 0")

        if search:
            where_clauses.append("o.target_id LIKE ?")
            params.append(f"%{search}%")

        # Flag filters
        for flag_col, flag_filter in [
            ("o.spectral_features", spectral_features),
            ("o.object_flags", object_flags),
            ("o.dq_flags", dq_flags),
        ]:
            if flag_filter:
                if isinstance(flag_filter, dict):
                    inc_any = flag_filter.get("include_any", 0)
                    inc_all = flag_filter.get("include_all", 0)
                    exclude = flag_filter.get("exclude", 0)
                else:
                    # FlagQuery object
                    inc_any = getattr(flag_filter, "include_any", 0)
                    inc_all = getattr(flag_filter, "include_all", 0)
                    exclude = getattr(flag_filter, "exclude", 0)

                if inc_any:
                    where_clauses.append(f"({flag_col} & ?) != 0")
                    params.append(inc_any)
                if inc_all:
                    where_clauses.append(f"({flag_col} & ?) = ?")
                    params.extend([inc_all, inc_all])
                if exclude:
                    where_clauses.append(f"({flag_col} & ?) = 0")
                    params.append(exclude)

        # Cone search via Haversine
        order_by_distance = False
        if cone_search:
            ra, dec, radius_arcsec = cone_search
            radius_deg = radius_arcsec / 3600.0
            # Pre-filter with bounding box
            cos_dec = math.cos(math.radians(dec))
            ra_margin = radius_deg / max(cos_dec, 0.01)
            where_clauses.append("o.ra BETWEEN ? AND ?")
            params.extend([ra - ra_margin, ra + ra_margin])
            where_clauses.append("o.dec BETWEEN ? AND ?")
            params.extend([dec - radius_deg, dec + radius_deg])
            order_by_distance = True

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # Validate sort column
        allowed_sorts = {
            "target_id", "ra", "dec", "redshift", "redshift_quality",
            "field", "observation", "max_snr", "max_exposure_time",
        }
        if sort not in allowed_sorts:
            sort = "target_id"
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"

        order_clause = f"o.{sort} {sort_dir}"
        if order_by_distance and sort == "target_id":
            # Default to distance sort for cone searches
            order_clause = "distance ASC"

        # Build the query
        if cone_search:
            ra, dec, _ = cone_search
            # Haversine-based distance in degrees
            distance_expr = f"""
                (2 * DEGREES(ASIN(SQRT(
                    POWER(SIN(RADIANS((o.dec - {dec}) / 2)), 2) +
                    COS(RADIANS({dec})) * COS(RADIANS(o.dec)) *
                    POWER(SIN(RADIANS((o.ra - {ra}) / 2)), 2)
                )))) AS distance
            """
            # SQLite doesn't have RADIANS/DEGREES/ASIN natively — use Python functions
            # Instead, do simple great-circle approximation
            distance_expr = f"""
                SQRT(
                    POWER((o.ra - {ra}) * COS(RADIANS({dec})), 2) +
                    POWER(o.dec - {dec}, 2)
                ) AS distance
            """
        else:
            distance_expr = "NULL AS distance"

        sql = f"""
            SELECT o.*, {distance_expr}
            FROM targets o
            WHERE {where_sql}
            ORDER BY {order_clause}
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)

        rows = self._conn.execute(sql, params).fetchall()

        # Convert to list of dicts and attach spectra
        cone_radius_deg = cone_search[2] / 3600.0 if cone_search else None
        results = []
        for row in rows:
            obj = dict(row)
            obj.pop("_synced_at", None)
            if not cone_search:
                obj.pop("distance", None)
            elif obj.get("distance") is not None and obj["distance"] > cone_radius_deg:
                continue

            # Fetch associated spectra
            spectra_rows = self._conn.execute(
                """SELECT spectra_id as id, target_id, grating, fits_path,
                          signal_to_noise, exposure_time, reduction_version
                   FROM spectra WHERE target_id = ?""",
                (obj["target_id"],),
            ).fetchall()
            obj["spectra"] = [dict(s) for s in spectra_rows]

            results.append(obj)

        return results

    def count_targets(self, **filters) -> int:
        """Count targets matching filters (same params as query_targets)."""
        results = self.query_targets(**filters)
        return len(results)

    def get_object(self, target_id: str) -> Optional[dict]:
        """Get a single target by ID."""
        row = self._conn.execute(
            "SELECT * FROM targets WHERE target_id = ?", (target_id,)
        ).fetchone()
        if not row:
            return None
        obj = dict(row)
        obj.pop("_synced_at", None)

        spectra_rows = self._conn.execute(
            """SELECT spectra_id as id, target_id, grating, fits_path,
                      signal_to_noise, exposure_time, reduction_version
               FROM spectra WHERE target_id = ?""",
            (target_id,),
        ).fetchall()
        obj["spectra"] = [dict(s) for s in spectra_rows]
        return obj

    def get_spectra_for_object(
        self, target_id: str, grating: Optional[str] = None
    ) -> List[dict]:
        """Get spectra for a target, optionally filtered by grating."""
        if grating:
            rows = self._conn.execute(
                "SELECT * FROM spectra WHERE target_id = ? AND grating = ?",
                (target_id, grating),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM spectra WHERE target_id = ?", (target_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_distinct_values(self, column: str) -> list:
        """Get distinct values for a column (for metadata queries)."""
        allowed = {"field", "observation", "grating", "program_slug", "program_name"}
        if column == "grating":
            rows = self._conn.execute(
                "SELECT DISTINCT grating FROM spectra ORDER BY grating"
            ).fetchall()
            return [r[0] for r in rows if r[0]]

        if column not in allowed:
            return []
        rows = self._conn.execute(
            f"SELECT DISTINCT {column} FROM targets ORDER BY {column}"
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def get_synced_observations(self) -> List[str]:
        """Get list of observations that have been synced (have targets in DB)."""
        rows = self._conn.execute(
            "SELECT DISTINCT observation FROM targets ORDER BY observation"
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def get_observation_summary(self) -> List[dict]:
        """Get per-observation summary with program, field, counts, and download status."""
        rows = self._conn.execute("""
            SELECT
                o.observation,
                o.program_slug,
                o.field,
                COUNT(DISTINCT o.target_id) AS target_count,
                COUNT(DISTINCT s.spectra_id) AS spectrum_count,
                COUNT(DISTINCT CASE WHEN s.local_path IS NOT NULL
                      THEN s.spectra_id END) AS downloaded_count
            FROM targets o
            LEFT JOIN spectra s ON o.target_id = s.target_id
            GROUP BY o.observation
            ORDER BY o.observation
        """).fetchall()
        return [dict(r) for r in rows]

    def get_last_synced_at(self) -> Optional[str]:
        """Get the most recent _synced_at timestamp across all objects."""
        row = self._conn.execute(
            "SELECT MAX(_synced_at) FROM targets"
        ).fetchone()
        return row[0] if row and row[0] else None

    def get_max_updated_at(self) -> Optional[str]:
        """Get the most recent server-side updated_at across all targets.

        Used for incremental sync — avoids client/server clock skew by
        using the server's own timestamp as the ``updated_since`` marker.
        """
        row = self._conn.execute(
            "SELECT MAX(updated_at) FROM targets"
        ).fetchone()
        return row[0] if row and row[0] else None

    # -------------------------------------------------------------------------
    # Sync state operations (migrated from SyncState)
    # -------------------------------------------------------------------------

    def get_synced_files(self, observation: str) -> Dict[int, dict]:
        """Return {spectra_id: row_dict} for locally downloaded files in an observation.

        The returned dicts include ``local_file_hash`` (the hash of the file on
        disk) which ``compute_download_plan`` compares against the manifest's
        ``file_hash`` to detect updated files.
        """
        rows = self._conn.execute("""
            SELECT s.spectra_id, s.target_id, s.grating, s.fits_path,
                   s.local_path, s.local_file_hash, s.file_hash,
                   s.file_size, s.synced_at
            FROM spectra s
            JOIN targets o ON s.target_id = o.target_id
            WHERE o.observation = ? AND s.local_path IS NOT NULL
        """, (observation,)).fetchall()

        result = {}
        for row in rows:
            d = dict(row)
            # compute_download_plan compares local["file_hash"] against manifest
            # After the v3 schema split, the download hash is in local_file_hash
            d["file_hash"] = d.get("local_file_hash")
            result[row["spectra_id"]] = d

        # Also check for spectra with local_path matching the observation dir
        # (legacy data from before full catalog sync)
        legacy_rows = self._conn.execute("""
            SELECT spectra_id, target_id, grating, fits_path,
                   local_path, local_file_hash, file_hash, file_size, synced_at
            FROM spectra
            WHERE local_path IS NOT NULL AND local_path LIKE ?
        """, (f"{observation}/%",)).fetchall()
        for row in legacy_rows:
            if row["spectra_id"] not in result:
                d = dict(row)
                d["file_hash"] = d.get("local_file_hash")
                result[row["spectra_id"]] = d

        return result

    def verify_local_files(self, products_dir: Path, observation: Optional[str] = None) -> dict:
        """Reconcile database sync state with the local filesystem.

        Performs three checks:

        1. **Missing files**: spectra marked as downloaded but no longer
           on disk → clears ``local_path`` so they are re-downloaded.
        2. **Modified files**: spectra on disk whose mtime or size differs
           from stored values → re-hashes and updates ``local_file_hash``.
        3. **Discovered files**: spectra not marked as downloaded but the
           expected file exists on disk (e.g., from the pipeline) →
           computes hash and sets ``local_path``.

        Parameters
        ----------
        products_dir : Path
            Root products directory (contains ``<obs>/`` subdirs).
        observation : str, optional
            Limit check to a single observation. If None, checks all.

        Returns
        -------
        dict
            ``{"cleared": int, "rehashed": int, "discovered": int}``
        """
        from ..sync import compute_file_hash

        now = datetime.now(timezone.utc).isoformat()
        obs_filter = "AND o.observation = ?" if observation else ""
        obs_params: tuple = (observation,) if observation else ()

        # 1. Check tracked files: clear missing, re-hash modified
        tracked_rows = self._conn.execute(f"""
            SELECT s.spectra_id, s.local_path, s.local_file_mtime,
                   s.local_file_size, s.local_file_hash
            FROM spectra s
            JOIN targets o ON s.target_id = o.target_id
            WHERE s.local_path IS NOT NULL {obs_filter}
        """, obs_params).fetchall()

        cleared = 0
        rehashed = 0
        for row in tracked_rows:
            full_path = products_dir / row["local_path"]
            if not full_path.exists():
                self._conn.execute(
                    """UPDATE spectra SET local_path = NULL, local_file_hash = NULL,
                       local_file_mtime = NULL, local_file_size = NULL, synced_at = NULL
                       WHERE spectra_id = ?""",
                    (row["spectra_id"],),
                )
                cleared += 1
            else:
                st = full_path.stat()
                stored_mtime = row["local_file_mtime"]
                stored_size = row["local_file_size"]
                if (stored_mtime is not None
                        and stored_size is not None
                        and abs(st.st_mtime - stored_mtime) < 0.001
                        and st.st_size == stored_size):
                    continue  # Fast path: unchanged
                # Re-hash — mtime/size changed or not yet tracked (pre-v5)
                new_hash = compute_file_hash(full_path)
                self._conn.execute(
                    """UPDATE spectra SET local_file_hash = ?,
                       local_file_mtime = ?, local_file_size = ?
                       WHERE spectra_id = ?""",
                    (new_hash, st.st_mtime, st.st_size, row["spectra_id"]),
                )
                rehashed += 1

        # 2. Discover files that exist but aren't tracked
        untracked_rows = self._conn.execute(f"""
            SELECT s.spectra_id, s.fits_path, s.file_hash, o.observation
            FROM spectra s
            JOIN targets o ON s.target_id = o.target_id
            WHERE s.local_path IS NULL AND s.fits_path IS NOT NULL {obs_filter}
        """, obs_params).fetchall()

        discovered = 0
        for row in untracked_rows:
            filename = Path(row["fits_path"]).name
            obs_name = row["observation"]
            local_path = products_dir / obs_name / filename
            if local_path.exists():
                rel_path = f"{obs_name}/{filename}"
                st = local_path.stat()
                actual_hash = compute_file_hash(local_path)
                self._conn.execute(
                    """UPDATE spectra SET local_path = ?, local_file_hash = ?,
                       local_file_mtime = ?, local_file_size = ?, synced_at = ?
                       WHERE spectra_id = ?""",
                    (rel_path, actual_hash, st.st_mtime, st.st_size, now, row["spectra_id"]),
                )
                discovered += 1

        if cleared or discovered or rehashed:
            self._conn.commit()
        return {"cleared": cleared, "rehashed": rehashed, "discovered": discovered}

    def mark_synced(
        self,
        spectra_id: int,
        target_id: str,
        observation: str,
        grating: str,
        fits_path: str,
        local_path: str,
        file_hash: Optional[str],
        file_size: Optional[int],
        local_file_mtime: Optional[float] = None,
        local_file_size: Optional[int] = None,
    ) -> None:
        """Record that a file has been downloaded locally.

        The ``file_hash`` parameter is stored as ``local_file_hash`` — the
        hash of the downloaded file on disk. The server-authoritative
        ``file_hash`` column is only set by ``upsert_targets()``.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            INSERT INTO spectra
                (spectra_id, target_id, grating, fits_path, local_path,
                 local_file_hash, file_size, local_file_mtime, local_file_size,
                 synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spectra_id) DO UPDATE SET
                local_path = excluded.local_path,
                local_file_hash = excluded.local_file_hash,
                file_size = excluded.file_size,
                local_file_mtime = excluded.local_file_mtime,
                local_file_size = excluded.local_file_size,
                synced_at = excluded.synced_at
        """, (
            spectra_id, target_id, grating, fits_path,
            local_path, file_hash, file_size, local_file_mtime,
            local_file_size, now,
        ))
        self._conn.commit()

    def get_stale_files(self) -> List[dict]:
        """Return locally downloaded files whose server hash differs from local.

        After a metadata sync, the server's ``file_hash`` may have changed
        (e.g., reprocessed data). This method finds files where the local
        copy is outdated.
        """
        rows = self._conn.execute("""
            SELECT s.spectra_id, s.target_id, s.grating, s.fits_path,
                   s.local_path, s.file_hash AS server_hash,
                   s.local_file_hash, o.observation
            FROM spectra s
            JOIN targets o ON s.target_id = o.target_id
            WHERE s.local_path IS NOT NULL
              AND s.file_hash IS NOT NULL
              AND s.local_file_hash IS NOT NULL
              AND s.file_hash != s.local_file_hash
        """).fetchall()
        return [dict(r) for r in rows]

    def purge_stale_rows(self, sync_timestamp: str) -> dict:
        """Delete objects and spectra not seen in the latest full sync.

        After a full sync, rows with ``_synced_at < sync_timestamp`` were
        not in the server response — meaning they were deleted on the server.

        Only call this after a **full** sync (not incremental).

        Parameters
        ----------
        sync_timestamp : str
            ISO 8601 timestamp from the start of the sync.

        Returns
        -------
        dict
            ``{"purged_objects": int, "purged_spectra": int,
              "orphaned_files": list}``
        """
        # Find spectra that will be purged and have local files
        orphaned = self._conn.execute(
            """SELECT local_path FROM spectra
               WHERE _synced_at < ? AND local_path IS NOT NULL""",
            (sync_timestamp,),
        ).fetchall()
        orphaned_files = [r["local_path"] for r in orphaned]

        cursor_s = self._conn.execute(
            "DELETE FROM spectra WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged_spectra = cursor_s.rowcount

        cursor_o = self._conn.execute(
            "DELETE FROM targets WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged_objects = cursor_o.rowcount

        self._conn.commit()
        return {
            "purged_objects": purged_objects,
            "purged_spectra": purged_spectra,
            "orphaned_files": orphaned_files,
        }

    def get_pending_downloads(
        self,
        observations: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
    ) -> Dict[str, List[dict]]:
        """Find spectra that need downloading, grouped by observation.

        A spectrum needs downloading if:

        - ``local_file_hash IS NULL`` (never downloaded), OR
        - ``local_file_hash != file_hash`` (stale — server has newer version)

        Parameters
        ----------
        observations : list of str, optional
            Limit to these observations. If None, checks all.
        gratings : list of str, optional
            Limit to these gratings.

        Returns
        -------
        dict
            ``{observation_name: [spectra_dicts]}`` for observations
            needing downloads. Each dict includes a ``status`` key
            (``"new"`` or ``"updated"``).
        """
        where = ["s.fits_path IS NOT NULL"]
        params: list = []

        if observations:
            placeholders = ",".join("?" * len(observations))
            where.append(f"o.observation IN ({placeholders})")
            params.extend(observations)

        if gratings:
            placeholders = ",".join("?" * len(gratings))
            where.append(f"UPPER(s.grating) IN ({placeholders})")
            params.extend(g.upper() for g in gratings)

        # Core condition: needs download
        where.append("""(
            s.local_file_hash IS NULL
            OR (s.file_hash IS NOT NULL AND s.local_file_hash != s.file_hash)
        )""")

        where_sql = " AND ".join(where)

        rows = self._conn.execute(f"""
            SELECT s.spectra_id, s.target_id, s.grating, s.fits_path,
                   s.file_hash, s.file_size, s.local_file_hash,
                   o.observation
            FROM spectra s
            JOIN targets o ON s.target_id = o.target_id
            WHERE {where_sql}
            ORDER BY o.observation, s.spectra_id
        """, params).fetchall()

        result: Dict[str, List[dict]] = {}
        for row in rows:
            d = dict(row)
            d["status"] = "new" if d["local_file_hash"] is None else "updated"
            obs = d["observation"]
            result.setdefault(obs, []).append(d)

        return result

    def remove_observation(self, observation: str) -> int:
        """Remove sync state for an observation (nullify local_path)."""
        # For spectra linked to targets in this observation
        cursor = self._conn.execute("""
            UPDATE spectra SET local_path = NULL, synced_at = NULL
            WHERE target_id IN (
                SELECT target_id FROM targets WHERE observation = ?
            )
        """, (observation,))
        count = cursor.rowcount

        # Also handle spectra with local_path matching the observation dir
        cursor2 = self._conn.execute("""
            UPDATE spectra SET local_path = NULL, synced_at = NULL
            WHERE local_path LIKE ?
        """, (f"{observation}/%",))
        count += cursor2.rowcount

        # Remove the targets themselves
        self._conn.execute(
            "DELETE FROM targets WHERE observation = ?", (observation,)
        )
        self._conn.commit()
        return count

    def get_observation_stats(self, observation: str) -> dict:
        """Get sync stats for an observation."""
        row = self._conn.execute("""
            SELECT
                COUNT(*) as synced_count,
                COALESCE(SUM(s.file_size), 0) as total_bytes
            FROM spectra s
            WHERE s.local_path IS NOT NULL
            AND (
                s.target_id IN (SELECT target_id FROM targets WHERE observation = ?)
                OR s.local_path LIKE ?
            )
        """, (observation, f"{observation}/%")).fetchone()
        return dict(row) if row else {"synced_count": 0, "total_bytes": 0}

    def get_last_sync(self, observation: str) -> Optional[str]:
        """Get timestamp of last completed sync for an observation."""
        row = self._conn.execute("""
            SELECT completed_at FROM sync_log
            WHERE observation = ? AND status = 'completed'
            ORDER BY completed_at DESC LIMIT 1
        """, (observation,)).fetchone()
        return row["completed_at"] if row else None

    def log_sync_start(self, observation: str) -> int:
        """Log the start of a sync operation. Returns the log entry ID."""
        cursor = self._conn.execute(
            "INSERT INTO sync_log (observation, started_at) VALUES (?, ?)",
            (observation, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return cursor.lastrowid

    def log_sync_complete(
        self, log_id: int, files_downloaded: int, files_skipped: int, bytes_downloaded: int
    ) -> None:
        """Log the completion of a sync operation."""
        self._conn.execute("""
            UPDATE sync_log SET completed_at = ?, files_downloaded = ?,
            files_skipped = ?, bytes_downloaded = ?, status = 'completed'
            WHERE id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            files_downloaded, files_skipped, bytes_downloaded, log_id,
        ))
        self._conn.commit()

    def find_local_path(self, target_id: str, grating: str) -> Optional[str]:
        """Check if a FITS file exists locally for a target + grating.

        Returns the relative local_path if downloaded, None otherwise.
        """
        row = self._conn.execute("""
            SELECT local_path FROM spectra
            WHERE target_id = ? AND grating = ? AND local_path IS NOT NULL
        """, (target_id, grating)).fetchone()
        return row["local_path"] if row else None

    # -------------------------------------------------------------------------
    # Sky-objects catalog operations (cross-program groupings)
    # -------------------------------------------------------------------------

    def upsert_sky_objects(self, objects_data: List[dict]) -> int:
        """Insert or update sky-objects from the /sync/objects endpoint.

        Serializes list fields (programs, gratings, member_target_ids) as
        semicolon-separated strings for SQLite storage.

        Parameters
        ----------
        objects_data : list of dict
            Objects from the sync endpoint, each with list-typed fields.

        Returns
        -------
        int
            Number of objects upserted.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0

        for obj in objects_data:
            programs = ";".join(obj.get("programs") or [])
            gratings = ";".join(obj.get("gratings") or [])
            member_ids = ";".join(str(m) for m in (obj.get("member_target_ids") or []))

            self._conn.execute("""
                INSERT INTO objects
                    (id, object_id, field, ra, dec,
                     n_targets, n_spectra, programs, gratings,
                     max_snr, max_exposure_time,
                     best_redshift, best_redshift_quality,
                     member_target_ids,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(object_id) DO UPDATE SET
                    field=excluded.field,
                    ra=excluded.ra, dec=excluded.dec,
                    n_targets=excluded.n_targets,
                    n_spectra=excluded.n_spectra,
                    programs=excluded.programs,
                    gratings=excluded.gratings,
                    max_snr=excluded.max_snr,
                    max_exposure_time=excluded.max_exposure_time,
                    best_redshift=excluded.best_redshift,
                    best_redshift_quality=excluded.best_redshift_quality,
                    member_target_ids=excluded.member_target_ids,
                    updated_at=excluded.updated_at,
                    _synced_at=excluded._synced_at
            """, (
                obj.get("id"),
                obj.get("object_id"),
                obj.get("field"),
                obj.get("ra"),
                obj.get("dec"),
                obj.get("n_targets", 0),
                obj.get("n_spectra", 0),
                programs,
                gratings,
                obj.get("max_snr"),
                obj.get("max_exposure_time"),
                obj.get("best_redshift"),
                obj.get("best_redshift_quality", 0),
                member_ids,
                obj.get("created_at"),
                obj.get("updated_at"),
                now,
            ))
            count += 1

        self._conn.commit()
        return count

    def query_sky_objects(
        self,
        fields: Optional[List[str]] = None,
        programs: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[int]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        sort: str = "object_id",
        sort_dir: str = "asc",
        limit: Optional[int] = None,
        offset: int = 0,
        **kwargs,
    ) -> List[dict]:
        """Query sky-objects from local SQLite store.

        Parameters
        ----------
        fields : list of str, optional
            Filter by field name.
        programs : list of str, optional
            Filter by program slug (semicolon-separated column).
        redshift_range : tuple of (min, max), optional
        redshift_quality : list of int, optional
        max_snr_range : tuple of (min, max), optional
        search : str, optional
            Text search on object_id (LIKE match).
        cone_search : tuple of (ra, dec, radius_arcsec), optional
        sort : str
            Column to sort by.
        sort_dir : str
            'asc' or 'desc'.
        limit, offset : int

        Returns
        -------
        list of dict
            Object records with list fields deserialized.
        """
        where_clauses = []
        params = []

        if fields:
            placeholders = ",".join("?" * len(fields))
            where_clauses.append(f"o.field IN ({placeholders})")
            params.extend(fields)

        if programs:
            # Programs stored as semicolon-separated; match any
            prog_clauses = []
            for prog in programs:
                prog_clauses.append("o.programs LIKE ?")
                params.append(f"%{prog}%")
            where_clauses.append(f"({' OR '.join(prog_clauses)})")

        if redshift_range:
            where_clauses.append("o.best_redshift >= ? AND o.best_redshift <= ?")
            params.extend(redshift_range)

        if redshift_quality:
            placeholders = ",".join("?" * len(redshift_quality))
            where_clauses.append(f"o.best_redshift_quality IN ({placeholders})")
            params.extend(redshift_quality)

        if max_snr_range:
            where_clauses.append("o.max_snr >= ? AND o.max_snr <= ?")
            params.extend(max_snr_range)

        if search:
            where_clauses.append("o.object_id LIKE ?")
            params.append(f"%{search}%")

        # Cone search
        order_by_distance = False
        if cone_search:
            ra, dec, radius_arcsec = cone_search
            radius_deg = radius_arcsec / 3600.0
            cos_dec = math.cos(math.radians(dec))
            ra_margin = radius_deg / max(cos_dec, 0.01)
            where_clauses.append("o.ra BETWEEN ? AND ?")
            params.extend([ra - ra_margin, ra + ra_margin])
            where_clauses.append("o.dec BETWEEN ? AND ?")
            params.extend([dec - radius_deg, dec + radius_deg])
            order_by_distance = True

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        allowed_sorts = {
            "object_id", "field", "ra", "dec", "best_redshift",
            "best_redshift_quality", "n_targets", "n_spectra",
            "max_snr", "max_exposure_time",
        }
        if sort not in allowed_sorts:
            sort = "object_id"
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"

        order_clause = f"o.{sort} {sort_dir}"
        if order_by_distance and sort == "object_id":
            order_clause = "distance ASC"

        if cone_search:
            ra, dec, _ = cone_search
            distance_expr = f"""
                SQRT(
                    POWER((o.ra - {ra}) * COS(RADIANS({dec})), 2) +
                    POWER(o.dec - {dec}, 2)
                ) AS distance
            """
        else:
            distance_expr = "NULL AS distance"

        sql = f"""
            SELECT o.*, {distance_expr}
            FROM objects o
            WHERE {where_sql}
            ORDER BY {order_clause}
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)

        rows = self._conn.execute(sql, params).fetchall()

        cone_radius_deg = cone_search[2] / 3600.0 if cone_search else None
        results = []
        for row in rows:
            obj = dict(row)
            obj.pop("_synced_at", None)

            if not cone_search:
                obj.pop("distance", None)
            elif obj.get("distance") is not None and obj["distance"] > cone_radius_deg:
                continue

            # Deserialize semicolon-separated fields to lists
            for col in ("programs", "gratings", "member_target_ids"):
                val = obj.get(col)
                obj[col] = val.split(";") if val else []

            results.append(obj)

        return results

    def get_max_objects_updated_at(self) -> Optional[str]:
        """Get the most recent server-side updated_at for sky-objects.

        Used for incremental sync of the objects table.
        """
        row = self._conn.execute(
            "SELECT MAX(updated_at) FROM objects"
        ).fetchone()
        return row[0] if row and row[0] else None

    def purge_stale_objects(self, sync_timestamp: str) -> int:
        """Delete sky-objects not seen in the latest full sync.

        Parameters
        ----------
        sync_timestamp : str
            ISO 8601 timestamp from the start of the sync.

        Returns
        -------
        int
            Number of objects purged.
        """
        cursor = self._conn.execute(
            "DELETE FROM objects WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged = cursor.rowcount
        self._conn.commit()
        return purged

    # Deprecated aliases (old names → new names)
    upsert_objects = upsert_targets
    query_objects = query_targets
    count_objects = count_targets

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
