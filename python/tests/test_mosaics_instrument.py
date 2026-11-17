"""The mosaic index holds every imaging instrument.

``mosaics`` (it was ``nircam_images``) carries an ``instrument``, which is part
of a row's identity, and a field deployment carries the instrument it belongs
to. These tests cover the client's side: an HST mosaic is discovered under its
own name and layout kinds, indexed with its instrument, pushed and mirrored
like NIRCam's, and named as its own release component. The database's side is
supabase/tests/check_mosaics_instrument.sql.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from campfire.api.client import SyncStream
from campfire.db.store import FINAL_PRODUCT_TYPES
from campfire.deploy import nircam as nc
from campfire.deploy import registry as reg
from campfire.deploy.supabase import insert_deployment
from campfire.dr import components_from_options
from campfire.sync import _newly_mirrored_types, sync_metadata
from campfire_layout import parse_key
from tests.fakes import FakePostgrest


def _mosaic_on_disk(root, instrument, field, filt, tile, *, scale='30mas',
                    suffixes=('_i2d.fits', '_sci.fits', '_wht.fits'), pngs=True):
    """The files one instrument's combine leaves for one tile; returns the base name."""
    fdir = root / 'products' / instrument / field / filt
    fdir.mkdir(parents=True, exist_ok=True)
    base = f'mosaic_{instrument}_{filt}_{field}_{scale}_{tile}'
    (fdir / f'{base}_manifest.json').write_text(json.dumps({
        'mosaic_name': base, 'field': field, 'filter': filt, 'tile': tile, 'pixel_scale': scale}))
    for sfx in suffixes:
        (fdir / f'{base}{sfx}').write_bytes(b'\x00')
    if pngs:
        (fdir / f'{base}_thumb.png').write_bytes(b'\x89PNG')
        (fdir / f'{base}_quicklook.png').write_bytes(b'\x89PNG')
    return base


# ---------------------------------------------------------------------------
# discovery and layout kinds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('instrument, filt', [('acswfc', 'f814w'), ('wfc3ir', 'f160w')])
def test_an_hst_mosaic_is_discovered_under_its_own_name_and_kind(tmp_path, instrument, filt):
    base = _mosaic_on_disk(tmp_path, instrument, 'cosmos', filt, 'A1')
    dirs = {'products': tmp_path / 'products' / instrument / 'cosmos'}
    found = nc.discover_mosaics(dirs, 'cosmos', [filt], nc.imaging_profile(instrument))

    assert sorted(m['extension'] for m in found) == ['i2d', 'sci', 'wht']
    assert {m['instrument'] for m in found} == {instrument}
    sci = next(m for m in found if m['extension'] == 'sci')
    # stored gzipped, under the instrument's own tree
    assert sci['storage_key'] == f'data/products/{instrument}/cosmos/{filt}/{base}_sci.fits.gz'
    assert sci['content_type'] == 'application/gzip'
    row = reg.row_for_key(sci['storage_key'], backend='osn', content_hash='sha256:' + 'a' * 64,
                          size_bytes=1, content_type='application/gzip')
    assert row['product_type'] == f'{instrument}_mosaic'
    assert row['instrument'] == instrument and row['field'] == 'cosmos' and row['filter'] == filt
    # the i2d cube stays on the machine, as NIRCam's does
    assert 'i2d' not in {m['extension'] for m in nc.deployable_mosaics(found)}


def test_a_mosaic_named_for_another_instrument_is_not_this_instruments(tmp_path):
    """A NIRCam-named manifest sitting in an ACS/WFC directory is not deployed as ACS/WFC."""
    fdir = tmp_path / 'products' / 'acswfc' / 'cosmos' / 'f814w'
    fdir.mkdir(parents=True)
    base = 'mosaic_nircam_f814w_cosmos_30mas_A1'
    (fdir / f'{base}_manifest.json').write_text(json.dumps({
        'mosaic_name': base, 'field': 'cosmos', 'filter': 'f814w', 'tile': 'A1', 'pixel_scale': '30mas'}))
    (fdir / f'{base}_sci.fits').write_bytes(b'\x00')
    dirs = {'products': tmp_path / 'products' / 'acswfc' / 'cosmos'}
    assert nc.discover_mosaics(dirs, 'cosmos', ['f814w'], nc.imaging_profile('acswfc')) == []


