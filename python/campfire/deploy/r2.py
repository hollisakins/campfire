"""Object-store upload transport, built on the shared ``campfire.storage`` core.

Two upload modes, selected by the resolved deploy auth mode — NOT by a silent
fallback (issue #250):

1. **Presigned URLs** (``login`` mode, the default): request batch presigned
   PutObject URLs from the CAMPFIRE web API and upload straight to the store. No
   object-store write credentials on the client — just ``campfire login``. If the
   presign endpoint is unavailable the upload FAILS loudly; it never silently
   drops to direct-boto3 (that silent switch was the footgun #250 removed).

2. **Direct boto3** (``service_role`` / ``local`` mode): an explicit opt-in that
   uses object-store credentials from the deploy config. Also the mechanism the
   maintenance paths (``remove`` / ``reconcile`` / ``registry copy``) use for the
   LIST / HEAD / DELETE / cross-bucket-COPY operations that presigned PutObject
   URLs structurally cannot express.

Destinations are explicit: **data products live on OSN** under canonical keys;
map tiles are the sole R2 exception (CDN edge). ``backend`` is therefore a
REQUIRED argument on the upload entry points — encoding an ``'r2'`` default
was the "surely this deploys to R2" trap.

Transport details shared with the download side via ``campfire.storage``:
pooled sessions with retry (PUT is idempotent, so it retries safely),
just-in-time presign batches (URLs are minted per 500-task batch immediately
before that batch transfers, so a multi-hour push never races the 1-hour
presign TTL), and per-item success callbacks that run on the calling thread
(safe for registry/mirror bookkeeping mid-transfer).
"""

import gzip
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, NamedTuple, Optional

import requests as http_requests  # module-global: patchable seam for tests
from urllib.parse import urlparse

from campfire_layout import is_compressed_key
from campfire.storage.session import create_transfer_session
from campfire.storage.transfer import run_transfers

# A returned presigned URL on this host means the web route signed R2, not OSN.
_R2_PRESIGN_HOST_FRAGMENT = 'r2.cloudflarestorage.com'

# gzip level for compressed products (nircam_mosaic FITS). Level 1 matches
# level 6's ratio on float32 FITS (the win is NaN-run collapse, not entropy
# coding) at ~40% less CPU — measured on a 55%-NaN f444w mosaic.
_GZIP_LEVEL = 1


@contextmanager
def _upload_body(local_path: Path, *, compress: bool):
    """Yield the path whose bytes to PUT for ``local_path``.

    For a compressed product (a ``.fits.gz`` key — see
    ``campfire_layout.is_compressed_key``) this gzips ``local_path`` to a sibling
    temp file and yields that, removing it afterwards; otherwise it yields
    ``local_path`` unchanged. Compression is deterministic (``mtime=0``) so a
    re-deploy of an unchanged mosaic produces byte-identical output. A temp
    *file* (not an on-the-fly stream) is deliberate: it preserves a known
    Content-Length for the single-PUT presigned path (a streamed generator would
    force chunked transfer-encoding, which presigned S3/OSN PutObject rejects)
    and lets boto3's managed transfer multipart large mosaics.

    The registry still records the *uncompressed* content hash and size (the
    local plain ``.fits``), so identity/dedup/verify are unaffected by the gzip.
    """
    if not compress:
        yield local_path
        return
    fd, tmp_name = tempfile.mkstemp(
        dir=str(local_path.parent), prefix=local_path.name + '.', suffix='.gz.tmp')
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as raw, open(local_path, 'rb') as src:
            with gzip.GzipFile(fileobj=raw, mode='wb',
                               compresslevel=_GZIP_LEVEL, mtime=0) as gz:
                shutil.copyfileobj(src, gz, length=8 << 20)
        yield tmp
    finally:
        tmp.unlink(missing_ok=True)


class UploadTask(NamedTuple):
    """Represents a file to be uploaded to the object store."""
    local_path: Path
    r2_key: str
    content_type: str


# ---------------------------------------------------------------------------
# Presigned URL mode
# ---------------------------------------------------------------------------

