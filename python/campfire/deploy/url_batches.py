"""Batch values for PostgREST ``in.(...)`` filters by encoded URL length.

A ``.in_('storage_key', keys)`` filter travels in the request line, and the
gateway in front of PostgREST caps that line: the self-hosted Kong (nginx)
answers ``414 URI too long`` above its 8 KB default (#657). Storage keys run
80 to 110 characters and URL-encoding turns every slash into three, so a
fixed count of keys per request (200 used to be the norm) was never a safe
unit. These helpers size each batch by what it will cost on the wire instead,
so a request stays well inside any gateway's limit whatever the keys look
like; the gateway's own buffer is raised too (ops/docker-compose.yml), belt
and braces.
"""
from __future__ import annotations

from typing import Iterable, Iterator
from urllib.parse import quote

#: Encoded bytes of filter values allowed per request. The rest of the request
#: line (path, select list, other filters) is a few hundred bytes, so this
#: keeps the whole line under 8 KB with room to spare.
MAX_FILTER_BYTES = 6000

#: Per-value overhead once encoded: the quotes supabase-py adds around a value
#: that needs them (``%22`` twice) and the separating comma (``%2C``).
_PER_VALUE_OVERHEAD = 9


def encoded_len(value: str) -> int:
    """Bytes *value* occupies inside an encoded ``in.(...)`` list."""
    return len(quote(str(value), safe='')) + _PER_VALUE_OVERHEAD


def url_batches(values: Iterable[str], max_bytes: int = MAX_FILTER_BYTES) -> Iterator[list[str]]:
    """Yield consecutive slices of *values* whose encoded length stays under
    *max_bytes*. Order is preserved, every value appears exactly once, and a
    single value longer than the budget still goes out alone (the gateway
    decides; nothing here can shorten it)."""
    batch: list[str] = []
    used = 0
    for v in values:
        n = encoded_len(v)
        if batch and used + n > max_bytes:
            yield batch
            batch, used = [], 0
        batch.append(v)
        used += n
    if batch:
        yield batch
