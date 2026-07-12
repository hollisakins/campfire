"""Bytes-only push: make the cloud match local products (no catalog writes).

``campfire push`` is the local→cloud half of the storage plane — the mirror
image of ``campfire pull``. It discovers tree products for a scope, diffs them
against the **server** registry (``campfire.storage.plan``), uploads only
new/changed content, and registers each landed object durably *per batch*
while the transfer runs. It never touches the science catalog (targets/spectra/
exposure upserts), never records a deployment, and never flips visibility —
that is ``campfire deploy``'s job, which now rides this same machinery and
dedup-skips everything a prior push already landed.

The intended slow-link workflow (CANDIDE → OSN): run ``campfire push`` for the
heavy bytes (resumable at file granularity — per-batch registration means an
interruption costs at most one batch), then ``campfire deploy`` afterwards,
which completes in minutes because every unchanged byte dedups.

Two things to know about push semantics:

* **Registry rows land with ``deployment_id=None``** (there is no deployment
  yet). Deployment-gated objects therefore stay admin-only until a real deploy
  attaches them (``set_active_deployment`` re-points unchanged objects at the
  new deployment). Spectrum-family objects follow their spectrum's
  ``deploy_status`` as always.
* **Pushing to the key of an already-published product replaces the served
  bytes immediately** (cloud-as-source-of-truth; same behavior as a re-deploy,
  just decoupled from the catalog update).

The local mirror ($CAMPFIRE_ROOT/meta/campfire.db — the same store ``campfire
pull``/``status`` use) caches the server rows and records the pushed_* stat
fast-path, so an unchanged re-push never re-reads file contents. The mirror is
a cache, not an authority: planning always re-fetches the candidate keys from
the server registry, so a stale mirror can never mis-skip.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from campfire_layout import KeyScheme, Scope, storage_key

from campfire.config import ensure_data_dir, meta_dir, products_dir
from campfire.storage.hashing import sci_dq_hashes_parallel
from campfire.storage.plan import PushPlan, identity_kind_for_key, plan_push
from campfire.storage.transfer import BatchFlusher
from campfire.deploy.r2 import UploadTask, upload_files_parallel

# Registry columns fetched for push planning — the mirror-row shape (everything
# the local storage_objects cache carries), so fetched rows double as a cache
# refresh for the keys being pushed.
_PLAN_COLUMNS = (
    'id, backend, bucket, storage_key, content_hash, sci_dq_hash, size_bytes, '
    'content_type, product_type, instrument, status, observation, field, '
    'filter, spectrum_id, exposure_ref, deployment_id, cfpipe_version, '
    'created_at, updated_at'
)

# Registration batch: server registry rows + local mirror bookkeeping are
# flushed every N landed files, so an interrupted push loses at most one batch
# of bookkeeping (the bytes themselves are already in the store; the next run
# re-plans those keys as changed and re-lands them idempotently).
REGISTRATION_BATCH = 200


def default_upload_workers() -> int:
    """Parallel upload streams for data pushes.

    Overridable via ``CAMPFIRE_DEPLOY_UPLOAD_WORKERS``. Defaults to 16: data
    products are many ~20-40 MB files whose upload is network-bound over a
    high-latency link, so extra concurrent streams help fill the uplink.
    Clamped to a sane range.
    """
    raw = os.environ.get('CAMPFIRE_DEPLOY_UPLOAD_WORKERS')
    if raw:
        try:
            return max(1, min(64, int(raw)))
        except ValueError:
            pass
    return 16


def open_reducer_store():
    """Open the local storage mirror for push bookkeeping, or None.

    Best-effort by design: the mirror only supplies the stat fast-path and the
    write-through cache — planning is correct without it (every candidate with
    a comparable server identity is hashed instead of stat-skipped). A schema
    mismatch (older client db) degrades gracefully rather than blocking a
    deploy.
    """
    try:
        from campfire.db.store import LocalStore
        ensure_data_dir()
        return LocalStore(meta_dir() / 'campfire.db')
    except Exception as e:
        print(f"  Note: local storage mirror unavailable ({e}); "
              f"pushing without the stat fast-path.")
        return None


def fetch_registry_rows(client, keys: list[str], *, backend: str) -> dict[str, dict]:
    """Fetch server registry rows for candidate keys → ``{storage_key: row}``.

    Always a live, key-scoped read (chunked ``IN`` filters) — never the full
    mirror — so push planning cannot act on stale state regardless of when
    ``campfire sync`` last ran. ``backend`` scopes to the destination store
    (a legacy R2 twin row must not satisfy an OSN push).
    """
    out: dict[str, dict] = {}
    CHUNK = 200
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i:i + CHUNK]
        resp = (
            client.table('storage_objects')
            .select(_PLAN_COLUMNS)
            .eq('backend', backend)
            .in_('storage_key', chunk)
            .execute()
        )
        for r in (resp.data or []):
            out[r['storage_key']] = r
    return out


def plan_remote_push(
    client,
    store,
    tasks: list[UploadTask],
    *,
    backend: str,
    max_workers: Optional[int] = None,
    progress: bool = True,
) -> tuple[PushPlan, dict[str, dict]]:
    """Plan a push: live server-row fetch + local fast-path + classification.

    Returns ``(plan, server_rows)`` — callers that index derived tables (the
    NIRCam mosaic path) need the server rows for the sizes/hashes of
    dedup-skipped objects.
    """
    keys = [t.r2_key for t in tasks]
    server_rows = fetch_registry_rows(client, keys, backend=backend)
    local_rows = store.get_storage_rows_by_keys(keys) if store else {}

    plan = plan_push(tasks, server_rows, local_rows=local_rows,
                     max_workers=max_workers, progress=progress)

    if store:
        # Cache the fresh server rows (preserves local_*/pushed_* on conflict)
        # and record identity-confirmed unchanged files so the next run takes
        # the stat fast path.
        if server_rows:
            store.upsert_storage_objects(list(server_rows.values()))
        for key, identity, mtime, size in plan.confirmed:
            store.mark_object_pushed(key, identity, mtime, size, commit=False)
        store.commit()

    return plan, server_rows


def registration_flusher(
    client,
    store,
    plan: PushPlan,
    *,
    backend: str,
    deployment_id: Optional[int] = None,
    uploaded_by: Optional[str] = None,
    cfpipe_version: Optional[str] = None,
    max_workers: Optional[int] = None,
    batch_size: int = REGISTRATION_BATCH,
    stored_sizes: Optional[dict[str, int]] = None,
) -> BatchFlusher:
    """Build the per-batch registration hook for an upload pass.

    Feed the returned flusher as ``on_success=flusher.add`` to
    ``upload_files_parallel`` and call ``flusher.flush()`` after. Every
    ``batch_size`` landed files it: builds registry rows (reusing the plan's
    hashes — no second read for plan-hashed files), upserts them to the server
    registry, mirrors them locally, and records the pushed_* fast-path state.
    Durable-progress is the point: bytes already in the store stay skippable
    even if the process dies mid-run.
    """
    from campfire.deploy.registry import build_registry_rows, upsert_storage_objects

    prod_dir = products_dir().resolve()

    def _flush(batch: list[UploadTask]) -> None:
        # Science-only digests for exposure-identity products: reuse the plan's
        # value when it hashed the file, compute for plan-unhashed (new) files.
        sci_dq: dict[str, str] = {}
        need = []
        for t in batch:
            if identity_kind_for_key(t.r2_key) != 'sci_dq':
                continue
            h = plan.identities.get(t.r2_key)
            if h:
                sci_dq[t.r2_key] = h
            else:
                need.append(t)
        if need:
            by_path = sci_dq_hashes_parallel(
                [t.local_path for t in need], max_workers=max_workers)
            for t in need:
                h = by_path.get(Path(t.local_path))
                if h:
                    sci_dq[t.r2_key] = h

        rows = build_registry_rows(
            batch, backend=backend, deployment_id=deployment_id,
            uploaded_by=uploaded_by, cfpipe_version=cfpipe_version,
            sci_dq_hashes=sci_dq, precomputed=plan.whole_file,
            max_workers=max_workers, stored_sizes=stored_sizes,
        )
        upsert_storage_objects(client, rows)

        if store:
            store.upsert_storage_objects(rows)
            by_key = {r['storage_key']: r for r in rows}
            for t in batch:
                row = by_key.get(t.r2_key)
                if row is None:
                    continue  # unregistered product type
                try:
                    st = os.stat(t.local_path)
                except OSError:
                    continue
                kind = identity_kind_for_key(t.r2_key)
                identity = row.get('sci_dq_hash') if kind == 'sci_dq' else row.get('content_hash')
                store.mark_object_pushed(
                    t.r2_key, identity, st.st_mtime, st.st_size, commit=False)
                # Pull-side write-through: a pushed tree file IS materialized
                # locally and byte-identical to the cloud copy — record it so
                # `status`/`pull` see it without a verify pass. Generated
                # (temp-dir) products fall outside the products tree and skip.
                try:
                    rel = Path(t.local_path).resolve().relative_to(prod_dir)
                except ValueError:
                    continue
                store.mark_object_synced(
                    storage_key=t.r2_key,
                    local_path=str(rel),
                    local_file_hash=row.get('content_hash'),
                    local_file_size=st.st_size,
                    local_file_mtime=st.st_mtime,
                )
            store.commit()

    return BatchFlusher(_flush, batch_size=batch_size)


# ---------------------------------------------------------------------------
# Scope-level pushes (the `campfire push` entry points)
# ---------------------------------------------------------------------------

def push_observation(
    obs_name: str,
    config: dict,
    *,
    source_ids: Optional[list[int]] = None,
    dry_run: bool = False,
    workers: Optional[int] = None,
    dry_run_note: bool = True,
) -> dict:
    """Push a NIRSpec observation's tree products to OSN (bytes only).

    Covers the pipeline-written products: stage-3 finals, canonical
    spectrum-exposure intermediates, and detector rate files. Deploy-generated
    derivatives (spectrum JSON, zfit JSON, thumbnails) are NOT pushed — they
    are cheap, regenerated at deploy time, and belong to the publication step.
    """
    from campfire.deploy.deploy import resolve_obs_dir, _filter_exposures_by_source_ids
    from campfire.deploy.discover import discover_rate_files, discover_spectrum_exposures
    from campfire.deploy.summary import filter_by_source_ids, get_spec_paths, load_summary
    from campfire.deploy.supabase import get_supabase_client, get_user_id_from_token

    obs_dir = resolve_obs_dir(obs_name)
    scope = Scope(obs=obs_name)
    tasks: list[UploadTask] = []
    cfpipe_version = None

    summary = load_summary(obs_dir, obs_name, required=False)
    if summary is not None and len(summary) > 0:
        cfpipe_version = summary.meta.get('cfpipe_version')
        if source_ids:
            summary = filter_by_source_ids(summary, source_ids)
        for p in get_spec_paths(summary, obs_dir):
            tasks.append(UploadTask(
                p, storage_key('nirspec_spec', scope, p.name,
                               scheme=KeyScheme.CANONICAL),
                'application/fits'))

    exposure_files = discover_spectrum_exposures(obs_dir)
    if source_ids:
        exposure_files = _filter_exposures_by_source_ids(exposure_files, source_ids)
    for p in exposure_files:
        tasks.append(UploadTask(
            p, storage_key('nirspec_spectrum_exposure', scope, p.name,
                           scheme=KeyScheme.CANONICAL),
            'application/fits'))

    # Rate files are source-independent (per-detector) — never filtered.
    for p in discover_rate_files(obs_dir):
        tasks.append(UploadTask(
            p, storage_key('nirspec_rate', scope, p.name,
                           scheme=KeyScheme.CANONICAL),
            'application/fits'))

    print(f"Observation: {obs_name}")
    print(f"  Tree products discovered: {len(tasks)}")
    if not tasks:
        print("  Nothing to push.")
        return {'planned': 0, 'pushed': 0, 'failed': 0, 'skipped': 0}

    client = get_supabase_client(config)
    store = open_reducer_store()
    plan, _server_rows = plan_remote_push(client, store, tasks, backend='osn')
    print(f"  Plan: {plan.summary()}")
    for t in plan.missing:
        print(f"    missing locally: {t.local_path}")

    if dry_run or not plan.to_upload:
        if dry_run and dry_run_note:
            print("Dry run — no changes made.")
        elif not dry_run:
            print("  Cloud already matches local products.")
        return {'planned': len(tasks), 'pushed': 0, 'failed': 0,
                'skipped': len(plan.unchanged)}

    user_id = get_user_id_from_token(config)
    # Transport-compressed uploads (gzipped mosaics) report their stored byte
    # count; the flusher writes it to registry.stored_size_bytes. Wired here as
    # well as in deploy so the slow-link workflow (push heavy bytes first, then
    # deploy dedup-skips them) records it too — deploy never re-uploads.
    stored_sizes: dict[str, int] = {}
    flusher = registration_flusher(
        client, store, plan, backend='osn', deployment_id=None,
        uploaded_by=user_id, cfpipe_version=cfpipe_version,
        stored_sizes=stored_sizes)

    upload_workers = workers or default_upload_workers()
    total_bytes = sum(plan.stats[t.r2_key][1] for t in plan.to_upload
                      if t.r2_key in plan.stats)
    print(f"  Pushing {len(plan.to_upload)} file(s) "
          f"({total_bytes / 1e9:.2f} GB) to OSN...")
    success, failed, failures = upload_files_parallel(
        config, plan.to_upload, desc='Pushing to OSN',
        max_workers=upload_workers, backend='osn', on_success=flusher.add,
        stored_sizes_out=stored_sizes)
    flusher.flush()

    print(f"  Pushed: {success}, Failed: {failed}, "
          f"Skipped (unchanged): {len(plan.unchanged)}")
    for msg in failures[:10]:
        print(f"    Error: {msg}")
    if len(failures) > 10:
        print(f"    ... and {len(failures) - 10} more")
    if failed:
        print("  Re-run `campfire push` to retry the failed files "
              "(landed files will dedup-skip).")

    return {'planned': len(tasks), 'pushed': success, 'failed': failed,
            'skipped': len(plan.unchanged)}


def push_field(
    field: str,
    config: dict,
    *,
    filters: Optional[list[str]] = None,
    dry_run: bool = False,
    workers: Optional[int] = None,
    dry_run_note: bool = True,
) -> dict:
    """Push a NIRCam field's tree products to OSN (bytes only).

    Covers canonical exposures (sci_dq identity), expmaps, preview/full PNGs,
    and mosaics (whole-file identity). No deployment is recorded and no
    ``nircam_exposures``/``nircam_images`` rows are touched — a later
    ``campfire deploy --field`` attaches everything to a deployment and
    dedup-skips the bytes this push landed.
    """
    from campfire.deploy.nircam import (
        _discover_filters, _resolve_nircam_dirs, build_fits_upload_tasks,
        build_upload_tasks, deployable_mosaics, discover_expmap_tasks,
        discover_exposures, discover_mosaics, _read_field_provenance,
    )
    from campfire.deploy.supabase import get_supabase_client, get_user_id_from_token

    dirs = _resolve_nircam_dirs(field)
    if not dirs['products'].exists():
        print(f"Error: Products directory not found: {dirs['products']}")
        return {'planned': 0, 'pushed': 0, 'failed': 0, 'skipped': 0}

    available = _discover_filters(dirs)
    if filters:
        # An explicit selection stays a selection: unknown filters are dropped
        # with a warning, and an all-unknown selection pushes NOTHING — falling
        # back to every filter here would turn a typo (--filter f999w) into an
        # unintended full-field transfer.
        missing = [f for f in filters if f not in available]
        if missing:
            print(f"Warning: no products for filter(s): {', '.join(missing)}")
        filters = [f for f in filters if f in available]
        if not filters:
            print("No matching filters — nothing to push.")
            return {'planned': 0, 'pushed': 0, 'failed': 0, 'skipped': 0}
    else:
        filters = available

    exposures = discover_exposures(dirs, filters)
    tasks: list[UploadTask] = list(build_fits_upload_tasks(field, exposures))
    tasks += list(discover_expmap_tasks(dirs, field, filters))
    tasks += [t[0] for t in build_upload_tasks(dirs, field, filters)]
    tasks += [UploadTask(m['path'], m['storage_key'], m['content_type'])
              for m in deployable_mosaics(discover_mosaics(dirs, field, filters))]

    print(f"Field: {field}")
    print(f"  Filters: {', '.join(filters)}")
    print(f"  Tree products discovered: {len(tasks)}")
    if not tasks:
        print("  Nothing to push.")
        return {'planned': 0, 'pushed': 0, 'failed': 0, 'skipped': 0}

    client = get_supabase_client(config)
    store = open_reducer_store()
    plan, _server_rows = plan_remote_push(client, store, tasks, backend='osn')
    print(f"  Plan: {plan.summary()}")

    if dry_run or not plan.to_upload:
        if dry_run and dry_run_note:
            print("Dry run — no changes made.")
        elif not dry_run:
            print("  Cloud already matches local products.")
        return {'planned': len(tasks), 'pushed': 0, 'failed': 0,
                'skipped': len(plan.unchanged)}

    cfpipe_version, _jwst, _crds = _read_field_provenance(dirs, field, filters)
    user_id = get_user_id_from_token(config)
    # Transport-compressed uploads (gzipped mosaics) report their stored byte
    # count; the flusher writes it to registry.stored_size_bytes. Wired here as
    # well as in deploy so the slow-link workflow (push heavy bytes first, then
    # deploy dedup-skips them) records it too — deploy never re-uploads.
    stored_sizes: dict[str, int] = {}
    flusher = registration_flusher(
        client, store, plan, backend='osn', deployment_id=None,
        uploaded_by=user_id, cfpipe_version=cfpipe_version,
        stored_sizes=stored_sizes)

    upload_workers = workers or default_upload_workers()
    total_bytes = sum(plan.stats[t.r2_key][1] for t in plan.to_upload
                      if t.r2_key in plan.stats)
    print(f"  Pushing {len(plan.to_upload)} file(s) "
          f"({total_bytes / 1e9:.2f} GB) to OSN...")
    success, failed, failures = upload_files_parallel(
        config, plan.to_upload, desc='Pushing to OSN',
        max_workers=upload_workers, backend='osn', on_success=flusher.add,
        stored_sizes_out=stored_sizes)
    flusher.flush()

    print(f"  Pushed: {success}, Failed: {failed}, "
          f"Skipped (unchanged): {len(plan.unchanged)}")
    for msg in failures[:10]:
        print(f"    Error: {msg}")
    if failed:
        print("  Re-run `campfire push` to retry the failed files "
              "(landed files will dedup-skip).")

    return {'planned': len(tasks), 'pushed': success, 'failed': failed,
            'skipped': len(plan.unchanged)}
