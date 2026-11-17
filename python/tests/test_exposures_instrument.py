"""The ``exposures`` review table holds every imaging instrument.

What the client has to get right now that NIRCam, ACS/WFC and WFC3/IR share
one table told apart by ``instrument``: a deploy writes and reads only its own
instrument's rows, a mask the pipeline found reaches ``mask_regions`` exactly
once, and a pull sends a reviewer's version of that mask (kept, reshaped or
deleted) back to ``reference/<instrument>/<field>/``.

Pure Python: the shared PostgREST fake, files under ``tmp_path``.
"""
import hashlib
import json

import pytest

from campfire.deploy import annotations
from campfire.deploy import nircam_exclusions as nx
from campfire.deploy import nircam_masks as nm
from campfire.deploy.local_first import GENERATED_HASH_MARK, body_lines, is_unedited_generated
from campfire.deploy.nircam import _resolve_imaging_dirs, _upsert_exposures
from tests.fakes import FakePostgrest


def _pipeline_mask(polygon='polygon(1.0,2.0,6.0,2.0,6.0,4.0,1.0,4.0) # text={satellite trail}'):
    """A region file as cfpipe's HST ``trails`` step writes it: its own hash of
    its content lines in the header. Returns ``(text, hash)``."""
    body = f'image\n{polygon}\n'
    digest = hashlib.sha256('\n'.join(body_lines(body)).encode()).hexdigest()[:16]
    return f'# Region file format: DS9 version 4.1\n{GENERATED_HASH_MARK} {digest}\n{body}', digest


def _record(filename, filt='f814w'):
    return {'field': 'cosmos', 'filter': filt, 'filename': filename,
            'detector': 'acswfc', 'stage': 'align', 'visit': 'jtst01',
            'png_path': None, 'full_png_path': None,
            'image_width': 4096, 'image_height': 4096,
            'date_obs': '2004-01-01', 'ra_center': 150.0, 'dec_center': 2.0}


_PAYLOAD = {'version': 1, 'polygons': [
    {'id': 'trail-1', 'vertices': [[1.0, 2.0], [6.0, 2.0], [6.0, 4.0], [1.0, 4.0]],
     'label': 'satellite trail', 'source': 'imported'}]}


def _patch(monkeypatch, client, tmp_path):
    monkeypatch.setattr('campfire.deploy.supabase.get_supabase_client', lambda config: client)
    monkeypatch.setattr(
        'campfire.deploy.nircam._resolve_imaging_dirs',
        lambda instrument, field: {'reference': tmp_path / instrument,
                                   'masks': tmp_path / instrument / 'masks'})


# ---------------------------------------------------------------------------
# directories
# ---------------------------------------------------------------------------

def test_every_imaging_instrument_has_the_same_shape_on_disk(monkeypatch, tmp_path):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    for instrument in ('nircam', 'acswfc', 'wfc3ir'):
        dirs = _resolve_imaging_dirs(instrument, 'cosmos')
        assert dirs['products'] == tmp_path / 'products' / instrument / 'cosmos'
        assert dirs['masks'] == tmp_path / 'reference' / instrument / 'cosmos' / 'masks'
    with pytest.raises(ValueError):
        _resolve_imaging_dirs('wfc3uvis', 'cosmos')


# ---------------------------------------------------------------------------
# deploy: rows of one instrument, the pipeline's mask seeded once
# ---------------------------------------------------------------------------

def test_a_new_row_carries_its_instrument_and_the_pipelines_mask():
    client = FakePostgrest(tables={'exposures': []})
    stats = _upsert_exposures(client, [_record('jtst01a0q_flc'), _record('jtst01a1q_flc')],
                              instrument='acswfc',
                              mask_seeds={('f814w', 'jtst01a0q_flc'): ('abc123', _PAYLOAD)})
    rows = {r['filename']: r for r in client.rows('exposures')}
    assert all(r['instrument'] == 'acswfc' for r in rows.values())
    assert rows['jtst01a0q_flc']['mask_regions'] == _PAYLOAD
    assert rows['jtst01a0q_flc']['mask_seed_hash'] == 'abc123'
    assert not rows['jtst01a1q_flc'].get('mask_regions')
    assert stats == {'seeded': 1, 'kept_reviewed': 0}


