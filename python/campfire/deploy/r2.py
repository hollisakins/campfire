"""
Cloudflare R2 upload helpers.

Provides a boto3-based S3 client for Cloudflare R2 and parallel upload
with progress bar via ThreadPoolExecutor.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple

import boto3
from botocore.config import Config
from tqdm import tqdm


class UploadTask(NamedTuple):
    """Represents a file to be uploaded to R2."""
    local_path: Path
    r2_key: str
    content_type: str


def get_r2_client(config: dict):
    """Create boto3 S3 client configured for Cloudflare R2."""
    r2 = config['r2']
    return boto3.client(
        's3',
        endpoint_url=f"https://{r2['account_id']}.r2.cloudflarestorage.com",
        aws_access_key_id=r2['access_key_id'],
        aws_secret_access_key=r2['secret_access_key'],
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def upload_to_r2(
    client,
    bucket: str,
    local_path: Path,
    r2_key: str,
    content_type: str | None = None,
    cache_control: str | None = None,
) -> None:
    """Upload a single file to R2."""
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


def upload_files_parallel(
    client,
    bucket: str,
    tasks: list[UploadTask],
    max_workers: int = 12,
    desc: str = 'Uploading',
) -> tuple[int, int, list[str]]:
    """
    Upload multiple files to R2 in parallel with progress bar.

    Returns:
        (success_count, failure_count, list_of_failure_messages)
    """
    if not tasks:
        return 0, 0, []

    success, failed = 0, 0
    failed_files = []

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
                except Exception as e:
                    failed += 1
                    failed_files.append(f"{task.local_path.name}: {e}")
                pbar.update(1)

    return success, failed, failed_files
