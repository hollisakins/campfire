"""Unit tests for the NIRCam exclusion round-trip + re-deploy preservation
(epic #261, N6). Pure-Python: a tiny fake Supabase client serves the PostgREST
chain, no DB. The DB-backed pipeline consumer is covered by
``pipeline/tests/test_nircam_exclusions.py``.
"""
import json
import types

from campfire.deploy import nircam_exclusions as nx
from campfire.deploy.nircam import _upsert_exposures


# ---------------------------------------------------------------------------
# fake client: .table().select().eq().in_().execute(), .upsert(), .insert()
# ---------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, rows, sink):
        self._rows = rows
        self._eq = []
        self._in = None
        self._cols = None
        self._sink = sink

    def select(self, *a, **k):
        # Honour PostgREST column projection: a column the caller did not ask
        # for is NOT present on the returned rows. Without this the fake hands
        # back every fixture key and a missing column in the real `.select()`
        # goes undetected (that is how the `detector` NOT-NULL bug shipped).
        cols = ','.join(str(x) for x in a if x)
        self._cols = [c.strip() for c in cols.split(',') if c.strip()] or None
        return self

    def eq(self, col, val):
        self._eq.append((col, val))
        return self

    def in_(self, col, vals):
        self._in = (col, set(vals))
        return self

    def upsert(self, data, on_conflict=None):
        self._sink['upserts'].append(list(data))
        return self

    def insert(self, data):
        self._sink['inserts'].append(list(data))
        return self

    def execute(self):
        rows = list(self._rows)
        if self._in:
            col, vals = self._in
            rows = [r for r in rows if r.get(col) in vals]
        for col, val in self._eq:
            rows = [r for r in rows if r.get(col) == val]
        if self._cols and '*' not in self._cols:
            rows = [{k: v for k, v in r.items() if k in self._cols}
                    for r in rows]
        return types.SimpleNamespace(data=rows)


class _FakeClient:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.sink = {'upserts': [], 'inserts': []}

    def table(self, name):
        return _FakeQuery(self._rows, self.sink)

    @property
    def upserts(self):
        return self.sink['upserts']

    @property
    def inserts(self):
        return self.sink['inserts']


def _patch_client(monkeypatch, rows):
    client = _FakeClient(rows)
    monkeypatch.setattr('campfire.deploy.supabase.get_supabase_client',
                        lambda config: client)
    return client


def _patch_dirs(monkeypatch, ref_dir):
    monkeypatch.setattr('campfire.deploy.nircam._resolve_nircam_dirs',
                        lambda field: {'reference': ref_dir})


# ---------------------------------------------------------------------------
# pull_exclusions → exposures.json
# ---------------------------------------------------------------------------

def test_pull_exclusions_writes_per_filter_json(monkeypatch, tmp_path):
    rows = [
        {'field': 'cosmos', 'filter': 'f444w', 'filename': 'jw1_04101_00003_nrcalong', 'review_status': 'excluded'},
        {'field': 'cosmos', 'filter': 'f444w', 'filename': 'jw1_04101_00001_nrcalong', 'review_status': 'excluded'},
        {'field': 'cosmos', 'filter': 'f200w', 'filename': 'jw1_02101_00002_nrca1', 'review_status': 'excluded'},
        {'field': 'cosmos', 'filter': 'f200w', 'filename': 'not_excluded', 'review_status': 'pending'},  # filtered out
    ]
    _patch_client(monkeypatch, rows)
    _patch_dirs(monkeypatch, tmp_path)
    nx.pull_exclusions('cosmos', config={})
    doc = json.loads((tmp_path / 'exposures.json').read_text())
    assert doc['version'] == 1
    assert doc['field'] == 'cosmos'
    assert doc['generated_by'] == 'campfire deploy nircam pull'
    assert doc['excluded'] == {
        'f200w': ['jw1_02101_00002_nrca1'],
        'f444w': ['jw1_04101_00001_nrcalong', 'jw1_04101_00003_nrcalong'],  # sorted
    }


def test_pull_exclusions_empty_still_writes(monkeypatch, tmp_path):
    _patch_client(monkeypatch, [])
    _patch_dirs(monkeypatch, tmp_path)
    nx.pull_exclusions('cosmos', config={})
    doc = json.loads((tmp_path / 'exposures.json').read_text())
    # Reversibility: un-excluding the last exposure yields an empty list, not a
    # stale file combine keeps honoring.
    assert doc['excluded'] == {}


def test_pull_exclusions_overwrites_no_merge(monkeypatch, tmp_path):
    (tmp_path / 'exposures.json').write_text(
        json.dumps({'version': 1, 'excluded': {'f444w': ['stale_root']}}))
    _patch_client(monkeypatch, [{'field': 'cosmos', 'filter': 'f200w',
                                 'filename': 'new_root', 'review_status': 'excluded'}])
    _patch_dirs(monkeypatch, tmp_path)
    nx.pull_exclusions('cosmos', config={})
    doc = json.loads((tmp_path / 'exposures.json').read_text())
    assert doc['excluded'] == {'f200w': ['new_root']}  # full replace, no merge


def test_pull_exclusions_dry_run_writes_nothing(monkeypatch, tmp_path):
    _patch_client(monkeypatch, [{'filter': 'f444w', 'filename': 'r'}])
    _patch_dirs(monkeypatch, tmp_path)
    nx.pull_exclusions('cosmos', config={}, dry_run=True)
    assert not (tmp_path / 'exposures.json').exists()


