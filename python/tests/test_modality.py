"""The storage-plane vocabulary (campfire/modality.py) and the narrowing it drives.

`campfire pull` / `campfire push` take a modality first; products default to the
finals for pull and to everything for push; narrowers only narrow; `--tile`
narrows mosaics by the tile their name (pull) or manifest (push) carries.
"""

from pathlib import Path

import click
import pytest

from campfire.db.store import LocalStore
from campfire.modality import (
    IMAGING,
    SPECTROSCOPY,
    migration_hint,
    mosaic_tile,
    resolve_instruments,
    resolve_products,
)


# --- products / instruments --------------------------------------------------

def test_pull_defaults_to_the_finals_push_to_everything():
    assert resolve_products(SPECTROSCOPY, ['nirspec'], (), verb='pull') == ['spectra']
    assert resolve_products(IMAGING, ['nircam'], (), verb='pull') == ['mosaics']
    assert resolve_products(SPECTROSCOPY, ['nirspec'], (), verb='push') == ['spectra', 'exposures', 'rates']
    assert resolve_products(IMAGING, ['nircam'], (), verb='push') == ['mosaics', 'exposures', 'expmaps']


def test_products_accept_commas_and_spaces_and_map_to_layout_types():
    names = resolve_products(IMAGING, ['nircam'], ('exposures,expmaps', 'EXPOSURES'), verb='pull')
    assert names == ['exposures', 'expmaps']
    assert IMAGING.pull_types(names) == ['nircam_exposure', 'nircam_expmap']
    assert SPECTROSCOPY.pull_types(['spectra', 'exposures']) == ['nirspec_spec', 'nirspec_spectrum_exposure']


def test_an_unknown_product_or_instrument_is_a_usage_error():
    with pytest.raises(click.UsageError, match='no product mosaics'):
        resolve_products(SPECTROSCOPY, ['nirspec'], ('mosaics',), verb='pull')
    with pytest.raises(click.UsageError, match='no instrument miri'):
        resolve_instruments(IMAGING, ('miri',))
    assert resolve_instruments(IMAGING, ()) == ['nircam']


def test_rates_are_pushed_but_not_pulled():
    assert resolve_products(SPECTROSCOPY, ['nirspec'], ('rates',), verb='push') == ['rates']
    with pytest.raises(click.UsageError, match='rates cannot be pulled'):
        resolve_products(SPECTROSCOPY, ['nirspec'], ('rates',), verb='pull')


def test_migration_hint_offers_both_modalities_for_a_bare_field():
    hint = migration_hint('pull', ['--field', 'nep', '--intermediate'])
    assert 'campfire pull spec --field nep --products spectra,exposures' in hint
    assert 'campfire pull im --field nep --products mosaics,exposures,expmaps' in hint
    # push carries rates in the spectroscopy rewrite (push can send them)
    assert '--products spectra,exposures,rates' in migration_hint('push', ['--obs', 'x', '--intermediate'])


# --- tiles -------------------------------------------------------------------

@pytest.mark.parametrize('field, name, tile', [
    ('cosmos', 'mosaic_nircam_f444w_cosmos_30mas_A1_sci.fits.gz', 'A1'),
    ('cosmos', 'mosaic_nircam_f444w_cosmos_30mas_A10_sci.fits.gz', 'A10'),
    ('nep', 'mosaic_nircam_f356w_nep_30mas_core_wht.fits.gz', 'core'),
    ('nep', 'mosaic_nircam_f356w_nep_30mas_E_ep2_sci.fits', 'E'),          # an epoch suffix
    ('goods-n', 'mosaic_nircam_f090w_goods-n_60mas_N_i2d.fits', 'N'),
    ('nep', 'jw05105001001_06101_00001_nrcb1.fits', None),                # an exposure
    ('nep', 'mosaic_nircam_f356w_cosmos_30mas_A1_sci.fits', None),        # another field
])
def test_mosaic_tile(field, name, tile):
    assert mosaic_tile(field, name) == tile


def _row(key, ptype, field='cosmos', filt='f444w'):
    return {'storage_key': key, 'content_hash': 'sha256:' + key[-12:], 'size_bytes': 1,
            'product_type': ptype, 'status': 'active', 'field': field, 'filter': filt}


