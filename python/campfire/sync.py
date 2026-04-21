"""CAMPFIRE sync engine.

Provides metadata synchronization (catalog pull) and FITS file downloading.
Session creation and manifest fetching are delegated to the ``api`` subpackage.
"""

import hashlib
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


def _make_progress(show, unit, desc):
    """Create a tqdm progress bar and page-complete callback."""
    if not show:
        return None, None
    pbar = tqdm(unit=unit, desc=desc)

    def callback(fetched, total):
        pbar.total = total
        pbar.n = fetched
        pbar.refresh()

    return pbar, callback


def _sync_objects(api, store, full, show_progress):
    """Sync objects from the server.

    Returns (object_count, purged_count, incremental, needs_full_sync,
    server_total, sync_timestamp).
    """
    updated_since = None
    if not full:
        updated_since = store.get_max_objects_updated_at()
    incremental = updated_since is not None

    sync_timestamp = datetime.now(timezone.utc).isoformat()

    pbar, callback = _make_progress(show_progress, "obj", "Objects")

    all_objects, server_total = api.fetch_all_objects(
        updated_since=updated_since,
        on_page_complete=callback,
    )
    if pbar:
        pbar.close()

    obj_count = store.upsert_objects(all_objects)

    needs_full_sync = False
    if incremental and server_total > 0:
        local_total = store._conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        if local_total != server_total:
            needs_full_sync = True

    purged = 0
    if not incremental:
        purged = store.purge_stale_objects(sync_timestamp)

    return obj_count, purged, incremental, needs_full_sync, server_total, sync_timestamp


def _sync_spectra(api, store, full, show_progress):
    """Sync spectra from the server.

    Returns (spectra_count, purge_result, incremental, sync_timestamp).
    """
    updated_since = None
    if not full:
        updated_since = store.get_max_spectra_updated_at()
    incremental = updated_since is not None

    sync_timestamp = datetime.now(timezone.utc).isoformat()

    pbar, callback = _make_progress(show_progress, "spec", "Spectra")

    all_spectra, _server_total = api.fetch_all_spectra(
        updated_since=updated_since,
        on_page_complete=callback,
    )
    if pbar:
        pbar.close()

    spec_count = store.upsert_spectra(all_spectra)

    purge_result = None
    if not incremental:
        purge_result = store.purge_stale_spectra(sync_timestamp)

    return spec_count, purge_result, incremental, sync_timestamp


def _sync_photometry(api, store, full, show_progress):
    """Sync object photometry from the server.

    Returns (record_count, purged_count).
    """
    updated_since = None
    if not full:
        updated_since = store.get_max_photometry_updated_at()

    sync_timestamp = datetime.now(timezone.utc).isoformat()

    pbar, callback = _make_progress(show_progress, "rec", "Photometry")

    all_records, _total_count = api.fetch_all_photometry(
        updated_since=updated_since,
        on_page_complete=callback,
    )
    if pbar:
        pbar.close()

    rec_count = store.upsert_photometry(all_records)

    purged = 0
    if updated_since is None:
        purged = store.purge_stale_photometry(sync_timestamp)

    return rec_count, purged


