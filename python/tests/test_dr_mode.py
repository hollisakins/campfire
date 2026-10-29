"""``Campfire(dr=...)`` (P5-1): a release is read from its local store and
never falls back to the live service (docs/design-data-releases.md §4.7)."""
from __future__ import annotations

import json
from unittest.mock import patch

import numpy as np
import pytest
from astropy.io import fits

from campfire.client import Campfire
from campfire.db.store import LocalStore
from campfire.exceptions import DRModeError

TAG = 'DR2026.0'


@pytest.fixture
def release_tree(tmp_path):
    """A loaded release under <root>/dr/<tag>/ (what load_release leaves)."""
    root = tmp_path / 'dr' / TAG
    (root / 'meta').mkdir(parents=True)
    (root / 'products').mkdir()
    store = LocalStore(root / 'meta' / 'campfire.db')
    store.upsert_objects([
        {'id': 1, 'object_id': 'OBJ-A', 'field': 'uds', 'ra': 34.0, 'dec': -5.0, 'redshift': 3.0,
         'redshift_auto': 3.0, 'redshift_inspected': 3.0, 'redshift_quality': 4, 'n_targets': 1,
         'n_spectra': 1, 'programs': ['pa'], 'gratings': ['PRISM'], 'observations': ['obs_a'],
         'member_target_ids': ['tA'], 'max_snr': 10.0, 'has_photometry': False, 'is_active': True},
        {'id': 2, 'object_id': 'OBJ-B', 'field': 'uds', 'ra': 34.1, 'dec': -5.1, 'redshift': 1.0,
         'redshift_auto': 1.0, 'redshift_inspected': None, 'redshift_quality': 0, 'n_targets': 1,
         'n_spectra': 1, 'programs': ['pa'], 'gratings': ['G395M'], 'observations': ['obs_a'],
         'member_target_ids': ['tB'], 'max_snr': 4.0, 'has_photometry': False, 'is_active': True},
    ])
    store.upsert_spectra([
        {'id': 10, 'spectrum_id': 'obs_a_prism_tA', 'target_id': 'tA', 'object_id': 'OBJ-A', 'grating': 'PRISM',
         'signal_to_noise': 10.0, 'program_slug': 'pa', 'observation': 'obs_a', 'field': 'uds'},
        {'id': 11, 'spectrum_id': 'obs_a_g395m_tB', 'target_id': 'tB', 'object_id': 'OBJ-B', 'grating': 'G395M',
         'signal_to_noise': 4.0, 'program_slug': 'pa', 'observation': 'obs_a', 'field': 'uds'},
    ])
    store.upsert_storage_objects([
        {'storage_key': 'spectra/obs_a/obs_a_prism_tA_spec.fits', 'content_hash': 'sha256:stampedA',
         'size_bytes': 10, 'product_type': 'nirspec_spec', 'instrument': 'nirspec', 'status': 'active',
         'observation': 'obs_a', 'field': 'uds', 'spectrum_id': 'obs_a_prism_tA', 'backend': 'osn', 'bucket': 'c'},
        {'storage_key': 'spectra/obs_a/obs_a_g395m_tB_spec.fits', 'content_hash': 'sha256:liveB',
         'size_bytes': 10, 'product_type': 'nirspec_spec', 'instrument': 'nirspec', 'status': 'active',
         'observation': 'obs_a', 'field': 'uds', 'spectrum_id': 'obs_a_g395m_tB', 'backend': 'osn', 'bucket': 'c'},
    ])
    store.upsert_line_fits([
        {'spectrum_id': 10, 'spectrum_name': 'obs_a_prism_tA', 'target_id': 'tA', 'grating': 'PRISM',
         'program_slug': 'pa', 'observation': 'obs_a', 'field': 'uds', 'object_id': 'OBJ-A', 'z_used': 3.0,
         'z_quality': 4, 'lines': {'Halpha': {'flux': 1e-18, 'flux_err': 1e-19, 'snr': 10}},
         'stale_redshift': False, 'stale_spectrum': False},
        {'spectrum_id': 11, 'spectrum_name': 'obs_a_g395m_tB', 'target_id': 'tB', 'grating': 'G395M',
         'program_slug': 'pa', 'observation': 'obs_a', 'field': 'uds', 'object_id': 'OBJ-B', 'z_used': 1.0,
         'z_quality': 3, 'lines': {'Halpha': {'flux': 2e-18, 'flux_err': 1e-19, 'snr': 20}},
         'stale_redshift': True, 'stale_spectrum': False},
    ])
    store.set_meta('release_tag', TAG)
    store.close()
    (root / 'release.json').write_text(json.dumps({'tag': TAG, 'state': 'published', 'doi': '10.1/x',
                                                    'counts': {'spectra': 2}, 'files': []}))
    return tmp_path


