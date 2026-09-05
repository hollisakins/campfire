"""
storage_objects registry — the shadow index of cloud storage (epic #210, F1).

Every object CAMPFIRE deploys to the data bucket gets one row here, keyed by its
canonical storage key (from the ``campfire_layout`` contract). The registry is
written by deploy (``build_registry_rows`` + ``upsert_storage_objects``, called
from the deploy hooks after a successful upload) and verified against the live
DB pointers and bucket contents by ``campfire verify --cloud``
(``cloud_reconcile_report``). The one-time A1/A2 migration machinery (backfill /
R2→OSN copy / prune, formerly the ``campfire deploy registry`` CLI subgroup)
was removed after the migrations completed (issue #371).

content_hash is scheme-prefixed: ``sha256:<hex>`` is authoritative (we hash the
local file we just uploaded); ``etag:<hex>`` is provisional, taken from an S3
``LIST``/``HEAD`` (no GET) by the retired migration-era backfill — the A1
copy+verify pass (#215) upgraded ``etag:`` → ``sha256:`` as it read the bytes.

Tiles are intentionally **not** registered per-object (decision F1-B): they stay
on R2, number in the tens of thousands per field/filter, and are already
byte-aggregated in ``map_layers.total_size_bytes`` (the budget RPC unions both).
"""

from __future__ import annotations

import os
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

from campfire.deploy.r2 import UploadTask

# Supabase upsert batch size — matches batch_upsert_spectra.
UPSERT_BATCH = 500

# Dead products that must NOT be registered: rgb/sed static cutouts are superseded
# by the on-the-fly /api/v1/cutout API and aren't served anywhere. row_for_key skips
# them, so neither deploy nor backfill ever indexes them.
UNREGISTERED_PRODUCT_TYPES = frozenset({'rgb', 'sed'})


# ---------------------------------------------------------------------------
# Hashing — single implementation lives in campfire.storage.hashing; these
# names are re-exported here for the deploy-side callers (and tests) that
# import them from the registry module.
# ---------------------------------------------------------------------------