def test_nircam_mosaic_discovery_is_what_it_was(tmp_path):
    base = _mosaic_on_disk(tmp_path, 'nircam', 'cosmos', 'f444w', 'A1')
    dirs = {'products': tmp_path / 'products' / 'nircam' / 'cosmos'}
    found = nc.discover_mosaics(dirs, 'cosmos', ['f444w'])        # no profile: NIRCam
    sci = next(m for m in found if m['extension'] == 'sci')
    assert sci['storage_key'] == f'data/products/nircam/cosmos/f444w/{base}_sci.fits.gz'
    assert sci['instrument'] == 'nircam'
    kinds = {parse_key(t.r2_key).product_type
             for t in nc.discover_mosaic_thumbnail_tasks(found, 'cosmos')}
    assert kinds == {'nircam_mosaic_thumbnail', 'nircam_mosaic_quicklook'}


def test_hst_thumbnails_have_their_own_kinds(tmp_path):
    base = _mosaic_on_disk(tmp_path, 'acswfc', 'cosmos', 'f814w', 'A1')
    dirs = {'products': tmp_path / 'products' / 'acswfc' / 'cosmos'}
    found = nc.deployable_mosaics(
        nc.discover_mosaics(dirs, 'cosmos', ['f814w'], nc.imaging_profile('acswfc')))
    tasks = nc.discover_mosaic_thumbnail_tasks(found, 'cosmos')
    assert sorted(t.r2_key for t in tasks) == [
        f'data/products/acswfc/cosmos/f814w/{base}_quicklook.png',
        f'data/products/acswfc/cosmos/f814w/{base}_thumb.png']
    assert {parse_key(t.r2_key).product_type for t in tasks} == {
        'acswfc_mosaic_thumbnail', 'acswfc_mosaic_quicklook'}


@pytest.mark.parametrize('key, kind', [
    ('data/products/acswfc/cosmos/f814w/mosaic_acswfc_f814w_cosmos_30mas_A1_thumb.png', 'acswfc_mosaic_thumbnail'),
    ('data/products/wfc3ir/cosmos/f160w/mosaic_wfc3ir_f160w_cosmos_30mas_A1_quicklook.png', 'wfc3ir_mosaic_quicklook'),
    ('data/products/acswfc/cosmos/f814w/mosaic_acswfc_f814w_cosmos_30mas_A1_sci.fits.gz', 'acswfc_mosaic'),
    ('data/products/acswfc/cosmos/f814w/mosaic_acswfc_f814w_cosmos_30mas_A1_manifest.json', 'acswfc_mosaic'),
    # the per-exposure triage PNGs keep their kinds
    ('data/products/acswfc/cosmos/f814w/j8pu6eigq_flc_preview.png', 'acswfc_exposure_preview'),
    ('data/products/acswfc/cosmos/f814w/j8pu6eigq_flc_full.png', 'acswfc_exposure_full'),
])
def test_layout_kinds_of_the_hst_products(key, kind):
    assert parse_key(key).product_type == kind


# ---------------------------------------------------------------------------
# the index rows
# ---------------------------------------------------------------------------

def _row(instrument, **over):
    row = {'instrument': instrument, 'field': 'cosmos', 'tile': 'A1', 'filter': 'f814w',
           'pixel_scale': '30mas', 'extension': 'sci', 'epoch': '',
           'file_path': f'data/products/{instrument}/cosmos/f814w/x_sci.fits.gz',
           'file_size': 10, 'deploy_status': 'published', 'deployment_id': 7}
    row.update(over)
    return row


def test_the_same_slot_under_another_instrument_is_another_row():
    client = FakePostgrest(tables={'mosaics': [_row('nircam', deploy_status='draft', deployment_id=3)]})
    assert nc._upsert_mosaics(client, [_row('acswfc')]) == 1
    rows = client.rows('mosaics')
    assert sorted(r['instrument'] for r in rows) == ['acswfc', 'nircam']
    # NIRCam's row of the same field / tile / filter / scale / extension is untouched
    nircam = next(r for r in rows if r['instrument'] == 'nircam')
    assert nircam['deploy_status'] == 'draft' and nircam['deployment_id'] == 3
    assert client.upserts[-1].on_conflict == 'instrument,field,tile,filter,pixel_scale,extension,epoch'

    # a redeploy of the same instrument replaces its own row
    nc._upsert_mosaics(client, [_row('acswfc', deployment_id=9)])
    acs = [r for r in client.rows('mosaics') if r['instrument'] == 'acswfc']
    assert len(acs) == 1 and acs[0]['deployment_id'] == 9


def test_a_row_without_its_instrument_is_refused():
    client = FakePostgrest(tables={'mosaics': []})
    row = _row('acswfc')
    del row['instrument']
    with pytest.raises(ValueError):
        nc._upsert_mosaics(client, [row])
    assert client.rows('mosaics') == []