def test_tiles_narrow_mosaics_only(tmp_path):
    store = LocalStore(tmp_path / 'campfire.db')
    base = 'data/products/nircam/cosmos/f444w/'
    store.upsert_storage_objects([
        _row(base + 'mosaic_nircam_f444w_cosmos_30mas_A1_sci.fits.gz', 'nircam_mosaic'),
        _row(base + 'mosaic_nircam_f444w_cosmos_30mas_A10_sci.fits.gz', 'nircam_mosaic'),
        _row(base + 'mosaic_nircam_f444w_cosmos_30mas_B2_sci.fits.gz', 'nircam_mosaic'),
        _row(base + 'jw01727001001_02101_00001_nrcalong.fits', 'nircam_exposure'),
    ])
    pending = store.get_pending_objects(
        fields=['cosmos'], product_types=['nircam_mosaic', 'nircam_exposure'], tiles=['A1'])
    names = sorted(Path(r['storage_key']).name for r in pending['cosmos'])
    assert names == ['jw01727001001_02101_00001_nrcalong.fits',
                     'mosaic_nircam_f444w_cosmos_30mas_A1_sci.fits.gz']
    store.close()


def test_stale_rows_carry_their_field(tmp_path):
    """`pull --stale` spans both modalities: a stale field-scoped mosaic must be
    scoped by its field (it has no observation)."""
    store = LocalStore(tmp_path / 'campfire.db')
    key = 'data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_A1_sci.fits.gz'
    store.upsert_storage_objects([_row(key, 'nircam_mosaic')])
    store.mark_object_synced(key, '/x', 'sha256:old')
    stale = store.get_stale_objects()
    assert [(s['field'], s['observation']) for s in stale] == [('cosmos', None)]
    store.close()


# --- push product selection ----------------------------------------------------

class _Stop(Exception):
    pass


@pytest.fixture()
def fake_field(tmp_path, monkeypatch):
    """A NIRCam field whose discovery steps return one task each, stopping the
    push at the Supabase client (after it has printed what it would plan)."""
    import campfire.deploy.nircam as nc
    import campfire.deploy.supabase as sup
    from campfire.deploy.r2 import UploadTask

    products = tmp_path / 'products'
    products.mkdir()
    monkeypatch.setattr(nc, '_resolve_nircam_dirs', lambda f: {'products': products})
    monkeypatch.setattr(nc, '_discover_filters', lambda d: ['f444w'])
    monkeypatch.setattr(nc, 'discover_exposures', lambda d, f: ['exp'])
    monkeypatch.setattr(nc, 'build_fits_upload_tasks', lambda f, e: [UploadTask(Path('e'), 'k/e', 'x')])
    monkeypatch.setattr(nc, 'build_upload_tasks', lambda d, f, fl: [(UploadTask(Path('p'), 'k/p', 'x'),)])
    monkeypatch.setattr(nc, 'discover_expmap_tasks', lambda d, f, fl: [UploadTask(Path('m'), 'k/m', 'x')])
    mosaics = [{'path': Path(t), 'storage_key': f'k/{t}', 'content_type': 'x', 'tile': t}
               for t in ('core', 'E')]
    monkeypatch.setattr(nc, 'discover_mosaics', lambda d, f, fl: mosaics)
    monkeypatch.setattr(nc, 'deployable_mosaics', lambda m: m)
    monkeypatch.setattr(nc, 'scope_mosaics_to_fields_toml', lambda m, f: m)

    def _stop(cfg):
        raise _Stop
    monkeypatch.setattr(sup, 'get_supabase_client', _stop)


@pytest.mark.parametrize('products, tiles, planned', [
    (None, None, 5),                     # everything: exposure + preview + expmap + 2 mosaics
    (['exposures'], None, 2),            # the exposure and its preview PNG
    (['expmaps'], None, 1),
    (['mosaics'], ['E'], 1),
    (['mosaics', 'expmaps'], ['core', 'E'], 3),
])
def test_push_field_products_and_tiles(fake_field, capsys, products, tiles, planned):
    from campfire.deploy.push import push_field
    with pytest.raises(_Stop):
        push_field('nep', {}, products=products, tiles=tiles)
    assert f'Tree products discovered: {planned}' in capsys.readouterr().out