PRESIGN_BATCH_SIZE = 500  # Max URLs per presign request (matches the web route)


def _get_presign_headers(config: dict) -> Optional[dict]:
    """Bearer auth headers for the presign endpoint, straight from the login
    ``TokenManager``; ``None`` when there is no usable login session.

    Authenticates directly against the stored OAuth session rather than gating on
    ``config['supabase']['supabase_token']`` (issue #250): the presign route wants
    the API access token, and coupling it to the Supabase token's presence is what
    let an ambient service-role key silently disable presigned uploads.
    """
    try:
        from campfire.api.session import resolve_base_url
        from campfire.auth.tokens import TokenManager
        tm = TokenManager(base_url=resolve_base_url())
        if not tm.is_oauth():
            return None
        access_token = tm.get_valid_token()
    except Exception:
        return None

    if not access_token:
        return None
    return {'Authorization': f'Bearer {access_token}'}


def _get_presign_base_url() -> str:
    """Get the web API base URL for presign requests."""
    from campfire.api.session import resolve_base_url
    return resolve_base_url()


def request_presigned_urls(
    config: dict,
    tasks: list[UploadTask],
    bucket: str = 'data',
    cache_control: Optional[str] = None,
    *,
    backend: str,
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
    backend : str
        Storage backend to sign against — REQUIRED. 'osn' for data products
        (canonical keys, epic #210 / #216); 'r2' only for map tiles.

    Returns
    -------
    dict or None
        Mapping of r2_key → presigned URL, or None if presigning
        is not available (the caller decides how loudly to fail).
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
            'backend': backend,
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


def _assert_presigned_backend_osn(urls: dict[str, str]) -> None:
    """Fail loudly if OSN was requested but the web route returned R2-host URLs.

    A web deployment that predates OSN upload support ignores the ``backend``
    field and signs the R2 'data' bucket. Uploading there would land bytes in R2
    under canonical keys while the deploy registers ``backend='osn'`` — a silent
    divergence between what the registry points at (OSN) and where bytes live
    (R2). Refuse before any upload so the operator updates/redeploys the web app.
    """
    r2_signed = [
        key for key, url in urls.items()
        if _R2_PRESIGN_HOST_FRAGMENT in urlparse(url).netloc
    ]
    if r2_signed:
        raise RuntimeError(
            "Requested OSN presigned uploads but the deploy API returned R2 URLs "
            f"(e.g. {r2_signed[0]}). The web deployment predates OSN upload support "
            "(the /api/v1/deploy/presign route must accept backend='osn'). Update and "
            "redeploy the web app before deploying NIRSpec products to OSN."
        )


def _upload_to_presigned_url(
    session, url: str, local_path: Path, content_type: str, *, compress: bool = False,
) -> None:
    """Upload a single file to a presigned PutObject URL via a pooled session.

    The shared session gives each worker a persistent connection (one TCP+TLS
    handshake per thread, not per file) and retries transient failures —
    presigned PutObject is idempotent, so a retried PUT can only converge.

    ``compress`` gzips the body first (compressed products only); the presign was
    minted for the ``.fits.gz`` key with a matching ``application/gzip``
    Content-Type, so the header stays consistent with the signed request.
    """
    with _upload_body(local_path, compress=compress) as body:
        with open(body, 'rb') as f:
            resp = session.put(
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
    *,
    session=None,
    on_success: Optional[Callable[[UploadTask], None]] = None,
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
    session : requests.Session, optional
        Pooled transfer session to reuse across batches. Created per-call when
        omitted.
    on_success : callable, optional
        ``on_success(task)`` runs on the calling thread as each upload lands —
        safe for single-threaded bookkeeping (registry batches, the local
        mirror) *during* the transfer, which is what makes an interrupted push
        resumable at file granularity.

    Returns
    -------
    tuple
        (success_count, failure_count, list_of_failure_messages)
    """
    runnable = [t for t in tasks if t.r2_key in urls]
    if not runnable:
        return 0, 0, []

    if session is None:
        session = create_transfer_session(max_workers, methods=("GET", "PUT"))

    def _success(task: UploadTask, _result) -> None:
        if succeeded_out is not None:
            succeeded_out.add(task.r2_key)
        if on_success is not None:
            on_success(task)

    result = run_transfers(
        runnable,
        lambda t: _upload_to_presigned_url(
            session, urls[t.r2_key], t.local_path, t.content_type,
            compress=is_compressed_key(t.r2_key)),
        max_workers=max_workers,
        desc=desc,
        label=lambda t: t.local_path.name,
        on_success=_success,
    )
    return result.succeeded, result.failed, result.failures


# ---------------------------------------------------------------------------
# Direct boto3 mode (explicit service-role / local opt-in)
# ---------------------------------------------------------------------------

def get_r2_client(config: dict):
    """Create a boto3 S3 client for the ``data`` storage backend.

    Endpoint/region/addressing-style come from config via the per-purpose
    backend factory (defaults to Cloudflare R2). Name kept for backward
    compatibility with existing importers.
    """
    from campfire.deploy.backend import make_client_for
    return make_client_for(config, 'data')


def download_to_path(client, bucket: str, key: str, dest_path: Path) -> None:
    """Download a single object to a local path via boto3 (managed, multipart).

    The backend-agnostic GET counterpart to :func:`upload_to_r2` — used by the
    R2->OSN copy (epic #210 / #215) to stream a source object to a temp file
    before hashing and re-uploading. ``client``/``bucket`` are whatever the
    caller resolved (R2 'data' source for the copy).
    """
    client.download_file(bucket, key, str(dest_path))


def upload_to_r2(
    client,
    bucket: str,
    local_path: Path,
    r2_key: str,
    content_type: str | None = None,
    cache_control: str | None = None,
    *,
    compress: bool = False,
) -> None:
    """Upload a single file via boto3 (any S3-compatible backend).

    ``compress`` gzips the body first (compressed products only); the caller's
    ``content_type`` should already be ``application/gzip`` for those.
    """
    extra_args = {}
    if content_type:
        extra_args['ContentType'] = content_type
    if cache_control:
        extra_args['CacheControl'] = cache_control

    with _upload_body(local_path, compress=compress) as body:
        client.upload_file(
            str(body),
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
    *,
    cache_control: Optional[str] = None,
    on_success: Optional[Callable[[UploadTask], None]] = None,
) -> tuple[int, int, list[str]]:
    """
    Upload multiple files via boto3 in parallel with progress bar.

    ``succeeded_out``, if given, collects the ``r2_key`` of each landed object;
    ``on_success(task)`` runs on the calling thread per landed object.

    Returns
    -------
    tuple
        (success_count, failure_count, list_of_failure_messages)
    """
    if not tasks:
        return 0, 0, []

    def _success(task: UploadTask, _result) -> None:
        if succeeded_out is not None:
            succeeded_out.add(task.r2_key)
        if on_success is not None:
            on_success(task)

    result = run_transfers(
        tasks,
        lambda t: upload_to_r2(client, bucket, t.local_path, t.r2_key,
                               t.content_type, cache_control,
                               compress=is_compressed_key(t.r2_key)),
        max_workers=max_workers,
        desc=desc,
        label=lambda t: t.local_path.name,
        on_success=_success,
    )
    return result.succeeded, result.failed, result.failures


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
    *,
    backend: str,
    on_success: Optional[Callable[[UploadTask], None]] = None,
) -> tuple[int, int, list[str]]:
    """
    Upload files to the object store, dispatching on the resolved deploy auth mode.

    This is the main entry point for all uploads. In ``login`` mode (the default)
    it uploads via presigned URLs ONLY — if presigning is unavailable it raises,
    rather than silently falling back to direct boto3 (the footgun #250 removed).
    In the explicit ``service_role`` / ``local`` modes it uploads directly with
    boto3 using local object-store credentials.

    Presigned URLs are minted **just-in-time per batch** (500 tasks), so a push
    that runs longer than the 1-hour presign TTL never fails late files on
    signature expiry.

    Parameters
    ----------
    config : dict
        Deploy config.
    tasks : list[UploadTask]
        Files to upload.
    bucket_id : str
        'data' for data products, 'tiles' for map tiles.
    max_workers : int
        Parallel upload threads.
    desc : str
        Progress bar description.
    cache_control : str, optional
        Cache-Control header for uploaded objects.
    succeeded_out : set[str], optional
        If given, collects the ``r2_key`` of each successfully-uploaded object so
        the caller can register exactly what landed (used for storage_objects).
    backend : str
        Destination backend — REQUIRED, no default. 'osn' for all data products
        (canonical keys; epic #210 / #216). 'r2' is correct ONLY for map tiles.
    on_success : callable, optional
        ``on_success(task)`` runs on the calling thread as each upload lands —
        the hook for durable mid-transfer bookkeeping (per-batch registry
        writes, local-mirror updates).

    Returns
    -------
    tuple
        (success_count, failure_count, list_of_failure_messages)
    """
    if not tasks:
        return 0, 0, []

    # Direct boto3 is an EXPLICIT opt-in (service-role / --local), never a silent
    # fallback. In login mode (default) uploads go only via presigned URLs.
    mode = config.get('supabase', {}).get('_auth_mode')
    if mode not in ('service_role', 'local'):
        session = create_transfer_session(max_workers, methods=("GET", "PUT"))
        total_success, total_failed = 0, 0
        all_failures: list[str] = []
        # Just-in-time presigning: mint each batch's URLs immediately before
        # that batch uploads so none can expire mid-run.
        for i in range(0, len(tasks), PRESIGN_BATCH_SIZE):
            batch = tasks[i:i + PRESIGN_BATCH_SIZE]
            urls = request_presigned_urls(
                config, batch, bucket=bucket_id, cache_control=cache_control,
                backend=backend,
            )
            if not urls:
                raise RuntimeError(
                    "Presigned upload URLs are unavailable, and login-mode deploys "
                    "upload only via presigned URLs (no direct-S3 fallback — issue #250). "
                    "This usually means the login session expired or the deploy API is "
                    "unreachable. Re-run `campfire login`; or, for an unattended run, set "
                    "CAMPFIRE_DEPLOY_MODE=service-role with CAMPFIRE_S3_* credentials to "
                    "upload directly."
                )
            # Guard against a web deployment that predates OSN upload support: an old
            # /deploy/presign ignores `backend` and signs R2, which would land bytes in
            # R2 under canonical keys while the registry records backend='osn' — silent
            # divergence. Refuse if we asked for OSN but got R2-host URLs.
            if backend == 'osn':
                _assert_presigned_backend_osn(urls)
            s, f, msgs = upload_files_presigned(
                urls, batch, max_workers=max_workers, desc=desc,
                succeeded_out=succeeded_out, session=session, on_success=on_success,
            )
            total_success += s
            total_failed += f
            all_failures.extend(msgs)
        return total_success, total_failed, all_failures

    # Explicit direct-boto3 upload via the per-purpose backend factory.
    from campfire.deploy.backend import (
        PURPOSE_SECTIONS, make_s3_client, resolve_backend,
    )

    if backend == 'osn':
        purpose = 'osn'
    else:
        purpose = 'tiles' if bucket_id == 'tiles' else 'data'
    if PURPOSE_SECTIONS[purpose] not in config:
        raise ValueError(
            f"No storage credentials available for the '{purpose}' backend. "
            "Run 'campfire login' to use presigned URLs, or provide storage "
            "credentials in deploy config."
        )

    # A present-but-incomplete section surfaces resolve_backend's specific
    # diagnostic (which key/endpoint is missing), not the generic message above.
    backend_cfg = resolve_backend(config, purpose)
    # Pool sized to the worker count: botocore's default pool (10) is smaller
    # than the thread pools used here, which silently serializes the excess.
    client = make_s3_client(backend_cfg, max_pool_connections=max(max_workers, 10))

    return upload_files_direct(
        client, backend_cfg.bucket, tasks, max_workers=max_workers, desc=desc,
        succeeded_out=succeeded_out, cache_control=cache_control,
        on_success=on_success,
    )
