"""Pooled, retrying HTTP sessions for presigned-URL transfers.

Presigned URLs are self-authenticating, so these sessions carry no auth
headers — what they carry is the transport tuning both directions need on a
high-latency link:

* **Connection reuse.** One persistent connection per worker thread. The old
  upload path called module-level ``requests.put`` per file, paying a full
  TCP+TLS handshake for every object — a large hidden tax at thousands of
  files over a ~150 ms RTT path.
* **Retry with backoff.** Transient 5xx/429 and connection errors retry
  automatically. PUT is safe to include: presigned PutObject is idempotent
  (same bytes, same key), so a retried upload can only converge.
"""

from __future__ import annotations

from typing import Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def create_transfer_session(
    max_workers: int = 4,
    methods: Iterable[str] = ("GET",),
) -> requests.Session:
    """Create a ``requests.Session`` pooled and retrying for parallel transfers.

    Parameters
    ----------
    max_workers : int
        Size of the connection pool — match the transfer thread count so each
        worker gets a persistent connection without contention.
    methods : iterable of str
        HTTP methods eligible for automatic retry. ``("GET",)`` for downloads,
        ``("GET", "PUT")`` for uploads (presigned PutObject is idempotent).
    """
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=list(methods),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=max_workers,
        pool_maxsize=max_workers,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
