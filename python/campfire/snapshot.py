"""Nightly sync catalog snapshots: download, verify and read.

A first-time (or ``--full``) ``campfire sync`` bootstraps from the server's
nightly public-scope catalog snapshot instead of paging every row of the five
``/sync/*`` streams out of the database (see ``sync_metadata``). The server
builds one gzip JSONL file per stream -- each line one row exactly as the live
route returns it -- and ``/sync/snapshot`` hands out presigned urls with each
file's sha256.

Anything unexpected here raises :class:`SnapshotError`; the caller then falls
back to the live walk, so a snapshot can only ever make a sync faster.
"""

import gzip
import hashlib
import json
from pathlib import Path
from typing import Dict, Iterator, List

import requests

#: The file format this client reads; the server's ``format_version`` must match.
SNAPSHOT_FORMAT_VERSION = 1

#: Server stream name -> the result key ``sync_metadata`` uses for it.
SNAPSHOT_STREAMS = {
    "objects": "objects",
    "spectra": "spectra",
    "storage": "storage",
    "photometry": "photometry",
    "lines": "line_fits",
}


class SnapshotError(Exception):
    """The snapshot cannot be used; walk the streams live instead."""


def validate_snapshot(info: dict) -> Dict[str, dict]:
    """Check a /sync/snapshot answer; return its files keyed by sync result key."""
    if info.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotError(
            f"snapshot format {info.get('format_version')} is not supported "
            f"(this client reads {SNAPSHOT_FORMAT_VERSION})"
        )
    if not info.get("snapshot_id") or not info.get("started_at"):
        raise SnapshotError("snapshot answer lacks an id or start time")
    files = {f.get("stream"): f for f in info.get("files") or []}
    missing = sorted(set(SNAPSHOT_STREAMS) - set(files))
    if missing:
        raise SnapshotError(f"snapshot lacks stream(s): {', '.join(missing)}")
    return {SNAPSHOT_STREAMS[s]: files[s] for s in SNAPSHOT_STREAMS}


def download_snapshot_file(
    file: dict, dest_dir: Path, session: requests.Session,
) -> Path:
    """Stream one snapshot file to ``dest_dir``, verifying its sha256.

    The file is stored with content-type application/gzip (not
    Content-Encoding), so the bytes received are the stored gzip bytes the
    server hashed.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{file['stream']}.jsonl.gz"
    tmp = path.with_name(path.name + ".tmp")
    hasher = hashlib.sha256()
    try:
        response = session.get(file["url"], stream=True, timeout=300)
        response.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in response.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                hasher.update(chunk)
    except (requests.RequestException, OSError) as e:
        # OSError: the local write (disk full, permissions) -- still a reason
        # to walk live rather than to fail the sync.
        tmp.unlink(missing_ok=True)
        raise SnapshotError(f"downloading the {file['stream']} snapshot failed: {e}") from e

    got = f"sha256:{hasher.hexdigest()}"
    if got != file.get("sha256"):
        tmp.unlink(missing_ok=True)
        raise SnapshotError(
            f"{file['stream']} snapshot hash mismatch: expected {file.get('sha256')}, got {got}"
        )
    tmp.replace(path)
    return path


def iter_snapshot_rows(path: Path, batch_size: int = 5000) -> Iterator[List[dict]]:
    """Yield a snapshot file's rows in batches (one JSON object per line)."""
    batch: List[dict] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                batch.append(json.loads(line))
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
    except (OSError, EOFError, json.JSONDecodeError) as e:
        raise SnapshotError(f"reading {path.name} failed: {e}") from e
    if batch:
        yield batch
