"""SQLite-based local store for CAMPFIRE metadata and sync state.

The database stores object- and spectrum-level metadata (populated during sync)
and tracks which FITS files have been downloaded locally. Targets were dropped
in Phase E; objects are first-class citizens, and spectra join to objects via
a denormalized ``object_id`` column.
"""

import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Schema version — bump when tables change. Existing DBs at a lower version
# will raise SchemaMismatchError and must be deleted + re-synced.
SCHEMA_VERSION = 4


# Columns exposed on `objects` from `/sync/objects`.
OBJECT_COLUMNS = [
    "id", "object_id", "field", "ra", "dec",
    "n_targets", "n_spectra",
    "programs", "gratings", "observations",
    "member_target_ids",
    "max_snr", "max_exposure_time",
    "redshift", "redshift_auto", "redshift_inspected", "redshift_quality",
    "last_inspected_at", "last_inspected_by",
    "last_data_change_at", "staleness_reason", "version", "is_active",
    "has_photometry", "photo_z", "photo_z_err_lo", "photo_z_err_hi",
    "created_at", "updated_at",
]

OBJECT_EXPORT_COLUMNS = [
    "object_id", "field", "ra", "dec",
    "redshift", "redshift_auto", "redshift_inspected", "redshift_quality",
    "n_targets", "n_spectra",
    "programs", "gratings", "observations", "member_target_ids",
    "max_snr", "max_exposure_time",
    "has_photometry", "photo_z", "photo_z_err_lo", "photo_z_err_hi",
    "last_inspected_at", "last_inspected_by",
    "last_data_change_at", "staleness_reason",
]

# Columns exposed on `spectra` — flat per-spectrum rows.
SPECTRA_COLUMNS = [
    "id", "spectrum_id", "target_id", "object_id", "grating", "fits_path",
    "file_hash", "file_size", "signal_to_noise", "exposure_time",
    "reduction_version", "redshift_auto", "dq_flags",
    "program_slug", "observation", "field",
    "local_path", "local_file_hash", "local_file_mtime", "local_file_size",
    "synced_at", "created_at", "updated_at",
]

SPECTRA_EXPORT_COLUMNS = [
    "spectrum_id", "target_id", "object_id", "grating", "fits_path",
    "file_hash", "file_size", "signal_to_noise", "exposure_time",
    "reduction_version", "redshift_auto", "dq_flags",
    "program_slug", "observation", "field", "local_path",
]

# Columns for the object_photometry table (unchanged from pre-Phase-E).
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
    observations TEXT,
    member_target_ids TEXT,
    max_snr REAL,
    max_exposure_time REAL,
    redshift REAL,
    redshift_auto REAL,
    redshift_inspected REAL,
    redshift_quality INTEGER DEFAULT 0,
    last_inspected_at TEXT,
    last_inspected_by TEXT,
    last_data_change_at TEXT,
    staleness_reason TEXT,
    version INTEGER,
    is_active INTEGER DEFAULT 1,
    has_photometry INTEGER DEFAULT 0,
    photo_z REAL,
    photo_z_err_lo REAL,
    photo_z_err_hi REAL,
    created_at TEXT,
    updated_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_objects_object_id ON objects(object_id);
CREATE INDEX IF NOT EXISTS idx_objects_field ON objects(field);
CREATE INDEX IF NOT EXISTS idx_objects_redshift ON objects(redshift);
CREATE INDEX IF NOT EXISTS idx_objects_redshift_quality ON objects(redshift_quality);
CREATE INDEX IF NOT EXISTS idx_objects_is_active ON objects(is_active);