# ---------------------------------------------------------------------------
# deployments
# ---------------------------------------------------------------------------

def test_a_field_deployment_records_its_instrument():
    client = FakePostgrest(tables={'deployments': []})
    insert_deployment(client, field='cosmos', instrument='acswfc', status='draft')
    insert_deployment(client, observation='obs1', status='draft')
    rows = client.rows('deployments')
    assert rows[0]['instrument'] == 'acswfc' and rows[0]['field'] == 'cosmos'
    assert 'instrument' not in rows[1]          # an observation deployment has none
    with pytest.raises(ValueError):
        insert_deployment(client, observation='obs1', instrument='nircam')


def test_provenance_takes_from_an_exposure_what_the_mosaic_lacks(tmp_path):
    """An HST mosaic carries the CAMPFIRE version only; the calibration
    software's version and the CRDS context are on the exposures."""
    from astropy.io import fits

    fdir = tmp_path / 'products' / 'acswfc' / 'cosmos' / 'f814w'
    fdir.mkdir(parents=True)
    base = 'mosaic_acswfc_f814w_cosmos_30mas_A1'
    (fdir / f'{base}_manifest.json').write_text(json.dumps({
        'mosaic_name': base, 'field': 'cosmos', 'filter': 'f814w', 'tile': 'A1', 'pixel_scale': '30mas'}))
    hdr = fits.Header()
    hdr['CMPFRVER'] = '1.2.3'
    fits.PrimaryHDU(header=hdr).writeto(fdir / f'{base}_i2d.fits')
    hdr = fits.Header()
    hdr['CMPFRVER'] = '9.9.9'              # the mosaic's own value wins
    hdr['CAL_VER'] = '10.4.0'
    hdr['CRDS_CTX'] = 'hst_1234.pmap'
    fits.PrimaryHDU(header=hdr).writeto(fdir / 'jtst01a0q_flc.fits')

    dirs = {'products': tmp_path / 'products' / 'acswfc' / 'cosmos'}
    profile = nc.imaging_profile('acswfc')
    assert nc._read_field_provenance(dirs, 'cosmos', ['f814w'], profile=profile) == \
        ('1.2.3', '10.4.0', 'hst_1234.pmap')
    # with the mosaics left out of the deploy, the exposure speaks for itself
    assert nc._read_field_provenance(dirs, 'cosmos', ['f814w'], prefer_mosaic=False,
                                     profile=profile) == ('9.9.9', '10.4.0', 'hst_1234.pmap')


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------

def test_a_field_push_covers_every_instrument_on_the_machine(monkeypatch):
    from campfire.deploy import push as push_mod
    seen = []

    def fake(field, config, instrument, **kw):
        seen.append((instrument, kw['dry_run_note']))
        return {'planned': 2, 'pushed': 1, 'failed': 0, 'skipped': 1}

    monkeypatch.setattr(push_mod, '_push_field_instrument', fake)
    monkeypatch.setattr('campfire.deploy.nircam.instruments_on_disk',
                        lambda field: ['nircam', 'acswfc'])
    total = push_mod.push_field('cosmos', {}, dry_run=True)
    # one after another, the dry-run note once at the end, the counts summed
    assert seen == [('nircam', False), ('acswfc', True)]
    assert total == {'planned': 4, 'pushed': 2, 'failed': 0, 'skipped': 2}

    seen.clear()
    push_mod.push_field('cosmos', {}, instrument='wfc3ir')
    assert [s[0] for s in seen] == ['wfc3ir']

    # a field with nothing on this machine is still asked for as NIRCam's
    seen.clear()
    monkeypatch.setattr('campfire.deploy.nircam.instruments_on_disk', lambda field: [])
    push_mod.push_field('cosmos', {})
    assert [s[0] for s in seen] == ['nircam']


# ---------------------------------------------------------------------------
# the local index mirrors HST mosaics
# ---------------------------------------------------------------------------

def test_hst_mosaics_are_finals():
    assert {'nircam_mosaic', 'acswfc_mosaic', 'wfc3ir_mosaic'} <= set(FINAL_PRODUCT_TYPES)


