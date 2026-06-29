"""
Cloudflare R2 upload helpers.

Supports two upload modes:

1. **Presigned URLs** (preferred): Requests batch presigned PutObject URLs from
   the CAMPFIRE web API and uploads directly to R2. No R2 credentials needed
   on the client — just ``campfire login``.

2. **Direct boto3** (legacy fallback): Uses R2 credentials from deploy config.
   Used when presigned URL generation is not available.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple, Optional

import requests as http_requests
from tqdm import tqdm


class UploadTask(NamedTuple):
    """Represents a file to be uploaded to R2."""
    local_path: Path
    r2_key: str
    content_type: str


# ---------------------------------------------------------------------------
# Presigned URL mode
# ---------------------------------------------------------------------------

PRESIGN_BATCH_SIZE = 500  # Max URLs per presign request


def _get_presign_headers(config: dict) -> Optional[dict]:
    """Get auth headers for presign endpoint, or None if not available."""
    sb = config.get('supabase', {})
    token = sb.get('supabase_token')
    if not token:
        return None

    # Try to get the access token from stored credentials for API auth
    try:
        from campfire.api.session import resolve_base_url
        from campfire.auth.tokens import TokenManager
        tm = TokenManager(base_url=resolve_base_url())
        access_token = tm.get_valid_token()
        return {'Authorization': f'Bearer {access_token}'}
    except Exception:
        return None


def _get_presign_base_url() -> str:
    """Get the web API base URL for presign requests."""
    from campfire.api.session import resolve_base_url
    return resolve_base_url()


def request_presigned_urls(
    config: dict,
    tasks: list[UploadTask],
    bucket: str = 'data',
    cache_control: Optional[str] = None,
) -> Optional[dict[str, str]]:
    """
    Request presigned PutObject URLs from the web API in batches.

    Parameters
    ----------
    config : dict
        Deploy config (used for auth).
    tasks : list[UploadTask]
        Files to upload.
    bucket : str
        Bucket identifier: 'data' or 'tiles'.
    cache_control : str, optional
        Cache-Control header to set on uploaded objects.

    Returns
    -------
    dict or None
        Mapping of r2_key → presigned URL, or None if presigning
        is not available (falls back to direct upload).
    """
    headers = _get_presign_headers(config)
    if not headers:
        return None

    base_url = _get_presign_base_url()
    presign_url = f"{base_url}/deploy/presign"

    all_urls: dict[str, str] = {}

    # Batch requests to stay within server limits
    for i in range(0, len(tasks), PRESIGN_BATCH_SIZE):
        batch = tasks[i:i + PRESIGN_BATCH_SIZE]
        payload = {
            'bucket': bucket,
            'uploads': [
                {'key': t.r2_key, 'content_type': t.content_type}
                for t in batch
            ],
        }
        if cache_control:
            payload['cache_control'] = cache_control

        try:
            resp = http_requests.post(presign_url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            all_urls.update(data['urls'])
        except Exception as e:
            print(f"  Warning: Presign request failed: {e}")
            return None

    return all_urls


def _upload_to_presigned_url(url: str, local_path: Path, content_type: str) -> None:
    """Upload a single file to a presigned PutObject URL."""
    with open(local_path, 'rb') as f:
        resp = http_requests.put(
            url,
            data=f,
            headers={'Content-Type': content_type},
            timeout=300,
        )
        resp.raise_for_status()


def upload_files_presigned(
    urls: dict[str, str],
    tasks: list[UploadTask],
    max_workers: int = 12,
    desc: str = 'Uploading',
    succeeded_out: Optional[set[str]] = None,
) -> tuple[int, int, list[str]]:
    """
    Upload files using presigned URLs.

    Parameters
    ----------
    urls : dict
        Mapping of r2_key → presigned URL.
    tasks : list[UploadTask]
        Files to upload (must match keys in urls).
    max_workers : int
        Parallel upload threads.
    desc : str
        Progress bar description.
    succeeded_out : set[str], optional
        If given, the ``r2_key`` of each successfully-uploaded object is added to
        this set. Lets the caller register only objects that actually landed
        (write-after-success) without changing the return arity.

    Returns
    -------
    tuple
        (success_count, failure_count, list_of_failure_messages)
    """
    if not tasks:
        return 0, 0, []

    success, failed = 0, 0
    failed_files: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(
                _upload_to_presigned_url,
                urls[task.r2_key], task.local_path, task.content_type,
            ): task
            for task in tasks
            if task.r2_key in urls
        }

        with tqdm(total=len(future_to_task), desc=desc, unit='file') as pbar:
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    future.result()
                    success += 1
                    if succeeded_out is not None:
                        succeeded_out.add(task.r2_key)
                except Exception as e:
                    failed += 1
                    failed_files.append(f"{task.local_path.name}: {e}")
                pbar.update(1)

    return success, failed, failed_files


# ---------------------------------------------------------------------------
# Direct boto3 mode (legacy fallback)
# ---------------------------------------------------------------------------

def get_r2_client(config: dict):
    """Create a boto3 S3 client for the ``data`` storage backend.

    Endpoint/region/addressing-style come from config via the per-purpose
    backend factory (defaults to Cloudflare R2). Name kept for backward
    compatibility with existing importers.
    """
    from campfire.deploy.backend import make_client_for
    return make_client_for(config, 'data')


def upload_to_r2(
    client,
    bucket: str,
    local_path: Path,
    r2_key: str,
    content_type: str | None = None,
    cache_control: str | None = None,
) -> None:
    """Upload a single file to R2 via boto3."""
    extra_args = {}
    if content_type:
        extra_args['ContentType'] = content_type
    if cache_control:
        extra_args['CacheControl'] = cache_control

    client.upload_file(
        str(local_path),
        bucket,
        r2_key,
        ExtraArgs=extra_args or None,
    )


def upload_files_direct(
    client,
    bucket: str,
    tasks: list[UploadTask],
    max_workers: int = 12,
    desc: str = 'Uploading',
    succeeded_out: Optional[set[str]] = None,
) -> tuple[int, int, list[str]]:
    """
    Upload multiple files to R2 via boto3 in parallel with progress bar.

    ``succeeded_out``, if given, collects the ``r2_key`` of each landed object.

    Returns
    -------
    tuple
        (success_count, failure_count, list_of_failure_messages)
    """
    if not tasks:
        return 0, 0, []

    success, failed = 0, 0
    failed_files: list[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_task = {
            executor.submit(
                upload_to_r2, client, bucket,
                task.local_path, task.r2_key, task.content_type,
            ): task
            for task in tasks
        }

        with tqdm(total=len(tasks), desc=desc, unit='file') as pbar:
            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    future.result()
                    success += 1
                    if succeeded_out is not None:
                        succeeded_out.add(task.r2_key)
                except Exception as e:
                    failed += 1
                    failed_files.append(f"{task.local_path.name}: {e}")
                pbar.update(1)

    return success, failed, failed_files


# ---------------------------------------------------------------------------
# Unified upload interface
# ---------------------------------------------------------------------------

def upload_files_parallel(
    config: dict,
    tasks: list[UploadTask],
    bucket_id: str = 'data',
    max_workers: int = 12,
    desc: str = 'Uploading',
    cache_control: Optional[str] = None,
    succeeded_out: Optional[set[str]] = None,
) -> tuple[int, int, list[str]]:
    """
    Upload files to R2, using presigned URLs if available, else direct boto3.

    This is the main entry point for all R2 uploads. It tries presigned URLs
    first (no R2 credentials needed), falling back to direct boto3 upload.

    Parameters
    ----------
    config : dict
        Deploy config.
    tasks : list[UploadTask]
        Files to upload.
    bucket_id : str
        'data' for spectra/rgb/sed, 'tiles' for map tiles.
    max_workers : int
        Parallel upload threads.
    desc : str
        Progress bar description.
    cache_control : str, optional
        Cache-Control header for uploaded objects.
    succeeded_out : set[str], optional
        If given, collects the ``r2_key`` of each successfully-uploaded object so
        the caller can register exactly what landed (used for storage_objects).

    Returns
    -------
    tuple
        (success_count, failure_count, list_of_failure_messages)
    """
    if not tasks:
        return 0, 0, []

    # Try presigned URL mode first
    urls = request_presigned_urls(config, tasks, bucket=bucket_id, cache_control=cache_control)
    if urls:
        return upload_files_presigned(
            urls, tasks, max_workers=max_workers, desc=desc, succeeded_out=succeeded_out,
        )

    # Fall back to direct boto3 upload via the per-purpose backend factory.
    from campfire.deploy.backend import (
        PURPOSE_SECTIONS, make_s3_client, resolve_backend,
    )

    purpose = 'tiles' if bucket_id == 'tiles' else 'data'
    if PURPOSE_SECTIONS[purpose] not in config:
        raise ValueError(
            f"No storage credentials available for the '{bucket_id}' bucket. "
            "Run 'campfire login' to use presigned URLs, or provide storage "
            "credentials in deploy config."
        )

    # A present-but-incomplete section surfaces resolve_backend's specific
    # diagnostic (which key/endpoint is missing), not the generic message above.
    backend_cfg = resolve_backend(config, purpose)
    client = make_s3_client(backend_cfg)
    bucket_name = backend_cfg.bucket

    # For direct mode, apply cache_control per-file
    if cache_control:
        original_tasks = tasks
        tasks_with_cache = []
        for task in original_tasks:
            tasks_with_cache.append(task)

        success, failed = 0, 0
        failed_files: list[str] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(
                    upload_to_r2, client, bucket_name,
                    task.local_path, task.r2_key, task.content_type, cache_control,
                ): task
                for task in tasks
            }

            with tqdm(total=len(tasks), desc=desc, unit='file') as pbar:
                for future in as_completed(future_to_task):
                    task = future_to_task[future]
                    try:
                        future.result()
                        success += 1
                        if succeeded_out is not None:
                            succeeded_out.add(task.r2_key)
                    except Exception as e:
                        failed += 1
                        failed_files.append(f"{task.local_path.name}: {e}")
                    pbar.update(1)

        return success, failed, failed_files

    return upload_files_direct(
        client, bucket_name, tasks, max_workers=max_workers, desc=desc,
        succeeded_out=succeeded_out,
    )
