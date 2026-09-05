"""Cursor pagination for the /api/v1 list endpoints and the registry walker
(perf T2-F, #511).

The list iterators follow the server's ``next_cursor`` instead of walking
OFFSET pages; ``_iter_rows`` keysets on the PK and pushes equality filters to
the server instead of fetching the whole table.
"""

from campfire.api.client import APIClient, _build_query_params
from campfire.deploy import registry


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self):
        return self._payload


class _Session:
    """Scripted APISession stand-in: hands out canned pages, records params."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = []

    def get(self, path, params=None, timeout=None):
        self.calls.append((path, dict(params or {})))
        return _Resp(self._pages.pop(0))

    def _ensure_valid_token(self):
        pass


# ---------------------------------------------------------------------------
# _build_query_params
# ---------------------------------------------------------------------------

def test_query_params_send_cursor_not_offset():
    p = _build_query_params(cursor="abc", offset=500, limit=10)
    assert p["cursor"] == "abc"
    assert "offset" not in p           # never both
    assert p["limit"] == 10


def test_query_params_offset_only_when_nonzero_and_count_flag():
    assert "offset" not in _build_query_params()
    assert _build_query_params(offset=200)["offset"] == 200
    assert "count" not in _build_query_params()
    assert _build_query_params(count=False)["count"] == "false"
    assert _build_query_params(count=True)["count"] == "true"


# ---------------------------------------------------------------------------
# iter_objects / iter_spectra follow next_cursor
# ---------------------------------------------------------------------------

def test_iter_objects_follows_next_cursor():
    session = _Session([
        {"data": [{"object_id": "A"}, {"object_id": "B"}],
         "pagination": {"total": 5, "limit": 2, "has_more": True, "next_cursor": "c1"}},
        {"data": [{"object_id": "C"}, {"object_id": "D"}],
         "pagination": {"total": -1, "limit": 2, "has_more": True, "next_cursor": "c2"}},
        {"data": [{"object_id": "E"}],
         "pagination": {"total": -1, "limit": 2, "has_more": False, "next_cursor": None}},
    ])
    api = APIClient(session)
    rows = list(api.iter_objects(limit=2, fields=["cosmos"]))
    assert [r["object_id"] for r in rows] == ["A", "B", "C", "D", "E"]
    paths = [c[0] for c in session.calls]
    params = [c[1] for c in session.calls]
    assert paths == ["/objects"] * 3
    # First page: no cursor, no offset (server counts by default). Later
    # pages: the previous page's cursor, still no offset, no explicit count.
    assert "cursor" not in params[0] and "offset" not in params[0]
    assert params[1]["cursor"] == "c1" and "offset" not in params[1]
    assert params[2]["cursor"] == "c2"
    assert all("count" not in p for p in params)
    assert all(p["fields"] == "cosmos" and p["limit"] == 2 for p in params)


def test_iter_spectra_stops_when_server_returns_no_cursor():
    # A stale server (or the last page) answers without next_cursor: the walk
    # ends after that page instead of looping.
    session = _Session([
        {"data": [{"spectrum_id": "s1"}], "pagination": {"total": 3, "limit": 1, "offset": 0}},
    ])
    api = APIClient(session)
    assert [r["spectrum_id"] for r in api.iter_spectra(limit=1)] == ["s1"]
    assert len(session.calls) == 1
    assert session.calls[0][0] == "/spectra/list"


def test_iter_objects_strips_caller_pagination_kwargs():
    session = _Session([
        {"data": [{"object_id": "A"}], "pagination": {"next_cursor": None}},
    ])
    api = APIClient(session)
    list(api.iter_objects(offset=999, cursor="stale", count=True))
    p = session.calls[0][1]
    assert "offset" not in p and "cursor" not in p and "count" not in p


# ---------------------------------------------------------------------------
# registry._iter_rows keyset + pushed-down filters
# ---------------------------------------------------------------------------

class _Query:
    def __init__(self, table, log, pages):
        self._log = log
        self._pages = pages
        self.ops = {"table": table, "eq": [], "gt": None, "order": None, "limit": None, "select": None}

    def select(self, cols):
        self.ops["select"] = cols
        return self

    def eq(self, col, val):
        self.ops["eq"].append((col, val))
        return self

    def gt(self, col, val):
        self.ops["gt"] = (col, val)
        return self

    def order(self, col):
        self.ops["order"] = col
        return self

    def limit(self, n):
        self.ops["limit"] = n
        return self

    def execute(self):
        self._log.append(self.ops)
        class R:
            data = self._pages.pop(0)
        return R()


class _Client:
    def __init__(self, pages):
        self.log = []
        self._pages = pages

    def table(self, name):
        return _Query(name, self.log, self._pages)


def test_iter_rows_keysets_on_id_and_pushes_filters():
    client = _Client([
        [{"storage_key": "k1", "id": 10}, {"storage_key": "k2", "id": 20}],
        [{"storage_key": "k3", "id": 30}],           # short page => stop
    ])
    rows = list(registry._iter_rows(client, "storage_objects", "storage_key", page=2,
                                    filters={"bucket": "data", "backend": "osn"}))
    assert [r["storage_key"] for r in rows] == ["k1", "k2", "k3"]
    assert len(client.log) == 2
    first, second = client.log
    # cursor column added to the projection, filters pushed to the server
    assert first["select"] == "storage_key, id"
    assert first["eq"] == [("bucket", "data"), ("backend", "osn")]
    assert first["gt"] is None and first["order"] == "id" and first["limit"] == 2
    # second page seeks past the last id instead of offsetting
    assert second["gt"] == ("id", 20)
    assert second["eq"] == first["eq"]


def test_iter_rows_exact_multiple_makes_one_extra_empty_request():
    client = _Client([
        [{"id": 1}, {"id": 2}],
        [],
    ])
    assert [r["id"] for r in registry._iter_rows(client, "spectra", "id", page=2)] == [1, 2]
    assert len(client.log) == 2 and client.log[1]["gt"] == ("id", 2)


def test_registry_keys_filters_server_side():
    client = _Client([[{"storage_key": "spectra/x.fits", "id": 1}, {"storage_key": None, "id": 2}]])
    keys = registry.registry_keys(client, bucket="data", backend="r2")
    assert keys == ["spectra/x.fits"]
    assert client.log[0]["eq"] == [("bucket", "data"), ("backend", "r2")]
    # no backend => only the bucket predicate
    client2 = _Client([[]])
    registry.registry_keys(client2, bucket="tiles")
    assert client2.log[0]["eq"] == [("bucket", "tiles")]
