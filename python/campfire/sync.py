"""CAMPFIRE sync engine for bulk downloading spectra.

Computes download plans by diffing manifests against local state, and
downloads files in parallel with hash verification. Session creation and
manifest fetching are delegated to the ``api`` subpackage.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from tqdm import tqdm

from .api.session import create_download_session
from .exceptions import DownloadError


def compute_download_plan(
    manifest: dict,
    synced_files: Dict[int, dict],
) -> Tuple[List[dict], List[dict], List[dict]]:
    """Compare manifest against local state to determine what needs downloading.

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


def sync_observation(
    api_session,
    obs_name: str,
    data_dir: Path,
    state,
    max_workers: int = 4,
    dry_run: bool = False,
    download_session: Optional[requests.Session] = None,
    manifest: Optional[dict] = None,
) -> dict:
    """Sync a single observation: fetch manifest, diff, download, update state.

    Parameters
    ----------
    api_session : APISession or APIClient
        Authenticated session or client for fetching manifests.
    obs_name : str
        Observation name.
    data_dir : Path
        Local data directory.
    state : LocalStore or SyncState
        State tracker with get_synced_files/mark_synced methods.
    max_workers : int
        Parallel download workers.
    dry_run : bool
        If True, compute plan but don't download.
    download_session : requests.Session, optional
        Reusable download session for presigned URLs.
    manifest : dict, optional
        Pre-fetched manifest (avoids re-fetching if caller already has it).

    Returns
    -------
    dict
        Stats with counts of new/updated/skipped/failed files.
    """
    if manifest is None:
        from .api.client import APIClient
        if isinstance(api_session, APIClient):
            manifest = api_session.fetch_manifest(obs_name)
        else:
            # Treat as APISession — build an APIClient on the fly
            client = APIClient(api_session)
            manifest = client.fetch_manifest(obs_name)

    synced = state.get_synced_files(obs_name)
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

    log_id = state.log_sync_start(obs_name)
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
                    state.mark_synced(
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

    state.log_sync_complete(log_id, stats["downloaded"], stats["skipped"], bytes_downloaded)
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
