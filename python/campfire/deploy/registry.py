"""
storage_objects registry — the shadow index of cloud storage (epic #210, F1).

Every object CAMPFIRE deploys to the data bucket gets one row here, keyed by its
canonical storage key (from the ``campfire_layout`` contract). The registry is:

* written **going-forward** by deploy (``build_registry_rows`` +
  ``upsert_storage_objects``, called from the deploy hooks after a successful
  upload), and
* **backfilled** from historical pointers and bucket orphans
  (``backfill`` / ``reconcile`` / ``budget``, wired to the
  ``campfire deploy registry`` CLI subgroup).

It is a **shadow** index in F1: additive and inert. Nothing reads it as
authoritative until a coverage gate (``reconcile``) proves 100% of live pointers
have rows and deploy has written rows for a full cycle.

content_hash is scheme-prefixed: ``sha256:<hex>`` is authoritative (we hash the
local file we just uploaded; spectra backfill reuses the stored ``file_hash``);
``etag:<hex>`` is provisional, taken from an S3 ``LIST``/``HEAD`` (no GET) for
backfilled/orphan objects with no stored sha256. The A1 copy+verify pass (#215)
upgrades ``etag:`` → ``sha256:`` when it reads the bytes to copy.

Tiles are intentionally **not** registered per-object (decision F1-B): they stay
on R2, number in the tens of thousands per field/filter, and are already
byte-aggregated in ``map_layers.total_size_bytes`` (the budget RPC unions both).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from campfire_layout import bucket_for, is_known_key, parse_key
from campfire_layout.products import get as get_product

from campfire.deploy.r2 import UploadTask

# Streamed-hash chunk size (1 MiB).
_HASH_CHUNK = 1 << 20

# Supabase upsert batch size — matches batch_upsert_spectra.
UPSERT_BATCH = 500


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def hash_file(path: Path) -> tuple[str, int]:
    """Return ``('sha256:<hex>', size_bytes)`` for a local file, streamed."""
    h = hashlib.sha256()
    size = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return f"sha256:{h.hexdigest()}", size


def normalize_etag(etag: str | None) -> str | None:
    """Coerce an S3 ETag to a ``'etag:<hex>'`` content_hash token.

    S3 returns the ETag wrapped in double quotes (and multipart uploads append a
    ``-<partcount>`` suffix that is not an md5 — still fine as a provisional
    token). Returns None for a missing/empty ETag.
    """
    if not etag:
        return None
    cleaned = etag.strip().strip('"')
    if not cleaned:
        return None
    return f"etag:{cleaned}"


# ---------------------------------------------------------------------------
# Key → row mapping
# ---------------------------------------------------------------------------

def _exposure_ref_for(product_type: str, filename: str) -> str | None:
    """Stable per-exposure reference for exposure-level intermediates (epic #210).

    For ``nirspec_spectrum_exposure`` the canonical filename
    (``{root}_{nod}_nrs[12]_{source}.fits``) IS the natural unique exposure key, so
    the ref is the filename stem (drop ``.fits``). Backs the partial-unique
    ``(product_type, exposure_ref) WHERE status='active'`` registry contract
    (one current object per product/exposure). None for non-exposure products
    (their exposure_ref stays NULL; NULLs are distinct, so finals never collide).
    """
    if product_type == 'nirspec_spectrum_exposure' and filename.endswith('.fits'):
        return filename[: -len('.fits')]
    return None


def _spectrum_id_for(filename: str) -> str | None:
    """Derive the owning spectrum_id from a spectrum-family filename.

    Mirrors the ``spectra.spectrum_id`` GENERATED column (strip the trailing
    ``_spec.fits``); spectrum JSON (``_spec.json``) belongs to the same spectrum.
    Returns None for products that aren't tied to a single spectrum (zfit, rgb,
    sed, photometry, nircam previews).
    """
    for suffix in ('_spec.fits', '_spec.json'):
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return None


def row_for_key(
    storage_key: str,
    *,
    backend: str,
    content_hash: str,
    size_bytes: int,
    content_type: str,
    deployment_id: Optional[int] = None,
    uploaded_by: Optional[str] = None,
    cfpipe_version: Optional[str] = None,
    status: str = 'active',
    bucket: Optional[str] = None,
) -> dict | None:
    """Build one ``storage_objects`` row dict from a storage key + integrity info.

    Resolves ``product_type``/scope from the key via the ``campfire_layout``
    contract — no ad-hoc parsing. Returns None for keys that should not be
    registered (tiles, or non-cloud products), so callers can ``filter(None)``.
    """
    parsed = parse_key(storage_key, bucket=bucket)
    product_type = parsed.product_type
    obj_bucket = bucket or bucket_for(product_type)

    # Decision F1-B: tiles are not indexed per-object.
    if obj_bucket == 'tiles':
        return None

    spec = get_product(product_type)
    instrument = spec.instrument.value if spec.instrument is not None else None
    scope = parsed.scope

    # updated_at bumps on re-deploy so freshness is visible; created_at is left to
    # the column default (preserved across the on-conflict update — it is not in
    # the payload).
    now_iso = datetime.now(timezone.utc).isoformat()

    return {
        'backend': backend,
        'bucket': obj_bucket,
        'storage_key': storage_key,
        'content_hash': content_hash,
        'size_bytes': int(size_bytes),
        'content_type': content_type,
        'product_type': product_type,
        'instrument': instrument,
        'status': status,
        'observation': scope.obs,
        'field': scope.field,
        'spectrum_id': _spectrum_id_for(parsed.filename),
        # Exposure-level intermediates (nirspec_spectrum_exposure) carry a stable
        # exposure_ref (filename stem) backing the partial-unique registry contract;
        # finals/previews leave it NULL (NULLs are distinct, so they never collide).
        'exposure_ref': _exposure_ref_for(product_type, parsed.filename),
        'deployment_id': deployment_id,
        'cfpipe_version': cfpipe_version,
        'uploaded_by': uploaded_by,
        'updated_at': now_iso,
    }


def build_registry_rows(
    tasks: Iterable[UploadTask],
    *,
    backend: str,
    deployment_id: Optional[int] = None,
    uploaded_by: Optional[str] = None,
    cfpipe_version: Optional[str] = None,
    succeeded_keys: Optional[set[str]] = None,
) -> list[dict]:
    """Build ``storage_objects`` rows for the objects an upload actually landed.

    For each :class:`UploadTask` whose ``r2_key`` is in ``succeeded_keys`` (or
    all, if ``succeeded_keys`` is None), hashes the local file and maps the key
    to a row via :func:`row_for_key`. Tiles and non-cloud products are skipped.
    """
    rows: list[dict] = []
    for task in tasks:
        if succeeded_keys is not None and task.r2_key not in succeeded_keys:
            continue
        local = Path(task.local_path)
        if not local.exists():
            continue
        content_hash, size_bytes = hash_file(local)
        row = row_for_key(
            task.r2_key,
            backend=backend,
            content_hash=content_hash,
            size_bytes=size_bytes,
            content_type=task.content_type,
            deployment_id=deployment_id,
            uploaded_by=uploaded_by,
            cfpipe_version=cfpipe_version,
        )
        if row is not None:
            rows.append(row)
    return rows


def upsert_storage_objects(client, rows: list[dict], batch_size: int = UPSERT_BATCH) -> int:
    """Upsert registry rows keyed on ``(backend, bucket, storage_key)``.

    Idempotent: a re-deploy of the same key overwrites the existing row in place
    (stable legacy keys ⇒ no supersede churn in F1). Returns rows written.
    """
    if not rows:
        return 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        client.table('storage_objects').upsert(
            batch, on_conflict='backend,bucket,storage_key'
        ).execute()
    return len(rows)


# ---------------------------------------------------------------------------
# Reconciliation (the coverage gate) — pure set logic, unit-testable
# ---------------------------------------------------------------------------

class ReconcileReport:
    """Result of comparing live DB pointers, registry rows, and bucket objects."""

    def __init__(self, missing: set[str], dangling: set[str], orphans: set[str],
                 adoptable: set[str]):
        self.missing = missing      # live pointer with no registry row (coverage gap)
        self.dangling = dangling    # registry row whose key is absent from the bucket
        self.orphans = orphans      # bucket key with no registry row
        self.adoptable = adoptable  # orphans that parse to a known product (insertable)

    @property
    def covered(self) -> bool:
        """True when every live pointer has a registry row (the F1 gate)."""
        return not self.missing

    def summary(self) -> str:
        return (
            f"coverage: {'OK' if self.covered else 'GAP'} "
            f"(missing={len(self.missing)}, dangling={len(self.dangling)}, "
            f"orphans={len(self.orphans)}, adoptable={len(self.adoptable)})"
        )


def compute_reconcile(
    live_pointers: Iterable[str],
    registry_keys: Iterable[str],
    bucket_keys: Optional[Iterable[str]] = None,
) -> ReconcileReport:
    """Classify the three storage vocabularies against each other.

    ``live_pointers`` = the union of denormalized DB storage keys
    (spectra.fits_path, nircam_images.file_path, nircam_exposures png paths).
    ``registry_keys`` = storage_objects.storage_key for the data bucket.
    ``bucket_keys`` = actual object keys from a bucket LIST (optional; when None,
    dangling/orphan detection is skipped — coverage is still computed).
    """
    live = {k for k in live_pointers if k}
    registry = {k for k in registry_keys if k}
    missing = live - registry

    if bucket_keys is None:
        return ReconcileReport(missing, set(), set(), set())

    bucket = {k for k in bucket_keys if k}
    dangling = registry - bucket
    orphans = bucket - registry
    adoptable = {k for k in orphans if is_known_key(k)}
    return ReconcileReport(missing, dangling, orphans, adoptable)


# ---------------------------------------------------------------------------
# DB / bucket helpers for the CLI commands
# ---------------------------------------------------------------------------

def _iter_rows(client, table: str, columns: str, page: int = 1000) -> Iterator[dict]:
    """Page through a Supabase table, yielding row dicts."""
    start = 0
    while True:
        resp = client.table(table).select(columns).range(start, start + page - 1).execute()
        data = resp.data or []
        for row in data:
            yield row
        if len(data) < page:
            break
        start += page


def live_pointers(client) -> dict[str, list[str]]:
    """Collect the live denormalized storage pointers, grouped by source table."""
    spectra = [r['fits_path'] for r in _iter_rows(client, 'spectra', 'fits_path') if r.get('fits_path')]
    nircam_images = [r['file_path'] for r in _iter_rows(client, 'nircam_images', 'file_path') if r.get('file_path')]
    nircam_exposures: list[str] = []
    for r in _iter_rows(client, 'nircam_exposures', 'png_path, full_png_path'):
        if r.get('png_path'):
            nircam_exposures.append(r['png_path'])
        if r.get('full_png_path'):
            nircam_exposures.append(r['full_png_path'])
    return {
        'spectra': spectra,
        'nircam_images': nircam_images,
        'nircam_exposures': nircam_exposures,
    }


def registry_keys(client, bucket: str = 'data') -> list[str]:
    """All storage keys currently in the registry for a bucket."""
    return [
        r['storage_key']
        for r in _iter_rows(client, 'storage_objects', 'storage_key, bucket')
        if r.get('bucket') == bucket and r.get('storage_key')
    ]


# ---------------------------------------------------------------------------
# delete-local interlock (epic #210, B4)
# ---------------------------------------------------------------------------

class DeleteLocalPlan:
    """What ``delete-local`` would do, after the verified-in-cloud interlock.

    Only files that have an *active registry row with a sha256 hash* are ever
    candidates — so a file is never deleted unless a verified cloud copy exists.
    ``--verify`` additionally hashes the local file and requires it to match the
    registry hash (the cloud copy is byte-identical to what we're about to drop).
    """

    def __init__(self):
        self.deletable: list[tuple] = []   # (local_path: Path, key, size_bytes)
        self.skipped: list[tuple] = []     # (local_path: Path, key, reason)
        self.absent: list[str] = []        # registered keys with no local file

    @property
    def total_bytes(self) -> int:
        return sum(sz for _, _, sz in self.deletable)


def plan_delete_local(
    client, observation: str, products_root: Path, *,
    verify: bool = False, bucket: str = 'data',
) -> DeleteLocalPlan:
    """Build a safe delete plan for an observation's local product files.

    Enumerates the observation's *active registry rows* (objects known to be in
    the cloud), maps each to its local path via the layout contract, and clears
    only those that pass the interlock. Files with no registry row are never
    touched — they are not in the candidate set.
    """
    from campfire.config import products_relpath

    resp = (
        client.table('storage_objects')
        .select('storage_key, content_hash, size_bytes')
        .eq('observation', observation)
        .eq('bucket', bucket)
        .eq('status', 'active')
        .execute()
    )
    rows = resp.data or []
    plan = DeleteLocalPlan()
    for r in rows:
        key = r.get('storage_key')
        if not key:
            continue
        chash = r.get('content_hash') or ''
        local = Path(products_root) / products_relpath(key)
        if not local.exists():
            plan.absent.append(key)
            continue
        # Interlock: the cloud copy must carry an authoritative sha256 (an etag is
        # provisional — never delete a local file against an unverified cloud hash).
        if not chash.startswith('sha256:'):
            plan.skipped.append((local, key, 'registry hash is provisional (etag), not sha256'))
            continue
        if verify:
            local_hash, _ = hash_file(local)
            if local_hash != chash:
                plan.skipped.append((local, key, 'local hash != cloud hash'))
                continue
        size = r.get('size_bytes') or local.stat().st_size
        plan.deletable.append((local, key, int(size)))
    return plan


def _data_client_and_bucket(config: dict):
    """Resolve a boto3 client + bucket name for the data backend (LIST/HEAD).

    Backfill/reconcile need to read object metadata, which the presigned-URL
    path can't do — so they require data-backend credentials.
    """
    from campfire.deploy.backend import make_s3_client, resolve_backend
    bcfg = resolve_backend(config, 'data')
    return make_s3_client(bcfg), bcfg.bucket, bcfg.backend


def list_bucket_keys(config: dict, prefix: str = '') -> Iterator[str]:
    """Yield every object key under ``prefix`` in the data bucket (paginated)."""
    client, bucket, _ = _data_client_and_bucket(config)
    token = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': prefix}
        if token:
            kwargs['ContinuationToken'] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            yield obj['Key']
        if not resp.get('IsTruncated'):
            break
        token = resp.get('NextContinuationToken')


def head_object(config: dict, key: str) -> tuple[str | None, int | None]:
    """HEAD a data-bucket object → (content_hash 'etag:<x>', size_bytes)."""
    client, bucket, _ = _data_client_and_bucket(config)
    resp = client.head_object(Bucket=bucket, Key=key)
    return normalize_etag(resp.get('ETag')), resp.get('ContentLength')


def resolve_backend_label(config: dict) -> str:
    """Data-backend label ('r2'|'osn') from config, defaulting to 'r2'.

    The presigned-URL deploy path has no local backend block; in F1 the data
    bucket is R2, so 'r2' is the correct fallback.
    """
    try:
        from campfire.deploy.backend import resolve_backend
        return resolve_backend(config, 'data').backend
    except Exception:
        return 'r2'


# ---------------------------------------------------------------------------
# Backfill orchestrators (used by `campfire deploy registry backfill`)
# ---------------------------------------------------------------------------

def backfill_spectra(client, *, backend: str, dry_run: bool = False) -> tuple[int, int]:
    """Backfill registry rows from ``spectra`` (authoritative sha256 + size).

    Reuses the stored ``file_hash`` (sha256) and ``file_size``; no bucket access
    needed. Returns ``(n_rows, n_skipped)`` where skipped rows lack a stored
    ``file_hash`` (rare legacy rows — pick those up via the HEAD/orphan pass).
    """
    rows: list[dict] = []
    skipped = 0
    for r in _iter_rows(client, 'spectra', 'fits_path, file_hash, file_size'):
        key = r.get('fits_path')
        if not key:
            continue
        file_hash = r.get('file_hash')
        if not file_hash:
            skipped += 1
            continue
        size = r.get('file_size')
        if size is None:
            skipped += 1
            continue
        row = row_for_key(
            key,
            backend=backend,
            content_hash=f"sha256:{file_hash}",
            size_bytes=int(size),
            content_type='application/fits',
        )
        if row is not None:
            rows.append(row)
    if not dry_run:
        upsert_storage_objects(client, rows)
    return len(rows), skipped


def backfill_via_head(
    client,
    config: dict,
    pointers: Iterable[str],
    *,
    backend: str,
    content_type: str = 'application/octet-stream',
    dry_run: bool = False,
) -> tuple[int, int]:
    """Backfill rows for keys with no stored hash (nircam, orphans) via S3 HEAD.

    HEAD yields size + ETag (no GET), recorded as a provisional ``etag:`` hash.
    Returns ``(n_rows, n_failed)`` where failed = HEAD error or unparseable key.
    """
    rows: list[dict] = []
    failed = 0
    for key in pointers:
        if not key:
            continue
        try:
            content_hash, size = head_object(config, key)
        except Exception:
            failed += 1
            continue
        if not content_hash or size is None:
            failed += 1
            continue
        try:
            row = row_for_key(
                key, backend=backend, content_hash=content_hash,
                size_bytes=int(size), content_type=content_type,
            )
        except Exception:
            failed += 1
            continue
        if row is not None:
            rows.append(row)
    if not dry_run:
        upsert_storage_objects(client, rows)
    return len(rows), failed