def test_push_expmaps_include_the_field_layout_plot(fake_field, tmp_path, capsys):
    """`expmaps` means the coverage maps and the layout plot drawn from them, in
    push as in `campfire deploy --field --products expmaps`."""
    from campfire.deploy.push import push_field
    (tmp_path / 'products' / 'nep_layout.png').write_bytes(b'png')
    with pytest.raises(_Stop):
        push_field('nep', {}, products=['expmaps'])
    assert 'Tree products discovered: 2' in capsys.readouterr().out


# --- push --tile: a tile no named field has is a usage error ------------------

@pytest.fixture()
def push_cli(fake_field, monkeypatch):
    import campfire.cli as main_cli
    import campfire.deploy.cli as dcli
    import campfire.deploy.push as push_mod

    pushed = []
    monkeypatch.setattr(dcli, '_push_config', lambda *a, **k: {})
    monkeypatch.setattr(push_mod, 'push_field', lambda field, cfg, **kw: pushed.append((field, kw)))
    main_cli._register_deploy_group()
    return main_cli.cli, pushed


def test_field_mosaic_tiles(fake_field):
    from campfire.deploy.push import field_mosaic_tiles
    assert field_mosaic_tiles('nep') == {'core', 'E'}
    assert field_mosaic_tiles('nep', ['f999w']) == set()


@pytest.mark.parametrize('products', [[], ['--products', 'mosaics']])
def test_push_unknown_tile_is_a_usage_error_before_any_upload(push_cli, products):
    from click.testing import CliRunner
    cli, pushed = push_cli
    res = CliRunner().invoke(cli, ['push', 'im', '--field', 'nep', '--tile', 'core', 'Q', *products])
    assert res.exit_code == 2
    assert 'No mosaics for tile(s) Q' in res.output and 'available: E, core' in res.output
    assert pushed == []


def test_push_known_tile_proceeds(push_cli):
    from click.testing import CliRunner
    cli, pushed = push_cli
    res = CliRunner().invoke(cli, ['push', 'im', '--field', 'nep', '--tile', 'E'])
    assert res.exit_code == 0, res.output
    assert [(f, kw['tiles']) for f, kw in pushed] == [('nep', ['E'])]


# --- pull im --all: fields known only by their intermediates -------------------

def test_pull_imaging_all_discovers_fields_without_mosaics(tmp_path, monkeypatch):
    """The catalog sync mirrors finals only, so a field deployed with
    --no-mosaics has no row until its exposures are indexed; --all must index
    field-wide and take its roster from what came back."""
    from click.testing import CliRunner
    import campfire.cli as main_cli
    import campfire.sync as sync_mod

    store = LocalStore(tmp_path / 'campfire.db')
    store.upsert_storage_objects([
        _row('data/products/nircam/cosmos/f444w/mosaic_nircam_f444w_cosmos_30mas_A1_sci.fits.gz',
             'nircam_mosaic')])
    monkeypatch.setattr(store, 'get_synced_observations', lambda: ['obs1'])
    monkeypatch.setattr(store, 'close', lambda: None)
    refreshed, verified = [], []

    def _refresh(api, st, *, product_types, observations=None, fields=None, show_progress=False):
        refreshed.append(fields)
        st.upsert_storage_objects([
            _row(f'data/products/nircam/{f}/f444w/jw0_{f}_nrcalong.fits', 'nircam_exposure', field=f)
            for f in ('cosmos', 'nep')])
        return {'rows': 2, 'purged': 0, 'orphaned_files': []}

    def _verify(root, **kw):
        verified.append(kw['fields'])
        raise _Stop

    monkeypatch.setattr(store, 'verify_local_objects', _verify)
    monkeypatch.setattr(sync_mod, 'refresh_storage_scope', _refresh)
    monkeypatch.setattr(main_cli, 'resolve_base_url', lambda: 'http://x')
    monkeypatch.setattr(main_cli, 'APISession', lambda **kw: None)
    monkeypatch.setattr(main_cli, 'APIClient', lambda s: None)
    monkeypatch.setattr(main_cli, '_open_store', lambda: store)
    monkeypatch.setattr(main_cli, '_sync_before_pull', lambda api, st: None)
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))

    res = CliRunner().invoke(main_cli.cli, ['pull', 'im', '--all', '--products', 'exposures'])
    assert isinstance(res.exception, _Stop), res.output
    assert refreshed == [None]
    assert verified == [['cosmos', 'nep']]
