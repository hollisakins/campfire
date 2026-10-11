"""Pins the semantics of the shared PostgREST fake (``tests/fakes.py``) that
the migrated deploy tests lean on: in-memory filtering, ordering/paging,
applied writes and their records, response overrides, rpc handlers and error
injection. If one of these changes, the fake's consumers change meaning."""
import pytest

from tests.fakes import FakePostgrest, UpsertRecord


@pytest.fixture
def client(fake_postgrest):
    return fake_postgrest(tables={"t": [
        {"id": 1, "a": None, "b": "x", "n": 3},
        {"id": 2, "a": 3, "b": "y", "n": 1},
        {"id": 3, "a": 5, "b": "Yes", "n": 2},
    ]})


# --- reads -------------------------------------------------------------------

def test_filters_apply_in_memory(client):
    sel = lambda q: [r["id"] for r in q.execute().data]  # noqa: E731
    t = lambda: client.table("t").select("*")  # noqa: E731
    assert sel(t().eq("b", "x")) == [1]
    assert sel(t().neq("b", "x")) == [2, 3]
    assert sel(t().in_("id", [1, 3])) == [1, 3]
    assert sel(t().is_("a", "null")) == [1]
    assert sel(t().not_.is_("a", "null")) == [2, 3]
    assert sel(t().is_("a", "not.null")) == [2, 3]
    assert sel(t().gt("n", 1)) == [1, 3]
    assert sel(t().gte("n", 2).lt("n", 3)) == [3]
    assert sel(t().lte("n", 1)) == [2]
    assert sel(t().like("b", "Y%")) == [3]
    assert sel(t().ilike("b", "y%")) == [2, 3]
    assert sel(t().eq("missing_column", None)) == [1, 2, 3]   # absent reads as None


def test_order_range_limit(client):
    q = client.table("t").select("*").order("n").range(0, 1)
    assert [r["id"] for r in q.execute().data] == [2, 3]
    q = client.table("t").select("*").order("n", desc=True).limit(1)
    assert [r["id"] for r in q.execute().data] == [1]
    # NULLS LAST ascending, NULLS FIRST descending (Postgres defaults)
    assert [r["id"] for r in client.table("t").select("*").order("a").execute().data] == [2, 3, 1]
    assert [r["id"] for r in client.table("t").select("*").order("a", desc=True).execute().data] == [1, 3, 2]


def test_single_and_maybe_single(client):
    assert client.table("t").select("*").eq("id", 2).single().execute().data["b"] == "y"
    assert client.table("t").select("*").eq("id", 9).maybe_single().execute().data is None
    with pytest.raises(Exception, match="multiple"):
        client.table("t").select("*").single().execute()
    with pytest.raises(Exception, match="multiple"):
        client.table("t").select("*").maybe_single().execute()


def test_count_and_call_log(client):
    resp = client.table("t").select("id", count="exact").gt("n", 1).execute()
    assert resp.count == 2 and len(resp.data) == 2
    call, = client.calls_for("t", "select")
    assert call.columns == "id" and call.count == "exact"
    assert [(f.op, f.column, f.value) for f in call.filters] == [("gt", "n", 1)]


def test_response_override_is_served_verbatim(client):
    client.responses["t"] = [{"id": 99}]
    assert client.table("t").select("*").eq("id", 1).execute().data == [{"id": 99}]
    client.responses["t"] = lambda q: [{"filters": len(q.filters)}]
    assert client.table("t").select("*").eq("id", 1).eq("b", "x").execute().data == [{"filters": 2}]


# --- writes ------------------------------------------------------------------

def test_insert_assigns_ids_from_the_seeded_sequence(client):
    rows = client.table("t").insert([{"b": "new"}, {"id": 50, "b": "explicit"}]).execute().data
    assert [r["id"] for r in rows] == [4, 50]
    assert len(client.tables["t"]) == 5
    assert client.inserts[0].data == [{"b": "new"}, {"id": 50, "b": "explicit"}]  # raw payload
    assert client.inserted_rows() == [{"b": "new"}, {"id": 50, "b": "explicit"}]


