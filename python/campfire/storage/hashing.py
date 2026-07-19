"""Content hashing — the single implementation for both transfer directions.

Two kinds of content identity live here:

* **Whole-file** (``hash_file`` / ``compute_file_hash``): the authoritative
  ``content_hash`` stored in the ``storage_objects`` registry and verified on
  download. Byte-exact, so it churns whenever a FITS is re-saved with fresh
  header timestamps.
* **Science-only** (``sci_dq_hash``): SHA-256 over the SCI+DQ+CFMASK arrays of
  a FITS file — the *change-detection* identity for push dedup (epic #261, D1).
  Stable across a science-identical re-save, which is exactly why whole-file
  hashes can't drive "should I re-upload this exposure".

Historically ``campfire/sync.py`` and ``campfire/deploy/registry.py`` each had
their own streamer (64 KB vs 1 MB chunks, hash-only vs hash+size); both now
delegate here.
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Optional

_HASH_CHUNK = 1 << 20  # 1 MB: one sequential read beats many small reads on NFS


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


def compute_file_hash(path: Path) -> str:
    """SHA-256 of a file as ``'sha256:<hex>'`` (hash only, no size)."""
    return hash_file(Path(path))[0]


def default_hash_workers() -> int:
    """Thread count for parallel local-file hashing.

    Whole-file (and NIRCam SCI+DQ) hashing of a field's worth of exposures is
    I/O-bound — the reads overlap well across threads, so a small pool cuts the
    serial hash time to a fraction. Sized off the CPU count and capped so we
    don't thrash a spinning disk with too many concurrent large reads.
    """
    return min(16, (os.cpu_count() or 4) * 2)


def hash_files_parallel(
    paths: Iterable[Path], *, max_workers: Optional[int] = None,
    progress_desc: Optional[str] = None,
) -> dict[Path, tuple[str, int]]:
    """Hash many local files concurrently → ``{path: ('sha256:<hex>', size)}``.

    Order-independent (callers key by path). De-duplicates repeated paths so a
    file is hashed once. Falls back to a serial loop for a single file.
    ``progress_desc`` shows a tqdm bar — hashing hundreds of GB off NFS takes
    minutes, and silence there is indistinguishable from a hang.
    """
    unique = list({Path(p) for p in paths})
    if not unique:
        return {}
    workers = max(1, min(max_workers or default_hash_workers(), len(unique)))
    if workers == 1 and not progress_desc:
        return {p: hash_file(p) for p in unique}
    pbar = None
    if progress_desc:
        from tqdm import tqdm
        pbar = tqdm(total=len(unique), desc=progress_desc, unit='file')
    out: dict[Path, tuple[str, int]] = {}
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(hash_file, p): p for p in unique}
            for fut in as_completed(futures):
                out[futures[fut]] = fut.result()
                if pbar:
                    pbar.update(1)
    finally:
        if pbar:
            pbar.close()
    return out


def sci_dq_hash(path) -> Optional[str]:
    """Return ``'sha256:<hex>'`` over the SCI+DQ+CFMASK arrays, or None.

    The science-only change-detection digest for the push dedup (epic #261,
    D1). Reproduces ``campfire_pipeline.nircam.manifest.compute_file_hash``
    client-side (the client must not import the pipeline):
    ``do_not_scale_image_data=True`` hashes the raw stored bytes regardless of
    BZERO/BSCALE, ``memmap=False`` forces a real sequential read.

    CFMASK (the user manual-mask extension) is included so a mask edit — which
    the N7 freeze records as CFMASK on the canonical without touching SCI/DQ —
    still re-uploads the exposure. It is hashed *last*, and the ``not in hdul``
    guard skips it when absent, so an un-masked exposure hashes byte-identically
    to the old SCI+DQ-only digest (no spurious re-upload on the first
    post-N7 deploy).

    Returns None if none of the arrays are present or the file is unreadable
    (never dedup on an empty hash).
    """
    from astropy.io import fits

    h = hashlib.sha256()
    hashed_any = False
    try:
        with fits.open(path, memmap=False, do_not_scale_image_data=True) as hdul:
            for extname in ('SCI', 'DQ', 'CFMASK'):
                if extname not in hdul:
                    continue
                data = hdul[extname].data
                if data is not None:
                    h.update(data.tobytes())
                    hashed_any = True
    except Exception:
        return None
    return f'sha256:{h.hexdigest()}' if hashed_any else None


def sci_dq_hashes_parallel(
    paths: Iterable[Path], *, max_workers: Optional[int] = None,
    progress_desc: Optional[str] = None,
) -> dict[Path, Optional[str]]:
    """Compute :func:`sci_dq_hash` for many files concurrently → ``{path: hash}``.

    The reads are I/O-bound and overlap across threads. A per-file failure
    records ``None`` (never dedup on it). When ``progress_desc`` is given, a
    tqdm bar tracks the pass — computing these digests is the dominant local
    cost before a NIRCam upload starts.
    """
    unique = list({Path(p) for p in paths})
    if not unique:
        return {}
    workers = max(1, min(max_workers or default_hash_workers(), len(unique)))
    out: dict[Path, Optional[str]] = {}
    pbar = None
    if progress_desc:
        from tqdm import tqdm
        pbar = tqdm(total=len(unique), desc=progress_desc, unit='file')
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(sci_dq_hash, p): p for p in unique}
            for fut in as_completed(futures):
                path = futures[fut]
                try:
                    out[path] = fut.result()
                except Exception:
                    out[path] = None
                if pbar:
                    pbar.update(1)
    finally:
        if pbar:
            pbar.close()
    return out
