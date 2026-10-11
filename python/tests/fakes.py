"""Shared in-memory fake of the supabase-py / PostgREST client.

One implementation replaces the per-file ``_FakeQuery`` / ``_FakeClient``
copies the deploy tests used to hand-roll. It imitates the fluent builder the
application code drives::

    client.table(name).select(cols).eq(col, v).in_(col, vs).order(col).range(lo, hi).execute()
    client.table(name).insert(rows).execute()
    client.table(name).upsert(rows, on_conflict='slug').execute()
    client.table(name).update(patch).eq(col, v).execute()
    client.table(name).delete().in_('id', ids).execute()
    client.table(name).select('*').eq(...).maybe_single().execute()
    client.rpc(name, params).execute()

Semantics
---------
* **Tables** are ``{name: [row, ...]}`` dicts of plain-dict rows (``client.tables``).
  Reads filter the rows in memory with the operators the code uses (``eq``,
  ``neq``, ``in_``, ``is_``, ``gt``/``gte``/``lt``/``lte``, ``like``/``ilike``,
  and the ``not_`` prefix), then apply ``order`` / ``range`` / ``limit``.
  A column a row lacks reads as ``None``. Column projection in ``select``
  is *not* applied (rows come back whole) — none of the per-file fakes did it
  and several tests compare whole rows.
* **Writes are applied** (``apply_writes=True``): ``insert`` appends copies
  (assigning an auto-increment ``id`` from ``next_id`` when the row has
  none), ``upsert`` merges on the ``on_conflict`` columns (falling back to
  ``id``, else append), ``update`` patches the matching rows in place, and
  ``delete`` removes them. Each returns the affected rows as PostgREST's
  ``return=representation`` would. Writes are also **recorded** exactly as
  handed in (``upserts``, ``inserts``, ``updates``, ``deletes``, ``rpcs``) —
  these are the assertion surface. ``upserts`` entries unpack as
  ``(table, on_conflict, data)``, the shape most tests already used.
* **Pluggable responses**: ``client.responses[table] = rows`` (or a callable
  taking the ``FakeQuery``) short-circuits every *select* on that table and is
  returned verbatim — filters are recorded but not applied. ``rpc_handlers``
  does the same for RPCs: a static value, or a callable of ``params``.
* **Error injection**: ``client.inject_error(exc, table=..., op=..., rpc=...,
  times=1)`` raises *exc* from the next matching ``execute()``.
* ``calls`` keeps every executed query as a :class:`QueryCall` (table, op,
  filters, order, range, payload...) for shape assertions.

Nothing here touches the network; it is pure data structure.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, NamedTuple, Optional


# --------------------------------------------------------------------------
# Records (what tests assert on)
# --------------------------------------------------------------------------

class Filter(NamedTuple):
    op: str
    column: str
    value: Any
    negated: bool = False


class UpsertRecord(NamedTuple):
    table: str
    on_conflict: Optional[str]
    data: Any            # the payload exactly as handed in (dict or list)


class InsertRecord(NamedTuple):
    table: str
    data: Any


class UpdateRecord(NamedTuple):
    table: str
    filters: tuple
    patch: dict


class DeleteRecord(NamedTuple):
    table: str
    filters: tuple
    rows: list           # the rows that were removed


class RpcRecord(NamedTuple):
    name: str
    params: Any


@dataclass
class QueryCall:
    table: str
    op: str                                  # select | insert | upsert | update | delete
    columns: Optional[str] = None
    filters: list = field(default_factory=list)
    order: list = field(default_factory=list)   # [(column, desc)]
    limit: Optional[int] = None
    range: Optional[tuple] = None
    payload: Any = None
    on_conflict: Optional[str] = None
    single: Optional[str] = None             # 'single' | 'maybe' | None
    count: Optional[str] = None


class FakeResponse:
    """What ``execute()`` returns: ``.data`` (+ ``.count`` when requested)."""

    def __init__(self, data, count=None):
        self.data = data
        self.count = count

    def __repr__(self):
        return f"FakeResponse(data={self.data!r}, count={self.count!r})"


def _api_error(message: str, code: str):
    """Build a ``postgrest.exceptions.APIError`` when the package is present
    (it is in the campfire env), else a local stand-in with the same shape."""
    payload = {"message": message, "code": code, "hint": None, "details": None}
    try:
        from postgrest.exceptions import APIError
        return APIError(payload)
    except Exception:  # pragma: no cover - postgrest missing
        class APIError(Exception):
            def __init__(self, error):
                super().__init__(error.get("message"))
                self.message = error.get("message")
                self.code = error.get("code")
                self.details = error.get("details")
                self.hint = error.get("hint")
        return APIError(payload)


# --------------------------------------------------------------------------
# Query builder
# --------------------------------------------------------------------------

def _like_regex(pattern: str, flags: int = 0):
    out = ""
    for ch in pattern:
        if ch == "%":
            out += ".*"
        elif ch == "_":
            out += "."
        else:
            out += re.escape(ch)
    return re.compile("^" + out + "$", flags | re.DOTALL)


def _matches(row: dict, f: Filter) -> bool:
    val = row.get(f.column)
    op, want = f.op, f.value
    if op == "eq":
        hit = val == want
    elif op == "neq":
        hit = val != want
    elif op == "in":
        hit = val in want
    elif op == "is":
        hit = val is want if want is None else val == want
    elif op == "gt":
        hit = val is not None and val > want
    elif op == "gte":
        hit = val is not None and val >= want
    elif op == "lt":
        hit = val is not None and val < want
    elif op == "lte":
        hit = val is not None and val <= want
    elif op == "like":
        hit = val is not None and bool(_like_regex(want).match(str(val)))
    elif op == "ilike":
        hit = val is not None and bool(_like_regex(want, re.IGNORECASE).match(str(val)))
    else:  # pragma: no cover - guarded at construction
        raise NotImplementedError(op)
    return (not hit) if f.negated else hit


class _NotProxy:
    """``query.not_.<filter>(...)`` negates the next filter."""

    def __init__(self, query):
        self._q = query

    def __getattr__(self, name):
        method = getattr(self._q, name)
        if name not in FakeQuery._FILTER_METHODS:
            raise AttributeError(f"not_ cannot prefix {name!r}")

        def negated(*args, **kwargs):
            self._q._negate_next = True
            return method(*args, **kwargs)
        return negated


class FakeQuery:
    _FILTER_METHODS = ("eq", "neq", "in_", "is_", "gt", "gte", "lt", "lte", "like", "ilike")

    def __init__(self, client: "FakePostgrest", table: str):
        self._client = client
        self._table = table
        self._op = "select"
        self._columns: Optional[str] = None
        self._count: Optional[str] = None
        self._filters: list[Filter] = []
        self._order: list[tuple[str, bool]] = []
        self._limit: Optional[int] = None
        self._range: Optional[tuple[int, int]] = None
        self._payload: Any = None
        self._on_conflict: Optional[str] = None
        self._single: Optional[str] = None
        self._negate_next = False

    # -- verbs ------------------------------------------------------------
    def select(self, columns: str = "*", *, count: Optional[str] = None, **_kw):
        if self._op == "select":
            self._columns = columns
        self._count = count
        return self

    def insert(self, data, **_kw):
        self._op, self._payload = "insert", data
        return self

    def upsert(self, data, on_conflict: Optional[str] = None, **_kw):
        self._op, self._payload, self._on_conflict = "upsert", data, on_conflict
        return self

    def update(self, patch: dict, **_kw):
        self._op, self._payload = "update", patch
        return self

    def delete(self, **_kw):
        self._op = "delete"
        return self

    # -- filters ----------------------------------------------------------
    def _add(self, op, column, value):
        self._filters.append(Filter(op, column, value, self._negate_next))
        self._negate_next = False
        return self

    def eq(self, column, value):
        return self._add("eq", column, value)

    def neq(self, column, value):
        return self._add("neq", column, value)

    def in_(self, column, values: Iterable):
        return self._add("in", column, list(values))

    def is_(self, column, value):
        # postgrest: is_('col', 'null') / 'not.null' / True / False / None
        if isinstance(value, str):
            low = value.lower()
            if low == "null":
                value = None
            elif low == "not.null":
                self._negate_next = not self._negate_next
                value = None
            elif low in ("true", "false"):
                value = low == "true"
        return self._add("is", column, value)

    def gt(self, column, value):
        return self._add("gt", column, value)

    def gte(self, column, value):
        return self._add("gte", column, value)

    def lt(self, column, value):
        return self._add("lt", column, value)

    def lte(self, column, value):
        return self._add("lte", column, value)

    def like(self, column, pattern):
        return self._add("like", column, pattern)

    def ilike(self, column, pattern):
        return self._add("ilike", column, pattern)

    @property
    def not_(self):
        return _NotProxy(self)

    # -- modifiers --------------------------------------------------------
    def order(self, column, *, desc: bool = False, **_kw):
        self._order.append((column, bool(desc)))
        return self

    def limit(self, n: int, **_kw):
        self._limit = int(n)
        return self

    def range(self, lo: int, hi: int, **_kw):
        self._range = (int(lo), int(hi))
        return self

    def single(self):
        self._single = "single"
        return self

    def maybe_single(self):
        self._single = "maybe"
        return self

    # -- introspection (for response callables / assertions) --------------
    @property
    def table(self):
        return self._table

    @property
    def op(self):
        return self._op

    @property
    def filters(self):
        return tuple(self._filters)

    @property
    def payload(self):
        return self._payload

    # -- execution --------------------------------------------------------
    def _select_rows(self, rows: list[dict]) -> list[dict]:
        rows = [r for r in rows if all(_matches(r, f) for f in self._filters)]
        # Successive stable sorts, last-added first, give primary-key priority
        # to the first order() call. Postgres: NULLS LAST asc, NULLS FIRST desc.
        for column, desc in reversed(self._order):
            # (is_null, value): ascending puts NULLs last; reversing the same
            # key for descending puts them first — Postgres's defaults.
            rows.sort(key=lambda r, c=column: (r.get(c) is None, r.get(c)),
                      reverse=desc)
        if self._range is not None:
            lo, hi = self._range
            rows = rows[lo:hi + 1]
        if self._limit is not None:
            rows = rows[:self._limit]
        return rows

    def execute(self) -> FakeResponse:
        c = self._client
        call = QueryCall(
            table=self._table, op=self._op, columns=self._columns,
            filters=list(self._filters), order=list(self._order),
            limit=self._limit, range=self._range, payload=self._payload,
            on_conflict=self._on_conflict, single=self._single, count=self._count,
        )
        c.calls.append(call)
        c._maybe_raise(table=self._table, op=self._op)

        table = c.tables.setdefault(self._table, [])
        if self._op == "select":
            override = c.responses.get(self._table)
            if override is not None:
                rows = override(self) if callable(override) else override
                rows = list(rows)
            else:
                rows = self._select_rows(table)
            data: Any = rows
            if self._single == "single":
                if len(rows) != 1:
                    raise _api_error(
                        "JSON object requested, multiple (or no) rows returned",
                        "PGRST116")
                data = rows[0]
            elif self._single == "maybe":
                if len(rows) > 1:
                    raise _api_error(
                        "JSON object requested, multiple (or no) rows returned",
                        "PGRST116")
                data = rows[0] if rows else None
            count = len(rows) if self._count else None
            return FakeResponse(data, count)

        if self._op == "insert":
            c.inserts.append(InsertRecord(self._table, self._payload))
            rows = c._apply_insert(table, self._payload)
            return FakeResponse(rows)

        if self._op == "upsert":
            c.upserts.append(UpsertRecord(self._table, self._on_conflict, self._payload))
            rows = c._apply_upsert(table, self._payload, self._on_conflict)
            return FakeResponse(rows)

        if self._op == "update":
            c.updates.append(UpdateRecord(self._table, tuple(self._filters), self._payload))
            rows = [r for r in table if all(_matches(r, f) for f in self._filters)]
            if c.apply_writes:
                for r in rows:
                    r.update(self._payload)
            return FakeResponse(rows)

        if self._op == "delete":
            rows = [r for r in table if all(_matches(r, f) for f in self._filters)]
            c.deletes.append(DeleteRecord(self._table, tuple(self._filters), rows))
            if c.apply_writes:
                keep = [r for r in table if not any(r is d for d in rows)]
                table[:] = keep
            return FakeResponse(rows)

        raise NotImplementedError(self._op)  # pragma: no cover


class _FakeRpcCall:
    def __init__(self, client: "FakePostgrest", name: str, params):
        self._client, self._name, self._params = client, name, params

    def execute(self) -> FakeResponse:
        c = self._client
        c._maybe_raise(rpc=self._name)
        handler = c.rpc_handlers.get(self._name, c.rpc_default)
        data = handler(self._params) if callable(handler) else handler
        return FakeResponse(data)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

@dataclass
class _FailRule:
    exc: BaseException
    table: Optional[str]
    op: Optional[str]
    rpc: Optional[str]
    remaining: Optional[int]      # None = forever

    def matches(self, *, table=None, op=None, rpc=None) -> bool:
        if self.rpc is not None:
            return rpc == self.rpc
        if rpc is not None and (self.table is not None or self.op is not None):
            return False
        if self.table is not None and table != self.table:
            return False
        if self.op is not None and op != self.op:
            return False
        return True


class FakePostgrest:
    """In-memory stand-in for the supabase-py client (see module docstring).

    Parameters
    ----------
    tables : dict[str, list[dict]], optional
        Seed rows per table. Rows are used as given (not copied), so a test
        may keep a reference and watch ``update`` patch it in place.
    rpc_handlers : dict[str, Any], optional
        ``{name: value | callable(params) -> value}`` served as ``.data``.
    rpc_default : Any
        ``.data`` for an RPC with no handler (``None``).
    responses : dict[str, list | callable], optional
        Per-table select overrides, returned verbatim.
    apply_writes : bool
        Mutate ``tables`` on insert/upsert/update/delete (default True).
    next_id : int, optional
        First auto-assigned ``id`` for inserted rows that carry none. The
        default (``None``) continues from the table's largest integer ``id``
        (1 for an empty table), like a sequence seeded with the data.
    """

    def __init__(
        self,
        tables: Optional[dict[str, list[dict]]] = None,
        *,
        rpc_handlers: Optional[dict[str, Any]] = None,
        rpc_default: Any = None,
        responses: Optional[dict[str, Any]] = None,
        apply_writes: bool = True,
        next_id: Optional[int] = None,
    ):
        self.tables: dict[str, list[dict]] = dict(tables or {})
        self.rpc_handlers: dict[str, Any] = dict(rpc_handlers or {})
        self.rpc_default = rpc_default
        self.responses: dict[str, Any] = dict(responses or {})
        self.apply_writes = apply_writes
        self.next_id = next_id

        self.calls: list[QueryCall] = []
        self.upserts: list[UpsertRecord] = []
        self.inserts: list[InsertRecord] = []
        self.updates: list[UpdateRecord] = []
        self.deletes: list[DeleteRecord] = []
        self.rpcs: list[RpcRecord] = []
        self._fail_rules: list[_FailRule] = []

    # -- supabase-py surface ----------------------------------------------
    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    # supabase-py also exposes .from_() as an alias
    from_ = table

    def rpc(self, name: str, params=None) -> _FakeRpcCall:
        self.rpcs.append(RpcRecord(name, params))
        return _FakeRpcCall(self, name, params)

    # -- test configuration -----------------------------------------------
    def inject_error(self, exc: BaseException, *, table: Optional[str] = None,
                     op: Optional[str] = None, rpc: Optional[str] = None,
                     times: Optional[int] = 1) -> None:
        """Raise *exc* from the next *times* matching ``execute()`` calls
        (``times=None`` for every call). ``table`` / ``op`` match table
        queries, ``rpc`` matches an RPC by name; no selector matches all."""
        self._fail_rules.append(_FailRule(exc, table, op, rpc, times))

    def _maybe_raise(self, *, table=None, op=None, rpc=None) -> None:
        for rule in list(self._fail_rules):
            if rule.matches(table=table, op=op, rpc=rpc):
                if rule.remaining is not None:
                    rule.remaining -= 1
                    if rule.remaining <= 0:
                        self._fail_rules.remove(rule)
                raise rule.exc

    # -- assertion helpers ------------------------------------------------
    def rows(self, table: str) -> list[dict]:
        return self.tables.setdefault(table, [])

    def calls_for(self, table: str, op: Optional[str] = None) -> list[QueryCall]:
        return [c for c in self.calls
                if c.table == table and (op is None or c.op == op)]

    def rpc_names(self) -> list[str]:
        return [r.name for r in self.rpcs]

    def upserted_rows(self, table: Optional[str] = None) -> list[dict]:
        """Every upserted row, flattened, in order."""
        out: list[dict] = []
        for rec in self.upserts:
            if table is None or rec.table == table:
                out.extend(_as_rows(rec.data))
        return out

    def inserted_rows(self, table: Optional[str] = None) -> list[dict]:
        out: list[dict] = []
        for rec in self.inserts:
            if table is None or rec.table == table:
                out.extend(_as_rows(rec.data))
        return out

    def deleted_rows(self, table: Optional[str] = None) -> list[dict]:
        out: list[dict] = []
        for rec in self.deletes:
            if table is None or rec.table == table:
                out.extend(rec.rows)
        return out

    # -- write application -------------------------------------------------
    def _apply_insert(self, table: list[dict], payload) -> list[dict]:
        out = []
        for row in _as_rows(payload):
            stored = dict(row)
            if stored.get("id") is None:
                stored["id"] = self._allocate_id(table)
            if self.apply_writes:
                table.append(stored)
            out.append(stored)
        return out

    def _allocate_id(self, table: list[dict]) -> int:
        if self.next_id is None:
            ids = [r["id"] for r in table if isinstance(r.get("id"), int)]
            self.next_id = (max(ids) + 1) if ids else 1
        value, self.next_id = self.next_id, self.next_id + 1
        return value

    def _apply_upsert(self, table: list[dict], payload, on_conflict) -> list[dict]:
        keys = [k.strip() for k in on_conflict.split(",")] if on_conflict else None
        out = []
        for row in _as_rows(payload):
            if not self.apply_writes:
                out.append(dict(row))
                continue
            match = None
            if keys and all(k in row for k in keys):
                for r in table:
                    if all(r.get(k) == row[k] for k in keys):
                        match = r
                        break
            elif "id" in row:
                for r in table:
                    if r.get("id") == row["id"]:
                        match = r
                        break
            if match is not None:
                match.update(copy.copy(row))
                out.append(match)
            else:
                stored = dict(row)
                table.append(stored)
                out.append(stored)
        return out


def _as_rows(payload) -> list[dict]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        return [payload]
    return list(payload)


__all__ = [
    "FakePostgrest", "FakeQuery", "FakeResponse", "QueryCall", "Filter",
    "UpsertRecord", "InsertRecord", "UpdateRecord", "DeleteRecord", "RpcRecord",
]