@pytest.fixture
def no_session():
    """The live session must never be built in release mode."""
    with patch('campfire.client.APISession', side_effect=AssertionError('APISession built in dr mode')) as m:
        yield m


def test_opens_the_release_store_without_credentials(release_tree, no_session):
    cf = Campfire(dr=TAG, data_dir=release_tree)
    assert cf.is_local and cf.dr == TAG
    assert cf.release['doi'] == '10.1/x' and cf.release['tag'] == TAG
    no_session.assert_not_called()


def test_a_release_not_on_this_machine_fails_closed(tmp_path, no_session):
    with pytest.raises(DRModeError, match=f'campfire pull --dr {TAG}'):
        Campfire(dr=TAG, data_dir=tmp_path)


def test_a_schema_mismatch_names_the_pull(release_tree, no_session):
    import sqlite3
    db = release_tree / 'dr' / TAG / 'meta' / 'campfire.db'
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE _meta SET value = '1' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(DRModeError, match='schema v1'):
        Campfire(dr=TAG, data_dir=release_tree)


def test_queries_answer_from_the_store_and_carry_the_channel(release_tree, no_session):
    cf = Campfire(dr=TAG, data_dir=release_tree)
    objs = cf.query_objects(redshift_range=(2, 4))
    assert list(objs['object_id']) == ['OBJ-A']
    assert objs.meta['channel'] == 'dr' and objs.meta['release'] == TAG
    spec = cf.query_spectra(gratings=['g395m'])
    assert list(spec['spectrum_id']) == ['obs_a_g395m_tB'] and spec.meta['channel'] == 'dr'
    assert spec['file_hash'][0] == 'sha256:liveB'
    assert cf.get_object('OBJ-A').object_id == 'OBJ-A'
    assert cf.get_spectrum('obs_a_prism_tA')['fits_path'] == 'spectra/obs_a/obs_a_prism_tA_spec.fits'
    empty = cf.query_objects(redshift_range=(8, 9))
    assert len(empty) == 0 and empty.meta['channel'] == 'dr'


def test_live_tables_say_live():
    with patch('campfire.client.APISession') as MockSession:
        MockSession.return_value.base_url = 'https://t/api/v1'
        with patch('campfire.client.APIClient') as MockApi:
            MockApi.return_value.query_objects.return_value = ([], {})
            with pytest.warns(UserWarning):
                cf = Campfire(data_dir='/nonexistent/campfire-root')
            assert cf.dr is None and cf.release is None
            assert cf.query_objects().meta['channel'] == 'live'


def test_lines_drop_stale_fits_by_default_on_a_release(release_tree, no_session):
    cf = Campfire(dr=TAG, data_dir=release_tree)
    t = cf.query_lines()
    assert list(t['spectrum_name']) == ['obs_a_prism_tA'] and t.meta['channel'] == 'dr'
    both = cf.query_lines(exclude_stale=False)
    assert sorted(both['spectrum_name']) == ['obs_a_g395m_tB', 'obs_a_prism_tA']


def test_the_live_client_keeps_stale_fits_by_default(tmp_path):
    (tmp_path / 'meta').mkdir()
    store = LocalStore(tmp_path / 'meta' / 'campfire.db')
    store.upsert_line_fits([
        {'spectrum_id': 1, 'spectrum_name': 's1', 'z_quality': 4, 'lines': {}, 'stale_redshift': True},
    ])
    store.close()
    with patch('campfire.client.APISession') as MockSession:
        MockSession.return_value.base_url = 'https://t/api/v1'
        cf = Campfire(data_dir=tmp_path)
        assert list(cf.query_lines()['spectrum_name']) == ['s1']


