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
#   v6 (epic #210): storage_objects is now the single download/availability
#   layer. The spectra table holds science metadata only; file keys, hashes, and
#   local-download bookkeeping moved to the storage_objects mirror.
#   v7: storage_objects.filter — per-filter NIRCam scope column (mirrors the
#   server registry), so `campfire download --filters` scopes without key parsing.
#   v8 (storage unification): storage_objects.sci_dq_hash mirrors the server's
#   science-only NIRCam identity, and the pushed_* columns carry the PUSH-side
#   bookkeeping (`campfire push` / deploy dedup) — the mirror now serves both
#   transfer directions.
SCHEMA_VERSION = 8


# Product-type classes the client download engine understands. Finals are the
# default download set; --intermediate adds the canonical spectrum-exposures.
# Both are layout-mirrored (have a local relpath), so the same engine places
# them. Widen these tuples as more product types become client-fetchable.
FINAL_PRODUCT_TYPES = ("nirspec_spec", "nircam_mosaic")
INTERMEDIATE_PRODUCT_TYPES = (
    "nirspec_spectrum_exposure", "nircam_exposure", "nircam_expmap",
)
DOWNLOADABLE_PRODUCT_TYPES = FINAL_PRODUCT_TYPES + INTERMEDIATE_PRODUCT_TYPES


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

# Columns exposed on `spectra` — flat per-spectrum rows. The provenance block
# (cfpipe_version, crds_context, jwst_version, date_obs, reduced_at) is carried
# verbatim from the FITS primary header through the catalog so a flux value can
# be traced to its exact pipeline version + CRDS context without opening FITS.
SPECTRA_COLUMNS = [
    "id", "spectrum_id", "target_id", "object_id", "grating", "fits_path",
    "file_hash", "file_size", "signal_to_noise", "exposure_time",
    "cfpipe_version", "crds_context", "jwst_version", "date_obs", "reduced_at",
    "redshift_auto", "dq_flags",
    "program_slug", "observation", "field",
    "local_path", "local_file_hash", "local_file_mtime", "local_file_size",
    "synced_at", "created_at", "updated_at",
]

