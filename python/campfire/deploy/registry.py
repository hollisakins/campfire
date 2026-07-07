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
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from campfire_layout import (
    KeyScheme,
    LayoutError,
    bucket_for,
    is_known_key,
    parse_key,
    storage_key,
)
from campfire_layout.products import get as get_product

from campfire.deploy.r2 import UploadTask, download_to_path, upload_to_r2

# Streamed-hash chunk size (1 MiB).
_HASH_CHUNK = 1 << 20

# Supabase upsert batch size — matches batch_upsert_spectra.
UPSERT_BATCH = 500

# Dead products that must NOT be registered: rgb/sed static cutouts are superseded
# by the on-the-fly /api/v1/cutout API and aren't served anywhere. row_for_key skips
# them, so neither deploy nor backfill ever indexes them.
UNREGISTERED_PRODUCT_TYPES = frozenset({'rgb', 'sed'})


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


def normalize_sha256(file_hash: str | None) -> str | None:
    """Coerce a stored file hash to a single ``'sha256:<hex>'`` content_hash token.

    Deploy stores ``spectra.file_hash`` **already scheme-prefixed** (``sha256:<hex>``,
    from :func:`hash_file`), so a naive ``f"sha256:{file_hash}"`` would DOUBLE the
    prefix (``sha256:sha256:<hex>``) — which slips past the ``^(sha256|etag):``
    CHECK but then never matches a freshly-computed hash, breaking copy-verify and
    download-verify. Idempotent and defensive: collapses any number of leading
    ``sha256:`` prefixes to exactly one (so a pre-doubled value is repaired rather
    than propagated), prepends ``sha256:`` to a bare hex digest, and returns None
    for an empty value.
    """
    if not file_hash:
        return None
    h = file_hash.strip()
    while h.startswith('sha256:'):
        h = h[len('sha256:'):]
    return f"sha256:{h}" if h else None


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
    """Stable per-exposure reference for exposure-level intermediates (epic #210/#261).

    For ``nirspec_spectrum_exposure`` the canonical filename
    (``{root}_{nod}_nrs[12]_{source}.fits``) IS the natural unique exposure key, for
    ``nircam_exposure`` the JWST rootname (``jw..._nrc{a,b}{1-4,long}.fits``) is, and
    for ``nirspec_rate`` the rate rootname stem (``jw..._nrsN_rate``, one per
    exposure×detector) is, so the ref is the filename stem (drop ``.fits``). Backs
    the partial-unique ``(product_type, exposure_ref) WHERE status='active'`` registry
    contract (one current object per product/exposure). None for non-exposure products
    (their exposure_ref stays NULL; NULLs are distinct, so finals never collide).
    """
    if product_type in ('nirspec_spectrum_exposure', 'nircam_exposure', 'nirspec_rate') and filename.endswith('.fits'):
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
    sci_dq_hash: Optional[str] = None,
) -> dict | None:
    """Build one ``storage_objects`` row dict from a storage key + integrity info.

    Resolves ``product_type``/scope from the key via the ``campfire_layout``
    contract — no ad-hoc parsing. Returns None for keys that should not be
    registered (tiles, dead rgb/sed products, or non-cloud products), so callers
    can ``filter(None)``.
    """
    parsed = parse_key(storage_key, bucket=bucket)
    product_type = parsed.product_type
    obj_bucket = bucket or bucket_for(product_type)

    # Decision F1-B: tiles are not indexed per-object.
    if obj_bucket == 'tiles':
        return None

    # Dead products (rgb/sed) are never registered — single source of truth, so both
    # deploy hooks and backfill/orphan adoption skip them.
    if product_type in UNREGISTERED_PRODUCT_TYPES:
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
        # Science-only change-detection digest (epic #261, N1). Only NIRCam
        # canonical exposures carry one today; None leaves the column NULL.
        'sci_dq_hash': sci_dq_hash,
        'size_bytes': int(size_bytes),
        'content_type': content_type,
        'product_type': product_type,
        'instrument': instrument,
        'status': status,
        'observation': scope.obs,
        'field': scope.field,
        # Typed scope column for per-filter NIRCam products (mosaic/exposure/
        # expmap/previews); None for NIRSpec and field-level products. Lets the
        # registry + download plan scope by filter without parsing keys.
        'filter': scope.filt,
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
    sci_dq_hashes: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Build ``storage_objects`` rows for the objects an upload actually landed.

    For each :class:`UploadTask` whose ``r2_key`` is in ``succeeded_keys`` (or
    all, if ``succeeded_keys`` is None), hashes the local file and maps the key
    to a row via :func:`row_for_key`. Tiles and non-cloud products are skipped.

    ``sci_dq_hashes`` optionally supplies a precomputed ``sha256:<hex>`` science-only
    digest per storage key (NIRCam exposures, epic #261) — the ``content_hash`` is
    still the whole-file sha256, but ``sci_dq_hash`` carries the science-only digest
    that change-detection compares against.
    """
    sci_dq_hashes = sci_dq_hashes or {}
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
            sci_dq_hash=sci_dq_hashes.get(task.r2_key),
        )
        if row is not None:
            rows.append(row)
    return rows


def set_active_deployment(client, keys: list[str], deployment_id: int) -> int:
    """Point active registry rows at a deployment without re-uploading (epic #261).

    Re-deploying a NIRCam field records a NEW deployment, but exposures/mosaics whose
    bytes are unchanged are dedup-skipped (not re-registered), so their rows would
    keep the PRIOR deployment_id — and thus its (possibly draft) visibility. This
    re-points those skipped rows to the current deployment so a publish or re-deploy
    flips the whole field's visibility consistently. Chunked; returns keys touched.
    """
    if not keys:
        return 0
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        (client.table('storage_objects')
         .update({'deployment_id': deployment_id})
         .in_('storage_key', chunk).eq('status', 'active').execute())
    return len(keys)


def fetch_active_content_hashes(client, keys: list[str]) -> dict[str, str]:
    """Return ``{storage_key: content_hash}`` for the active registry rows among *keys*.

    The whole-file change-detection read for the NIRCam mosaic dedup (epic #261,
    N2): deploy skips re-uploading a mosaic whose bytes are unchanged. Only rows
    with an authoritative ``sha256:`` hash are returned (a provisional ``etag:``
    can't be compared to a fresh local sha256, so those always re-upload).
    """
    if not keys:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        resp = (
            client.table('storage_objects')
            .select('storage_key, content_hash')
            .in_('storage_key', chunk)
            .eq('status', 'active')
            .execute()
        )
        for r in (resp.data or []):
            h = r.get('content_hash')
            if h and h.startswith('sha256:'):
                out[r['storage_key']] = h
    return out


def fetch_active_sci_dq_hashes(client, keys: list[str]) -> dict[str, str]:
    """Return ``{storage_key: sci_dq_hash}`` for the active registry rows among *keys*.

    The change-detection read for the NIRCam exposure dedup (epic #261, N1): deploy
    compares each candidate exposure's local ``sha256(SCI+DQ)`` against the stored
    digest and skips the upload when they match. Only rows that both exist and carry
    a non-NULL ``sci_dq_hash`` are returned, so a first-time deploy (or a legacy row
    with no digest) always re-uploads. Chunked to keep the ``in_`` filter small.
    """
    if not keys:
        return {}
    out: dict[str, str] = {}
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        resp = (
            client.table('storage_objects')
            .select('storage_key, sci_dq_hash')
            .in_('storage_key', chunk)
            .eq('status', 'active')
            .execute()
        )
        for r in (resp.data or []):
            if r.get('sci_dq_hash'):
                out[r['storage_key']] = r['sci_dq_hash']
    return out


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


def relocate_objects(client, rows: list[dict]) -> int:
    """Re-home registry rows in place by primary key ``id`` (epic #210 / #215).

    The R2->OSN copy changes a row's ``backend``, ``storage_key`` (legacy->canonical)
    and ``content_hash`` (etag->sha256) all at once.

    This must be a real per-row **UPDATE keyed on ``id``** — NOT an upsert.
    ``upsert_storage_objects``' conflict target is ``(backend, bucket, storage_key)``;
    since all three change, an upsert there would INSERT a second row (and for
    exposure products violate the partial-unique). Upserting on the PK ``id`` does
    NOT help either: PostgreSQL evaluates NOT NULL on the candidate INSERT tuple
    *before* the ``ON CONFLICT`` arbiter, so a partial payload (omitting the other
    NOT NULL columns ``bucket``/``content_type``/``product_type``) raises a not-null
    violation before it can route to DO UPDATE. A bare UPDATE only touches the named
    columns and never validates the omitted ones — the only correct in-place mutation.
    ``product_type``/``exposure_ref``/``status`` are untouched, so the partial-unique
    ``(product_type, exposure_ref) WHERE status='active'`` always holds and exactly
    one active row per logical object survives.

    Each row carries its existing ``id`` plus the changed columns. Updates are
    independent and idempotent; a crash mid-loop is resume-safe because already-
    migrated rows are ``backend='osn'`` and the copy's candidate query only selects
    ``backend='r2'`` rows. Returns rows written.
    """
    if not rows:
        return 0
    for row in rows:
        payload = {k: v for k, v in row.items() if k != 'id'}
        client.table('storage_objects').update(payload).eq('id', row['id']).execute()
    return len(rows)


def relocate_objects_batch(client, rows: list[dict], batch_size: int = UPSERT_BATCH) -> int:
    """Batched in-place relocate via full-row ``upsert(on_conflict='id')``.

    Each row must be **complete** — carry every NOT NULL column — because Postgres
    validates the candidate INSERT tuple before the ON CONFLICT arbiter; a partial
    payload would raise a not-null violation (the same trap that rules out a partial
    id-upsert in :func:`relocate_objects`). Verified against real Postgres: a full-row
    id-upsert updates in place with no duplicate rows.

    This is the throughput path for the parallel copy: S3 workers (boto3, thread-safe)
    hand complete rows to the **single** calling thread, which batches the DB writes
    here. The supabase client must never be used from multiple threads at once — its
    HTTP/2 connection segfaults under concurrent access — so all relocation funnels
    through this one-thread batched call. Returns rows written.
    """
    if not rows:
        return 0
    for i in range(0, len(rows), batch_size):
        client.table('storage_objects').upsert(rows[i:i + batch_size], on_conflict='id').execute()
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


def _canonical_identity(key: str) -> str:
    """Scheme-invariant identity for a storage key: its CANONICAL form, or the key
    verbatim if it does not parse. A1 (#215) re-keyed migrated objects
    legacy→canonical while the denormalized DB pointers (spectra.fits_path,
    nircam_*) stayed legacy, so the vocabularies only line up under
    canonicalization. Non-layout / unsafe keys raise and are compared as-is."""
    try:
        return canonical_key_for(key)
    except Exception:
        return key


def compute_reconcile(
    live_pointers: Iterable[str],
    registry_keys: Iterable[str],
    bucket_keys: Optional[Iterable[str]] = None,
    *,
    danglable_keys: Optional[Iterable[str]] = None,
) -> ReconcileReport:
    """Classify the three storage vocabularies against each other **by canonical
    identity**, reporting the original keys.

    ``live_pointers`` = the union of denormalized DB storage keys
    (spectra.fits_path, nircam_images.file_path, nircam_exposures png paths) — all
    LEGACY. ``registry_keys`` = storage_objects.storage_key (CANONICAL for objects
    A1 migrated to OSN; LEGACY for the rest). ``bucket_keys`` = actual object keys
    from a bucket LIST (optional; when None, dangling/orphan detection is skipped —
    coverage is still computed).

    #249: because A1 re-keyed migrated objects legacy→canonical while fits_path
    stayed legacy, a naïve set difference reports every migrated final as
    ``missing``. We collapse each key to its canonical identity for the set math
    (legacy + canonical duplicates of the same object coincide), then report the
    original keys so the operator sees the real fits_path / bucket key.

    NOTE: ``bucket_keys`` today comes from a single data-bucket (R2) LIST. Objects
    that live on OSN fall into two classes: (a) A1-migrated finals, whose legacy R2
    twin is still in the bucket, so canonicalizing keeps them covered; and (b) since
    #261/N1, **OSN-native** NIRCam FITS/expmaps with *no* R2 twin. A row in class (b)
    is absent from an R2 LIST by design, not because it's dangling — so ``dangling``
    is computed only over ``danglable_keys`` (the registry rows whose home is the
    LISTed backend; the caller passes the R2-backed subset). ``missing``/``orphans``
    still use the full ``registry_keys`` so A1 OSN rows keep covering their R2 twins.
    When R2 *data* is retired (deferred A2 work) and the LIST unions OSN, class (b)
    gets real dangling detection. ``danglable_keys=None`` ⇒ all registry keys are
    danglable (back-compat: the pre-N1 world where every object had an R2 presence).
    """
    # canonical identity -> original key, per vocabulary (last-writer-wins on a
    # legacy+canonical collision is fine: same logical object).
    live = {_canonical_identity(k): k for k in live_pointers if k}
    registry = {_canonical_identity(k): k for k in registry_keys if k}
    missing = {live[c] for c in (live.keys() - registry.keys())}

    if bucket_keys is None:
        return ReconcileReport(missing, set(), set(), set())

    bucket = {_canonical_identity(k): k for k in bucket_keys if k}
    danglable = (registry if danglable_keys is None
                 else {_canonical_identity(k): k for k in danglable_keys if k})
    dangling = {danglable[c] for c in (danglable.keys() - bucket.keys())}
    orphans = {bucket[c] for c in (bucket.keys() - registry.keys())}
    adoptable = {k for k in orphans if is_known_key(k)}
    return ReconcileReport(missing, dangling, orphans, adoptable)


# ---------------------------------------------------------------------------
# DB / bucket helpers for the CLI commands
# ---------------------------------------------------------------------------

def _iter_rows(client, table: str, columns: str, *, order_by: str = 'id',
               page: int = 1000) -> Iterator[dict]:
    """Page through a Supabase table in a STABLE order, yielding row dicts.

    Range/offset pagination WITHOUT an ``ORDER BY`` is non-deterministic in
    Postgres: across pages rows silently repeat and others are skipped, so a full
    scan under-covers a large table (and the gap varies run-to-run). That under-
    coverage is why a backfill over ~50k spectra registered only a partial, shifting
    subset. Ordering by a unique key (the PK ``id`` on every table this is used on:
    spectra / nircam_images / nircam_exposures / storage_objects) makes the scan
    complete and repeatable.
    """
    start = 0
    while True:
        resp = (
            client.table(table).select(columns)
            .order(order_by)
            .range(start, start + page - 1).execute()
        )
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


def registry_keys(client, bucket: str = 'data', backend: str | None = None) -> list[str]:
    """All storage keys currently in the registry for a bucket.

    ``backend`` optionally restricts to one storage backend ('r2' | 'osn') — used by
    reconcile to compute dangling only over rows whose home is the LISTed backend
    (an OSN-native object can't be confirmed against an R2-only bucket LIST).
    """
    return [
        r['storage_key']
        for r in _iter_rows(client, 'storage_objects', 'storage_key, bucket, backend')
        if r.get('bucket') == bucket and r.get('storage_key')
        and (backend is None or r.get('backend') == backend)
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
    client, observation, products_root: Path, *,
    field: Optional[str] = None, verify: bool = False, bucket: str = 'data',
) -> DeleteLocalPlan:
    """Build a safe delete plan for a scope's local product files.

    Scope is an ``observation`` (NIRSpec) or a ``field`` (NIRCam, whose rows carry
    ``observation IS NULL``) — pass exactly one. Enumerates that scope's *active
    registry rows* (objects known to be in the cloud), maps each to its local path
    via the layout contract, and clears only those that pass the interlock. Files
    with no registry row are never touched — they are not in the candidate set.
    """
    from campfire.config import products_relpath

    q = (
        client.table('storage_objects')
        .select('storage_key, content_hash, size_bytes')
        .eq('bucket', bucket)
        .eq('status', 'active')
    )
    q = q.eq('field', field) if field is not None else q.eq('observation', observation)
    resp = q.execute()
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


def _data_client_and_bucket(config: dict, *, max_pool_connections: Optional[int] = None):
    """Resolve a boto3 client + bucket name for the data backend (LIST/HEAD).

    Backfill/reconcile need to read object metadata, which the presigned-URL
    path can't do — so they require data-backend credentials.
    """
    from campfire.deploy.backend import make_s3_client, resolve_backend
    bcfg = resolve_backend(config, 'data')
    return (make_s3_client(bcfg, max_pool_connections=max_pool_connections),
            bcfg.bucket, bcfg.backend)


def _osn_client_and_bucket(config: dict, *, max_pool_connections: Optional[int] = None):
    """Resolve a boto3 client + bucket + label for the OSN data destination.

    The R2->OSN copy (#215) holds this dest client alongside the R2 'data' source
    (``_data_client_and_bucket``). Requires the ``[r2_osn]`` config section
    (CAMPFIRE_S3_OSN_* env). The returned label is normally ``'osn'`` and is what
    the relocated rows record in ``storage_objects.backend``.
    """
    from campfire.deploy.backend import make_s3_client, resolve_backend
    bcfg = resolve_backend(config, 'osn')
    return (make_s3_client(bcfg, max_pool_connections=max_pool_connections),
            bcfg.bucket, bcfg.backend)


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

def active_osn_canonical_keys(client, bucket: str = 'data') -> set[str]:
    """All active OSN storage keys (canonical) — the set of already-migrated objects.

    Backfill consults this to stay **migration-aware**: after the R2->OSN re-key the
    registry tracks an object by its canonical key while R2 *retains* the legacy
    bytes, so a naive backfill (reading spectra.fits_path, or listing R2) re-discovers
    the legacy key as "missing" and resurrects a duplicate r2-legacy row. Skipping any
    key whose canonical form is already here prevents that.
    """
    keys: set[str] = set()
    start = 0
    page = 1000
    while True:
        rows = (
            client.table('storage_objects').select('storage_key')
            .eq('backend', 'osn').eq('bucket', bucket).eq('status', 'active')
            .order('id').range(start, start + page - 1).execute().data or []
        )
        for r in rows:
            if r.get('storage_key'):
                keys.add(r['storage_key'])
        if len(rows) < page:
            break
        start += page
    return keys


def backfill_spectra(
    client, *, backend: str, dry_run: bool = False,
    migrated_keys: Optional[set] = None,
) -> tuple[int, int, int]:
    """Backfill registry rows from ``spectra`` (authoritative sha256 + size).

    Reuses the stored ``file_hash`` (sha256) and ``file_size``; no bucket access
    needed. Migration-aware: an object whose canonical key is already an active OSN
    row (``migrated_keys``) is skipped rather than re-registered as an r2 duplicate.
    Returns ``(n_rows, n_no_hash, n_already_migrated)``.
    """
    migrated = migrated_keys if migrated_keys is not None else set()
    rows: list[dict] = []
    skipped = 0
    already = 0
    for r in _iter_rows(client, 'spectra', 'fits_path, file_hash, file_size'):
        key = r.get('fits_path')
        if not key:
            continue
        try:
            if canonical_key_for(key) in migrated:
                already += 1
                continue
        except LayoutError:
            pass  # unmappable key — row_for_key will reject it below
        content_hash = normalize_sha256(r.get('file_hash'))
        if not content_hash:
            skipped += 1
            continue
        size = r.get('file_size')
        if size is None:
            skipped += 1
            continue
        row = row_for_key(
            key,
            backend=backend,
            content_hash=content_hash,
            size_bytes=int(size),
            content_type='application/fits',
        )
        if row is not None:
            rows.append(row)
    if not dry_run:
        upsert_storage_objects(client, rows)
    return len(rows), skipped, already


def backfill_via_head(
    client,
    config: dict,
    pointers: Iterable[str],
    *,
    backend: str,
    content_type: str = 'application/octet-stream',
    dry_run: bool = False,
    max_workers: int = 16,
) -> tuple[int, int]:
    """Backfill rows for keys with no stored hash (nircam, orphans) via S3 HEAD.

    HEAD yields size + ETag (no GET), recorded as a provisional ``etag:`` hash.
    Returns ``(n_rows, n_failed)`` where failed = HEAD error or unparseable key.

    The HEADs run in a thread pool against a **single reused** S3 client. The old
    serial path rebuilt a boto3 client per key and blocked on each round-trip — for
    a few thousand nircam pointers that was ~15 min, and the orphan pass (tens of
    thousands of objects) was effectively unusable. Pooling cuts it to minutes.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from campfire.deploy.backend import make_s3_client, resolve_backend

    keys = [k for k in pointers if k]
    if not keys:
        return 0, 0

    bcfg = resolve_backend(config, 'data')
    s3 = make_s3_client(bcfg, max_pool_connections=max_workers)
    bucket = bcfg.bucket

    def _head_row(key: str) -> dict | None:
        """HEAD one key and build its row, or raise/return None on failure."""
        resp = s3.head_object(Bucket=bucket, Key=key)
        content_hash = normalize_etag(resp.get('ETag'))
        size = resp.get('ContentLength')
        if not content_hash or size is None:
            return None
        return row_for_key(
            key, backend=backend, content_hash=content_hash,
            size_bytes=int(size), content_type=content_type,
        )

    rows: list[dict] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_head_row, key): key for key in keys}
        for fut in as_completed(futures):
            try:
                row = fut.result()
            except Exception:
                failed += 1          # HEAD error (missing object, etc.)
                continue
            if row is None:
                failed += 1          # no ETag/size, or unparseable/unknown key
                continue
            rows.append(row)
    if not dry_run:
        upsert_storage_objects(client, rows)
    return len(rows), failed


# ---------------------------------------------------------------------------
# R2 -> OSN copy + re-key + verify (epic #210, Track A / #215)
# ---------------------------------------------------------------------------

# Live products migrated by default. RGB/SED are dead (no web call sites, superseded
# by the on-the-fly cutout API) and deliberately excluded; dual-read serves any
# non-migrated object from R2 by fallback, so omission is self-correcting.
DEFAULT_COPY_PRODUCT_TYPES = (
    'nirspec_spec',
    'spectrum_json',
    'zfit',
    'photometry_pz',
    'nirspec_spectrum_exposure',
    'nirspec_rate',
)


class CopyError(Exception):
    """A single object failed to copy/verify (skip it; never abort the run)."""


@dataclass
class CopyReport:
    """Outcome of an R2->OSN copy pass."""
    planned: list[tuple] = field(default_factory=list)    # (legacy, canonical, size) — dry-run
    copied: list[tuple] = field(default_factory=list)     # (legacy, canonical, size)
    skipped: list[tuple] = field(default_factory=list)    # (legacy, reason) — unparseable/underivable
    failed: list[tuple] = field(default_factory=list)     # (legacy, reason) — transfer/verify error

    @property
    def bytes_copied(self) -> int:
        return sum(sz for _, _, sz in self.copied)

    @property
    def bytes_planned(self) -> int:
        return sum(sz for _, _, sz in self.planned)

    @property
    def ok(self) -> bool:
        """True when nothing failed (skips are reported but not fatal on their own)."""
        return not self.failed

    def summary(self) -> str:
        if self.planned:
            return (f"plan: {len(self.planned)} object(s), {self.bytes_planned} bytes; "
                    f"skipped={len(self.skipped)}")
        return (f"copied={len(self.copied)} ({self.bytes_copied} bytes), "
                f"skipped={len(self.skipped)}, failed={len(self.failed)}")


def canonical_key_for(legacy_key: str) -> str:
    """Derive the CANONICAL storage key for an existing (legacy) key.

    ``parse_key`` resolves product_type/scope/filename for either scheme, then
    ``storage_key(..., scheme=CANONICAL)`` rebuilds the canonical (``data/`` +
    relpath) form. Raises ``LayoutError`` for unknown/unsafe keys.
    """
    pk = parse_key(legacy_key)
    return storage_key(pk.product_type, pk.scope, pk.filename, scheme=KeyScheme.CANONICAL)


def copy_candidates(
    client,
    *,
    observations: Optional[Iterable[str]] = None,
    fields: Optional[Iterable[str]] = None,
    product_types: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    bucket: str = 'data',
) -> list[dict]:
    """Active ``backend='r2'`` registry rows eligible for migration to OSN.

    Filtering ``backend='r2'`` means already-migrated (``osn``) rows are invisible,
    so re-running the copy is idempotent/resumable for free. Optional scope filters
    narrow to specific observations/fields/product types. ``product_types`` defaults
    to :data:`DEFAULT_COPY_PRODUCT_TYPES` (excludes dead rgb/sed).

    Paginates with a stable id order: a single ``.execute()`` is silently capped at
    PostgREST's max-rows (so a >cap candidate set would be truncated and the copy
    would migrate only a prefix). ``limit``, when given, caps the total returned
    (for piloting); otherwise every matching row is returned.
    """
    types = list(product_types) if product_types is not None else list(DEFAULT_COPY_PRODUCT_TYPES)

    def _page(start: int, end: int):
        q = (
            client.table('storage_objects')
            # Full column set: the parallel copy upserts these rows back in place
            # (on_conflict='id'), so every NOT NULL column must be present.
            .select('id, backend, bucket, storage_key, content_hash, size_bytes, '
                    'content_type, product_type, status, observation, field, '
                    'exposure_ref, spectrum_id')
            .eq('backend', 'r2')
            .eq('bucket', bucket)
            .eq('status', 'active')
        )
        if types:
            q = q.in_('product_type', types)
        if observations:
            q = q.in_('observation', list(observations))
        if fields:
            q = q.in_('field', list(fields))
        return q.order('id').range(start, end).execute().data or []

    out: list[dict] = []
    start = 0
    page = 1000
    while True:
        rows = _page(start, start + page - 1)
        out.extend(rows)
        if limit and len(out) >= limit:
            return out[:limit]
        if len(rows) < page:
            break
        start += page
    return out


def _migrate_one(
    row: dict,
    *,
    src_client, src_bucket: str,
    dst_client, dst_bucket: str,
    verify_readback: bool,
    tmp_dir: Optional[str] = None,
) -> tuple[str, str, int]:
    """Copy one object R2->OSN and verify. Returns ``(canonical_key, sha256, size)``.

    GET (R2) -> temp -> hash -> [drift guard / etag->sha256 upgrade] -> PUT (OSN
    canonical) -> verify. Raises :class:`CopyError` on any integrity failure; the
    caller records it and moves on WITHOUT relocating the row (so the object stays
    ``r2``-legacy and dual-read keeps serving R2 — safe).
    """
    legacy = row['storage_key']
    canonical = canonical_key_for(legacy)
    content_type = row.get('content_type') or 'application/octet-stream'

    # mkstemp opens an fd we don't use (download_file opens its own handle); close
    # it immediately or a bulk run leaks two fds per object and hits RLIMIT_NOFILE.
    _fd, _name = tempfile.mkstemp(dir=tmp_dir, prefix='cfcopy_')
    os.close(_fd)
    tmp = Path(_name)
    rb_tmp: Optional[Path] = None
    try:
        download_to_path(src_client, src_bucket, legacy, tmp)
        sha, size = hash_file(tmp)

        existing = row.get('content_hash') or ''
        if existing.startswith('sha256:') and existing != sha:
            raise CopyError(
                f"sha256 drift: registry has {existing} but downloaded bytes hash to {sha}"
            )

        upload_to_r2(dst_client, dst_bucket, tmp, canonical, content_type)

        if verify_readback:
            _rb_fd, _rb_name = tempfile.mkstemp(dir=tmp_dir, prefix='cfcopyrb_')
            os.close(_rb_fd)
            rb_tmp = Path(_rb_name)
            download_to_path(dst_client, dst_bucket, canonical, rb_tmp)
            rb_sha, rb_size = hash_file(rb_tmp)
            if rb_sha != sha:
                raise CopyError(
                    f"readback hash mismatch on OSN: expected {sha}, got {rb_sha}"
                )
        else:
            head = dst_client.head_object(Bucket=dst_bucket, Key=canonical)
            if head.get('ContentLength') != size:
                raise CopyError(
                    f"readback size mismatch on OSN: expected {size}, "
                    f"got {head.get('ContentLength')}"
                )
        return canonical, sha, size
    finally:
        tmp.unlink(missing_ok=True)
        if rb_tmp is not None:
            rb_tmp.unlink(missing_ok=True)


def copy_objects(
    client,
    *,
    src_client, src_bucket: str,
    dst_client, dst_bucket: str,
    dst_backend: str = 'osn',
    observations: Optional[Iterable[str]] = None,
    fields: Optional[Iterable[str]] = None,
    product_types: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    dry_run: bool = True,
    verify_readback: bool = True,
    tmp_dir: Optional[str] = None,
    progress: bool = False,
    max_workers: int = 8,
) -> CopyReport:
    """Migrate active data objects R2->OSN, re-key to canonical, verify, relocate.

    Per object: derive the canonical key (skip + report ``LayoutError``); when not
    ``dry_run``, GET from ``src``, hash, PUT to ``dst`` (canonical), verify
    (readback hash by default; size-match if ``verify_readback`` is False), then
    relocate the registry row **in place by id** to ``osn``+canonical+``sha256:``.
    The DB flip happens strictly AFTER verify, so dual-read never points at an
    unverified/missing OSN object; until the flip, dual-read serves the retained R2
    bytes. Per-object failures are recorded and skipped — the run continues.

    Concurrency split (load-bearing): the **S3** work (GET/hash/PUT/readback) runs
    across a thread pool of ``max_workers`` because boto3 clients are thread-safe;
    the boto3 clients must be built with a connection pool >= ``max_workers`` (the
    CLI sizes them). The **DB** writes do NOT run in the workers — the supabase
    client's HTTP/2 connection is not thread-safe and segfaults under concurrent use
    — so every worker hands its verified, complete row back to this single calling
    thread, which flips them in batches via :func:`relocate_objects_batch`.
    Resume-safe: a crash flips at most the last unflushed batch late, and the
    candidate query only re-selects ``backend='r2'`` rows, so a re-run re-copies just
    the remainder idempotently.

    ``dry_run`` (the default) only derives + plans (no transfer, no DB write).
    """
    candidates = copy_candidates(
        client, observations=observations, fields=fields,
        product_types=product_types, limit=limit,
    )
    report = CopyReport()

    if dry_run:
        for row in candidates:
            legacy = row.get('storage_key')
            if not legacy:
                continue
            try:
                canonical = canonical_key_for(legacy)
            except LayoutError as e:
                report.skipped.append((legacy, f'unknown/unsafe key: {e}'))
                continue
            report.planned.append((legacy, canonical, int(row.get('size_bytes') or 0)))
        return report

    # Pre-filter unmappable keys (cheap, no I/O) so the pool only does real work.
    work: list[dict] = []
    for row in candidates:
        legacy = row.get('storage_key')
        if not legacy:
            continue
        try:
            canonical_key_for(legacy)
        except LayoutError as e:
            report.skipped.append((legacy, f'unknown/unsafe key: {e}'))
            continue
        work.append(row)

    def _copy_one(row: dict) -> tuple:
        """Worker (S3 ONLY — never touches Supabase): GET+hash+PUT+verify one object.

        The Supabase client is NOT thread-safe (its HTTP/2 connection segfaults under
        concurrent multi-thread use), so workers do only boto3 work (which IS thread-
        safe) and hand the verified, fully-formed registry row back to the single
        calling thread for the DB write. Returns ('copied', legacy, canonical, size,
        upsert_row) or ('failed', legacy, error).
        """
        legacy = row['storage_key']
        try:
            canonical, sha, size = _migrate_one(
                row, src_client=src_client, src_bucket=src_bucket,
                dst_client=dst_client, dst_bucket=dst_bucket,
                verify_readback=verify_readback, tmp_dir=tmp_dir,
            )
        except Exception as e:  # CopyError or any boto3/IO error — skip this object
            return ('failed', legacy, str(e))
        # A COMPLETE row for the in-place upsert(on_conflict='id'): the candidate row
        # carries every NOT NULL column (so the candidate INSERT tuple is valid before
        # the ON CONFLICT arbiter), and we override only the migrated fields. The bytes
        # are verified on OSN at this point; the row is not flipped until the main
        # thread flushes the batch.
        upsert_row = {
            **row,
            'backend': dst_backend,
            'storage_key': canonical,
            'content_hash': sha,
            'size_bytes': int(size),
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        return ('copied', legacy, canonical, int(size), upsert_row)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    bar = None
    if progress:
        from tqdm import tqdm
        bar = tqdm(total=len(work), desc='Copying R2->OSN', unit='obj')

    # All DB writes happen HERE, on the single calling thread, batched. Workers only
    # do (thread-safe) S3; a crash flips at most the last unflushed batch's worth of
    # rows late, which a resume re-copies idempotently.
    relocate_batch: list[dict] = []

    def _flush():
        if relocate_batch:
            relocate_objects_batch(client, relocate_batch)
            relocate_batch.clear()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(_copy_one, row) for row in work]
        for fut in as_completed(futures):
            result = fut.result()
            if result[0] == 'failed':
                report.failed.append((result[1], result[2]))
            else:
                _, legacy, canonical, size, upsert_row = result
                report.copied.append((legacy, canonical, size))
                relocate_batch.append(upsert_row)
                if len(relocate_batch) >= UPSERT_BATCH:
                    _flush()
            if bar is not None:
                bar.update(1)
        _flush()
    if bar is not None:
        bar.close()

    return report


def find_migration_conflicts(
    client,
    *,
    observations: Optional[Iterable[str]] = None,
    bucket: str = 'data',
) -> list[str]:
    """Detect freeze-violation duplicates (epic #210 / #215 gap G1).

    During the shadow window a re-deploy of an already-migrated observation writes a
    fresh ``r2``-legacy row whose canonical form collides with an existing
    ``osn``-canonical row → two active rows for one logical object (dual-read would
    serve stale OSN bytes). Returns the legacy keys whose canonical form already has
    an active ``osn`` row, so the copy command can warn before running.
    """
    candidates = copy_candidates(client, observations=observations, bucket=bucket)
    if not candidates:
        return []
    # Map each candidate (r2-legacy) to its canonical form.
    canon_by_legacy: dict[str, str] = {}
    for row in candidates:
        legacy = row.get('storage_key')
        if not legacy:
            continue
        try:
            canon_by_legacy[legacy] = canonical_key_for(legacy)
        except LayoutError:
            continue
    if not canon_by_legacy:
        return []
    wanted = set(canon_by_legacy.values())

    # Collect the active osn storage_keys and intersect LOCALLY. We deliberately do
    # NOT filter by `storage_key IN (...)`: that list is hundreds of ~70-char keys
    # and overflows the PostgREST request URI (Kong returns a bare 400 'Bad Request').
    # Scope by observation when given (a short list) so the scan stays small; an osn
    # row's observation is preserved by relocate, so a collision shares the obs.
    present: set[str] = set()
    start = 0
    page = 1000
    while True:
        q = (
            client.table('storage_objects')
            .select('storage_key')
            .eq('backend', 'osn')
            .eq('bucket', bucket)
            .eq('status', 'active')
            .order('id')
            .range(start, start + page - 1)
        )
        if observations:
            q = q.in_('observation', list(observations))
        rows = q.execute().data or []
        for r in rows:
            k = r.get('storage_key')
            if k and k in wanted:
                present.add(k)
        if len(rows) < page:
            break
        start += page
    return [legacy for legacy, canon in canon_by_legacy.items() if canon in present]


# ---------------------------------------------------------------------------
# Registry cleanup (`campfire deploy registry prune`)
# ---------------------------------------------------------------------------

def find_r2_duplicate_ids(client, bucket: str = 'data') -> list[int]:
    """IDs of active ``r2`` rows whose canonical key is already an active ``osn`` row.

    These are duplicates left by a migration-unaware backfill (it resurrected the
    retained R2 legacy key for an object already on OSN). The OSN row is
    authoritative; these r2 rows are stale and safe to delete.
    """
    osn = active_osn_canonical_keys(client, bucket)
    if not osn:
        return []
    dup_ids: list[int] = []
    start = 0
    page = 1000
    while True:
        rows = (
            client.table('storage_objects').select('id, storage_key')
            .eq('backend', 'r2').eq('bucket', bucket).eq('status', 'active')
            .order('id').range(start, start + page - 1).execute().data or []
        )
        for r in rows:
            k = r.get('storage_key')
            if not k:
                continue
            try:
                if canonical_key_for(k) in osn:
                    dup_ids.append(r['id'])
            except LayoutError:
                continue
        if len(rows) < page:
            break
        start += page
    return dup_ids


def find_dead_product_ids(client, bucket: str = 'data') -> list[int]:
    """IDs of rows for dead product types (rgb/sed) that should never be registered."""
    ids: list[int] = []
    for pt in sorted(UNREGISTERED_PRODUCT_TYPES):
        start = 0
        page = 1000
        while True:
            rows = (
                client.table('storage_objects').select('id')
                .eq('product_type', pt).eq('bucket', bucket)
                .order('id').range(start, start + page - 1).execute().data or []
            )
            ids.extend(r['id'] for r in rows if r.get('id') is not None)
            if len(rows) < page:
                break
            start += page
    return ids


def delete_objects(client, ids: list[int], batch_size: int = UPSERT_BATCH) -> int:
    """Hard-delete registry rows by primary key, batched. Returns count deleted."""
    if not ids:
        return 0
    for i in range(0, len(ids), batch_size):
        client.table('storage_objects').delete().in_('id', ids[i:i + batch_size]).execute()
    return len(ids)
