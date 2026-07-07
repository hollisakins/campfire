"""CAMPFIRE sync engine.

Provides metadata synchronization (catalog pull) and FITS file downloading.
Session creation and manifest fetching are delegated to the ``api`` subpackage.
"""

import hashlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

from .api.session import create_download_session
from .exceptions import DownloadError


def compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hash of a file, returning ``sha256:<hex>`` format."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _make_progress(show, unit, desc, position=None):
    """Create a tqdm progress bar and page-complete callback.

    ``position`` pins the bar to a fixed terminal row so the four sync streams
    render as a stable stacked group while they fetch concurrently. When
    ``show`` is False, returns ``(None, None)`` and the paginator skips progress
    reporting entirely.
    """
    if not show:
        return None, None
    pbar = tqdm(unit=unit, desc=desc, position=position, leave=True)

    def callback(fetched, total):
        # total is only known once the first page returns (include_counts); until
        # then the bar renders indeterminate, then snaps to a percentage.
        if total:
            pbar.total = total
        pbar.n = fetched
        pbar.refresh()

    return pbar, callback


def _apply_objects(store, fetched, updated_since, sync_ts):
    """Apply fetched objects to the local store (main-thread write phase).

    Returns (object_count, purged_count, incremental, needs_full_sync).
    """
    all_objects, server_total = fetched
    incremental = updated_since is not None

    obj_count = store.upsert_objects(all_objects)

    needs_full_sync = False
    if incremental and server_total > 0:
        local_total = store._conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        if local_total != server_total:
            needs_full_sync = True

    purged = 0
    if not incremental:
        purged = store.purge_stale_objects(sync_ts)

    return obj_count, purged, incremental, needs_full_sync


def _apply_spectra(store, fetched, updated_since, sync_ts):
    """Apply fetched spectra to the local store.

    Returns (spectra_count, purge_result, incremental).
    """
    all_spectra, _server_total = fetched
    incremental = updated_since is not None

    spec_count = store.upsert_spectra(all_spectra)

    purge_result = None
    if not incremental:
        purge_result = store.purge_stale_spectra(sync_ts)

    return spec_count, purge_result, incremental


def _apply_photometry(store, fetched, updated_since, sync_ts):
    """Apply fetched photometry to the local store.

    Returns (record_count, purged_count).
    """
    all_records, _total_count = fetched

    rec_count = store.upsert_photometry(all_records)

    purged = 0
    if updated_since is None:
        purged = store.purge_stale_photometry(sync_ts)

    return rec_count, purged


def _sync_tags(api, store, show_progress):
    """Sync tag metadata from the server."""
    try:
        tags_data = api.fetch_tags()
        return store.upsert_tags(tags_data)
    except requests.RequestException:
        return 0


def _apply_storage(store, fetched, updated_since, sync_ts):
    """Apply the fetched storage_objects mirror to the local store.

    Returns (row_count, purged, orphaned).
    """
    all_rows, _server_total = fetched
    incremental = updated_since is not None

    n = store.upsert_storage_objects(all_rows)

    purged = 0
    orphaned: List[str] = []
    if not incremental:
        res = store.purge_stale_storage_objects(sync_ts)
        purged = res["purged"]
        orphaned = res["orphaned_files"]

    return n, purged, orphaned


# The four independent /sync/* catalogs, each pinned to a fixed progress row
# (option A): a stable stacked group so every count stays anchored to a real
# per-entity total instead of a meaningless catalog-wide sum. Fields:
# (result key, APIClient method name, tqdm unit, padded bar label).
_FETCH_STREAMS = (
    ("objects", "fetch_all_objects", "obj", "Objects   "),
    ("spectra", "fetch_all_spectra", "spec", "Spectra   "),
    ("storage", "fetch_all_storage", "obj", "Storage   "),
    ("photometry", "fetch_all_photometry", "rec", "Photometry"),
)