# ---------------------------------------------------------------------------
# import_skip: fields.toml skip globs → DB review_status='excluded'
# ---------------------------------------------------------------------------

def _patch_field_skip(monkeypatch, skip):
    fake_field = types.SimpleNamespace(skip=skip)
    monkeypatch.setattr('campfire_pipeline.nircam.field.Field',
                        types.SimpleNamespace(load=lambda name: fake_field))


def test_import_skip_matches_globs_additive(monkeypatch):
    _patch_field_skip(monkeypatch, ['jw1_04101_'])  # prefix glob, like fields.toml
    rows = [
        {'field': 'cosmos', 'filter': 'f444w', 'detector': 'nrcalong', 'filename': 'jw1_04101_00003_nrcalong', 'review_status': 'pending'},
        {'field': 'cosmos', 'filter': 'f444w', 'detector': 'nrcalong', 'filename': 'jw1_04101_00001_nrcalong', 'review_status': 'excluded'},  # already
        {'field': 'cosmos', 'filter': 'f200w', 'detector': 'nrca1', 'filename': 'jw1_02101_00002_nrca1', 'review_status': 'approved'},  # no match
    ]
    client = _patch_client(monkeypatch, rows)
    nx.import_skip('cosmos', config={})
    # Only the pending, matching one is newly excluded (additive; skips the
    # already-excluded and the non-matching approved row).
    assert len(client.upserts) == 1
    batch = client.upserts[0]
    assert [r['filename'] for r in batch] == ['jw1_04101_00003_nrcalong']
    assert batch[0]['review_status'] == 'excluded'


# Columns on `nircam_exposures` that are NOT NULL with no DB default. Postgres
# validates NOT NULL on the PROPOSED INSERT tuple BEFORE resolving ON CONFLICT,
# so an upsert omitting any of these fails with 23502 even though import_skip
# only ever updates a row that already exists. (`id` -> nextval, `stage` ->
# 'uncal', `correction` -> 'none' all carry defaults and may be omitted.)
_NOT_NULL_NO_DEFAULT = ('field', 'filter', 'detector', 'filename')


def test_import_skip_upsert_payload_carries_not_null_columns(monkeypatch):
    """Regression: the upsert payload omitted `detector` and every real
    import-skip run died with 23502 (null value in column "detector").
    The fake client cannot enforce NOT NULL, so assert the contract directly.
    """
    _patch_field_skip(monkeypatch, ['jw1_04101_'])
    rows = [{'field': 'cosmos', 'filter': 'f444w', 'detector': 'nrcblong',
             'filename': 'jw1_04101_00003_nrcblong', 'review_status': 'pending'}]
    client = _patch_client(monkeypatch, rows)
    nx.import_skip('cosmos', config={})

    assert len(client.upserts) == 1
    payload = client.upserts[0][0]
    missing = [c for c in _NOT_NULL_NO_DEFAULT if payload.get(c) is None]
    assert not missing, f"upsert payload omits NOT NULL column(s): {missing}"
    # carried through from the row, not invented
    assert payload['detector'] == 'nrcblong'


def test_import_skip_dry_run_no_write(monkeypatch):
    _patch_field_skip(monkeypatch, ['jw1_04101_'])
    rows = [{'field': 'cosmos', 'filter': 'f444w', 'detector': 'nrcalong',
             'filename': 'jw1_04101_00003_nrcalong', 'review_status': 'pending'}]
    client = _patch_client(monkeypatch, rows)
    nx.import_skip('cosmos', config={}, dry_run=True)
    assert client.upserts == []


def test_import_skip_no_patterns_noop(monkeypatch):
    _patch_field_skip(monkeypatch, [])
    client = _patch_client(monkeypatch, [{'filter': 'f444w', 'filename': 'x',
                                          'review_status': 'pending'}])
    nx.import_skip('cosmos', config={})
    assert client.upserts == []


# ---------------------------------------------------------------------------
# Part C — inspection state survives re-reduction (_upsert_exposures)
# ---------------------------------------------------------------------------

def test_redeploy_preserves_inspection_state():
    """A second deploy over an existing row must not touch review_status,
    correction, notes, or mask_regions (spec: inspection survives re-reduction).
    """
    existing = [{'field': 'cosmos', 'filter': 'f444w', 'filename': 'root1'}]
    client = _FakeClient(existing)
    record = {
        'field': 'cosmos', 'filter': 'f444w', 'filename': 'root1',
        'detector': 'nrcalong', 'stage': 'outlier',
        'png_path': 'data/products/nircam/cosmos/f444w/root1_preview.png',
        'full_png_path': 'data/products/nircam/cosmos/f444w/root1_full.png',
        'image_width': 2048, 'image_height': 2048,
        'visit': 'v1', 'date_obs': '2026-01-01',
        'ra_center': 150.0, 'dec_center': 2.0,
    }
    _upsert_exposures(client, [record])

    assert not client.inserts  # existing row → update path, no insert
    assert len(client.upserts) == 1
    update = client.upserts[0][0]
    for protected in ('review_status', 'correction', 'notes', 'mask_regions'):
        assert protected not in update, f"{protected} must be preserved on re-deploy"
    # pipeline-derived columns ARE refreshed
    assert update['stage'] == 'outlier'
    assert update['png_path'].endswith('root1_preview.png')