@pytest.mark.parametrize('call', [
    lambda cf: cf.query_objects(remote=True),
    lambda cf: cf.query_spectra(remote=True),
    lambda cf: cf.query_objects(q={'and': []}),
    lambda cf: list(cf.iter_spectra(remote=True)),
    lambda cf: cf.sync(),
    lambda cf: cf.download(observations=['obs_a']),
    lambda cf: cf.get_spectrum_data('obs_a_prism_tA'),
    lambda cf: cf.get_cutout('OBJ-A'),
])
def test_every_live_branch_fails_closed(release_tree, no_session, call):
    cf = Campfire(dr=TAG, data_dir=release_tree)
    with pytest.raises(DRModeError):
        call(cf)


def test_imaging_is_refused_even_when_the_shared_cache_holds_the_file(release_tree, no_session, monkeypatch):
    # the cutout cache under the data root is shared with live clients: a
    # release client must refuse before reading it, or live imagery leaks
    monkeypatch.setenv('CAMPFIRE_ROOT', str(release_tree))
    cache = release_tree / 'cutouts'
    cache.mkdir()
    (cache / 'OBJ-A_fov5.png').write_bytes(b'live png')
    (cache / 'OBJ-A_fov5_shutters.png').write_bytes(b'live png')
    cf = Campfire(dr=TAG, data_dir=release_tree)
    for call in (lambda: cf.get_cutout('OBJ-A'), lambda: cf.get_shutters('OBJ-A'),
                 lambda: cf.get_fits_cutout('uds', 34.0, -5.0), lambda: cf.get_cutout_figure('uds', 34.0, -5.0),
                 lambda: cf.get_shutters_region('OBJ-A'), lambda: cf.plot_cutout('OBJ-A')):
        with pytest.raises(DRModeError, match='live products'):
            call()


def test_metadata_comes_from_the_store(release_tree, no_session):
    cf = Campfire(dr=TAG, data_dir=release_tree)
    assert cf.get_fields() == ['uds'] and cf.get_gratings() == ['G395M', 'PRISM']
    assert cf.get_observations() == ['obs_a']
    assert list(cf.get_programs()['slug']) == ['pa']
    meta = cf.get_metadata()
    assert meta['doi'] == '10.1/x' and meta['fields'] == ['uds']
    assert len(cf.get_tags()) == 0


def _write_spectrum_fits(path):
    wave = np.linspace(1.0, 5.0, 10)
    cols = [fits.Column(name='wave', format='D', array=wave),
            fits.Column(name='flux', format='D', array=np.ones(10)),
            fits.Column(name='err', format='D', array=np.ones(10) * 0.1)]
    hdu = fits.BinTableHDU.from_columns(cols, name='SPEC1D')
    fits.HDUList([fits.PrimaryHDU(), hdu]).writeto(path)


def test_open_spectrum_reads_the_release_tree_and_never_the_service(release_tree, no_session):
    cf = Campfire(dr=TAG, data_dir=release_tree)
    with pytest.raises(DRModeError, match=f'campfire pull --dr {TAG}'):
        cf.open_spectrum('obs_a_prism_tA')
    # the pull lands the file and records it on the storage row
    root = release_tree / 'dr' / TAG
    rel = 'nirspec/obs_a/obs_a_prism_tA_spec.fits'
    dest = root / 'products' / rel
    dest.parent.mkdir(parents=True)
    _write_spectrum_fits(dest)
    store = LocalStore(root / 'meta' / 'campfire.db')
    store.mark_object_synced('spectra/obs_a/obs_a_prism_tA_spec.fits', rel, 'sha256:stampedA', 10, 1.0)
    store.close()
    cf = Campfire(dr=TAG, data_dir=release_tree)
    sd = cf.open_spectrum('obs_a_prism_tA')
    assert sd.spectrum_id == 'obs_a_prism_tA' and len(sd.wavelength) == 10