def _fetch_all_concurrent(api, cursors, use_bars, show_progress):
    """Fetch the four independent /sync/* catalogs concurrently.

    Each stream is network-bound and independent, so wall time collapses from the
    sum of the four fetches toward the slowest single one. The SQLite store is
    single-threaded, so workers only touch the network here; every write happens
    on the caller's thread afterwards. Returns ``{key: (rows, server_total)}``.
    """
    bars = []
    results: Dict[str, Tuple[List[dict], int]] = {}
    try:
        with ThreadPoolExecutor(max_workers=len(_FETCH_STREAMS)) as executor:
            future_to_key = {}
            for position, (key, method_name, unit, desc) in enumerate(_FETCH_STREAMS):
                pbar, callback = _make_progress(use_bars, unit, desc, position=position)
                bars.append(pbar)
                method = getattr(api, method_name)
                future = executor.submit(
                    method, updated_since=cursors[key], on_page_complete=callback
                )
                future_to_key[future] = key
            # Surfaces the first failing stream's exception once the pool drains.
            for future in as_completed(future_to_key):
                results[future_to_key[future]] = future.result()
    finally:
        for pbar in bars:
            if pbar is not None:
                pbar.close()
        if use_bars:
            # Drop the cursor below the (leave=True) stacked bars so whatever the
            # caller prints next doesn't overwrite them.
            sys.stderr.write("\n" * len(_FETCH_STREAMS))
            sys.stderr.flush()

    if show_progress and not use_bars:
        # Non-TTY (CI, redirected logs): no live bars, so emit one completion
        # line per stream instead.
        for key, _method_name, _unit, desc in _FETCH_STREAMS:
            rows = results.get(key, ([], 0))[0]
            print(f"  {desc.strip()}: {len(rows):,}", file=sys.stderr)

    return results


def sync_metadata(
    api, store, meta_dir: Path,
    show_progress: bool = False,
    full: bool = False,
) -> dict:
    """Sync the objects + spectra catalog from the server.

    On first sync (or ``full=True``), fetches the entire catalog.
    On subsequent syncs, only fetches records modified since the last
    sync (incremental), using the server-side ``updated_at`` timestamp.

    Returns
    -------
    dict
        Summary with keys: observations, objects, spectra, photometry,
        tags, stale_count, stale_files, incremental.
    """
    from .db.export import export_catalogs

    use_bars = show_progress and sys.stderr.isatty()

    # One timestamp captured before any fetch. Purge removes rows whose
    # _synced_at predates it; every upsert below stamps a strictly later time, so
    # only rows the server no longer returns get purged.
    sync_ts = datetime.now(timezone.utc).isoformat()

    # Incremental cursors are store reads, so resolve them here on the main thread
    # before the workers start (workers must not touch the single-threaded
    # SQLite connection).
    cursors = {
        "objects": None if full else store.get_max_objects_updated_at(),
        "spectra": None if full else store.get_max_spectra_updated_at(),
        "storage": None if full else store.get_max_storage_updated_at(),
        "photometry": None if full else store.get_max_photometry_updated_at(),
    }

    # 1. Fetch all four catalogs concurrently (network only).
    fetched = _fetch_all_concurrent(api, cursors, use_bars, show_progress)

    # 2. Apply to the local store serially (single-threaded SQLite connection).
    obj_count, obj_purged, incremental, needs_full_sync = _apply_objects(
        store, fetched["objects"], cursors["objects"], sync_ts
    )
    spec_count, spec_purge, spec_incremental = _apply_spectra(
        store, fetched["spectra"], cursors["spectra"], sync_ts
    )
    storage_count, storage_purged, storage_orphaned = _apply_storage(
        store, fetched["storage"], cursors["storage"], sync_ts
    )
    phot_count, phot_purged = _apply_photometry(
        store, fetched["photometry"], cursors["photometry"], sync_ts
    )

    # 3. Sync tag metadata (single request), then export CSVs.
    tags_count = _sync_tags(api, store, show_progress)

    export_catalogs(store, meta_dir)

    # 4. Detect stale local files (server hash != local hash, via the mirror)
    stale = store.get_stale_objects()

    obs_set = set(store.get_synced_observations())

    result = {
        "observations": len(obs_set),
        "objects": obj_count,
        "objects_purged": obj_purged,
        "spectra": spec_count,
        "storage_objects": storage_count,
        "storage_purged": storage_purged,
        "photometry": phot_count,
        "photometry_purged": phot_purged,
        "tags": tags_count,
        "stale_count": len(stale),
        "stale_files": stale,
        "incremental": incremental and spec_incremental,
        "needs_full_sync": needs_full_sync,
    }
    if spec_purge:
        result["purged_spectra"] = spec_purge["purged_spectra"]
    if storage_orphaned:
        result["orphaned_files"] = storage_orphaned

    return result