def _store(meta=None, storage_cursor='2026-01-03T00:00:00Z'):
    store = MagicMock()
    held = dict(meta or {})
    store.get_meta.side_effect = lambda k: held.get(k)
    store.set_meta.side_effect = lambda k, v: held.__setitem__(k, v)
    for name in ('objects', 'spectra', 'photometry', 'line_fits'):
        getattr(store, f'get_max_{name}_updated_at').return_value = '2026-01-01T00:00:00Z'
    store.get_max_storage_updated_at.return_value = storage_cursor
    for name in ('objects', 'spectra', 'storage_objects', 'photometry', 'line_fits'):
        getattr(store, f'upsert_{name}').side_effect = lambda rows: len(rows)
    store.upsert_tags.side_effect = lambda data: len(data)
    store.drop_unmirrored_storage_rows.return_value = 0
    store.delete_storage_objects_by_ids.side_effect = lambda ids: {'purged': 0, 'orphaned_files': []}
    store.delete_objects_by_ids.side_effect = lambda ids: 0
    store.delete_spectra_by_ids.side_effect = lambda ids: 0
    store.get_stale_objects.return_value = []
    store.get_synced_observations.return_value = []
    store._conn.execute.return_value.fetchone.return_value = [0]
    return store, held


def _api(storage_calls):
    api = MagicMock()

    def storage(updated_since=None, on_page_complete=None, product_types=None, **_kw):
        storage_calls.append((updated_since, tuple(product_types or ())))
        rows = [{'storage_key': f'k-{t}'} for t in (product_types or ())] if updated_since is None else []
        return SyncStream(rows, len(rows), [])

    empty = lambda updated_since=None, on_page_complete=None, **_kw: SyncStream([], 0, [])  # noqa: E731
    api.fetch_all_storage.side_effect = storage
    for name in ('objects', 'spectra', 'photometry', 'line_fits'):
        getattr(api, f'fetch_all_{name}').side_effect = empty
    api.fetch_tags.return_value = []
    return api


@pytest.fixture
def _no_export(monkeypatch):
    monkeypatch.setattr('campfire.db.export.export_catalogs', lambda store, meta_dir: (0, 0))


def test_an_existing_index_walks_a_newly_mirrored_kind_once(_no_export):
    """An index synced before the HST mosaics were mirrored has a storage cursor
    newer than their registry rows. Its next sync fetches those kinds whole;
    the one after does not."""
    store, held = _store()                       # no record of what was mirrored: the old pair
    assert _newly_mirrored_types(store) == ['acswfc_mosaic', 'wfc3ir_mosaic']

    calls = []
    sync_metadata(_api(calls), store, Path('/tmp/meta'), show_progress=False, full=False)
    assert calls[0] == ('2026-01-03T00:00:00Z', tuple(FINAL_PRODUCT_TYPES))   # the incremental walk
    assert calls[1] == (None, ('acswfc_mosaic', 'wfc3ir_mosaic'))             # the new kinds, whole
    assert len(calls) == 2
    upserted = [c.args[0] for c in store.upsert_storage_objects.call_args_list]
    assert [{'storage_key': 'k-acswfc_mosaic'}, {'storage_key': 'k-wfc3ir_mosaic'}] in upserted
    assert json.loads(held['mirrored_product_types']) == sorted(FINAL_PRODUCT_TYPES)

    calls.clear()
    sync_metadata(_api(calls), store, Path('/tmp/meta'), show_progress=False, full=False)
    assert calls == [('2026-01-03T00:00:00Z', tuple(FINAL_PRODUCT_TYPES))]


def test_a_full_sync_needs_no_extra_walk(_no_export):
    store, held = _store(storage_cursor=None)
    calls = []
    sync_metadata(_api(calls), store, Path('/tmp/meta'), show_progress=False, full=True,
                  use_snapshot=False)
    assert calls == [(None, tuple(FINAL_PRODUCT_TYPES))]
    assert json.loads(held['mirrored_product_types']) == sorted(FINAL_PRODUCT_TYPES)


# ---------------------------------------------------------------------------
# release components
# ---------------------------------------------------------------------------

def test_a_release_component_is_one_instrument_of_one_field():
    comps = components_from_options(
        nirspec=True, fields=('egs', 'acswfc/cosmos', 'cosmos', 'wfc3ir/cosmos', 'egs'))
    # a bare field is its NIRCam component; duplicates collapse; order is stable
    assert comps == ['nirspec', 'acswfc/cosmos', 'nircam/cosmos', 'nircam/egs', 'wfc3ir/cosmos']
    assert components_from_options(nirspec=False, fields=('nircam/cosmos',)) == ['nircam/cosmos']
    for bad in ('miri/cosmos', 'acswfc/', 'acswfc/cosmos/x'):
        with pytest.raises(ValueError):
            components_from_options(nirspec=False, fields=(bad,))
