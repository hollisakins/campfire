"""The parallel transfer harness — one loop for both directions.

``run_transfers`` is the ThreadPoolExecutor + ``as_completed`` + tqdm +
failure-collection pattern that ``sync.download_objects`` and the two upload
loops in ``deploy/r2.py`` each reimplemented. The one behavioral guarantee
that matters architecturally: **callbacks run on the calling thread**, inside
the ``as_completed`` drain loop. That makes per-item bookkeeping safe against
single-threaded stores (the SQLite mirror, the Supabase client's HTTP/2
connection) without any locking — workers only touch the network/disk.

``BatchFlusher`` builds on that guarantee to turn per-item success callbacks
into chunked writes (e.g. registry upserts every N objects), so progress is
durable *during* a long transfer instead of only at the end — the difference
between resuming at file 301/500 and restarting at file 1 after an
interruption.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Generic, Iterable, List, NamedTuple, Optional, TypeVar

from tqdm import tqdm

T = TypeVar("T")
R = TypeVar("R")


class TransferResult(NamedTuple):
    """Outcome of a :func:`run_transfers` pass."""

    succeeded: int
    failed: int
    failures: List[str]  # human-readable "item: error" lines


def run_transfers(
    items: Iterable[T],
    worker: Callable[[T], R],
    *,
    max_workers: int,
    desc: str = "Transferring",
    unit: str = "file",
    label: Callable[[T], str] = str,
    on_success: Optional[Callable[[T, R], None]] = None,
    on_failure: Optional[Callable[[T, Exception], None]] = None,
    show_progress: bool = True,
) -> TransferResult:
    """Run ``worker(item)`` for every item in a thread pool, with progress.

    ``worker`` does the I/O (upload one file, download one object) and runs on
    pool threads. ``on_success(item, result)`` / ``on_failure(item, exc)`` run
    on the **calling thread** as completions drain, so they may safely touch
    single-threaded state (SQLite, the Supabase client). A callback exception
    is not swallowed — it aborts the drain loop, because losing bookkeeping
    silently is worse than stopping.

    Returns a :class:`TransferResult`; failures never raise out of the pool.
    """
    items = list(items)
    if not items:
        return TransferResult(0, 0, [])

    succeeded, failed = 0, 0
    failures: List[str] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {executor.submit(worker, item): item for item in items}
        pbar = tqdm(total=len(items), desc=desc, unit=unit, disable=not show_progress)
        try:
            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    result = future.result()
                except Exception as e:
                    failed += 1
                    failures.append(f"{label(item)}: {e}")
                    if on_failure is not None:
                        on_failure(item, e)
                else:
                    succeeded += 1
                    if on_success is not None:
                        on_success(item, result)
                pbar.update(1)
        finally:
            pbar.close()

    return TransferResult(succeeded, failed, failures)


class BatchFlusher(Generic[T]):
    """Collect items and flush them through a callback every ``batch_size``.

    Single-threaded by design — feed it only from :func:`run_transfers`
    callbacks (which run on the calling thread). Typical use is durable
    mid-transfer bookkeeping::

        flusher = BatchFlusher(lambda rows: upsert_storage_objects(sb, rows),
                               batch_size=200)
        run_transfers(tasks, upload_one,
                      on_success=lambda t, _: flusher.add(registry_row(t)), ...)
        flusher.flush()   # the remainder

    A flush failure propagates (and aborts the transfer drain) — registry
    durability is the point, so it must not fail silently. Items from a failed
    flush are retained and retried on the next flush.
    """

    def __init__(self, flush_fn: Callable[[List[T]], None], *, batch_size: int = 200):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self._flush_fn = flush_fn
        self._batch_size = batch_size
        self._pending: List[T] = []
        self.flushed = 0

    def add(self, item: T) -> None:
        self._pending.append(item)
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        try:
            self._flush_fn(batch)
        except Exception:
            # Retain for a later retry (e.g. the caller's final flush()).
            self._pending = batch + self._pending
            raise
        self.flushed += len(batch)