def _download_and_verify_key(
    obj: dict,
    download_url: str,
    products_dir: Path,
    download_session: requests.Session,
) -> dict:
    """Download one storage object, verify its hash, return local-state dict.

    The destination is derived from the object's storage key via the shared
    layout contract (``products_relpath``), so every product type lands in the
    same tree the pipeline writes and deploy reads (``products/nirspec/<obs>/…``).
    """
    from .config import products_relpath

    key = obj["storage_key"]
    rel = products_relpath(key)
    local_path = products_dir / rel
    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_name(local_path.name + ".tmp")

    expected = obj.get("content_hash")
    try:
        response = download_session.get(download_url, stream=True, timeout=300)
        response.raise_for_status()

        hasher = hashlib.sha256()
        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                f.write(chunk)
                hasher.update(chunk)

        computed_hash = f"sha256:{hasher.hexdigest()}"

        # Only verify against authoritative sha256: hashes (etag: rows are
        # provisional bucket metadata, not a content digest).
        if expected and expected.startswith("sha256:") and computed_hash != expected:
            tmp_path.unlink()
            raise DownloadError(
                f"Hash mismatch for {key}: expected {expected}, got {computed_hash}"
            )

        tmp_path.rename(local_path)
        st = local_path.stat()
        return {
            "storage_key": key,
            "local_path": str(local_path.relative_to(products_dir)),
            "local_file_hash": computed_hash,
            "local_file_size": st.st_size,
            "local_file_mtime": st.st_mtime,
        }
    except DownloadError:
        raise
    except requests.RequestException as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise DownloadError(f"Failed to download {key}: {e}")


def download_objects(
    api_client,
    observations: List[str],
    product_types: List[str],
    store,
    products_dir: Path,
    max_workers: int = 4,
    dry_run: bool = False,
    download_session: Optional[requests.Session] = None,
    gratings: Optional[List[str]] = None,
    fields: Optional[List[str]] = None,
    filters: Optional[List[str]] = None,
) -> dict:
    """Download storage objects for the given observations/fields + product types.

    The single, product-type-agnostic download engine: plan locally from the
    storage_objects mirror, presign the to-download set, fetch in parallel,
    verify ``content_hash``, and record local state. ``fields`` selects
    field-scoped NIRCam rows (``observation IS NULL``); ``filters`` narrows those
    to specific NIRCam filters. Used for NIRSpec finals, intermediates, and
    NIRCam alike.
    """
    pending = store.get_pending_objects(
        observations=list(observations),
        product_types=list(product_types),
        gratings=gratings,
        fields=list(fields) if fields else None,
        filters=list(filters) if filters else None,
    )
    to_download = [row for rows in pending.values() for row in rows]

    stats = {
        "to_download": len(to_download),
        "download_bytes": sum(r.get("size_bytes") or 0 for r in to_download),
        "downloaded": 0,
        "failed": 0,
        "unauthorized": 0,
        "by_observation": {obs: len(rows) for obs, rows in pending.items()},
    }

    if dry_run or not to_download:
        return stats

    products_dir.mkdir(parents=True, exist_ok=True)

    urls = api_client.presign_keys([r["storage_key"] for r in to_download])
    fetchable = [r for r in to_download if r["storage_key"] in urls]
    stats["unauthorized"] = len(to_download) - len(fetchable)

    dl_session = download_session or create_download_session(max_workers)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_obj = {
            executor.submit(
                _download_and_verify_key, r, urls[r["storage_key"]], products_dir, dl_session
            ): r
            for r in fetchable
        }
        with tqdm(total=len(fetchable), desc="Downloading", unit="file") as pbar:
            for future in as_completed(future_to_obj):
                obj = future_to_obj[future]
                try:
                    result = future.result()
                    store.mark_object_synced(
                        storage_key=result["storage_key"],
                        local_path=result["local_path"],
                        local_file_hash=result["local_file_hash"],
                        local_file_size=result["local_file_size"],
                        local_file_mtime=result["local_file_mtime"],
                    )
                    stats["downloaded"] += 1
                except Exception as e:
                    stats["failed"] += 1
                    tqdm.write(f"  Failed: {obj['storage_key']}: {e}")
                pbar.update(1)

    return stats


def format_size(size_bytes: int) -> str:
    """Format byte count as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
