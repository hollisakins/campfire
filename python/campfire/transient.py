"""Retrying a call on a transient transport error.

A data-release run (``dr export``, ``dr archive``, ``dr purge`` / ``archive
gc``) makes thousands of requests over one session, and a single dropped
connection used to cost a manual rerun: the loops marked the file or member
failed and moved on (issue #666). The calls those loops make are idempotent
(a page fetch, a conditional PUT, a claim refresh, a DeleteObjects, a ledger
mark bound to a claim), so a transport error is retried a few times with a
short backoff before it counts as a failure. Nothing else is retried: a
PostgREST refusal, a precondition failure or a hash mismatch is an answer,
not a dropped line.
"""
from __future__ import annotations

import time
from typing import Callable, Optional, TypeVar

T = TypeVar('T')

#: Exception classes (by name, anywhere in the MRO) that mean the request
#: never reached the server or its answer never arrived: httpx
#: (``TransportError`` covers ConnectError, ReadTimeout, RemoteProtocolError
#: "Server disconnected", ...), requests, botocore / urllib3 and the stdlib.
_TRANSIENT_NAMES = frozenset({
    'TransportError',                 # httpx
    'ConnectionError', 'Timeout',     # requests (and the stdlib ConnectionError)
    'HTTPClientError', 'EndpointConnectionError', 'ConnectionClosedError',
    'ReadTimeoutError', 'ConnectTimeoutError', 'ResponseStreamingError',   # botocore
    'ProtocolError', 'IncompleteRead', 'RemoteDisconnected',               # urllib3 / http.client
    'TimeoutError', 'ConnectionResetError', 'BrokenPipeError', 'ConnectionAbortedError',
})

#: S3 error codes the service itself asks the client to retry.
_TRANSIENT_S3_CODES = frozenset({
    'RequestTimeout', 'SlowDown', 'ServiceUnavailable', 'InternalError', 'Throttling',
    'ThrottlingException', '503', '500', '502', '504',
})


def is_transient(exc: BaseException) -> bool:
    """Whether *exc* is a dropped connection, a timeout or a server-side
    "try again", as opposed to an answer."""
    for cls in type(exc).__mro__:
        if cls.__name__ in _TRANSIENT_NAMES:
            return True
    resp = getattr(exc, 'response', None)
    if isinstance(resp, dict):
        code = str((resp.get('Error') or {}).get('Code') or '')
        status = (resp.get('ResponseMetadata') or {}).get('HTTPStatusCode')
        if code in _TRANSIENT_S3_CODES or status in (500, 502, 503, 504):
            return True
    return False


#: The pause between attempts (tests replace it).
_sleep = time.sleep


def retry_transient(fn: Callable[[], T], *, attempts: int = 3, backoff: float = 1.0,
                    what: str = '', log: Optional[Callable[[str], None]] = None,
                    sleep: Optional[Callable[[float], None]] = None) -> T:
    """Call *fn* and return its result; on a transient error retry it up to
    *attempts* times in all, sleeping ``backoff * 2**n`` seconds between
    attempts. Any other exception, and the last transient one, propagate."""
    n = 0
    while True:
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - classified below
            n += 1
            if n >= attempts or not is_transient(e):
                raise
            if log:
                log(f"  retrying {what or 'the request'} after a transient error "
                    f"({type(e).__name__}: {str(e)[:120]}; attempt {n} of {attempts})")
            (sleep or _sleep)(backoff * (2 ** (n - 1)))