from campfire.storage.hashing import (  # noqa: E402  (re-export)
    default_hash_workers,
    hash_file,
    hash_files_parallel,
)


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
    sed, photometry, nircam previews). The 1-D sidecar (``_spec_1d.json``,
    #508) belongs to its spectrum like the full JSON does.
    """
    for suffix in ('_spec.fits', '_spec_1d.json', '_spec.json'):
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
    wcs_hash: Optional[str] = None,
    stored_size_bytes: Optional[int] = None,
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
        # Astrometric half of the same change-detection identity: a re-aligned
        # exposure has identical SCI/DQ pixels and a different WCS, so without
        # this the re-alignment never reaches the cloud. NULL for everything
        # that compares on the whole-file hash.
        'wcs_hash': wcs_hash,
        'size_bytes': int(size_bytes),
        # Bytes as stored in the bucket for transport-compressed products
        # (gzipped mosaic FITS); NULL = stored verbatim (== size_bytes, which
        # stays the LOGICAL uncompressed size the sync engine's dedup and stat
        # fast-path compare against local plain files).
        'stored_size_bytes': stored_size_bytes,
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
    wcs_hashes: Optional[dict[str, str]] = None,
    max_workers: Optional[int] = None,
    precomputed: Optional[dict[str, tuple[str, int]]] = None,
    stored_sizes: Optional[dict[str, int]] = None,
) -> list[dict]:
    """Build ``storage_objects`` rows for the objects an upload actually landed.

    For each :class:`UploadTask` whose ``r2_key`` is in ``succeeded_keys`` (or
    all, if ``succeeded_keys`` is None), hashes the local file and maps the key
    to a row via :func:`row_for_key`. Tiles and non-cloud products are skipped.

    The whole-file hashing runs across a thread pool (``max_workers``, default
    :func:`default_hash_workers`) — for a field's worth of exposures this
    registration read is I/O-bound and was a serial bottleneck after upload.

    ``sci_dq_hashes`` optionally supplies a precomputed ``sha256:<hex>`` science-only
    digest per storage key (NIRCam exposures, epic #261) — the ``content_hash`` is
    still the whole-file sha256, but ``sci_dq_hash`` carries the science-only digest
    that change-detection compares against. ``wcs_hashes`` supplies the astrometric
    digest that goes with it; the pair is the exposure identity, and an exposure
    registered without the WCS half would dedup as unchanged after a re-alignment.

    ``precomputed`` optionally supplies ``{r2_key: ('sha256:<hex>', size)}``
    whole-file digests already computed upstream (the push planner hashes
    changed files to decide the upload) — those files are not re-read here.
    """
    sci_dq_hashes = sci_dq_hashes or {}
    wcs_hashes = wcs_hashes or {}
    precomputed = precomputed or {}
    stored_sizes = stored_sizes or {}
    selected: list[tuple[UploadTask, Path]] = []
    for task in tasks:
        if succeeded_keys is not None and task.r2_key not in succeeded_keys:
            continue
        local = Path(task.local_path)
        if not local.exists():
            continue
        selected.append((task, local))

    hashed = hash_files_parallel(
        (local for task, local in selected if task.r2_key not in precomputed),
        max_workers=max_workers,
    )

    rows: list[dict] = []
    for task, local in selected:
        content_hash, size_bytes = precomputed.get(task.r2_key) or hashed[local]
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
            wcs_hash=wcs_hashes.get(task.r2_key),
            stored_size_bytes=stored_sizes.get(task.r2_key),
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


def backfill_wcs_hashes(client, pairs: list[tuple[str, str]]) -> int:
    """Record ``wcs_hash`` on legacy rows the push planner proved unchanged.

    ``pairs`` is ``PushPlan.backfill``: keys whose cloud object is byte-identical
    to the local file (whole-file sha256 == the row's ``content_hash``), so the
    local astrometric digest describes the cloud copy too. Writing it costs no
    transfer and completes the row's exposure identity, so the *next* push
    compares on both components — and a subsequent re-alignment is finally seen.

    Guarded with ``is_('wcs_hash', 'null')`` so this can only ever fill a hole:
    a row another reducer has since re-uploaded (with its own, possibly
    different, WCS) is left alone rather than being stamped with ours. Returns
    the number of rows written.
    """
    if not pairs:
        return 0
    # Group by digest so identical values go out as one filtered UPDATE rather
    # than one request per exposure (a field's exposures rarely share a WCS, but
    # the grouping costs nothing and bounds the request count when they do).
    by_hash: dict[str, list[str]] = {}
    for key, wcs in pairs:
        if wcs:
            by_hash.setdefault(wcs, []).append(key)
    written = 0
    for wcs, keys in by_hash.items():
        for i in range(0, len(keys), 200):
            chunk = keys[i:i + 200]
            resp = (client.table('storage_objects')
                    .update({'wcs_hash': wcs})
                    .in_('storage_key', chunk)
                    .eq('status', 'active')
                    .is_('wcs_hash', 'null')
                    .execute())
            written += len(resp.data or [])
    return written


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


def canonical_key_for(legacy_key: str) -> str:
    """Derive the CANONICAL storage key for an existing (legacy) key.

    ``parse_key`` resolves product_type/scope/filename for either scheme, then
    ``storage_key(..., scheme=CANONICAL)`` rebuilds the canonical (``data/`` +
    relpath) form. Raises ``LayoutError`` for unknown/unsafe keys.
    """
    pk = parse_key(legacy_key)
    return storage_key(pk.product_type, pk.scope, pk.filename, scheme=KeyScheme.CANONICAL)


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
               page: int = 1000, filters: Optional[dict] = None) -> Iterator[dict]:
    """Keyset-page through a Supabase table, yielding row dicts.

    Range/offset pagination WITHOUT an ``ORDER BY`` is non-deterministic in
    Postgres: across pages rows silently repeat and others are skipped, so a full
    scan under-covers a large table (and the gap varies run-to-run). That under-
    coverage is why a backfill over ~50k spectra registered only a partial, shifting
    subset. Ordering by a unique key (the PK ``id`` on every table this is used on:
    spectra / nircam_images / nircam_exposures / storage_objects) makes the scan
    complete and repeatable.

    Since perf T2-F (#511) the walk is a *keyset* on ``order_by`` — each page
    asks for ``order_by > <last value>`` — instead of ``.range()`` offsets:
    a deep OFFSET page over the 665k-row ``storage_objects`` table cost ~2.6 s
    (the server re-reads and discards every earlier row) against ~8 ms for a
    PK seek, so a full walk went O(N²/page). ``order_by`` must be a unique
    column; ``filters`` are ``{column: value}`` equality predicates pushed into
    the query so the server, not the client, does the narrowing. The cursor
    column is always fetched (it is added to ``columns`` if absent) and left
    on the yielded rows.
    """
    cols = [c.strip() for c in columns.split(',') if c.strip()]
    if order_by not in cols:
        cols.append(order_by)
    select = ', '.join(cols)
    last = None
    while True:
        q = client.table(table).select(select)
        for col, val in (filters or {}).items():
            q = q.eq(col, val)
        if last is not None:
            q = q.gt(order_by, last)
        resp = q.order(order_by).limit(page).execute()
        data = resp.data or []
        for row in data:
            yield row
        if len(data) < page:
            break
        last = data[-1][order_by]


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

    The ``bucket`` / ``backend`` predicates are pushed into the query (T2-F,
    #511) — previously every row of the registry was fetched and filtered in
    Python — and the walk is a keyset on ``id``.
    """
    filters: dict = {'bucket': bucket}
    if backend is not None:
        filters['backend'] = backend
    return [
        r['storage_key']
        for r in _iter_rows(client, 'storage_objects', 'storage_key', filters=filters)
        if r.get('storage_key')
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
    """Resolve a boto3 client + bucket + label for the OSN data backend.

    Used by ``list_bucket_keys(purpose='osn')`` (the ``campfire verify --cloud``
    LIST). Requires the ``[r2_osn]`` config section (CAMPFIRE_S3_OSN_* env).
    The returned label is normally ``'osn'``.
    """
    from campfire.deploy.backend import make_s3_client, resolve_backend
    bcfg = resolve_backend(config, 'osn')
    return (make_s3_client(bcfg, max_pool_connections=max_pool_connections),
            bcfg.bucket, bcfg.backend)


def list_bucket_keys(config: dict, prefix: str = '', *, purpose: str = 'data') -> Iterator[str]:
    """Yield every object key under ``prefix`` in a data bucket (paginated).

    ``purpose`` selects the backend block: ``'data'`` (the legacy R2 data
    bucket) or ``'osn'`` (the OSN home of all current data products).
    """
    if purpose == 'osn':
        client, bucket, _ = _osn_client_and_bucket(config)
    else:
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


def cloud_reconcile_report(config: dict, client, *, include_buckets: bool = True) -> dict:
    """Multi-backend registry↔bucket↔DB verification (``campfire verify --cloud``).

    Three vocabularies are compared: the live denormalized DB pointers, the
    registry rows, and — per storage backend — the actual bucket contents.
    **OSN is the primary data bucket** (all current data products live there;
    map tiles are the sole R2 exception), so the OSN LIST is the one that
    verifies current products; the legacy R2 data-bucket LIST covers pre-#216
    remnants until A2 retires them. Each backend's ``dangling`` is computed
    only over registry rows homed on that backend.

    Returns a JSON-able dict; ``ok`` is False when live pointers lack registry
    rows (``missing``) or registry rows lack bucket objects (``dangling``) —
    the drift conditions a scheduled CI run should alarm on. A bucket whose
    credentials are unavailable is reported under ``errors`` and does NOT fail
    the report (coverage is still checked).
    """
    pointers = live_pointers(client)
    live = pointers['spectra'] + pointers['nircam_images'] + pointers['nircam_exposures']
    reg_keys = registry_keys(client)

    base = compute_reconcile(live, reg_keys, None)
    report: dict = {
        'live_pointers': len({k for k in live if k}),
        'registry_rows': len(set(reg_keys)),
        'missing': sorted(base.missing),
        'backends': {},
        'errors': {},
    }

    if include_buckets:
        # (purpose, registry backend label) — OSN first: it is the data home.
        targets = [('osn', 'osn'), ('data', resolve_backend_label(config))]
        for purpose, label in targets:
            try:
                bucket_keys = list(list_bucket_keys(config, purpose=purpose))
            except Exception as e:
                report['errors'][label] = str(e)
                continue
            danglable = registry_keys(client, backend=label)
            rep = compute_reconcile(live, reg_keys, bucket_keys,
                                    danglable_keys=danglable)
            report['backends'][label] = {
                'bucket_objects': len(bucket_keys),
                'registry_rows': len(set(danglable)),
                'dangling': sorted(rep.dangling),
                'orphans': sorted(rep.orphans),
                'adoptable': sorted(rep.adoptable),
            }

    any_dangling = any(b['dangling'] for b in report['backends'].values())
    report['ok'] = not report['missing'] and not any_dangling
    return report


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