def test_the_same_file_name_under_another_instrument_is_another_row():
    """A NIRCam row with this field, filter and file name is not this deploy's."""
    client = FakePostgrest(tables={'exposures': [
        {'instrument': 'nircam', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a0q_flc', 'review_status': 'excluded'}]})
    _upsert_exposures(client, [_record('jtst01a0q_flc')], instrument='acswfc')
    rows = client.rows('exposures')
    assert sorted(r['instrument'] for r in rows) == ['acswfc', 'nircam']
    assert next(r for r in rows if r['instrument'] == 'nircam')['review_status'] == 'excluded'


@pytest.mark.parametrize('row_mask, row_seed, expect_mask, expect_stats', [
    # the reviewer deleted the seeded mask: the same file content never comes back
    (None, 'abc123', None, {'seeded': 0, 'kept_reviewed': 0}),
    # the reviewer reshaped it: kept, whatever the pipeline's file now says
    ({'version': 1, 'polygons': [{'vertices': [[9, 9], [8, 8], [7, 9]]}]}, 'older',
     {'version': 1, 'polygons': [{'vertices': [[9, 9], [8, 8], [7, 9]]}]},
     {'seeded': 0, 'kept_reviewed': 1}),
    # deployed before the trail step ran: no mask, never seeded -> seeded now
    (None, None, _PAYLOAD, {'seeded': 1, 'kept_reviewed': 0}),
    # the pipeline's mask changed and the old one was deleted in review: the
    # new content is a new finding, shown once
    (None, 'older', _PAYLOAD, {'seeded': 1, 'kept_reviewed': 0}),
])
def test_a_redeploy_never_undoes_a_review(row_mask, row_seed, expect_mask, expect_stats):
    client = FakePostgrest(tables={'exposures': [
        {'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a0q_flc', 'review_status': 'approved',
         'mask_regions': row_mask, 'mask_seed_hash': row_seed}]})
    stats = _upsert_exposures(client, [_record('jtst01a0q_flc')], instrument='acswfc',
                              mask_seeds={('f814w', 'jtst01a0q_flc'): ('abc123', _PAYLOAD)})
    row = client.rows('exposures')[0]
    assert row['mask_regions'] == expect_mask
    assert row['review_status'] == 'approved'
    assert stats == expect_stats


# ---------------------------------------------------------------------------
# pull: the reviewed mask comes back to reference/<instrument>/<field>/
# ---------------------------------------------------------------------------

def test_pull_writes_each_instruments_masks_in_its_own_tree(monkeypatch, tmp_path):
    client = FakePostgrest(tables={'exposures': [
        {'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a0q_flc', 'mask_regions': _PAYLOAD, 'mask_seed_hash': 'abc'},
        {'instrument': 'nircam', 'field': 'cosmos', 'filter': 'f444w',
         'filename': 'jw1_nrcalong', 'mask_regions': _PAYLOAD, 'mask_seed_hash': None},
    ]})
    _patch(monkeypatch, client, tmp_path)
    nm.pull_masks('cosmos', config={}, instrument='acswfc')
    assert (tmp_path / 'acswfc' / 'masks' / 'f814w' / 'jtst01a0q_flc.reg').exists()
    assert not (tmp_path / 'nircam').exists()
    assert not (tmp_path / 'acswfc' / 'masks' / 'f444w').exists()


def test_pull_replaces_the_pipelines_unedited_mask_with_the_reviewed_one(monkeypatch, tmp_path):
    text, digest = _pipeline_mask()
    reg = tmp_path / 'acswfc' / 'masks' / 'f814w' / 'jtst01a0q_flc.reg'
    reg.parent.mkdir(parents=True)
    reg.write_text(text)
    reviewed = {'version': 1, 'polygons': [
        {'vertices': [[1.0, 2.0], [60.0, 2.0], [60.0, 40.0]], 'label': 'satellite trail'}]}
    client = FakePostgrest(tables={'exposures': [
        {'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a0q_flc', 'mask_regions': reviewed, 'mask_seed_hash': digest}]})
    _patch(monkeypatch, client, tmp_path)
    nm.pull_masks('cosmos', config={}, instrument='acswfc')
    got, _ = nm.polygons_from_regfile(reg.read_text())
    assert got == [{'vertices': [[1.0, 2.0], [60.0, 2.0], [60.0, 40.0]],
                    'label': 'satellite trail'}]
    # it is the reviewer's file now: the trail step will not take it back
    assert not is_unedited_generated(reg.read_text())


def test_pull_clears_a_pipeline_mask_a_reviewer_removed(monkeypatch, tmp_path):
    text, digest = _pipeline_mask()
    masks = tmp_path / 'acswfc' / 'masks' / 'f814w'
    masks.mkdir(parents=True)
    (masks / 'jtst01a0q_flc.reg').write_text(text)
    # a hand-drawn mask for an exposure the portal has no mask for is kept
    (masks / 'jtst01a1q_flc.reg').write_text('image\npolygon(5,5,9,5,9,9)\n')
    client = FakePostgrest(tables={'exposures': [
        {'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a0q_flc', 'mask_regions': None, 'mask_seed_hash': digest},
        {'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a1q_flc', 'mask_regions': None, 'mask_seed_hash': 'zzz'},
        # never seeded, no mask: nothing to say about it
        {'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a2q_flc', 'mask_regions': None, 'mask_seed_hash': None},
    ]})
    _patch(monkeypatch, client, tmp_path)
    nm.pull_masks('cosmos', config={}, instrument='acswfc')

    cleared = (masks / 'jtst01a0q_flc.reg').read_text()
    assert nm.polygons_from_regfile(cleared) == ([], [])
    assert not is_unedited_generated(cleared)      # so the trail step keeps it
    assert 'polygon(5,5,9,5,9,9)' in (masks / 'jtst01a1q_flc.reg').read_text()
    assert not (masks / 'jtst01a2q_flc.reg').exists()

    # and the drift report has nothing to say about the cleared one
    report = nm.mask_drift_report('cosmos', config={}, instrument='acswfc')
    assert not any('jtst01a0q_flc' in p for p in report['stale'] + report['local_only'])


def test_drift_report_names_a_pipeline_mask_the_portal_has_not_seen(monkeypatch, tmp_path):
    text, _ = _pipeline_mask()
    masks = tmp_path / 'acswfc' / 'masks' / 'f814w'
    masks.mkdir(parents=True)
    (masks / 'jtst01a0q_flc.reg').write_text(text)
    client = FakePostgrest(tables={'exposures': []})
    _patch(monkeypatch, client, tmp_path)
    report = nm.mask_drift_report('cosmos', config={}, instrument='acswfc')
    assert len(report['pipeline_pending']) == 1
    assert report['stale'] == [] and report['local_only'] == []


def test_exclusions_are_per_instrument(monkeypatch, tmp_path):
    client = FakePostgrest(tables={'exposures': [
        {'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a0q_flc', 'review_status': 'excluded'},
        {'instrument': 'nircam', 'field': 'cosmos', 'filter': 'f444w',
         'filename': 'jw1_nrcalong', 'review_status': 'excluded'},
    ]})
    _patch(monkeypatch, client, tmp_path)
    nx.pull_exclusions('cosmos', config={}, instrument='acswfc')
    doc = json.loads((tmp_path / 'acswfc' / 'exposures.json').read_text())
    assert doc['instrument'] == 'acswfc'
    assert doc['excluded'] == {'f814w': ['jtst01a0q_flc']}


def test_a_field_pull_covers_every_instrument_it_has(monkeypatch, tmp_path):
    client = FakePostgrest(tables={'exposures': [
        {'id': 1, 'instrument': 'acswfc', 'field': 'cosmos', 'filter': 'f814w',
         'filename': 'jtst01a0q_flc', 'review_status': 'excluded', 'mask_regions': _PAYLOAD},
        {'id': 2, 'instrument': 'nircam', 'field': 'cosmos', 'filter': 'f444w',
         'filename': 'jw1_nrcalong', 'review_status': 'excluded', 'mask_regions': None},
        {'id': 3, 'instrument': 'wfc3ir', 'field': 'uds', 'filter': 'f160w',
         'filename': 'itst01a0q_flt', 'review_status': 'pending', 'mask_regions': None},
    ]})
    _patch(monkeypatch, client, tmp_path)
    assert annotations.field_instruments(client, 'cosmos') == ['nircam', 'acswfc']
    annotations.pull_field_annotations('cosmos', config={})
    assert (tmp_path / 'acswfc' / 'masks' / 'f814w' / 'jtst01a0q_flc.reg').exists()
    assert (tmp_path / 'acswfc' / 'exposures.json').exists()
    assert (tmp_path / 'nircam' / 'exposures.json').exists()
    assert not (tmp_path / 'wfc3ir').exists()