def test_insert_next_id_is_configurable():
    client = FakePostgrest(next_id=42)
    assert client.table("deployments").insert({"x": 1}).execute().data == [{"x": 1, "id": 42}]
    assert client.table("deployments").insert({"x": 2}).execute().data[0]["id"] == 43


def test_upsert_merges_on_conflict_columns_and_records_the_payload(client):
    client.table("t").upsert({"id": 1, "b": "patched"}, on_conflict="id").execute()
    client.table("t").upsert([{"b": "patched", "n": 30}], on_conflict="b").execute()  # merges into id 1 again
    client.table("t").upsert({"b": "fresh"}, on_conflict="b").execute()               # no match -> append
    assert client.tables["t"][0] == {"id": 1, "a": None, "b": "patched", "n": 30}
    assert client.tables["t"][-1] == {"b": "fresh"}
    assert client.upserts == [
        UpsertRecord("t", "id", {"id": 1, "b": "patched"}),
        UpsertRecord("t", "b", [{"b": "patched", "n": 30}]),
        UpsertRecord("t", "b", {"b": "fresh"}),
    ]
    (table, on_conflict, data), = client.upserts[:1]     # tuple-unpackable
    assert (table, on_conflict) == ("t", "id")
    assert client.upserted_rows("t")[1] == {"b": "patched", "n": 30}


def test_update_patches_matching_rows_in_place_and_returns_them(client):
    row = client.tables["t"][1]
    resp = client.table("t").update({"b": "z"}).eq("id", 2).execute()
    assert resp.data == [row] and row["b"] == "z"
    assert client.table("t").update({"b": "q"}).eq("id", 404).execute().data == []
    assert [u.patch for u in client.updates] == [{"b": "z"}, {"b": "q"}]


def test_delete_removes_and_records_rows(client):
    resp = client.table("t").delete().in_("id", [1, 3]).execute()
    assert [r["id"] for r in resp.data] == [1, 3]
    assert [r["id"] for r in client.tables["t"]] == [2]
    assert [r["id"] for r in client.deleted_rows("t")] == [1, 3]


def test_apply_writes_false_only_records():
    client = FakePostgrest(tables={"t": [{"id": 1}]}, apply_writes=False)
    client.table("t").upsert({"id": 1, "b": 2}, on_conflict="id").execute()
    client.table("t").delete().eq("id", 1).execute()
    assert client.tables["t"] == [{"id": 1}]
    assert len(client.upserts) == 1 and len(client.deletes) == 1


# --- rpc + errors ------------------------------------------------------------

def test_rpc_handlers_and_default():
    client = FakePostgrest(rpc_handlers={"is_admin": True, "echo": lambda p: p["x"] * 2},
                           rpc_default=0)
    assert client.rpc("is_admin").execute().data is True
    assert client.rpc("echo", {"x": 21}).execute().data == 42
    assert client.rpc("anything_else", {"p": 1}).execute().data == 0
    assert client.rpcs == [("is_admin", None), ("echo", {"x": 21}), ("anything_else", {"p": 1})]
    assert client.rpc_names() == ["is_admin", "echo", "anything_else"]


def test_error_injection_scopes_and_counts(client):
    client.inject_error(RuntimeError("rpc down"), rpc="is_admin")
    client.inject_error(ValueError("write refused"), table="t", op="upsert", times=None)
    with pytest.raises(RuntimeError):
        client.rpc("is_admin").execute()
    assert client.rpc("is_admin").execute().data is None       # one-shot consumed
    client.rpc("other").execute()                               # other RPCs untouched
    client.table("t").select("*").execute()                     # other ops untouched
    for _ in range(2):
        with pytest.raises(ValueError):
            client.table("t").upsert({"id": 1}, on_conflict="id").execute()
    assert client.upserts == []                                 # raised before recording