def _sync_tags(api, store, show_progress):
    """Sync tag metadata from the server."""
    try:
        tags_data = api.fetch_tags()
        return store.upsert_tags(tags_data)
    except requests.RequestException:
        return 0


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

    # 1. Sync objects
    (obj_count, obj_purged, incremental, needs_full_sync,
     server_total, _obj_ts) = _sync_objects(api, store, full, show_progress)

    # 2. Sync spectra
    spec_count, spec_purge, spec_incremental, _spec_ts = _sync_spectra(
        api, store, full, show_progress
    )

    # 3. Sync photometry
    phot_count, phot_purged = _sync_photometry(api, store, full, show_progress)

    # 4. Sync tag metadata
    tags_count = _sync_tags(api, store, show_progress)

    # 5. Export CSVs
    export_catalogs(store, meta_dir)

    # 6. Detect stale local files
    stale = store.get_stale_files()

    obs_set = set(store.get_synced_observations())

    result = {
        "observations": len(obs_set),
        "objects": obj_count,
        "objects_purged": obj_purged,
        "spectra": spec_count,
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
        result["orphaned_files"] = spec_purge["orphaned_files"]

    return result


def compute_download_plan(
    manifest: dict,
    synced_files: Dict[int, dict],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Compare manifest against local state to determine what needs downloading.

    The ``synced_files`` dict is keyed by the integer PK (``spectra_id``);
    ``synced_files[id]["file_hash"]`` is the locally hashed value (the server
    hash lives in the manifest entry).
    """
    new_files = []
    updated_files = []
    up_to_date = []

    for spec in manifest.get("spectra", []):
        spectra_id = spec["spectra_id"]
        local = synced_files.get(spectra_id)

        if local is None:
            new_files.append(spec)
        elif spec.get("file_hash") and local.get("file_hash") != spec["file_hash"]:
            updated_files.append(spec)
        else:
            up_to_date.append(spec)

    return new_files, updated_files, up_to_date


def download_and_verify(
    spec: dict,
    obs_dir: Path,
    products_dir: Path,
    download_session: requests.Session,
) -> dict:
    """Download a single file, verify checksum, return result dict."""
    filename = Path(spec["fits_path"]).name
    local_path = obs_dir / filename
    tmp_path = local_path.with_suffix(".tmp")

    try:
        response = download_session.get(spec["download_url"], stream=True, timeout=300)
        response.raise_for_status()

        hasher = hashlib.sha256()

        with open(tmp_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=65536):
                f.write(chunk)
                hasher.update(chunk)

        computed_hash = f"sha256:{hasher.hexdigest()}"

        if spec.get("file_hash") and computed_hash != spec["file_hash"]:
            tmp_path.unlink()
            raise DownloadError(
                f"Hash mismatch for {spec['fits_path']}: "
                f"expected {spec['file_hash']}, got {computed_hash}"
            )

        tmp_path.rename(local_path)

        st = local_path.stat()
        return {
            "id": spec.get("spectra_id"),
            "spectrum_id": spec.get("spectrum_id"),
            "target_id": spec.get("target_id"),
            "observation": spec.get("observation", obs_dir.name),
            "grating": spec["grating"],
            "fits_path": spec["fits_path"],
            "local_path": str(local_path.relative_to(products_dir)),
            "file_hash": computed_hash,
            "file_size": st.st_size,
            "local_file_mtime": st.st_mtime,
            "local_file_size": st.st_size,
        }

    except DownloadError:
        raise
    except requests.RequestException as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise DownloadError(f"Failed to download {spec['fits_path']}: {e}")


def download_observation(
    api_client,
    obs_name: str,
    products_dir: Path,
    store,
    max_workers: int = 4,
    dry_run: bool = False,
    download_session: Optional[requests.Session] = None,
    manifest: Optional[dict] = None,
    grating_filter: Optional[List[str]] = None,
) -> dict:
    """Download FITS files for a single observation."""
    if manifest is None:
        manifest = api_client.fetch_manifest(obs_name)

    if grating_filter:
        grating_set = set(g.upper() for g in grating_filter)
        manifest = dict(manifest)
        manifest["spectra"] = [
            s for s in manifest.get("spectra", [])
            if s.get("grating", "").upper() in grating_set
        ]

    synced = store.get_synced_files(obs_name)
    new_files, updated_files, up_to_date = compute_download_plan(manifest, synced)

    to_download = new_files + updated_files
    total_bytes = sum(s.get("file_size") or 0 for s in to_download)

    stats = {
        "observation": obs_name,
        "new_count": len(new_files),
        "updated_count": len(updated_files),
        "up_to_date_count": len(up_to_date),
        "download_bytes": total_bytes,
        "downloaded": 0,
        "failed": 0,
        "skipped": len(up_to_date),
    }

    if dry_run or not to_download:
        return stats

    obs_dir = products_dir / obs_name
    obs_dir.mkdir(parents=True, exist_ok=True)

    dl_session = download_session or create_download_session(max_workers)

    log_id = store.log_sync_start(obs_name)
    bytes_downloaded = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_spec = {
            executor.submit(download_and_verify, spec, obs_dir, products_dir, dl_session): spec
            for spec in to_download
        }

        with tqdm(total=len(to_download), desc=obs_name, unit="file") as pbar:
            for future in as_completed(future_to_spec):
                spec = future_to_spec[future]
                try:
                    result = future.result()
                    store.mark_synced(
                        spectrum_id=result["spectrum_id"],
                        local_path=result["local_path"],
                        file_hash=result["file_hash"],
                        file_size=result["file_size"],
                        local_file_mtime=result.get("local_file_mtime"),
                        local_file_size=result.get("local_file_size"),
                    )
                    stats["downloaded"] += 1
                    bytes_downloaded += result.get("file_size") or 0
                except Exception as e:
                    stats["failed"] += 1
                    tqdm.write(f"  Failed: {spec['fits_path']}: {e}")
                pbar.update(1)

    store.log_sync_complete(log_id, stats["downloaded"], stats["skipped"], bytes_downloaded)
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