SPECTRA_EXPORT_COLUMNS = [
    "spectrum_id", "target_id", "object_id", "grating", "fits_path",
    "file_hash", "file_size", "signal_to_noise", "exposure_time",
    "cfpipe_version", "crds_context", "jwst_version", "date_obs", "reduced_at",
    "redshift_auto", "dq_flags",
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

-- spectra: science metadata only. File keys, hashes, sizes, and local-download
-- bookkeeping live in storage_objects (the single download/availability layer),
-- joined on spectrum_id. query_spectra/get_spectrum surface fits_path/local_path/
-- file_hash via that join for backward compatibility.
CREATE TABLE IF NOT EXISTS spectra (
    id INTEGER PRIMARY KEY,
    spectrum_id TEXT UNIQUE NOT NULL,
    target_id TEXT,
    object_id TEXT,
    grating TEXT NOT NULL,
    signal_to_noise REAL,
    exposure_time REAL,
    cfpipe_version TEXT,
    crds_context TEXT,
    jwst_version TEXT,
    date_obs TEXT,
    reduced_at TEXT,
    redshift_auto REAL,
    dq_flags INTEGER DEFAULT 0,
    program_slug TEXT,
    observation TEXT,
    field TEXT,
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
CREATE INDEX IF NOT EXISTS idx_spectra_crds_context ON spectra(crds_context);
CREATE INDEX IF NOT EXISTS idx_spectra_cfpipe_version ON spectra(cfpipe_version);

-- storage_objects: local mirror of the server registry (epic #210). The single
-- local↔cloud availability layer for every product type and BOTH transfer
-- directions. Server columns mirror /api/v1/sync/storage (content_hash is the
-- server's authoritative whole-file hash; sci_dq_hash the science-only NIRCam
-- identity). The local_* columns track what's materialized on disk (pull side);
-- the pushed_* columns track what this machine last confirmed in the cloud
-- (push side: pushed_identity is the content identity — sci_dq for NIRCam
-- exposures, whole-file otherwise — at last successful push, pushed_mtime/size
-- the file stat at that moment, giving the rsync-style skip-without-rehashing
-- fast path). Both families are preserved across metadata refreshes.
CREATE TABLE IF NOT EXISTS storage_objects (
    storage_key TEXT PRIMARY KEY,
    id INTEGER,
    backend TEXT,
    bucket TEXT,
    content_hash TEXT,
    sci_dq_hash TEXT,
    size_bytes INTEGER,
    content_type TEXT,
    product_type TEXT,
    instrument TEXT,
    status TEXT,
    observation TEXT,
    field TEXT,
    filter TEXT,
    spectrum_id TEXT,
    exposure_ref TEXT,
    deployment_id INTEGER,
    cfpipe_version TEXT,
    created_at TEXT,
    updated_at TEXT,
    local_path TEXT,
    local_file_hash TEXT,
    local_file_mtime REAL,
    local_file_size INTEGER,
    synced_at TEXT,
    pushed_identity TEXT,
    pushed_mtime REAL,
    pushed_size INTEGER,
    pushed_at TEXT,
    _synced_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_so_observation ON storage_objects(observation);
CREATE INDEX IF NOT EXISTS idx_so_product_type ON storage_objects(product_type);
CREATE INDEX IF NOT EXISTS idx_so_spectrum_id ON storage_objects(spectrum_id);
CREATE INDEX IF NOT EXISTS idx_so_obs_product ON storage_objects(observation, product_type);
CREATE INDEX IF NOT EXISTS idx_so_field_filter ON storage_objects(field, filter);

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

    def get_meta(self, key: str) -> Optional[str]:
        """Read a value from the ``_meta`` key/value table (None if absent)."""
        row = self._conn.execute(
            "SELECT value FROM _meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        """Upsert a value into the ``_meta`` key/value table."""
        self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)", (key, str(value))
        )
        self._conn.commit()

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

            # INSERT OR REPLACE: reconciliation on the server can rewrite
            # either side of the (id, object_id) pair without touching the
            # other (e.g. a sub-arcsec ra/dec shift bumps the IAU name but
            # keeps the row id), so a single ON CONFLICT clause can't catch
            # the crossed case. There are no local-only columns on objects,
            # so wholesale replacement is safe.
            self._conn.execute(
                """
                INSERT OR REPLACE INTO objects
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
        """Insert or update spectra (science metadata) from /sync/spectra.

        File keys, hashes, and local-download state live in storage_objects
        (synced separately); any fits_path/file_hash in the payload is ignored.
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0

        for spec in spectra_data:
            self._conn.execute(
                """
                INSERT INTO spectra
                    (id, spectrum_id, target_id, object_id, grating,
                     signal_to_noise, exposure_time,
                     cfpipe_version, crds_context, jwst_version, date_obs, reduced_at,
                     redshift_auto, dq_flags,
                     program_slug, observation, field,
                     created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(spectrum_id) DO UPDATE SET
                    target_id=excluded.target_id,
                    object_id=excluded.object_id,
                    grating=excluded.grating,
                    signal_to_noise=excluded.signal_to_noise,
                    exposure_time=excluded.exposure_time,
                    cfpipe_version=excluded.cfpipe_version,
                    crds_context=excluded.crds_context,
                    jwst_version=excluded.jwst_version,
                    date_obs=excluded.date_obs,
                    reduced_at=excluded.reduced_at,
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
                    spec.get("signal_to_noise"),
                    spec.get("exposure_time"),
                    spec.get("cfpipe_version"),
                    spec.get("crds_context"),
                    spec.get("jwst_version"),
                    spec.get("date_obs"),
                    spec.get("reduced_at"),
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
        crds_context: Optional[List[str]] = None,
        cfpipe_version: Optional[List[str]] = None,
        reduced_after: Optional[str] = None,
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

        ``crds_context`` / ``cfpipe_version`` restrict to spectra reduced
        against the given CRDS pmap(s) / pipeline version(s); ``reduced_after``
        (an ISO-8601 string) keeps only spectra whose ``reduced_at`` is on or
        after it — letting a user carve a calibration-homogeneous subsample
        without opening any FITS.
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

        if crds_context:
            placeholders = ",".join("?" * len(crds_context))
            where.append(f"s.crds_context IN ({placeholders})")
            params.extend(crds_context)

        if cfpipe_version:
            placeholders = ",".join("?" * len(cfpipe_version))
            where.append(f"s.cfpipe_version IN ({placeholders})")
            params.extend(cfpipe_version)

        if reduced_after:
            where.append("s.reduced_at >= ?")
            params.append(reduced_after)

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

        # File info (key, hash, size, local availability) lives in storage_objects
        # now; surface it on read via the spectrum's active nirspec_spec row so
        # callers (export, open_spectrum) keep seeing fits_path/local_path/file_hash.
        sql = f"""
            SELECT s.*,
                   o.redshift AS redshift,
                   o.redshift_quality AS redshift_quality,
                   o.ra AS ra,
                   o.dec AS dec,
                   so.storage_key AS fits_path,
                   so.content_hash AS file_hash,
                   so.size_bytes AS file_size,
                   so.local_path AS local_path,
                   {distance_expr}
            FROM spectra s
            LEFT JOIN objects o ON o.object_id = s.object_id
            LEFT JOIN storage_objects so
                   ON so.spectrum_id = s.spectrum_id
                  AND so.product_type = 'nirspec_spec'
                  AND so.status = 'active'
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
        """Single spectrum lookup by spectrum_id (with file info joined in)."""
        row = self._conn.execute(
            """
            SELECT s.*,
                   so.storage_key AS fits_path,
                   so.content_hash AS file_hash,
                   so.size_bytes AS file_size,
                   so.local_path AS local_path
            FROM spectra s
            LEFT JOIN storage_objects so
                   ON so.spectrum_id = s.spectrum_id
                  AND so.product_type = 'nirspec_spec'
                  AND so.status = 'active'
            WHERE s.spectrum_id = ?
            """,
            (spectrum_id,),
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
        """Delete spectra (science rows) not seen in the latest full sync.

        Orphaned local files are reported by purge_stale_storage_objects now
        (file/download state lives in storage_objects).
        """
        cursor = self._conn.execute(
            "DELETE FROM spectra WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged = cursor.rowcount
        self._conn.commit()
        return {"purged_spectra": purged, "orphaned_files": []}

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
        """Per-observation summary with program, field, and finals download status.

        spectrum_count is the science catalog count; downloaded_count is how many
        of those finals are materialized locally, read from the storage_objects
        mirror (product_type='nirspec_spec').
        """
        rows = self._conn.execute(
            """
            SELECT
                s.observation,
                s.program_slug,
                s.field,
                COUNT(DISTINCT s.object_id) AS object_count,
                COUNT(*) AS spectrum_count,
                COUNT(CASE WHEN so.local_path IS NOT NULL THEN 1 END) AS downloaded_count
            FROM spectra s
            LEFT JOIN storage_objects so
                   ON so.spectrum_id = s.spectrum_id
                  AND so.product_type = 'nirspec_spec'
                  AND so.status = 'active'
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
    # storage_objects mirror — the single download/availability layer
    # -------------------------------------------------------------------------
    def upsert_storage_objects(self, rows: List[dict]) -> int:
        """Insert/update the local storage_objects mirror from /sync/storage.

        Preserves the local_* (pull) and pushed_* (push) bookkeeping on
        conflict — those track this machine's state and must survive a metadata
        refresh. ``content_hash`` is the server's authoritative whole-file hash
        (the staleness reference); ``sci_dq_hash`` the server's science-only
        NIRCam identity (the push-dedup reference).
        """
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for r in rows:
            self._conn.execute(
                """
                INSERT INTO storage_objects
                    (storage_key, id, backend, bucket, content_hash, sci_dq_hash,
                     size_bytes, content_type, product_type, instrument, status,
                     observation, field, filter, spectrum_id, exposure_ref,
                     deployment_id, cfpipe_version, created_at, updated_at, _synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(storage_key) DO UPDATE SET
                    id=excluded.id,
                    backend=excluded.backend,
                    bucket=excluded.bucket,
                    content_hash=excluded.content_hash,
                    sci_dq_hash=excluded.sci_dq_hash,
                    size_bytes=excluded.size_bytes,
                    content_type=excluded.content_type,
                    product_type=excluded.product_type,
                    instrument=excluded.instrument,
                    status=excluded.status,
                    observation=excluded.observation,
                    field=excluded.field,
                    filter=excluded.filter,
                    spectrum_id=excluded.spectrum_id,
                    exposure_ref=excluded.exposure_ref,
                    deployment_id=excluded.deployment_id,
                    cfpipe_version=excluded.cfpipe_version,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    _synced_at=excluded._synced_at
                """,
                (
                    r.get("storage_key"),
                    r.get("id"),
                    r.get("backend"),
                    r.get("bucket"),
                    r.get("content_hash"),
                    r.get("sci_dq_hash"),
                    r.get("size_bytes"),
                    r.get("content_type"),
                    r.get("product_type"),
                    r.get("instrument"),
                    r.get("status"),
                    r.get("observation"),
                    r.get("field"),
                    r.get("filter"),
                    r.get("spectrum_id"),
                    r.get("exposure_ref"),
                    r.get("deployment_id"),
                    r.get("cfpipe_version"),
                    r.get("created_at"),
                    r.get("updated_at"),
                    now,
                ),
            )
            count += 1
        self._conn.commit()
        return count

    # ------------------------------------------------------------------
    # Push-side bookkeeping (`campfire push` / deploy dedup)
    # ------------------------------------------------------------------

    def get_storage_rows_by_keys(self, keys: List[str]) -> Dict[str, dict]:
        """Fetch mirror rows for specific storage keys → ``{storage_key: row}``.

        The push planner's read: candidate keys come from local discovery, and
        the returned rows carry both the server identities (content_hash /
        sci_dq_hash) and this machine's pushed_* fast-path state.
        """
        out: Dict[str, dict] = {}
        CHUNK = 500
        for i in range(0, len(keys), CHUNK):
            chunk = keys[i:i + CHUNK]
            ph = ",".join("?" * len(chunk))
            rows = self._conn.execute(
                f"SELECT * FROM storage_objects WHERE storage_key IN ({ph})",
                chunk,
            ).fetchall()
            for row in rows:
                d = dict(row)
                out[d["storage_key"]] = d
        return out

    def mark_object_pushed(
        self,
        storage_key: str,
        identity: Optional[str],
        mtime: Optional[float],
        size: Optional[int],
        *,
        commit: bool = True,
    ) -> None:
        """Record that the local file for ``storage_key`` is confirmed in-cloud.

        ``identity`` is the content identity that was pushed (sci_dq hash for
        NIRCam exposures, whole-file sha256 otherwise); ``mtime``/``size`` are
        the file's stat at that moment — together they are the next run's
        skip-without-rehashing fast path. Also called for dedup-skipped files
        whose identity matched the cloud (refreshing the stat), so a
        science-identical re-save takes the fast path from then on.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE storage_objects SET pushed_identity = ?, pushed_mtime = ?,
               pushed_size = ?, pushed_at = ? WHERE storage_key = ?""",
            (identity, mtime, size, now, storage_key),
        )
        if commit:
            self._conn.commit()

    def commit(self) -> None:
        """Flush pending writes (for callers batching mark_object_* calls)."""
        self._conn.commit()

    def get_max_storage_updated_at(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT MAX(updated_at) FROM storage_objects"
        ).fetchone()
        return row[0] if row and row[0] else None

    def purge_stale_storage_objects(self, sync_timestamp: str) -> dict:
        """Delete mirror rows not seen in the latest full sync.

        Returns local files that are now orphaned (their registry row went away)
        so the caller can optionally clean them up.
        """
        orphaned = self._conn.execute(
            """SELECT local_path FROM storage_objects
               WHERE _synced_at < ? AND local_path IS NOT NULL""",
            (sync_timestamp,),
        ).fetchall()
        orphaned_files = [r["local_path"] for r in orphaned]

        cursor = self._conn.execute(
            "DELETE FROM storage_objects WHERE _synced_at < ?",
            (sync_timestamp,),
        )
        purged = cursor.rowcount
        self._conn.commit()
        return {"purged": purged, "orphaned_files": orphaned_files}

    def get_pending_objects(
        self,
        observations: Optional[List[str]] = None,
        product_types: Optional[List[str]] = None,
        gratings: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
        filters: Optional[List[str]] = None,
    ) -> Dict[str, List[dict]]:
        """Find storage objects that need downloading, grouped by scope.

        A row is pending if it isn't materialized locally, or its local hash no
        longer matches the server's content_hash. ``gratings`` narrows NIRSpec
        finals (joined to the science spectrum); the registry has no grating
        column, so exposure-level intermediates are always included. ``filters``
        narrows per-filter NIRCam products against the typed ``filter`` scope
        column; rows without a filter (NIRSpec, field-level) are always included,
        mirroring the grating rule. ``fields`` selects field-scoped NIRCam rows
        (``observation IS NULL``); results are grouped by ``observation`` for
        NIRSpec and by ``field`` for NIRCam.
        """
        where = ["so.status = 'active'"]
        params: list = []

        if product_types:
            ph = ",".join("?" * len(product_types))
            where.append(f"so.product_type IN ({ph})")
            params.extend(product_types)

        if filters:
            ph = ",".join("?" * len(filters))
            where.append(f"(so.filter IS NULL OR UPPER(so.filter) IN ({ph}))")
            params.extend(f.upper() for f in filters)

        # Scope: observations (NIRSpec) and/or fields (NIRCam, observation NULL).
        scope = []
        if observations:
            ph = ",".join("?" * len(observations))
            scope.append(f"so.observation IN ({ph})")
            params.extend(observations)
        if fields:
            ph = ",".join("?" * len(fields))
            scope.append(f"so.field IN ({ph})")
            params.extend(fields)
        if scope:
            where.append("(" + " OR ".join(scope) + ")")

        join = ""
        if gratings:
            join = "LEFT JOIN spectra sp ON sp.spectrum_id = so.spectrum_id"
            ph = ",".join("?" * len(gratings))
            where.append(f"(so.spectrum_id IS NULL OR UPPER(sp.grating) IN ({ph}))")
            params.extend(g.upper() for g in gratings)

        where.append(
            "(so.local_file_hash IS NULL OR "
            "(so.content_hash IS NOT NULL AND so.local_file_hash != so.content_hash))"
        )
        where_sql = " AND ".join(where)

        rows = self._conn.execute(
            f"""
            SELECT so.storage_key, so.content_hash, so.size_bytes, so.product_type,
                   so.observation, so.field, so.filter, so.spectrum_id, so.exposure_ref,
                   so.local_file_hash
            FROM storage_objects so
            {join}
            WHERE {where_sql}
            ORDER BY so.observation, so.field, so.storage_key
            """,
            params,
        ).fetchall()

        result: Dict[str, List[dict]] = {}
        for row in rows:
            d = dict(row)
            d["status"] = "new" if d["local_file_hash"] is None else "updated"
            # NIRSpec rows key on observation; field-scoped NIRCam rows
            # (observation NULL) key on field.
            result.setdefault(d["observation"] or d.get("field"), []).append(d)
        return result

    def mark_object_synced(
        self,
        storage_key: str,
        local_path: str,
        local_file_hash: Optional[str],
        local_file_size: Optional[int] = None,
        local_file_mtime: Optional[float] = None,
    ) -> None:
        """Record that a storage object has been materialized locally."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE storage_objects SET local_path = ?, local_file_hash = ?,
               local_file_mtime = ?, local_file_size = ?, synced_at = ?
               WHERE storage_key = ?""",
            (local_path, local_file_hash, local_file_mtime, local_file_size, now, storage_key),
        )
        self._conn.commit()

    def get_stale_objects(self) -> List[dict]:
        """Locally materialized objects whose server hash differs from local."""
        rows = self._conn.execute(
            """
            SELECT storage_key, observation, product_type, spectrum_id,
                   local_path, content_hash AS server_hash, local_file_hash
            FROM storage_objects
            WHERE local_path IS NOT NULL
              AND content_hash IS NOT NULL
              AND local_file_hash IS NOT NULL
              AND content_hash != local_file_hash
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def verify_local_objects(
        self,
        products_dir: Path,
        observation: Optional[str] = None,
        product_types: Optional[List[str]] = None,
        show_progress: bool = False,
        *,
        observations: Optional[List[str]] = None,
        fields: Optional[List[str]] = None,
        deep: bool = False,
        max_workers: Optional[int] = None,
    ) -> dict:
        """Reconcile the storage_objects mirror's local state with the filesystem.

        Clears rows whose file vanished, re-checks files whose stat changed, and
        discovers already-present files (a fresh mirror after a schema bump — or
        a reducer tree the pipeline wrote — gets adopted into the ledger).
        Scoped to mirrored, downloadable product types, and optionally to
        ``observations`` (NIRSpec) / ``fields`` (field-scoped NIRCam rows).

        Cheap by design (rsync-style quick check): filesystem state comes from
        ONE bulk directory scan — candidate directories are derived from the
        keys and listed with ``os.scandir`` (READDIRPLUS-friendly, threaded
        across directories), so no per-file stat calls and, by default, **no
        file reads**. A present file whose size matches the server's
        ``size_bytes`` ADOPTS the server ``content_hash`` as its local hash —
        a presumption, not a verification (accepted trade-off: same-size silent
        corruption goes undetected; truncation/partials are caught by the size
        check). A size mismatch clears/skips the row so the object shows as
        pending and ``pull`` re-fetches it.

        ``deep=True`` restores true content hashing (parallel) for files whose
        stat changed or that were newly discovered — the explicit paranoid mode
        behind ``campfire verify --deep``. Where hashing is load-bearing it
        happens elsewhere regardless: downloads stream-hash against
        ``content_hash``, push hashes changed candidates for dedup identity,
        and ``drop-local --verify`` content-verifies before deleting.
        """
        import os as _os
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from campfire_layout import LayoutError

        from ..config import products_relpath
        from ..storage.hashing import default_hash_workers, hash_files_parallel

        now = datetime.now(timezone.utc).isoformat()
        types = list(product_types or DOWNLOADABLE_PRODUCT_TYPES)
        type_ph = ",".join("?" * len(types))
        clauses = [f"product_type IN ({type_ph})", "status = 'active'"]
        params: list = list(types)
        obs_list = list(observations or ([observation] if observation else []))
        scope = []
        if obs_list:
            ph = ",".join("?" * len(obs_list))
            scope.append(f"observation IN ({ph})")
            params.extend(obs_list)
        if fields:
            ph = ",".join("?" * len(fields))
            scope.append(f"field IN ({ph})")
            params.extend(fields)
        if scope:
            clauses.append("(" + " OR ".join(scope) + ")")
        where = " AND ".join(clauses)

        rows = self._conn.execute(
            f"""SELECT storage_key, content_hash, size_bytes, local_path,
                       local_file_hash, local_file_mtime, local_file_size
                FROM storage_objects
                WHERE {where}""",
            params,
        ).fetchall()

        # Map each row to its canonical relpath (pure string work, no I/O) and
        # collect the parent directories — the whole candidate set lives in
        # ~(#observations + #field×filter) directories, so one listing per
        # directory replaces one stat per file.
        candidates: list = []  # (row, rel_path_str)
        dirs: set = set()
        for row in rows:
            rel = row["local_path"]
            if rel is None:
                try:
                    rel = products_relpath(row["storage_key"])
                except (LayoutError, ValueError):
                    continue
            candidates.append((row, rel))
            dirs.add(_os.path.dirname(rel))

        # --- One bulk scan: {relpath: (size, mtime)} for every regular file in
        # the candidate directories. scandir batches attributes per directory
        # (NFS READDIRPLUS / macOS getattrlistbulk); directories scan in a
        # thread pool so per-directory round-trips overlap.
        def _scan(rel_dir: str) -> list:
            out = []
            try:
                with _os.scandir(products_dir / rel_dir) as it:
                    for entry in it:
                        try:
                            if entry.is_file(follow_symlinks=False):
                                st = entry.stat(follow_symlinks=False)
                                out.append((f"{rel_dir}/{entry.name}" if rel_dir else entry.name,
                                            (st.st_size, st.st_mtime)))
                        except OSError:
                            continue
            except OSError:
                pass  # directory absent: every file in it is absent
            return out

        fs_stats: dict = {}
        dir_list = sorted(dirs)
        workers = max(1, min(max_workers or default_hash_workers(), len(dir_list) or 1))
        pbar = None
        if show_progress and dir_list:
            from tqdm import tqdm
            pbar = tqdm(total=len(dir_list), desc="Scanning local tree", unit="dir")
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for fut in as_completed([ex.submit(_scan, d) for d in dir_list]):
                    fs_stats.update(fut.result())
                    if pbar:
                        pbar.update(1)
        finally:
            if pbar:
                pbar.close()

        # --- In-memory classification. Every SQLite write stays on this thread.
        cleared = rehashed = discovered = mismatched = 0
        to_hash: list = []  # deep mode: (row, rel, size, mtime, is_discovery)

        def _adoptable(row, size: int) -> bool:
            h = row["content_hash"]
            return (row["size_bytes"] is not None and size == row["size_bytes"]
                    and h is not None and h.startswith("sha256:"))

        for row, rel in candidates:
            stat = fs_stats.get(rel)
            tracked = row["local_path"] is not None

            if stat is None:
                if tracked:
                    # File vanished — clear the pull-side bookkeeping.
                    self._conn.execute(
                        """UPDATE storage_objects SET local_path = NULL,
                           local_file_hash = NULL, local_file_mtime = NULL,
                           local_file_size = NULL, synced_at = NULL
                           WHERE storage_key = ?""",
                        (row["storage_key"],),
                    )
                    cleared += 1
                continue

            size, mtime = stat
            if (
                tracked
                and row["local_file_mtime"] is not None
                and row["local_file_size"] is not None
                and abs(mtime - row["local_file_mtime"]) < 0.001
                and size == row["local_file_size"]
            ):
                continue  # unchanged since last recorded — nothing to do

            # New file, or stat changed since recorded.
            if deep:
                to_hash.append((row, rel, size, mtime, not tracked))
            elif _adoptable(row, size):
                self._conn.execute(
                    """UPDATE storage_objects SET local_path = ?, local_file_hash = ?,
                       local_file_mtime = ?, local_file_size = ?, synced_at = ?
                       WHERE storage_key = ?""",
                    (rel, row["content_hash"], mtime, size, now, row["storage_key"]),
                )
                if tracked:
                    rehashed += 1
                else:
                    discovered += 1
            else:
                # Size disagrees with the cloud object (or no authoritative
                # hash to adopt): a partial/foreign file. Leave it out of the
                # ledger so the object reads as pending and pull re-fetches.
                mismatched += 1
                if tracked:
                    self._conn.execute(
                        """UPDATE storage_objects SET local_path = NULL,
                           local_file_hash = NULL, local_file_mtime = NULL,
                           local_file_size = NULL, synced_at = NULL
                           WHERE storage_key = ?""",
                        (row["storage_key"],),
                    )
                    cleared += 1

        # --- deep mode: true content hashing, parallel, only what changed.
        if to_hash:
            hashes = hash_files_parallel(
                [products_dir / rel for _, rel, _, _, _ in to_hash],
                max_workers=max_workers,
                progress_desc="Hashing changed/discovered files" if show_progress else None,
            )
            for row, rel, size, mtime, is_discovery in to_hash:
                actual_hash = hashes[Path(products_dir / rel)][0]
                self._conn.execute(
                    """UPDATE storage_objects SET local_path = ?, local_file_hash = ?,
                       local_file_mtime = ?, local_file_size = ?, synced_at = ?
                       WHERE storage_key = ?""",
                    (rel, actual_hash, mtime, size, now, row["storage_key"]),
                )
                if is_discovery:
                    discovered += 1
                else:
                    rehashed += 1

        if cleared or discovered or rehashed:
            self._conn.commit()
        return {"cleared": cleared, "rehashed": rehashed,
                "discovered": discovered, "mismatched": mismatched}

    def remove_observation_objects(self, observation: str) -> int:
        """Clear local-download state for all storage objects in an observation."""
        cursor = self._conn.execute(
            """UPDATE storage_objects
               SET local_path = NULL, local_file_hash = NULL,
                   local_file_mtime = NULL, local_file_size = NULL, synced_at = NULL
               WHERE observation = ?""",
            (observation,),
        )
        count = cursor.rowcount
        self._conn.commit()
        return count

    def get_object_stats(
        self, observation: str, product_types: Optional[List[str]] = None
    ) -> dict:
        """Downloaded count + bytes for an observation's objects (optionally by type)."""
        clauses = ["local_path IS NOT NULL", "observation = ?", "status = 'active'"]
        params: list = [observation]
        if product_types:
            ph = ",".join("?" * len(product_types))
            clauses.append(f"product_type IN ({ph})")
            params.extend(product_types)
        row = self._conn.execute(
            f"""SELECT COUNT(*) AS synced_count, COALESCE(SUM(size_bytes), 0) AS total_bytes
                FROM storage_objects WHERE {' AND '.join(clauses)}""",
            params,
        ).fetchone()
        return dict(row) if row else {"synced_count": 0, "total_bytes": 0}

    def get_object_summary(self) -> List[dict]:
        """Per-observation finals/intermediates availability + local materialization.

        Drives `campfire status`. Counts active mirror rows by product class and
        how many are present on disk. Includes obs that have only intermediates
        (e.g. an admin's draft with no published finals).
        """
        rows = self._conn.execute(
            """
            SELECT
                observation,
                field,
                COUNT(CASE WHEN product_type IN ('nirspec_spec') THEN 1 END) AS finals_available,
                COUNT(CASE WHEN product_type IN ('nirspec_spec') AND local_path IS NOT NULL THEN 1 END) AS finals_local,
                COUNT(CASE WHEN product_type IN ('nirspec_spectrum_exposure') THEN 1 END) AS intermediates_available,
                COUNT(CASE WHEN product_type IN ('nirspec_spectrum_exposure') AND local_path IS NOT NULL THEN 1 END) AS intermediates_local,
                COALESCE(SUM(CASE WHEN local_path IS NOT NULL THEN size_bytes END), 0) AS local_bytes,
                COALESCE(SUM(size_bytes), 0) AS available_bytes
            FROM storage_objects
            WHERE status = 'active'
            GROUP BY observation, field
            ORDER BY observation
            """
        ).fetchall()
        return [dict(r) for r in rows]

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
        """Return the relative local_path for a spectrum's final FITS if downloaded."""
        row = self._conn.execute(
            """SELECT local_path FROM storage_objects
               WHERE spectrum_id = ? AND product_type = 'nirspec_spec'
                 AND status = 'active' AND local_path IS NOT NULL""",
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
