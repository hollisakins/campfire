"""Unit tests for the NIRCam exclusion round-trip + re-deploy preservation
(epic #261, N6). Pure-Python: a tiny fake Supabase client serves the PostgREST
chain, no DB. The DB-backed pipeline consumer is covered by
``pipeline/tests/test_nircam_exclusions.py``.
"""
import json

from campfire.deploy import nircam_exclusions as nx
from campfire.deploy.nircam import _upsert_exposures
from tests.fakes import FakePostgrest


def _exposures_client(rows):
    return FakePostgrest(tables={'nircam_exposures': rows})


def _patch_client(monkeypatch, rows):
    client = _exposures_client(rows)
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


def test_pull_exclusions_keeps_local(monkeypatch, tmp_path):
    (tmp_path / 'exposures.json').write_text(
        json.dumps({'version': 1, 'excluded': {'f444w': ['local_root']}}))
    _patch_client(monkeypatch, [{'field': 'cosmos', 'filter': 'f200w',
                                 'filename': 'new_root', 'review_status': 'excluded'}])
    _patch_dirs(monkeypatch, tmp_path)
    nx.pull_exclusions('cosmos', config={})
    doc = json.loads((tmp_path / 'exposures.json').read_text())
    assert doc['excluded'] == {'f200w': ['new_root'], 'f444w': ['local_root']}  # union, local never removed


def test_pull_exclusions_dry_run_writes_nothing(monkeypatch, tmp_path):
    _patch_client(monkeypatch, [{'filter': 'f444w', 'filename': 'r'}])
    _patch_dirs(monkeypatch, tmp_path)
    nx.pull_exclusions('cosmos', config={}, dry_run=True)
    assert not (tmp_path / 'exposures.json').exists()


# ---------------------------------------------------------------------------
# Part C — inspection state survives re-reduction (_upsert_exposures)
# ---------------------------------------------------------------------------

def test_redeploy_preserves_inspection_state():
    """A second deploy over an existing row must not touch review_status,
    correction, notes, or mask_regions (spec: inspection survives re-reduction).
    """
    existing = [{'field': 'cosmos', 'filter': 'f444w', 'filename': 'root1'}]
    client = _exposures_client(existing)
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
    update = client.upserted_rows()[0]
    for protected in ('review_status', 'correction', 'notes', 'mask_regions'):
        assert protected not in update, f"{protected} must be preserved on re-deploy"
    # pipeline-derived columns ARE refreshed
    assert update['stage'] == 'outlier'
    assert update['png_path'].endswith('root1_preview.png')


def test_pull_exclusions_keeps_unreadable_local_file(monkeypatch, tmp_path, capsys):
    """A malformed hand-edited exposures.json is kept as it is; the pull does not abort."""
    bad = '{"excluded": {"f444w": ["local_root"]'           # truncated JSON
    (tmp_path / 'exposures.json').write_text(bad)
    _patch_client(monkeypatch, [{'field': 'cosmos', 'filter': 'f200w',
                                 'filename': 'new_root', 'review_status': 'excluded'}])
    _patch_dirs(monkeypatch, tmp_path)
    nx.pull_exclusions('cosmos', config={})
    assert (tmp_path / 'exposures.json').read_text() == bad
    assert 'cannot read it' in capsys.readouterr().out
