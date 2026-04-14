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


# Schema version — bump when tables change.
# Squashed to v1 on 2026-04-06; existing databases must be deleted and re-synced.
SCHEMA_VERSION = 3

# Column lists used by both store and export
TARGET_COLUMNS = [
    "id", "target_id", "program_slug", "program_name", "field", "observation",
    "ra", "dec", "redshift", "redshift_auto", "redshift_inspected",
    "redshift_quality", "spectral_features", "dq_flags",
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
    "redshift_quality", "spectral_features", "dq_flags",
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
    "has_photometry", "photo_z", "photo_z_err_lo", "photo_z_err_hi",
    "member_target_ids",
    "created_at", "updated_at",
]

OBJECT_EXPORT_COLUMNS = [
    "object_id", "field", "ra", "dec",
    "best_redshift", "best_redshift_quality",
    "n_targets", "n_spectra",
    "programs", "gratings",
    "max_snr", "max_exposure_time",
    "has_photometry", "photo_z", "photo_z_err_lo", "photo_z_err_hi",
    "member_target_ids",
]

# Columns for the object_photometry table
PHOTOMETRY_COLUMNS = [
    "id", "object_id", "field", "catalog_name", "catalog_id",
    "match_distance_arcsec", "photometry",
    "photo_z", "photo_z_err_lo", "photo_z_err_hi", "has_pz",
    "created_at", "updated_at",
]

