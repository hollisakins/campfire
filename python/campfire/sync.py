"""CAMPFIRE sync engine.

Provides metadata synchronization (catalog pull) and FITS file downloading.
Session creation and manifest fetching are delegated to the ``api`` subpackage.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

from .api.session import create_download_session
from .exceptions import DownloadError


def sync_metadata(api, store, data_dir: Path) -> dict:
    """Sync the full object/spectra catalog from the server.

    Fetches all accessible observations' metadata, upserts into the local
    SQLite database, exports CSV catalogs, and detects stale local files.

    Parameters
    ----------
    api : APIClient
        Authenticated API client.
    store : LocalStore
        Local database to update.
    data_dir : Path
        Data directory (for CSV export).

    Returns
    -------
    dict
        Summary with keys: observations, objects, spectra, stale_count, stale_files.
    """
    # TODO: Support incremental sync with updated_since parameter once the
    # API supports it, to avoid re-fetching the full catalog on every sync.

    from .db.export import export_catalogs

    # 1. Get all accessible observations
    obs_list = api.get_observations()
    obs_names = [o["observation"] for o in obs_list]

    # 2. Fetch all object metadata (paginated)
    all_objects = api.fetch_all_objects(obs_names)

    # 3. Upsert into SQLite
    obj_count, spec_count = store.upsert_objects(all_objects)

    # 4. Export CSVs
    meta_dir = data_dir / ".campfire_meta"
    export_catalogs(store, meta_dir)

    # 5. Detect stale local files
    stale = store.get_stale_files()

    return {
        "observations": len(obs_names),
        "objects": obj_count,
        "spectra": spec_count,
        "stale_count": len(stale),
        "stale_files": stale,
    }


def compute_download_plan(
    manifest: dict,
    synced_files: Dict[int, dict],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Compare manifest against local state to determine what needs downloading.

    The ``synced_files`` dict should have ``file_hash`` set to the local
    download hash (``local_file_hash`` from the store). This is compared
    against the manifest's server-side ``file_hash``.

    Returns
    -------
    (new_files, updated_files, up_to_date_files)
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
    data_dir: Path,
    download_session: requests.Session,
) -> dict:
    """Download a single file, verify checksum, return result dict.

    Uses .tmp file pattern for atomic writes - partial downloads never
    appear as complete files. Retries are handled by the session's
    urllib3 Retry adapter.
    """
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

        # Verify hash (skip if server hash is NULL - not yet backfilled)
        if spec.get("file_hash") and computed_hash != spec["file_hash"]:
            tmp_path.unlink()
            raise DownloadError(
                f"Hash mismatch for {spec['fits_path']}: "
                f"expected {spec['file_hash']}, got {computed_hash}"
            )

        # Atomic rename
        tmp_path.rename(local_path)

        return {
            "spectra_id": spec["spectra_id"],
            "object_id": spec["object_id"],
            "observation": spec.get("observation", obs_dir.name),
            "grating": spec["grating"],
            "fits_path": spec["fits_path"],
            "local_path": str(local_path.relative_to(data_dir)),
            "file_hash": computed_hash,
            "file_size": local_path.stat().st_size,
        }

    except DownloadError:
        raise  # Don't retry hash mismatches
    except requests.RequestException as e:
        if tmp_path.exists():
            tmp_path.unlink()
        raise DownloadError(f"Failed to download {spec['fits_path']}: {e}")


def download_observation(
    api_client,
    obs_name: str,
    data_dir: Path,
    store,
    max_workers: int = 4,
    dry_run: bool = False,
    download_session: Optional[requests.Session] = None,
    manifest: Optional[dict] = None,
    grating_filter: Optional[List[str]] = None,
) -> dict:
    """Download FITS files for a single observation.

    Parameters
    ----------
    api_client : APIClient
        Authenticated API client for fetching manifests.
    obs_name : str
        Observation name.
    data_dir : Path
        Local data directory.
    store : LocalStore
        Database for tracking downloaded files.
    max_workers : int
        Parallel download workers.
    dry_run : bool
        If True, compute plan but don't download.
    download_session : requests.Session, optional
        Reusable download session for presigned URLs.
    manifest : dict, optional
        Pre-fetched manifest (avoids re-fetching if caller already has it).
    grating_filter : list of str, optional
        Only download spectra matching these gratings.

    Returns
    -------
    dict
        Stats with counts of new/updated/skipped/failed files.
    """
    if manifest is None:
        manifest = api_client.fetch_manifest(obs_name)

    # Apply grating filter to manifest entries
    if grating_filter:
        grating_set = set(g.upper() for g in grating_filter)
        manifest = dict(manifest)  # shallow copy
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

    # Create observation directory
    obs_dir = data_dir / obs_name
    obs_dir.mkdir(parents=True, exist_ok=True)

    # Use provided download session or create one
    dl_session = download_session or create_download_session(max_workers)

    log_id = store.log_sync_start(obs_name)
    bytes_downloaded = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_spec = {
            executor.submit(download_and_verify, spec, obs_dir, data_dir, dl_session): spec
            for spec in to_download
        }

        with tqdm(total=len(to_download), desc=obs_name, unit="file") as pbar:
            for future in as_completed(future_to_spec):
                spec = future_to_spec[future]
                try:
                    result = future.result()
                    store.mark_synced(
                        result["spectra_id"],
                        result["object_id"],
                        obs_name,
                        result["grating"],
                        result["fits_path"],
                        result["local_path"],
                        result["file_hash"],
                        result["file_size"],
                    )
                    stats["downloaded"] += 1
                    bytes_downloaded += result.get("file_size") or 0
                except Exception as e:
                    stats["failed"] += 1
                    tqdm.write(f"  Failed: {spec['fits_path']}: {e}")
                pbar.update(1)

    store.log_sync_complete(log_id, stats["downloaded"], stats["skipped"], bytes_downloaded)
    return stats


# Keep old name as alias for backward compatibility
sync_observation = download_observation


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
