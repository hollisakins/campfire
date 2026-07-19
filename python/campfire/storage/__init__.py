"""Shared local↔cloud storage engine.

One transfer core for both directions of the storage plane:

* ``campfire pull`` / ``campfire download`` — cloud→local (presigned GET)
* ``campfire push`` / ``campfire deploy``   — local→cloud (presigned PUT)

The pieces here were previously duplicated between the consumer sync engine
(``campfire/sync.py``) and the deploy transport (``campfire/deploy/r2.py``):
two SHA-256 streamers with different chunk sizes, two thread-pool transfer
loops, two presign clients, and sessions with retry on one side only. This
package is the single implementation; both sides delegate to it.

Backend vocabulary is purpose-based, never provider-based: ``data`` products
live on **OSN** under canonical keys; map ``tiles`` are the sole R2 exception
(CDN edge). Nothing in this package defaults to a backend — call sites say it
out loud (see ``presign.request_put_urls``).
"""

from .hashing import (
    compute_file_hash,
    default_hash_workers,
    hash_file,
    hash_files_parallel,
    sci_dq_hash,
)
from .session import create_transfer_session
from .transfer import BatchFlusher, TransferResult, run_transfers

__all__ = [
    "BatchFlusher",
    "TransferResult",
    "compute_file_hash",
    "create_transfer_session",
    "default_hash_workers",
    "hash_file",
    "hash_files_parallel",
    "run_transfers",
    "sci_dq_hash",
]