PHOTOMETRY_EXPORT_COLUMNS = [
    "object_id", "field", "catalog_name", "catalog_id",
    "match_distance_arcsec",
    "photo_z", "photo_z_err_lo", "photo_z_err_hi",
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

CREATE TABLE IF NOT EXISTS object_list_memberships (
    object_id TEXT NOT NULL,
    list_slug TEXT NOT NULL,
    PRIMARY KEY (object_id, list_slug)
);

CREATE INDEX IF NOT EXISTS idx_olm_list_slug ON object_list_memberships(list_slug);

CREATE TABLE IF NOT EXISTS object_lists (
    id INTEGER PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    visibility TEXT DEFAULT 'private',
    is_system INTEGER DEFAULT 0,
    member_count INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);

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
    has_photometry INTEGER DEFAULT 0,
    photo_z REAL,
    photo_z_err_lo REAL,
    photo_z_err_hi REAL,
    member_target_ids TEXT,
    created_at TEXT,
    updated_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_objects_object_id ON objects(object_id);
CREATE INDEX IF NOT EXISTS idx_objects_field ON objects(field);

CREATE TABLE IF NOT EXISTS object_photometry (
    id INTEGER PRIMARY KEY,
    object_id TEXT,
    field TEXT,
    catalog_name TEXT,
    catalog_id TEXT,
    match_distance_arcsec REAL,
    photometry TEXT,
    photo_z REAL,
    photo_z_err_lo REAL,
    photo_z_err_hi REAL,
    has_pz INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ophot_object_id ON object_photometry(object_id);

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


class SchemaMismatchError(Exception):
    """Raised when the on-disk schema version doesn't match the code."""

    def __init__(self, db_path: Path, found_version: int, expected_version: int):
        self.db_path = db_path
        self.found_version = found_version
        self.expected_version = expected_version
        super().__init__(
            f"Local database schema version {found_version} does not match "
            f"expected version {expected_version}."
        )


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

    def _init_schema(self) -> None:
        """Create tables if needed, or verify schema version matches."""
        has_meta = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'"
        ).fetchone() is not None

        if not has_meta:
            # Fresh install
            self._conn.executescript(_SCHEMA_SQL)
            self._conn.execute(
                "INSERT OR REPLACE INTO _meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()
        else:
            version = self._get_schema_version()
            if version != SCHEMA_VERSION:
                self._conn.close()
                raise SchemaMismatchError(self.db_path, version, SCHEMA_VERSION)

    def _get_schema_version(self) -> int:
        """Get current schema version from _meta table."""
        try:
            row = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

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
                     redshift_quality, spectral_features, dq_flags,
                     max_snr, max_exposure_time,
                     last_inspected_at, last_inspected_by,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                obj.get("dq_flags", 0),
                obj.get("max_snr"),
                obj.get("max_exposure_time"),
                obj.get("last_inspected_at"),
                obj.get("last_inspected_by"),
                obj.get("created_at"),
                obj.get("updated_at"),
                now,
            ))

            # Upsert list memberships
            lists = obj.get("lists") or []
            self._conn.execute(
                "DELETE FROM object_list_memberships WHERE object_id = ?",
                (target_id,),
            )
            if lists:
                self._conn.executemany(
                    "INSERT OR IGNORE INTO object_list_memberships (object_id, list_slug) VALUES (?, ?)",
                    [(target_id, slug) for slug in lists],
                )
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
        dq_flags: Optional[dict] = None,
        tags: Optional[List[str]] = None,
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
        spectral_features, dq_flags : dict, optional
            Flag filter dicts with keys 'include_any', 'include_all', 'exclude'.
        tags : list of str, optional
            Filter by tag slugs (match targets in any of the given tags).
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

        # Tag membership filter
        if tags:
            placeholders = ",".join("?" * len(tags))
            where_clauses.append(
                f"o.target_id IN (SELECT object_id FROM object_list_memberships WHERE list_slug IN ({placeholders}))"
            )
            params.extend(tags)

        # Flag filters
        for flag_col, flag_filter in [
            ("o.spectral_features", spectral_features),
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
                      signal_to_noise, exposure_time, reduction_version, local_path
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

    def verify_local_files(
        self,
        products_dir: Path,
        observation: Optional[str] = None,
        show_progress: bool = False,
    ) -> dict:
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
        show_progress : bool, optional
            Show a tqdm progress bar during verification. Default False.

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

        # 2. Discover files that exist but aren't tracked
        untracked_rows = self._conn.execute(f"""
            SELECT s.spectra_id, s.fits_path, s.file_hash, o.observation
            FROM spectra s
            JOIN targets o ON s.target_id = o.target_id
            WHERE s.local_path IS NULL AND s.fits_path IS NOT NULL {obs_filter}
        """, obs_params).fetchall()

        total = len(tracked_rows) + len(untracked_rows)
        pbar = None
        if show_progress and total > 0:
            from tqdm import tqdm
            pbar = tqdm(total=total, desc="Verifying local files", unit="file")

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
                    if pbar:
                        pbar.update(1)
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
            if pbar:
                pbar.update(1)

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
            if pbar:
                pbar.update(1)

        if pbar:
            pbar.close()

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
                     has_photometry, photo_z, photo_z_err_lo, photo_z_err_hi,
                     member_target_ids,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    has_photometry=excluded.has_photometry,
                    photo_z=excluded.photo_z,
                    photo_z_err_lo=excluded.photo_z_err_lo,
                    photo_z_err_hi=excluded.photo_z_err_hi,
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
                1 if obj.get("has_photometry") else 0,
                obj.get("photo_z"),
                obj.get("photo_z_err_lo"),
                obj.get("photo_z_err_hi"),
                member_ids,
                obj.get("created_at"),
                obj.get("updated_at"),
                now,
            ))
            # Upsert list memberships for this sky-object
            obj_lists = obj.get("lists") or []
            obj_id_str = obj.get("object_id")
            if obj_id_str:
                self._conn.execute(
                    "DELETE FROM object_list_memberships WHERE object_id = ?",
                    (obj_id_str,),
                )
                if obj_lists:
                    self._conn.executemany(
                        "INSERT OR IGNORE INTO object_list_memberships (object_id, list_slug) VALUES (?, ?)",
                        [(obj_id_str, slug) for slug in obj_lists],
                    )

            count += 1

        self._conn.commit()
        return count

    def upsert_tags(self, tags_data: list) -> int:
        """Insert or update tag metadata from the /sync/lists endpoint.

        Parameters
        ----------
        tags_data : list of dict
            Tag metadata dicts from the API.

        Returns
        -------
        int
            Number of tags upserted.
        """
        count = 0
        for lst in tags_data:
            self._conn.execute("""
                INSERT INTO object_lists
                    (id, slug, name, description, visibility, is_system,
                     member_count, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    visibility=excluded.visibility,
                    is_system=excluded.is_system,
                    member_count=excluded.member_count,
                    updated_at=excluded.updated_at
            """, (
                lst.get("id"),
                lst.get("slug"),
                lst.get("name"),
                lst.get("description"),
                lst.get("visibility"),
                1 if lst.get("is_system") else 0,
                lst.get("member_count", 0),
                lst.get("created_at"),
                lst.get("updated_at"),
            ))
            count += 1
        self._conn.commit()
        return count

    def get_tags(self) -> List[dict]:
        """Get all synced tags.

        Returns
        -------
        list of dict
        """
        rows = self._conn.execute(
            "SELECT * FROM object_lists ORDER BY is_system DESC, name"
        ).fetchall()
        return [dict(r) for r in rows]

    def query_sky_objects(
        self,
        fields: Optional[List[str]] = None,
        programs: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        has_photometry: Optional[bool] = None,
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
        gratings : list of str, optional
            Filter by grating availability (e.g. ['PRISM']).
            Matches objects that have any of the given gratings.
        tags : list of str, optional
            Filter by tag/list membership (e.g. ['lrd', 'blagn']).
            Matches objects in any of the given tags.
        has_photometry : bool, optional
            If True, only objects with photometry. If False, only without.
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

        if gratings:
            # Gratings stored as semicolon-separated; match any
            grat_clauses = []
            for grat in gratings:
                grat_clauses.append("o.gratings LIKE ?")
                params.append(f"%{grat}%")
            where_clauses.append(f"({' OR '.join(grat_clauses)})")

        if tags:
            placeholders = ",".join("?" * len(tags))
            where_clauses.append(
                f"o.object_id IN (SELECT object_id FROM object_list_memberships WHERE list_slug IN ({placeholders}))"
            )
            params.extend(tags)

        if has_photometry is True:
            where_clauses.append("o.has_photometry = 1")
        elif has_photometry is False:
            where_clauses.append("o.has_photometry = 0")

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

            # Convert has_photometry from int to bool
            obj["has_photometry"] = bool(obj.get("has_photometry"))

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

    # -------------------------------------------------------------------------
    # Object photometry operations
    # -------------------------------------------------------------------------

    def upsert_photometry(self, records: List[dict]) -> int:
        """Insert or update photometry records from the /sync/photometry endpoint.

        Parameters
        ----------
        records : list of dict
            Photometry records from the sync endpoint.

        Returns
        -------
        int
            Number of records upserted.
        """
        import json as _json

        now = datetime.now(timezone.utc).isoformat()
        count = 0

        for rec in records:
            phot = rec.get("photometry")
            if isinstance(phot, dict):
                phot = _json.dumps(phot)

            self._conn.execute("""
                INSERT INTO object_photometry
                    (id, object_id, field, catalog_name, catalog_id,
                     match_distance_arcsec, photometry,
                     photo_z, photo_z_err_lo, photo_z_err_hi, has_pz,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    object_id=excluded.object_id,
                    field=excluded.field,
                    catalog_name=excluded.catalog_name,
                    catalog_id=excluded.catalog_id,
                    match_distance_arcsec=excluded.match_distance_arcsec,
                    photometry=excluded.photometry,
                    photo_z=excluded.photo_z,
                    photo_z_err_lo=excluded.photo_z_err_lo,
                    photo_z_err_hi=excluded.photo_z_err_hi,
                    has_pz=excluded.has_pz,
                    updated_at=excluded.updated_at,
                    _synced_at=excluded._synced_at
            """, (
                rec.get("id"),
                rec.get("object_id"),
                rec.get("field"),
                rec.get("catalog_name"),
                rec.get("catalog_id"),
                rec.get("match_distance_arcsec"),
                phot,
                rec.get("photo_z"),
                rec.get("photo_z_err_lo"),
                rec.get("photo_z_err_hi"),
                1 if rec.get("has_pz") else 0,
                rec.get("created_at"),
                rec.get("updated_at"),
                now,
            ))
            count += 1

        self._conn.commit()
        return count

    def get_max_photometry_updated_at(self) -> Optional[str]:
        """Get the most recent server-side updated_at for photometry.

        Used for incremental sync of the object_photometry table.
        """
        row = self._conn.execute(
            "SELECT MAX(updated_at) FROM object_photometry"
        ).fetchone()
        return row[0] if row and row[0] else None

    def purge_stale_photometry(self, sync_timestamp: str) -> int:
        """Delete photometry records not seen in the latest full sync.

        Parameters
        ----------
        sync_timestamp : str
            ISO 8601 timestamp from the start of the sync.

        Returns
        -------
        int
            Number of records purged.
        """
        cursor = self._conn.execute(
            "DELETE FROM object_photometry WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged = cursor.rowcount
        self._conn.commit()
        return purged

    def query_photometry(
        self,
        object_ids: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
        catalogs: Optional[List[str]] = None,
        has_photo_z: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[dict]:
        """Query photometry records from local database.

        Parameters
        ----------
        object_ids : list of str, optional
            Filter to specific sky-object IDs.
        fields : list of str, optional
            Filter by field name.
        catalogs : list of str, optional
            Filter by catalog name.
        has_photo_z : bool, optional
            If True, only records with a photometric redshift.
            If False, only records without.
        limit, offset : int

        Returns
        -------
        list of dict
            Photometry records with deserialized JSONB photometry column.
        """
        import json as _json

        where_clauses: list = []
        params: list = []

        if object_ids:
            placeholders = ",".join("?" * len(object_ids))
            where_clauses.append(f"object_id IN ({placeholders})")
            params.extend(object_ids)

        if fields:
            placeholders = ",".join("?" * len(fields))
            where_clauses.append(f"field IN ({placeholders})")
            params.extend(fields)

        if catalogs:
            placeholders = ",".join("?" * len(catalogs))
            where_clauses.append(f"catalog_name IN ({placeholders})")
            params.extend(catalogs)

        if has_photo_z is True:
            where_clauses.append("has_pz = 1")
        elif has_photo_z is False:
            where_clauses.append("has_pz = 0")

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        sql = f"""
            SELECT * FROM object_photometry
            WHERE {where_sql}
            ORDER BY object_id, catalog_name
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)

        rows = self._conn.execute(sql, params).fetchall()

        results = []
        for row in rows:
            rec = dict(row)
            rec.pop("_synced_at", None)
            phot = rec.get("photometry")
            if isinstance(phot, str):
                try:
                    rec["photometry"] = _json.loads(phot)
                except (ValueError, TypeError):
                    rec["photometry"] = None
            results.append(rec)

        return results

    # -------------------------------------------------------------------------
    # Flat spectra queries
    # -------------------------------------------------------------------------

    def query_spectra(
        self,
        target_ids: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        programs: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        snr_range: Optional[Tuple[float, float]] = None,
        downloaded_only: bool = False,
        sort: str = "target_id",
        sort_dir: str = "asc",
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[dict]:
        """Query spectra with filters, returning one row per spectrum.

        Joins to targets for program/field/observation context.

        Parameters
        ----------
        target_ids : list of str, optional
            Filter to specific target IDs.
        gratings : list of str, optional
            Filter by grating (e.g. ['PRISM', 'G395M']).
        programs : list of str, optional
            Filter by program slug (via targets join).
        fields : list of str, optional
            Filter by field name (via targets join).
        observations : list of str, optional
            Filter by observation name (via targets join).
        snr_range : tuple of (min, max), optional
            Signal-to-noise range. Use None for open-ended, e.g. (10, None).
        downloaded_only : bool
            If True, only return spectra with a local file.
        sort : str
            Column to sort by.
        sort_dir : str
            'asc' or 'desc'.
        limit, offset : int

        Returns
        -------
        list of dict
            Spectrum rows with target context columns.
        """
        where_clauses = []
        params: list = []

        if target_ids:
            placeholders = ",".join("?" * len(target_ids))
            where_clauses.append(f"s.target_id IN ({placeholders})")
            params.extend(target_ids)

        if gratings:
            placeholders = ",".join("?" * len(gratings))
            where_clauses.append(f"s.grating IN ({placeholders})")
            params.extend(gratings)

        if programs:
            placeholders = ",".join("?" * len(programs))
            where_clauses.append(f"t.program_slug IN ({placeholders})")
            params.extend(programs)

        if fields:
            placeholders = ",".join("?" * len(fields))
            where_clauses.append(f"t.field IN ({placeholders})")
            params.extend(fields)

        if observations:
            placeholders = ",".join("?" * len(observations))
            where_clauses.append(f"t.observation IN ({placeholders})")
            params.extend(observations)

        if snr_range:
            lo, hi = snr_range
            if lo is not None:
                where_clauses.append("s.signal_to_noise >= ?")
                params.append(lo)
            if hi is not None:
                where_clauses.append("s.signal_to_noise <= ?")
                params.append(hi)

        if downloaded_only:
            where_clauses.append("s.local_path IS NOT NULL")

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        allowed_sorts = {
            "target_id", "grating", "signal_to_noise", "exposure_time",
            "spectra_id", "field", "observation", "program_slug",
        }
        if sort not in allowed_sorts:
            sort = "target_id"
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"

        # Map sort columns to qualified names
        sort_col = f"s.{sort}" if sort in (
            "target_id", "grating", "signal_to_noise", "exposure_time", "spectra_id",
        ) else f"t.{sort}"

        sql = f"""
            SELECT s.spectra_id, s.target_id, s.grating, s.fits_path,
                   s.signal_to_noise, s.exposure_time, s.reduction_version,
                   s.local_path,
                   t.program_slug, t.field, t.observation,
                   t.ra, t.dec, t.redshift, t.redshift_quality
            FROM spectra s
            JOIN targets t ON s.target_id = t.target_id
            WHERE {where_sql}
            ORDER BY {sort_col} {sort_dir}
        """
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)

        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # Object traversal helpers
    # -------------------------------------------------------------------------

    def get_sky_object(self, object_id: str) -> Optional[dict]:
        """Get a single sky-object with its member targets, spectra, and photometry.

        Parameters
        ----------
        object_id : str
            The sky-object ID.

        Returns
        -------
        dict or None
            Object dict with:

            - ``targets``: list of target dicts, each with a ``spectra`` list
            - ``photometry``: list of photometry record dicts (with deserialized JSON)

            None if not found.
        """
        row = self._conn.execute(
            "SELECT * FROM objects WHERE object_id = ?", (object_id,)
        ).fetchone()
        if not row:
            return None

        obj = dict(row)
        obj.pop("_synced_at", None)

        # Deserialize semicolon-separated fields
        for col in ("programs", "gratings", "member_target_ids"):
            val = obj.get(col)
            obj[col] = val.split(";") if val else []

        # Convert has_photometry from int to bool
        obj["has_photometry"] = bool(obj.get("has_photometry"))

        # Attach member targets with their spectra
        obj["targets"] = self.get_targets_for_object(object_id)

        # Attach photometry records
        obj["photometry"] = self.get_photometry_for_object(object_id)

        # Attach tag slugs
        tag_rows = self._conn.execute(
            "SELECT list_slug FROM object_list_memberships WHERE object_id = ?",
            (object_id,),
        ).fetchall()
        obj["tags"] = [r[0] for r in tag_rows]

        return obj

    def get_targets_for_object(self, object_id: str) -> List[dict]:
        """Get all member targets for a sky-object, with their spectra.

        Uses the objects.member_target_ids column to find matching targets.

        Parameters
        ----------
        object_id : str
            The sky-object ID.

        Returns
        -------
        list of dict
            Target dicts, each with a 'spectra' key containing spectrum rows.
        """
        # Get target IDs from objects table
        row = self._conn.execute(
            "SELECT member_target_ids FROM objects WHERE object_id = ?",
            (object_id,),
        ).fetchone()
        if not row or not row[0]:
            return []

        target_ids = row[0].split(";")
        placeholders = ",".join("?" * len(target_ids))
        target_rows = self._conn.execute(
            f"SELECT * FROM targets WHERE target_id IN ({placeholders})",
            target_ids,
        ).fetchall()

        results = []
        for trow in target_rows:
            target = dict(trow)
            target.pop("_synced_at", None)

            spectra_rows = self._conn.execute(
                """SELECT spectra_id as id, target_id, grating, fits_path,
                          signal_to_noise, exposure_time, reduction_version, local_path
                   FROM spectra WHERE target_id = ?""",
                (target["target_id"],),
            ).fetchall()
            target["spectra"] = [dict(s) for s in spectra_rows]
            results.append(target)

        return results

    def get_photometry_for_object(self, object_id: str) -> List[dict]:
        """Get photometry records for a sky-object.

        Parameters
        ----------
        object_id : str
            The sky-object ID.

        Returns
        -------
        list of dict
            Photometry records with deserialized JSON photometry column.
        """
        import json as _json

        rows = self._conn.execute(
            "SELECT * FROM object_photometry WHERE object_id = ? ORDER BY catalog_name",
            (object_id,),
        ).fetchall()

        results = []
        for row in rows:
            rec = dict(row)
            rec.pop("_synced_at", None)
            phot = rec.get("photometry")
            if isinstance(phot, str):
                try:
                    rec["photometry"] = _json.loads(phot)
                except (ValueError, TypeError):
                    rec["photometry"] = None
            results.append(rec)

        return results

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
