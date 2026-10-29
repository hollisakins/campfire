"""The release store (P5-1): fetching, verifying, merging and rebuilding
(docs/dr-format-v1.md "Local store")."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from campfire.db.store import LocalStore, SCHEMA_VERSION
from campfire.dr_store import (
    export_filename, load_release, open_store, read_release_json, release_dir,
)
from campfire.exceptions import DRLoadError

TAG = 'DR-t'


# ---------------------------------------------------------------------------
# a release as the files route hands it out
# ---------------------------------------------------------------------------

def gz_bytes(rows):
    import io
    raw = io.BytesIO()
    with gzip.GzipFile(filename='', fileobj=raw, mode='wb', compresslevel=6, mtime=0) as gz:
        for r in rows:
            gz.write(json.dumps(r, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
            gz.write(b'\n')
    return raw.getvalue()


def spectrum(i, program, obs, target, obj_name, obj_id, grating='PRISM', snr=10.0, exp=1000.0):
    return {'id': i, 'spectrum_id': f'{obs}_{grating.lower()}_{target}', 'target_id': target,
            'object_id': obj_name, 'object_db_id': obj_id, 'grating': grating,
            'fits_path': f'spectra/{obs}/{obs}_{grating.lower()}_{target}_spec.fits',
            'file_hash': f'sha256:live{i}', 'science_hash': f'sha256:sci{i}', 'file_size': 100 + i,
            'signal_to_noise': snr, 'exposure_time': exp, 'cfpipe_version': '1.0.0',
            'crds_context': 'jwst_1234.pmap', 'jwst_version': '1.17.0', 'date_obs': '2025-01-01',
            'reduced_at': '2025-02-01T00:00:00Z', 'redshift_auto': 2.0, 'chi2_min': 1.0,
            'confidence': 0.9, 'dq_flags': 0, 'program_slug': program, 'observation': obs,
            'field': 'uds'}


def objects_row(obj_id, name, program, *, n_spectra, gratings, observations, snr, z_source=None,
                reviewed=None):
    return {'id': obj_id, 'object_id': name, 'field': 'uds', 'ra': 34.0 + obj_id / 1000, 'dec': -5.0,
            'programs': [program], 'evidence_programs': [program],
            'observations': observations, 'gratings': gratings, 'n_targets': 1, 'n_spectra': n_spectra,
            'max_snr': snr, 'max_exposure_time': 1000.0, 'photo_z': None, 'photo_z_err_lo': None,
            'photo_z_err_hi': None, 'has_photometry': False, 'z_source_spectrum_id': z_source,
            'redshift_auto': 2.0, 'redshift_inspected': 2.1, 'redshift': 2.1, 'redshift_quality': 4,
            'inspected_used_auto': False, 'last_inspected_at': '2025-03-01T00:00:00Z',
            'last_inspected_by': None, 'version': 3, 'is_active': True, 'needs_review': False,
            'reviewed_spectrum_ids': reviewed}


def line_fit(spec, lines, stale=False):
    return {'spectrum_id': spec['id'], 'spectrum_name': spec['spectrum_id'], 'target_id': spec['target_id'],
            'grating': spec['grating'], 'program_slug': spec['program_slug'], 'observation': spec['observation'],
            'object_id': spec['object_id'], 'object_version': 3, 'z_used': 2.1, 'z_source': 'inspected',
            'z_quality': 4, 'z_fit': 2.1, 'z_fit_err': 0.001, 'dv': 0.0, 'dv_err': 1.0, 'sigma_v': 100.0,
            'sigma_v_err': 10.0, 'kin_source': 'fit', 'n_lines': len(lines), 'n_detected': len(lines),
            'n_broad': 0, 'chi2': 1.0, 'dof': 10, 'lines': lines, 'fit_version': '1', 'cfpipe_version': '1.0.0',
            'f_lsf': 1.0, 'spectrum_hash': spec['file_hash'], 'spectrum_science_hash': spec['science_hash'],
            'fitted_at': '2025-04-01T00:00:00Z', 'stale_redshift': stale, 'stale_spectrum': False}


def manifest_row(spec, *, stamped=True):
    key = spec['fits_path']
    return {'storage_key': key, 'component': 'nirspec', 'program_slug': spec['program_slug'],
            'field': None, 'product_type': 'nirspec_spec', 'spectrum_id': spec['spectrum_id'],
            'backend': 'osn', 'bucket': 'campfire', 'content_hash': spec['file_hash'],
            'science_hash': spec['science_hash'], 'size_bytes': spec['file_size'],
            'archive_key': f"archive/{spec['science_hash'][7:]}/{key}",
            'byte_state': 'stamped' if stamped else 'copied',
            'stamped_hash': f"sha256:stamped{spec['id']}" if stamped else None}


def build_release():
    """Two programs, three objects: X in both (one spectrum each), Y in pa only,
    Z in pb only; a fit per spectrum (one stale); the finals in the manifest
    plus one field mosaic."""
    sa = spectrum(1, 'pa', 'obs_a', 'tX', 'OBJ-X', 100, snr=8.0)
    sb = spectrum(2, 'pb', 'obs_b', 'tX2', 'OBJ-X', 100, grating='G395M', snr=12.0, exp=2000.0)
    sy = spectrum(3, 'pa', 'obs_a', 'tY', 'OBJ-Y', 101, snr=5.0)
    sz = spectrum(4, 'pb', 'obs_b', 'tZ', 'OBJ-Z', 102, snr=6.0)
    files = {
        ('objects', 'pa'): [
            objects_row(100, 'OBJ-X', 'pa', n_spectra=1, gratings=['PRISM'], observations=['obs_a'], snr=8.0,
                        z_source=None, reviewed=[1]),
            objects_row(101, 'OBJ-Y', 'pa', n_spectra=1, gratings=['PRISM'], observations=['obs_a'], snr=5.0),
        ],
        ('objects', 'pb'): [
            objects_row(100, 'OBJ-X', 'pb', n_spectra=1, gratings=['G395M'], observations=['obs_b'], snr=12.0,
                        z_source=2, reviewed=[2]),
            objects_row(102, 'OBJ-Z', 'pb', n_spectra=1, gratings=['PRISM'], observations=['obs_b'], snr=6.0),
        ],
        ('spectra', 'pa'): [sa, sy],
        ('spectra', 'pb'): [sb, sz],
        ('line_fits', 'pa'): [line_fit(sa, {'Halpha': {'flux': 1e-18, 'snr': 9}}),
                              line_fit(sy, {'Halpha': {'flux': 2e-18, 'snr': 3}}, stale=True)],
        ('line_fits', 'pb'): [line_fit(sb, {'OIII5007': {'flux': 3e-18, 'snr': 20}})],
        ('photometry', 'pa'): [{'id': 7, 'object_id': 100, 'programs': ['pa'], 'field': 'uds',
                                'catalog_name': 'cat', 'catalog_id': 'c1', 'ra': 34.1, 'dec': -5.0,
                                'match_distance_arcsec': 0.1, 'photometry': {'f444w': {'flux': 1.0}},
                                'photo_z': 2.0, 'photo_z_err_lo': 1.9, 'photo_z_err_hi': 2.1, 'has_pz': True}],
        ('photometry', 'pb'): [{'id': 7, 'object_id': 100, 'programs': ['pb'], 'field': 'uds',
                                'catalog_name': 'cat', 'catalog_id': 'c1', 'ra': 34.1, 'dec': -5.0,
                                'match_distance_arcsec': 0.1, 'photometry': {'f444w': {'flux': 1.0}},
                                'photo_z': 2.0, 'photo_z_err_lo': 1.9, 'photo_z_err_hi': 2.1, 'has_pz': True}],
        ('manifest', 'pa'): [manifest_row(sa), manifest_row(sy, stamped=False)],
        ('manifest', 'pb'): [manifest_row(sb), manifest_row(sz)],
        ('manifest', 'nircam/uds'): [{'storage_key': 'nircam/uds/f444w_mosaic.fits', 'component': 'nircam/uds',
                                      'program_slug': None, 'field': 'uds', 'product_type': 'nircam_mosaic',
                                      'spectrum_id': None, 'backend': 'osn', 'bucket': 'campfire',
                                      'content_hash': 'sha256:mosaic', 'science_hash': None, 'size_bytes': 5,
                                      'archive_key': 'archive/mosaic/nircam/uds/f444w_mosaic.fits',
                                      'byte_state': 'copied', 'stamped_hash': None}],
    }
    return files


class FakeRelease:
    """The files route + the presigned urls, in memory."""

    def __init__(self, files, *, format_version=1, state='published'):
        self.blobs = {}
        self.entries = []
        for (stream, scope), rows in files.items():
            data = gz_bytes(rows)
            url = f'https://osn.example/{stream}/{scope}?sig=1'
            self.blobs[url] = data
            self.entries.append({'stream': stream, 'scope': scope, 'rows': len(rows), 'size': len(data),
                                 'sha256': 'sha256:' + hashlib.sha256(data).hexdigest(), 'url': url})
        self.format_version = format_version
        self.state = state
        self.fetches = []
        self.route_calls = 0

    # the session: GET /dr/<tag>/files
    def get(self, path, timeout=None):
        self.route_calls += 1
        assert path == f'/dr/{TAG}/files'
        payload = {'tag': TAG, 'state': self.state, 'format_version': self.format_version,
                   'components': ['nirspec', 'nircam/uds'], 'counts': {'spectra': 4}, 'frozen_at': 'f',
                   'published_at': 'p', 'doi': '10.1/x', 'notes': None, 'known_issues': None,
                   'complete': True, 'files': [dict(e) for e in self.entries]}

        class R:
            status_code = 200
            def json(self_inner): return payload
            def raise_for_status(self_inner): pass
        return R()

    # the download
    def fetch(self, url, dest: Path):
        self.fetches.append(url)
        dest.write_bytes(self.blobs[url])


@pytest.fixture
def release():
    return FakeRelease(build_release())


def load(release, tmp_path, **kw):
    return load_release(release, TAG, data_dir=tmp_path, fetch=release.fetch, **kw)


# ---------------------------------------------------------------------------

def test_export_filename_matches_the_server_key_rule():
    assert export_filename('rubies') == 'rubies.jsonl.gz'
    assert export_filename('nircam/uds') == 'nircam_uds-' + hashlib.md5(b'nircam/uds').hexdigest()[:8] + '.jsonl.gz'
    # names that sanitise alike stay distinct
    assert export_filename('CAT v1') != export_filename('CAT_v1')
    # a name that is only dots is not a path element
    assert '/' not in export_filename('..') and export_filename('..') != '...jsonl.gz'


def test_release_dir_refuses_a_tag_that_is_not_a_name(tmp_path):
    with pytest.raises(DRLoadError):
        release_dir(tmp_path, '../x')
    assert release_dir(tmp_path, 'DR2026.1') == tmp_path / 'dr' / 'DR2026.1'


def test_loads_every_file_into_one_store_merged_by_id(release, tmp_path):
    rep = load(release, tmp_path)
    assert rep.rebuilt and len(rep.fetched) == len(release.entries) and rep.kept == []
    assert rep.counts == {'spectra': 4, 'objects': 3, 'line_fits': 3, 'photometry': 1, 'manifest': 5}
    root = tmp_path / 'dr' / TAG
    assert (root / 'exports' / 'objects' / 'pa.jsonl.gz').exists()
    assert (root / 'products').is_dir()
    rj = read_release_json(root)
    assert rj['tag'] == TAG and rj['doi'] == '10.1/x' and rj['schema_version'] == SCHEMA_VERSION
    assert {(f['stream'], f['scope']) for f in rj['files']} == {(e['stream'], e['scope']) for e in release.entries}

    store = open_store(root)
    try:
        x = store.get_object('OBJ-X')
        # one row, both programs, aggregates recomputed over the spectra held
        assert sorted(x['programs']) == ['pa', 'pb']
        assert x['n_spectra'] == 2 and x['n_targets'] == 2
        assert x['gratings'] == ['G395M', 'PRISM'] and x['observations'] == ['obs_a', 'obs_b']
        assert x['max_snr'] == 12.0 and x['max_exposure_time'] == 2000.0
        assert sorted(x['member_target_ids']) == ['tX', 'tX2']
        assert [s['spectrum_id'] for s in x['spectra']] == ['obs_a_prism_tX', 'obs_b_g395m_tX2']
        y = store.get_object('OBJ-Y')
        assert y['programs'] == ['pa'] and y['n_spectra'] == 1
        # the spectrum resolves its file through the manifest-made storage row:
        # the hash of the bytes a reader gets (the stamped copy)
        s = store.get_spectrum('obs_a_prism_tX')
        assert s['fits_path'] == 'spectra/obs_a/obs_a_prism_tX_spec.fits'
        assert s['file_hash'] == 'sha256:stamped1' and s['file_size'] == 101 and s['local_path'] is None
        sy = store.get_spectrum('obs_a_prism_tY')
        assert sy['file_hash'] == 'sha256:live3'  # copied, not stamped: the live bytes
        # the manifest is kept whole for the byte pull
        m = store._conn.execute("SELECT * FROM release_manifest WHERE storage_key = ?",
                                ('spectra/obs_a/obs_a_prism_tX_spec.fits',)).fetchone()
        assert m['archive_key'] == 'archive/sci1/spectra/obs_a/obs_a_prism_tX_spec.fits'
        assert m['byte_state'] == 'stamped' and m['stamped_hash'] == 'sha256:stamped1'
        mosaic = store._conn.execute("SELECT * FROM storage_objects WHERE storage_key = ?",
                                     ('nircam/uds/f444w_mosaic.fits',)).fetchone()
        assert mosaic['product_type'] == 'nircam_mosaic' and mosaic['field'] == 'uds' and mosaic['instrument'] == 'nircam'
        # photometry: one row for the object in both files, keyed by the object's name
        p = store.get_photometry_for_object('OBJ-X')
        assert p['id'] == 7 and p['photometry'] == {'f444w': {'flux': 1.0}}
        assert store._conn.execute("SELECT count(*) FROM catalog_matches").fetchone()[0] == 1
        # line fits carry the spectrum's field; the stale one is marked
        fits = {f['spectrum_name']: f for f in store.query_line_fits()}
        assert fits['obs_a_prism_tX']['field'] == 'uds' and fits['obs_a_prism_tX']['lines'] == {'Halpha': {'flux': 1e-18, 'snr': 9}}
        assert fits['obs_a_prism_tY']['stale_redshift'] is True
        assert [f['spectrum_name'] for f in store.query_line_fits(exclude_stale=True)] == ['obs_a_prism_tX', 'obs_b_g395m_tX2']
        assert store.get_meta('release_tag') == TAG
    finally:
        store.close()


def test_a_second_load_fetches_nothing_and_keeps_the_store(release, tmp_path):
    load(release, tmp_path)
    db = tmp_path / 'dr' / TAG / 'meta' / 'campfire.db'
    before = db.stat().st_mtime_ns
    release.fetches.clear()
    rep = load(release, tmp_path)
    assert release.fetches == [] and rep.fetched == [] and len(rep.kept) == len(release.entries)
    assert rep.rebuilt is False and rep.counts['spectra'] == 4
    assert db.stat().st_mtime_ns == before


def test_a_file_that_does_not_verify_is_refused_and_leaves_no_partial(release, tmp_path):
    bad = release.entries[0]
    bad['sha256'] = 'sha256:' + '0' * 64
    with pytest.raises(DRLoadError, match=f"{bad['stream']}/{bad['scope']}: sha256"):
        load(release, tmp_path)
    root = tmp_path / 'dr' / TAG
    assert not (root / 'meta' / 'campfire.db').exists()
    assert not list(root.rglob('*.part'))
    assert not (root / bad['stream'] / export_filename(bad['scope'])).exists()
    # a size mismatch is refused the same way
    bad['sha256'] = 'sha256:' + hashlib.sha256(release.blobs[bad['url']]).hexdigest()
    bad['size'] = 1
    with pytest.raises(DRLoadError, match='size'):
        load(release, tmp_path)


def test_a_kept_file_that_no_longer_verifies_is_fetched_again(release, tmp_path):
    load(release, tmp_path)
    e = release.entries[0]
    path = tmp_path / 'dr' / TAG / e['stream'] / export_filename(e['scope'])
    path = tmp_path / 'dr' / TAG / 'exports' / e['stream'] / export_filename(e['scope'])
    path.write_bytes(b'garbage')
    release.fetches.clear()
    rep = load(release, tmp_path)
    assert release.fetches == [e['url']] and rep.fetched == [(e['stream'], e['scope'])]
    assert rep.rebuilt is False  # same file set: the store stands


def test_a_changed_file_set_rebuilds_the_store_from_the_kept_files(release, tmp_path):
    load(release, tmp_path)
    # the route now hands out one more program file (access widened): rebuild
    extra = gz_bytes([objects_row(103, 'OBJ-W', 'pc', n_spectra=0, gratings=[], observations=[], snr=None)])
    url = 'https://osn.example/objects/pc'
    release.blobs[url] = extra
    release.entries.append({'stream': 'objects', 'scope': 'pc', 'rows': 1, 'size': len(extra),
                            'sha256': 'sha256:' + hashlib.sha256(extra).hexdigest(), 'url': url})
    release.fetches.clear()
    rep = load(release, tmp_path)
    assert release.fetches == [url] and rep.rebuilt is True
    assert rep.counts['objects'] == 4
    store = open_store(tmp_path / 'dr' / TAG)
    try:
        w = store.get_object('OBJ-W')
        assert w['n_spectra'] == 0 and w['member_target_ids'] == []
    finally:
        store.close()


def test_a_schema_bump_rebuilds_without_fetching_and_keeps_what_is_on_disk(release, tmp_path):
    load(release, tmp_path)
    root = tmp_path / 'dr' / TAG
    # a product pulled earlier is recorded on its storage row
    store = open_store(root)
    store.mark_object_synced('spectra/obs_a/obs_a_prism_tX_spec.fits', 'nirspec/obs_a/obs_a_prism_tX_spec.fits',
                             'sha256:stamped1', 101, 1.5)
    store.close()
    # the client moved on: the store's schema version no longer matches
    conn = sqlite3.connect(str(root / 'meta' / 'campfire.db'))
    conn.execute("UPDATE _meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION - 1),))
    conn.commit()
    conn.close()
    release.fetches.clear()
    rep = load(release, tmp_path)
    assert release.fetches == [] and rep.rebuilt is True
    store = open_store(root)
    try:
        assert store.find_local_path('obs_a_prism_tX') == 'nirspec/obs_a/obs_a_prism_tX_spec.fits'
        row = store._conn.execute("SELECT local_file_hash, local_file_size FROM storage_objects WHERE spectrum_id = 'obs_a_prism_tX'").fetchone()
        assert tuple(row) == ('sha256:stamped1', 101)
    finally:
        store.close()


def test_an_unknown_format_version_is_refused_before_anything_is_written(tmp_path):
    release = FakeRelease(build_release(), format_version=2)
    with pytest.raises(DRLoadError, match='format version 2'):
        load(release, tmp_path)
    assert not (tmp_path / 'dr').exists()


def test_a_release_with_no_readable_file_is_refused(tmp_path):
    release = FakeRelease({})
    with pytest.raises(DRLoadError, match='no export file'):
        load(release, tmp_path)


def test_a_scope_that_is_not_key_safe_lands_under_its_sanitised_name(release, tmp_path):
    load(release, tmp_path)
    root = tmp_path / 'dr' / TAG / 'exports' / 'manifest'
    assert (root / export_filename('nircam/uds')).exists()
    assert not (root / 'nircam').exists()


def test_a_failed_rebuild_leaves_the_previous_store(release, tmp_path, monkeypatch):
    load(release, tmp_path)
    root = tmp_path / 'dr' / TAG
    # widen the file set so a rebuild is due, then make the load fail
    extra = gz_bytes([{'id': 9, 'object_id': 'OBJ-BAD'}])
    url = 'https://osn.example/objects/pc'
    release.blobs[url] = extra
    release.entries.append({'stream': 'objects', 'scope': 'pc', 'rows': 1, 'size': len(extra),
                            'sha256': 'sha256:' + hashlib.sha256(extra).hexdigest(), 'url': url})
    import campfire.dr_store as m

    def boom(*a, **k):
        raise RuntimeError('load failed')
    monkeypatch.setattr(m, '_load_into_store', boom)
    with pytest.raises(RuntimeError):
        load(release, tmp_path)
    store = open_store(root)
    try:
        assert store.get_meta('release_tag') == TAG and store.get_object('OBJ-X') is not None
    finally:
        store.close()
    assert not [p for p in (root / 'meta').iterdir() if p.name.startswith('campfire-db-')]
