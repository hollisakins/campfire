"""scripts/backfill_science_hash.py: write order and straggler repair (P0b).

The script is loaded by path (it is not a package). A fake PostgREST-shaped
client records every call, so the tests pin two properties the resumability
argument rests on: per file the spectra row is written before the registry row
(the registry hash is the completion marker fetch_pending selects on), and the
repair pass copies an already-written registry hash onto a spectra row that is
still NULL without reading any bytes.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("campfire_pipeline.metadata.science_hash")
pytest.importorskip("tqdm")

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_science_hash.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("backfill_science_hash", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    """Records the builder chain; execute() asks the fake client for data."""

    def __init__(self, client, table):
        self.client = client
        self.table_name = table
        self.op = None
        self.payload = None
        self.filters: list[tuple] = []
        self._negate = False

    # builders -----------------------------------------------------------
    def select(self, cols):
        self.op = ("select", cols)
        return self

    def update(self, payload):
        self.op = ("update",)
        self.payload = payload
        return self

    @property
    def not_(self):
        self._negate = True
        return self

    def _f(self, name, *args):
        self.filters.append((("not." if self._negate else "") + name, *args))
        self._negate = False
        return self

    def eq(self, c, v):
        return self._f("eq", c, v)

    def gt(self, c, v):
        return self._f("gt", c, v)

    def in_(self, c, v):
        return self._f("in", c, list(v))

    def is_(self, c, v):
        return self._f("is", c, v)

    def order(self, c):
        return self

    def limit(self, n):
        self.page = n
        return self

    def execute(self):
        return _Result(self.client.respond(self))


class FakeSB:
    def __init__(self, spectra=None, registry=None):
        self.calls: list[_Query] = []
        # rows keyed as the tables are: spectra by spectrum_id, registry by id
        self.spectra = {r["spectrum_id"]: dict(r) for r in (spectra or [])}
        self.registry = {r["id"]: dict(r) for r in (registry or [])}

    def table(self, name):
        return _Query(self, name)

    def respond(self, q: _Query):
        self.calls.append(q)
        rows = self.spectra if q.table_name == "spectra" else self.registry
        if q.op[0] == "update":
            for r in rows.values():
                if all(self._match(r, f) for f in q.filters):
                    r.update(q.payload)
            return []
        out = [dict(r) for r in rows.values() if all(self._match(r, f) for f in q.filters)]
        out.sort(key=lambda r: r["id"])
        return out[: getattr(q, "page", None)]

    @staticmethod
    def _match(row, f):
        name, col, val = f
        neg = name.startswith("not.")
        name = name[4:] if neg else name
        v = row.get(col)
        if name == "eq":
            ok = v == val
        elif name == "gt":
            ok = v is not None and v > val
        elif name == "in":
            ok = v in val
        elif name == "is":
            ok = (v is None) if val == "null" else (v == val)
        else:  # pragma: no cover
            raise AssertionError(name)
        return (not ok) if neg else ok


H1 = "sha256:" + "11" * 32
H2 = "sha256:" + "22" * 32


def _updates(sb):
    return [(q.table_name, q.payload["science_hash"], q.filters) for q in sb.calls if q.op[0] == "update"]


def test_write_batch_writes_spectra_before_registry(script):
    sb = FakeSB(
        spectra=[{"id": 1, "spectrum_id": "obs_prism_clear_1", "science_hash": None}],
        registry=[
            {"id": 10, "spectrum_id": "obs_prism_clear_1", "product_type": "nirspec_spec",
             "status": "active", "science_hash": None},
            {"id": 11, "spectrum_id": "obs_prism_clear_1", "product_type": "nirspec_lines",
             "status": "active", "science_hash": None},
        ],
    )
    script.write_batch(sb, [(sb.registry[10], H1), (sb.registry[11], H2)])

    seq = [(t, h) for t, h, _ in _updates(sb)]
    # nirspec_spec: spectra first, then its registry row; nirspec_lines: registry only
    assert seq == [("spectra", H1), ("storage_objects", H1), ("storage_objects", H2)]
    assert sb.spectra["obs_prism_clear_1"]["science_hash"] == H1
    assert sb.registry[10]["science_hash"] == H1
    assert sb.registry[11]["science_hash"] == H2


def test_repair_stragglers_copies_registry_hash_onto_null_spectra(script):
    """A kill after the registry write (old order) or any other half-done write
    leaves spectra NULL while the registry has the hash: the repair pass fixes
    it from the registry alone and leaves rows the main loop still owns."""
    sb = FakeSB(
        spectra=[
            {"id": 1, "spectrum_id": "obs_prism_clear_1", "science_hash": None, "observation": "obs"},
            {"id": 2, "spectrum_id": "obs_prism_clear_2", "science_hash": None, "observation": "obs"},
            {"id": 3, "spectrum_id": "obs_prism_clear_3", "science_hash": H2, "observation": "obs"},
        ],
        registry=[
            {"id": 10, "spectrum_id": "obs_prism_clear_1", "product_type": "nirspec_spec",
             "status": "active", "science_hash": H1},                 # straggler
            {"id": 11, "spectrum_id": "obs_prism_clear_2", "product_type": "nirspec_spec",
             "status": "active", "science_hash": None},               # still pending
            {"id": 12, "spectrum_id": "obs_prism_clear_1", "product_type": "nirspec_lines",
             "status": "active", "science_hash": H2},                 # wrong product type
        ],
    )
    assert script.repair_stragglers(sb, None, dry_run=True) == 1
    assert sb.spectra["obs_prism_clear_1"]["science_hash"] is None   # dry run writes nothing

    assert script.repair_stragglers(sb, None, dry_run=False) == 1
    assert sb.spectra["obs_prism_clear_1"]["science_hash"] == H1
    assert sb.spectra["obs_prism_clear_2"]["science_hash"] is None
    assert sb.spectra["obs_prism_clear_3"]["science_hash"] == H2
    # idempotent: nothing left to repair
    assert script.repair_stragglers(sb, None, dry_run=False) == 0


def test_repair_stragglers_pages_and_scopes_to_obs(script, monkeypatch):
    monkeypatch.setattr(script, "PAGE", 2)
    spectra = [
        {"id": i, "spectrum_id": f"obs_prism_clear_{i}", "science_hash": None,
         "observation": "obs" if i != 4 else "other"}
        for i in range(1, 6)
    ]
    registry = [
        {"id": 100 + i, "spectrum_id": f"obs_prism_clear_{i}", "product_type": "nirspec_spec",
         "status": "active", "science_hash": H1}
        for i in range(1, 6)
    ]
    sb = FakeSB(spectra=spectra, registry=registry)
    assert script.repair_stragglers(sb, "obs", dry_run=False) == 4
    assert sb.spectra["obs_prism_clear_4"]["science_hash"] is None
    assert all(sb.spectra[f"obs_prism_clear_{i}"]["science_hash"] == H1 for i in (1, 2, 3, 5))
    # keyset paging on id: three select pages over the spectra table
    pages = [q for q in sb.calls if q.table_name == "spectra" and q.op[0] == "select"]
    assert len(pages) == 3