CREATE TABLE IF NOT EXISTS spectra (
    id INTEGER PRIMARY KEY,
    spectrum_id TEXT UNIQUE NOT NULL,
    target_id TEXT,
    object_id TEXT,
    grating TEXT NOT NULL,
    fits_path TEXT,
    file_hash TEXT,
    file_size INTEGER,
    signal_to_noise REAL,
    exposure_time REAL,
    reduction_version TEXT,
    redshift_auto REAL,
    dq_flags INTEGER DEFAULT 0,
    program_slug TEXT,
    observation TEXT,
    field TEXT,
    local_path TEXT,
    local_file_hash TEXT,
    local_file_mtime REAL,
    local_file_size INTEGER,
    synced_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_spectra_spectrum_id ON spectra(spectrum_id);
CREATE INDEX IF NOT EXISTS idx_spectra_target_id ON spectra(target_id);
CREATE INDEX IF NOT EXISTS idx_spectra_object_id ON spectra(object_id);
CREATE INDEX IF NOT EXISTS idx_spectra_observation ON spectra(observation);
CREATE INDEX IF NOT EXISTS idx_spectra_grating ON spectra(grating);
CREATE INDEX IF NOT EXISTS idx_spectra_dq_flags ON spectra(dq_flags) WHERE dq_flags != 0;

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

CREATE TABLE IF NOT EXISTS object_list_memberships (
    object_id TEXT NOT NULL,
    list_slug TEXT NOT NULL,
    PRIMARY KEY (object_id, list_slug)
);

CREATE INDEX IF NOT EXISTS idx_olm_list_slug ON object_list_memberships(list_slug);

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
    """SQLite database manager for local CAMPFIRE metadata and sync state."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=OFF")

        self._conn.create_function("COS", 1, math.cos)
        self._conn.create_function("SQRT", 1, math.sqrt)
        self._conn.create_function("RADIANS", 1, math.radians)
        self._conn.create_function("POWER", 2, math.pow)

        self._init_schema()

    def _init_schema(self) -> None:
        has_meta = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='_meta'"
        ).fetchone() is not None

        if not has_meta:
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
        try:
            row = self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ).fetchone()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # -------------------------------------------------------------------------
    # Objects
    # -------------------------------------------------------------------------
    def upsert_objects(self, objects_data: List[dict]) -> int:
        """Insert or update objects from the /sync/objects endpoint.

        Serializes list fields (programs, gratings, observations,
        member_target_ids) as semicolon-separated strings.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0

        for obj in objects_data:
            programs = ";".join(obj.get("programs") or [])
            gratings = ";".join(obj.get("gratings") or [])
            observations = ";".join(obj.get("observations") or [])
            member_ids = ";".join(str(m) for m in (obj.get("member_target_ids") or []))

            self._conn.execute(
                """
                INSERT INTO objects
                    (id, object_id, field, ra, dec,
                     n_targets, n_spectra, programs, gratings, observations,
                     member_target_ids, max_snr, max_exposure_time,
                     redshift, redshift_auto, redshift_inspected, redshift_quality,
                     last_inspected_at, last_inspected_by,
                     last_data_change_at, staleness_reason, version, is_active,
                     has_photometry, photo_z, photo_z_err_lo, photo_z_err_hi,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(object_id) DO UPDATE SET
                    field=excluded.field,
                    ra=excluded.ra, dec=excluded.dec,
                    n_targets=excluded.n_targets,
                    n_spectra=excluded.n_spectra,
                    programs=excluded.programs,
                    gratings=excluded.gratings,
                    observations=excluded.observations,
                    member_target_ids=excluded.member_target_ids,
                    max_snr=excluded.max_snr,
                    max_exposure_time=excluded.max_exposure_time,
                    redshift=excluded.redshift,
                    redshift_auto=excluded.redshift_auto,
                    redshift_inspected=excluded.redshift_inspected,
                    redshift_quality=excluded.redshift_quality,
                    last_inspected_at=excluded.last_inspected_at,
                    last_inspected_by=excluded.last_inspected_by,
                    last_data_change_at=excluded.last_data_change_at,
                    staleness_reason=excluded.staleness_reason,
                    version=excluded.version,
                    is_active=excluded.is_active,
                    has_photometry=excluded.has_photometry,
                    photo_z=excluded.photo_z,
                    photo_z_err_lo=excluded.photo_z_err_lo,
                    photo_z_err_hi=excluded.photo_z_err_hi,
                    updated_at=excluded.updated_at,
                    _synced_at=excluded._synced_at
                """,
                (
                    obj.get("id"),
                    obj.get("object_id"),
                    obj.get("field"),
                    obj.get("ra"),
                    obj.get("dec"),
                    obj.get("n_targets", 0),
                    obj.get("n_spectra", 0),
                    programs,
                    gratings,
                    observations,
                    member_ids,
                    obj.get("max_snr"),
                    obj.get("max_exposure_time"),
                    obj.get("redshift"),
                    obj.get("redshift_auto"),
                    obj.get("redshift_inspected"),
                    obj.get("redshift_quality", 0),
                    obj.get("last_inspected_at"),
                    obj.get("last_inspected_by"),
                    obj.get("last_data_change_at"),
                    obj.get("staleness_reason"),
                    obj.get("version"),
                    1 if obj.get("is_active", True) else 0,
                    1 if obj.get("has_photometry") else 0,
                    obj.get("photo_z"),
                    obj.get("photo_z_err_lo"),
                    obj.get("photo_z_err_hi"),
                    obj.get("created_at"),
                    obj.get("updated_at"),
                    now,
                ),
            )

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

    def query_objects(
        self,
        fields: Optional[List[str]] = None,
        programs: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[int]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        dq_flags: Optional[dict] = None,
        tags: Optional[List[str]] = None,
        inspected_only: Optional[bool] = None,
        staleness: Optional[bool] = None,
        has_photometry: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        sort: str = "object_id",
        sort_dir: str = "asc",
        limit: Optional[int] = None,
        offset: int = 0,
        **kwargs,
    ) -> List[dict]:
        """Query objects from local SQLite store."""
        where = ["o.is_active = 1"]
        params: list = []

        if fields:
            placeholders = ",".join("?" * len(fields))
            where.append(f"o.field IN ({placeholders})")
            params.extend(fields)

        if programs:
            prog_clauses = []
            for prog in programs:
                prog_clauses.append("(';' || o.programs || ';') LIKE ?")
                params.append(f"%;{prog};%")
            where.append(f"({' OR '.join(prog_clauses)})")

        if gratings:
            grat_clauses = []
            for g in gratings:
                grat_clauses.append("(';' || o.gratings || ';') LIKE ?")
                params.append(f"%;{g};%")
            where.append(f"({' OR '.join(grat_clauses)})")

        if observations:
            obs_clauses = []
            for obs in observations:
                obs_clauses.append("(';' || o.observations || ';') LIKE ?")
                params.append(f"%;{obs};%")
            where.append(f"({' OR '.join(obs_clauses)})")

        if redshift_range:
            where.append("o.redshift >= ? AND o.redshift <= ?")
            params.extend(redshift_range)

        if redshift_quality:
            placeholders = ",".join("?" * len(redshift_quality))
            where.append(f"o.redshift_quality IN ({placeholders})")
            params.extend(redshift_quality)

        if max_snr_range:
            where.append("o.max_snr >= ? AND o.max_snr <= ?")
            params.extend(max_snr_range)

        if inspected_only is True:
            where.append("o.redshift_quality > 0")
        elif inspected_only is False:
            where.append("COALESCE(o.redshift_quality, 0) = 0")

        if staleness:
            where.append(
                "o.last_data_change_at IS NOT NULL AND "
                "(o.last_inspected_at IS NULL OR o.last_data_change_at > o.last_inspected_at)"
            )

        if has_photometry is True:
            where.append("o.has_photometry = 1")
        elif has_photometry is False:
            where.append("o.has_photometry = 0")

        if search:
            where.append("(o.object_id LIKE ? OR (';' || o.member_target_ids || ';') LIKE ?)")
            params.append(f"%{search}%")
            params.append(f"%{search}%")

        if tags:
            placeholders = ",".join("?" * len(tags))
            where.append(
                f"o.object_id IN (SELECT object_id FROM object_list_memberships WHERE list_slug IN ({placeholders}))"
            )
            params.extend(tags)

        # dq_flags: per-spectrum → EXISTS subquery
        if dq_flags:
            inc_any = getattr(dq_flags, "include_any", None) or (
                dq_flags.get("include_any", 0) if isinstance(dq_flags, dict) else 0
            )
            inc_all = getattr(dq_flags, "include_all", None) or (
                dq_flags.get("include_all", 0) if isinstance(dq_flags, dict) else 0
            )
            exclude = getattr(dq_flags, "exclude", None) or (
                dq_flags.get("exclude", 0) if isinstance(dq_flags, dict) else 0
            )
            if inc_any:
                where.append(
                    "EXISTS (SELECT 1 FROM spectra s WHERE s.object_id = o.object_id AND (s.dq_flags & ?) != 0)"
                )
                params.append(inc_any)
            if inc_all:
                where.append(
                    "EXISTS (SELECT 1 FROM spectra s WHERE s.object_id = o.object_id AND (s.dq_flags & ?) = ?)"
                )
                params.extend([inc_all, inc_all])
            if exclude:
                where.append(
                    "NOT EXISTS (SELECT 1 FROM spectra s WHERE s.object_id = o.object_id AND (s.dq_flags & ?) != 0)"
                )
                params.append(exclude)

        order_by_distance = False
        if cone_search:
            ra, dec, radius_arcsec = cone_search
            radius_deg = radius_arcsec / 3600.0
            cos_dec = math.cos(math.radians(dec))
            ra_margin = radius_deg / max(cos_dec, 0.01)
            where.append("o.ra BETWEEN ? AND ?")
            params.extend([ra - ra_margin, ra + ra_margin])
            where.append("o.dec BETWEEN ? AND ?")
            params.extend([dec - radius_deg, dec + radius_deg])
            order_by_distance = True

        where_sql = " AND ".join(where) if where else "1=1"

        allowed_sorts = {
            "object_id", "field", "ra", "dec", "redshift", "redshift_quality",
            "n_targets", "n_spectra", "max_snr", "max_exposure_time",
            "photo_z", "updated_at",
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

            for col in ("programs", "gratings", "observations", "member_target_ids"):
                val = obj.get(col)
                obj[col] = val.split(";") if val else []

            obj["is_active"] = bool(obj.get("is_active"))
            obj["has_photometry"] = bool(obj.get("has_photometry"))

            results.append(obj)

        return results

    def count_objects(self, **filters) -> int:
        return len(self.query_objects(**filters))

    def get_object(self, object_id: str) -> Optional[dict]:
        """Single object with embedded spectra (list of spectrum dicts) and tags."""
        row = self._conn.execute(
            "SELECT * FROM objects WHERE object_id = ?", (object_id,)
        ).fetchone()
        if not row:
            return None
        obj = dict(row)
        obj.pop("_synced_at", None)

        for col in ("programs", "gratings", "observations", "member_target_ids"):
            val = obj.get(col)
            obj[col] = val.split(";") if val else []

        obj["is_active"] = bool(obj.get("is_active"))
        obj["has_photometry"] = bool(obj.get("has_photometry"))

        spec_rows = self._conn.execute(
            "SELECT * FROM spectra WHERE object_id = ? ORDER BY spectrum_id", (object_id,)
        ).fetchall()
        obj["spectra"] = [dict(s) for s in spec_rows]

        tag_rows = self._conn.execute(
            "SELECT list_slug FROM object_list_memberships WHERE object_id = ? ORDER BY list_slug",
            (object_id,),
        ).fetchall()
        obj["tags"] = [r["list_slug"] for r in tag_rows]

        return obj

    def get_photometry_for_object(self, object_id: str) -> Optional[dict]:
        """Return the (first) photometry record for an object, with photometry JSON deserialised."""
        import json as _json

        row = self._conn.execute(
            "SELECT * FROM object_photometry WHERE object_id = ? ORDER BY catalog_name LIMIT 1",
            (object_id,),
        ).fetchone()
        if not row:
            return None
        rec = dict(row)
        phot = rec.get("photometry")
        if isinstance(phot, str):
            try:
                rec["photometry"] = _json.loads(phot)
            except (ValueError, TypeError):
                rec["photometry"] = None
        return rec

    def get_max_objects_updated_at(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(updated_at) FROM objects"
        ).fetchone()
        return row[0] if row and row[0] else None

    def purge_stale_objects(self, sync_timestamp: str) -> int:
        cursor = self._conn.execute(
            "DELETE FROM objects WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged = cursor.rowcount
        self._conn.commit()
        return purged

    # -------------------------------------------------------------------------
    # Spectra
    # -------------------------------------------------------------------------
    def upsert_spectra(self, spectra_data: List[dict]) -> int:
        """Insert or update spectra from the /sync/spectra endpoint.

        Preserves ``local_path``, ``local_file_hash``, ``local_file_mtime``,
        ``local_file_size``, and ``synced_at`` on conflict — those are local
        download bookkeeping and must not be clobbered by metadata refresh.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0

        for spec in spectra_data:
            self._conn.execute(
                """
                INSERT INTO spectra
                    (id, spectrum_id, target_id, object_id, grating, fits_path,
                     file_hash, file_size, signal_to_noise, exposure_time,
                     reduction_version, redshift_auto, dq_flags,
                     program_slug, observation, field,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(spectrum_id) DO UPDATE SET
                    target_id=excluded.target_id,
                    object_id=excluded.object_id,
                    grating=excluded.grating,
                    fits_path=excluded.fits_path,
                    file_hash=excluded.file_hash,
                    file_size=COALESCE(excluded.file_size, spectra.file_size),
                    signal_to_noise=excluded.signal_to_noise,
                    exposure_time=excluded.exposure_time,
                    reduction_version=excluded.reduction_version,
                    redshift_auto=excluded.redshift_auto,
                    dq_flags=excluded.dq_flags,
                    program_slug=excluded.program_slug,
                    observation=excluded.observation,
                    field=excluded.field,
                    updated_at=excluded.updated_at,
                    _synced_at=excluded._synced_at
                """,
                (
                    spec.get("id"),
                    spec.get("spectrum_id"),
                    spec.get("target_id"),
                    spec.get("object_id"),
                    spec.get("grating"),
                    spec.get("fits_path"),
                    spec.get("file_hash"),
                    spec.get("file_size"),
                    spec.get("signal_to_noise"),
                    spec.get("exposure_time"),
                    spec.get("reduction_version"),
                    spec.get("redshift_auto"),
                    spec.get("dq_flags", 0),
                    spec.get("program_slug"),
                    spec.get("observation"),
                    spec.get("field"),
                    spec.get("created_at"),
                    spec.get("updated_at"),
                    now,
                ),
            )
            count += 1

        self._conn.commit()
        return count

    def query_spectra(
        self,
        fields: Optional[List[str]] = None,
        programs: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        observations: Optional[List[str]] = None,
        redshift_range: Optional[Tuple[float, float]] = None,
        redshift_quality: Optional[List[int]] = None,
        max_snr_range: Optional[Tuple[float, float]] = None,
        dq_flags: Optional[dict] = None,
        tags: Optional[List[str]] = None,
        inspected_only: Optional[bool] = None,
        has_photometry: Optional[bool] = None,
        search: Optional[str] = None,
        cone_search: Optional[Tuple[float, float, float]] = None,
        sort: str = "spectrum_id",
        sort_dir: str = "asc",
        limit: Optional[int] = None,
        offset: int = 0,
        **kwargs,
    ) -> List[dict]:
        """Query spectra (flat, one row per spectrum) with object-level filters.

        Inspection state (redshift, redshift_quality, inspected_only) is
        resolved through the parent object via ``spectra.object_id =
        objects.object_id``.
        """
        where = ["(o.is_active IS NULL OR o.is_active = 1)"]
        params: list = []

        if fields:
            placeholders = ",".join("?" * len(fields))
            where.append(f"s.field IN ({placeholders})")
            params.extend(fields)

        if programs:
            placeholders = ",".join("?" * len(programs))
            where.append(f"s.program_slug IN ({placeholders})")
            params.extend(programs)

        if gratings:
            placeholders = ",".join("?" * len(gratings))
            where.append(f"s.grating IN ({placeholders})")
            params.extend(gratings)

        if observations:
            placeholders = ",".join("?" * len(observations))
            where.append(f"s.observation IN ({placeholders})")
            params.extend(observations)

        if redshift_range:
            where.append("o.redshift >= ? AND o.redshift <= ?")
            params.extend(redshift_range)

        if redshift_quality:
            placeholders = ",".join("?" * len(redshift_quality))
            where.append(f"o.redshift_quality IN ({placeholders})")
            params.extend(redshift_quality)

        if max_snr_range:
            where.append("s.signal_to_noise >= ? AND s.signal_to_noise <= ?")
            params.extend(max_snr_range)

        if inspected_only is True:
            where.append("o.redshift_quality > 0")
        elif inspected_only is False:
            where.append("COALESCE(o.redshift_quality, 0) = 0")

        if has_photometry is True:
            where.append("o.has_photometry = 1")
        elif has_photometry is False:
            where.append("o.has_photometry = 0")

        if search:
            where.append("(s.spectrum_id LIKE ? OR s.target_id LIKE ?)")
            params.append(f"%{search}%")
            params.append(f"%{search}%")

        if tags:
            placeholders = ",".join("?" * len(tags))
            where.append(
                f"s.object_id IN (SELECT object_id FROM object_list_memberships WHERE list_slug IN ({placeholders}))"
            )
            params.extend(tags)

        if dq_flags:
            inc_any = getattr(dq_flags, "include_any", None) or (
                dq_flags.get("include_any", 0) if isinstance(dq_flags, dict) else 0
            )
            inc_all = getattr(dq_flags, "include_all", None) or (
                dq_flags.get("include_all", 0) if isinstance(dq_flags, dict) else 0
            )
            exclude = getattr(dq_flags, "exclude", None) or (
                dq_flags.get("exclude", 0) if isinstance(dq_flags, dict) else 0
            )
            if inc_any:
                where.append("(s.dq_flags & ?) != 0")
                params.append(inc_any)
            if inc_all:
                where.append("(s.dq_flags & ?) = ?")
                params.extend([inc_all, inc_all])
            if exclude:
                where.append("(s.dq_flags & ?) = 0")
                params.append(exclude)

        order_by_distance = False
        if cone_search:
            ra, dec, radius_arcsec = cone_search
            radius_deg = radius_arcsec / 3600.0
            cos_dec = math.cos(math.radians(dec))
            ra_margin = radius_deg / max(cos_dec, 0.01)
            where.append("o.ra BETWEEN ? AND ?")
            params.extend([ra - ra_margin, ra + ra_margin])
            where.append("o.dec BETWEEN ? AND ?")
            params.extend([dec - radius_deg, dec + radius_deg])
            order_by_distance = True

        where_sql = " AND ".join(where) if where else "1=1"

        allowed_sorts = {
            "spectrum_id": "s.spectrum_id",
            "target_id": "s.target_id",
            "object_id": "s.object_id",
            "grating": "s.grating",
            "field": "s.field",
            "observation": "s.observation",
            "signal_to_noise": "s.signal_to_noise",
            "exposure_time": "s.exposure_time",
            "redshift_auto": "s.redshift_auto",
            "redshift": "o.redshift",
            "redshift_quality": "o.redshift_quality",
            "ra": "o.ra",
            "dec": "o.dec",
        }
        sort_col = allowed_sorts.get(sort, "s.spectrum_id")
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"
        order_clause = f"{sort_col} {sort_dir}"
        if order_by_distance and sort == "spectrum_id":
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
            SELECT s.*,
                   o.redshift AS redshift,
                   o.redshift_quality AS redshift_quality,
                   o.ra AS ra,
                   o.dec AS dec,
                   {distance_expr}
            FROM spectra s
            LEFT JOIN objects o ON o.object_id = s.object_id
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
            rec = dict(row)
            rec.pop("_synced_at", None)
            if not cone_search:
                rec.pop("distance", None)
            elif rec.get("distance") is not None and rec["distance"] > cone_radius_deg:
                continue
            results.append(rec)

        return results

    def count_spectra(self, **filters) -> int:
        return len(self.query_spectra(**filters))

    def get_spectrum(self, spectrum_id: str) -> Optional[dict]:
        """Single spectrum lookup by spectrum_id."""
        row = self._conn.execute(
            "SELECT * FROM spectra WHERE spectrum_id = ?", (spectrum_id,)
        ).fetchone()
        if not row:
            return None
        rec = dict(row)
        rec.pop("_synced_at", None)
        return rec

    def get_max_spectra_updated_at(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(updated_at) FROM spectra"
        ).fetchone()
        return row[0] if row and row[0] else None

    def purge_stale_spectra(self, sync_timestamp: str) -> dict:
        """Delete spectra not seen in the latest full sync.

        Returns orphaned local files so the caller can clean them up.
        """
        orphaned = self._conn.execute(
            """SELECT local_path FROM spectra
               WHERE _synced_at < ? AND local_path IS NOT NULL""",
            (sync_timestamp,),
        ).fetchall()
        orphaned_files = [r["local_path"] for r in orphaned]

        cursor = self._conn.execute(
            "DELETE FROM spectra WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged = cursor.rowcount
        self._conn.commit()
        return {"purged_spectra": purged, "orphaned_files": orphaned_files}

    # -------------------------------------------------------------------------
    # Distinct values / observation summaries (read from spectra)
    # -------------------------------------------------------------------------
    def get_distinct_values(self, column: str) -> list:
        """Return distinct values for a spectra column (for metadata queries)."""
        allowed = {"field", "observation", "grating", "program_slug"}
        if column not in allowed:
            return []
        rows = self._conn.execute(
            f"SELECT DISTINCT {column} FROM spectra ORDER BY {column}"
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def get_synced_observations(self) -> List[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT observation FROM spectra ORDER BY observation"
        ).fetchall()
        return [r[0] for r in rows if r[0]]

    def get_observation_summary(self) -> List[dict]:
        """Per-observation summary with program, field, and download status."""
        rows = self._conn.execute(
            """
            SELECT
                s.observation,
                s.program_slug,
                s.field,
                COUNT(DISTINCT s.object_id) AS object_count,
                COUNT(*) AS spectrum_count,
                COUNT(CASE WHEN s.local_path IS NOT NULL THEN 1 END) AS downloaded_count
            FROM spectra s
            GROUP BY s.observation, s.program_slug, s.field
            ORDER BY s.observation
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_last_synced_at(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(_synced_at) FROM objects"
        ).fetchone()
        return row[0] if row and row[0] else None

    # -------------------------------------------------------------------------
    # Local-file bookkeeping
    # -------------------------------------------------------------------------
    def get_synced_files(self, observation: str) -> Dict[int, dict]:
        """Return {spectrum_id_pk: row_dict} for downloaded files in an observation."""
        rows = self._conn.execute(
            """
            SELECT s.id, s.spectrum_id, s.target_id, s.object_id, s.grating,
                   s.fits_path, s.local_path, s.local_file_hash, s.file_hash,
                   s.file_size, s.synced_at
            FROM spectra s
            WHERE s.observation = ? AND s.local_path IS NOT NULL
            """,
            (observation,),
        ).fetchall()

        result: Dict[int, dict] = {}
        for row in rows:
            d = dict(row)
            d["file_hash"] = d.get("local_file_hash")
            result[row["id"]] = d

        legacy_rows = self._conn.execute(
            """SELECT id, spectrum_id, target_id, object_id, grating, fits_path,
                      local_path, local_file_hash, file_hash, file_size, synced_at
               FROM spectra
               WHERE local_path IS NOT NULL AND local_path LIKE ?""",
            (f"{observation}/%",),
        ).fetchall()
        for row in legacy_rows:
            if row["id"] not in result:
                d = dict(row)
                d["file_hash"] = d.get("local_file_hash")
                result[row["id"]] = d

        return result

    def verify_local_files(
        self,
        products_dir: Path,
        observation: Optional[str] = None,
        show_progress: bool = False,
    ) -> dict:
        """Reconcile DB sync state with the local filesystem."""
        from ..sync import compute_file_hash

        now = datetime.now(timezone.utc).isoformat()
        obs_filter = "AND s.observation = ?" if observation else ""
        obs_params: tuple = (observation,) if observation else ()

        tracked_rows = self._conn.execute(
            f"""
            SELECT s.id, s.local_path, s.local_file_mtime,
                   s.local_file_size, s.local_file_hash
            FROM spectra s
            WHERE s.local_path IS NOT NULL {obs_filter}
            """,
            obs_params,
        ).fetchall()

        cleared = 0
        rehashed = 0

        untracked_rows = self._conn.execute(
            f"""
            SELECT s.id, s.fits_path, s.file_hash, s.observation
            FROM spectra s
            WHERE s.local_path IS NULL AND s.fits_path IS NOT NULL {obs_filter}
            """,
            obs_params,
        ).fetchall()

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
                       WHERE id = ?""",
                    (row["id"],),
                )
                cleared += 1
            else:
                st = full_path.stat()
                stored_mtime = row["local_file_mtime"]
                stored_size = row["local_file_size"]
                if (
                    stored_mtime is not None
                    and stored_size is not None
                    and abs(st.st_mtime - stored_mtime) < 0.001
                    and st.st_size == stored_size
                ):
                    if pbar:
                        pbar.update(1)
                    continue
                new_hash = compute_file_hash(full_path)
                self._conn.execute(
                    """UPDATE spectra SET local_file_hash = ?,
                       local_file_mtime = ?, local_file_size = ?
                       WHERE id = ?""",
                    (new_hash, st.st_mtime, st.st_size, row["id"]),
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
                       WHERE id = ?""",
                    (rel_path, actual_hash, st.st_mtime, st.st_size, now, row["id"]),
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
        spectrum_id: str,
        local_path: str,
        file_hash: Optional[str],
        file_size: Optional[int],
        local_file_mtime: Optional[float] = None,
        local_file_size: Optional[int] = None,
    ) -> None:
        """Record that a file has been downloaded locally, keyed by spectrum_id.

        The ``file_hash`` parameter is stored as ``local_file_hash``.
        Assumes the spectrum row already exists (inserted during sync).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE spectra SET local_path = ?, local_file_hash = ?,
               file_size = COALESCE(?, file_size),
               local_file_mtime = ?, local_file_size = ?, synced_at = ?
               WHERE spectrum_id = ?""",
            (
                local_path,
                file_hash,
                file_size,
                local_file_mtime,
                local_file_size,
                now,
                spectrum_id,
            ),
        )
        self._conn.commit()

    def get_stale_files(self) -> List[dict]:
        """Return locally downloaded files whose server hash differs from local."""
        rows = self._conn.execute(
            """
            SELECT s.id, s.spectrum_id, s.target_id, s.object_id, s.grating,
                   s.fits_path, s.local_path, s.file_hash AS server_hash,
                   s.local_file_hash, s.observation
            FROM spectra s
            WHERE s.local_path IS NOT NULL
              AND s.file_hash IS NOT NULL
              AND s.local_file_hash IS NOT NULL
              AND s.file_hash != s.local_file_hash
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_downloads(
        self,
        observations: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
    ) -> Dict[str, List[dict]]:
        """Find spectra that need downloading, grouped by observation."""
        where = ["s.fits_path IS NOT NULL"]
        params: list = []

        if observations:
            placeholders = ",".join("?" * len(observations))
            where.append(f"s.observation IN ({placeholders})")
            params.extend(observations)

        if gratings:
            placeholders = ",".join("?" * len(gratings))
            where.append(f"UPPER(s.grating) IN ({placeholders})")
            params.extend(g.upper() for g in gratings)

        where.append(
            "(s.local_file_hash IS NULL OR (s.file_hash IS NOT NULL AND s.local_file_hash != s.file_hash))"
        )
        where_sql = " AND ".join(where)

        rows = self._conn.execute(
            f"""
            SELECT s.id, s.spectrum_id, s.target_id, s.object_id, s.grating,
                   s.fits_path, s.file_hash, s.file_size, s.local_file_hash,
                   s.observation
            FROM spectra s
            WHERE {where_sql}
            ORDER BY s.observation, s.id
            """,
            params,
        ).fetchall()

        result: Dict[str, List[dict]] = {}
        for row in rows:
            d = dict(row)
            d["status"] = "new" if d["local_file_hash"] is None else "updated"
            obs = d["observation"]
            result.setdefault(obs, []).append(d)

        return result

    def remove_observation(self, observation: str) -> int:
        """Clear local-download state for all spectra in an observation."""
        cursor = self._conn.execute(
            """UPDATE spectra
               SET local_path = NULL, local_file_hash = NULL,
                   local_file_mtime = NULL, local_file_size = NULL,
                   synced_at = NULL
               WHERE observation = ? OR local_path LIKE ?""",
            (observation, f"{observation}/%"),
        )
        count = cursor.rowcount
        self._conn.commit()
        return count

    def get_observation_stats(self, observation: str) -> dict:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS synced_count,
                COALESCE(SUM(s.file_size), 0) AS total_bytes
            FROM spectra s
            WHERE s.local_path IS NOT NULL
              AND (s.observation = ? OR s.local_path LIKE ?)
            """,
            (observation, f"{observation}/%"),
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
                files_downloaded,
                files_skipped,
                bytes_downloaded,
                log_id,
            ),
        )
        self._conn.commit()

    def find_local_path(self, spectrum_id: str) -> Optional[str]:
        """Return the relative local_path for a spectrum if downloaded, else None."""
        row = self._conn.execute(
            "SELECT local_path FROM spectra WHERE spectrum_id = ? AND local_path IS NOT NULL",
            (spectrum_id,),
        ).fetchone()
        return row["local_path"] if row else None

    # -------------------------------------------------------------------------
    # Tags
    # -------------------------------------------------------------------------
    def upsert_tags(self, tags_data: list) -> int:
        count = 0
        for lst in tags_data:
            self._conn.execute(
                """
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
                """,
                (
                    lst.get("id"),
                    lst.get("slug"),
                    lst.get("name"),
                    lst.get("description"),
                    lst.get("visibility"),
                    1 if lst.get("is_system") else 0,
                    lst.get("member_count", 0),
                    lst.get("created_at"),
                    lst.get("updated_at"),
                ),
            )
            count += 1
        self._conn.commit()
        return count

    def get_tags(self) -> List[dict]:
        rows = self._conn.execute(
            "SELECT * FROM object_lists ORDER BY is_system DESC, name"
        ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------------------
    # Photometry
    # -------------------------------------------------------------------------
    def upsert_photometry(self, records: List[dict]) -> int:
        import json as _json

        now = datetime.now(timezone.utc).isoformat()
        count = 0

        for rec in records:
            phot = rec.get("photometry")
            if isinstance(phot, dict):
                phot = _json.dumps(phot)

            self._conn.execute(
                """
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
                """,
                (
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
                ),
            )
            count += 1

        self._conn.commit()
        return count

    def get_max_photometry_updated_at(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(updated_at) FROM object_photometry"
        ).fetchone()
        return row[0] if row and row[0] else None

    def purge_stale_photometry(self, sync_timestamp: str) -> int:
        cursor = self._conn.execute(
            "DELETE FROM object_photometry WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged = cursor.rowcount
        self._conn.commit()
        return purged

    def query_photometry(self) -> List[dict]:
        import json as _json

        rows = self._conn.execute(
            "SELECT * FROM object_photometry ORDER BY object_id, catalog_name"
        ).fetchall()

        results = []
        for row in rows:
            rec = dict(row)
            phot = rec.get("photometry")
            if isinstance(phot, str):
                try:
                    rec["photometry"] = _json.loads(phot)
                except (ValueError, TypeError):
                    rec["photometry"] = None
            results.append(rec)

        return results

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
