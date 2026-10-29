"""Provenance files (P5-3, design §4.8): built from the local ledgers,
checked against the disk, the archive and a pulled release."""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire.db.store import LocalStore
from campfire.provenance import FORMAT, ProvenanceError, build, check, load

H_AAA = 'sha256:' + hashlib.sha256(b'AAA').hexdigest()


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    (tmp_path / 'meta').mkdir()
    store = LocalStore(tmp_path / 'meta' / 'campfire.db')
    store.upsert_objects([
        {'id': 1, 'object_id': 'OBJ-A', 'field': 'uds', 'ra': 34.0, 'dec': -5.0, 'redshift': 5.2, 'redshift_auto': 5.1,
         'redshift_inspected': 5.2, 'redshift_quality': 4, 'version': 3, 'programs': ['pa'], 'gratings': ['PRISM'],
         'observations': ['obs_a'], 'n_targets': 1, 'n_spectra': 1, 'is_active': True, 'lists': ['mylist']},
        {'id': 2, 'object_id': 'OBJ-B', 'field': 'uds', 'ra': 34.1, 'dec': -5.1, 'redshift': 1.0, 'redshift_auto': 1.0,
         'redshift_quality': 0, 'version': 1, 'programs': ['pa'], 'gratings': ['PRISM'], 'observations': ['obs_a'],
         'n_targets': 1, 'n_spectra': 1, 'is_active': True},
    ])
    store.upsert_spectra([
        {'id': 10, 'spectrum_id': 'obs_a_prism_t1', 'target_id': 't1', 'object_id': 'OBJ-A', 'grating': 'PRISM',
         'program_slug': 'pa', 'observation': 'obs_a', 'field': 'uds', 'science_hash': 'sha256:sciA',
         'cfpipe_version': '1.0.0', 'crds_context': 'jwst_1535.pmap'},
        {'id': 11, 'spectrum_id': 'obs_a_prism_t2', 'target_id': 't2', 'object_id': 'OBJ-B', 'grating': 'PRISM',
         'program_slug': 'pa', 'observation': 'obs_a', 'field': 'uds', 'science_hash': 'sha256:sciB',
         'cfpipe_version': '1.0.0', 'crds_context': 'jwst_1535.pmap'},
    ])
    store.upsert_storage_objects([
        {'storage_key': 'spectra/obs_a/obs_a_prism_t1_spec.fits', 'content_hash': H_AAA, 'size_bytes': 3,
         'product_type': 'nirspec_spec', 'status': 'active', 'observation': 'obs_a', 'spectrum_id': 'obs_a_prism_t1'},
        {'storage_key': 'spectra/obs_a/obs_a_prism_t2_spec.fits', 'content_hash': 'sha256:' + 'b' * 64, 'size_bytes': 3,
         'product_type': 'nirspec_spec', 'status': 'active', 'observation': 'obs_a', 'spectrum_id': 'obs_a_prism_t2'},
    ])
    f = tmp_path / 'products' / 'nirspec' / 'obs_a' / 'obs_a_prism_t1_spec.fits'
    f.parent.mkdir(parents=True)
    f.write_bytes(b'AAA')
    store.mark_object_synced('spectra/obs_a/obs_a_prism_t1_spec.fits', 'nirspec/obs_a/obs_a_prism_t1_spec.fits',
                             H_AAA, 3, f.stat().st_mtime)
    store.close()
    # a pulled release holding t1's science under a stamped copy
    dr = tmp_path / 'dr' / 'DR-t' / 'meta'
    dr.mkdir(parents=True)
    conn = sqlite3.connect(str(dr / 'campfire.db'))
    conn.executescript("""
        CREATE TABLE release_manifest (storage_key TEXT PRIMARY KEY, component TEXT, program_slug TEXT, field TEXT,
          product_type TEXT, spectrum_id TEXT, backend TEXT, bucket TEXT, content_hash TEXT, science_hash TEXT,
          size_bytes INTEGER, archive_key TEXT, byte_state TEXT, stamped_hash TEXT);
        INSERT INTO release_manifest (storage_key, product_type, spectrum_id, content_hash, science_hash, archive_key, byte_state, stamped_hash)
        VALUES ('spectra/obs_a/obs_a_prism_t1_spec.fits', 'nirspec_spec', 'obs_a_prism_t1', 'sha256:old', 'sha256:sciA', 'archive/x', 'stamped', 'sha256:stampedA');
    """)
    conn.commit()
    conn.close()
    return tmp_path


def test_build_records_the_objects_now_and_the_bytes_on_disk(mirror):
    store = LocalStore(mirror / 'meta' / 'campfire.db')
    doc = build(store, mirror, observations=['obs_a'], base_url='https://t/api/v1', latest_dr='DR-t')
    store.close()
    assert doc['format'] == FORMAT and doc['channel'] == 'live' and doc['latest_dr'] == 'DR-t'
    assert [o['object_id'] for o in doc['objects']] == ['OBJ-A', 'OBJ-B']
    a = doc['objects'][0]
    assert a['version'] == 3 and a['redshift'] == 5.2 and a['redshift_quality'] == 4 and a['programs'] == ['pa']
    f1, f2 = doc['files']
    assert f1['spectrum_id'] == 'obs_a_prism_t1' and f1['sha256'] == H_AAA == f1['server_hash']
    assert f1['local_path'] == 'nirspec/obs_a/obs_a_prism_t1_spec.fits' and f1['science_hash'] == 'sha256:sciA'
    assert f1['in_releases'] == ['DR-t'] and f1['cfpipe_version'] == '1.0.0'
    assert f2['sha256'] is None and f2['local_path'] is None and f2['in_releases'] == []
    assert doc['summary'] == {'objects': 2, 'files': 2, 'files_on_disk': 1}


def test_build_selects_by_list_program_and_name(mirror):
    store = LocalStore(mirror / 'meta' / 'campfire.db')
    assert [o['object_id'] for o in build(store, mirror, lists=['mylist'])['objects']] == ['OBJ-A']
    assert [o['object_id'] for o in build(store, mirror, programs=['pa'])['objects']] == ['OBJ-A', 'OBJ-B']
    assert [o['object_id'] for o in build(store, mirror, objects=['OBJ-B'])['objects']] == ['OBJ-B']
    with pytest.raises(ProvenanceError, match='select something'):
        build(store, mirror)
    with pytest.raises(ProvenanceError, match='not in the local catalog'):
        build(store, mirror, objects=['OBJ-Z'])
    store.close()


def _doc(mirror, **sel):
    store = LocalStore(mirror / 'meta' / 'campfire.db')
    try:
        return build(store, mirror, **sel)
    finally:
        store.close()


def test_check_offline_against_the_disk_the_local_stores_and_a_release(mirror):
    doc = _doc(mirror, observations=['obs_a'])
    store = LocalStore(mirror / 'meta' / 'campfire.db')
    res = check(doc, mirror, store=store, dr='DR-t')
    r1, r2 = res['files']
    assert r1['local'] == 'ok' and r1['archive'] == 'in DR-t (local store; the server was not asked)'
    assert r1['release'] == 'in DR-t, same science'
    assert r2['local'] == 'not-recorded' and r2['release'] == 'not in DR-t'
    assert [o['state'] for o in res['objects']] == ['unchanged', 'unchanged']
    assert res['problems'] == 0 and res['summary']['ok'] == 1

    # the file on disk changed, the object was re-inspected
    (mirror / 'products' / 'nirspec' / 'obs_a' / 'obs_a_prism_t1_spec.fits').write_bytes(b'AAB')
    store.upsert_objects([{'id': 1, 'object_id': 'OBJ-A', 'redshift': 5.3, 'redshift_quality': 4, 'version': 4,
                           'programs': ['pa'], 'is_active': True}])
    res = check(doc, mirror, store=store)
    assert res['files'][0]['local'] == 'modified' and res['problems'] == 1
    assert res['objects'][0]['state'] == 'changed: version 3 → 4, redshift 5.2 → 5.3'
    store.close()


def test_check_asks_the_server_for_the_recorded_bytes(mirror):
    doc = _doc(mirror, objects=['OBJ-A'])
    asked = []

    class Api:
        def identify_files(self, files):
            asked.extend(files)
            return [{'releases': [{'tag': 'DR2026.1', 'current': False, 'byte_state': 'stamped',
                                   'live_deployed_at': '2026-11-03T00:00:00Z'}], 'live': [], 'collected': []}]

    res = check(doc, mirror, api=Api())
    assert asked == [{'sha256': H_AAA, 'science_hash': 'sha256:sciA'}]
    assert res['files'][0]['archive'] == 'in DR2026.1, superseded in live on 2026-11-03'
    with pytest.raises(ProvenanceError, match='not on this machine'):
        check(doc, mirror, dr='DR-nope')


def test_check_never_lends_the_registry_s_science_hash_to_other_bytes(mirror):
    # the author opened bytes that were not the registry's (recorded as such)
    # and the pipeline was not there to hash their science: no science hash
    # may be claimed for them, so a release holding the registry's science
    # does not match
    doc = _doc(mirror, objects=['OBJ-A'])
    doc['files'][0].update({'sha256': 'sha256:' + 'c' * 64, 'local_science_hash': None})
    asked = []

    class Api:
        def identify_files(self, files):
            asked.extend(files)
            return [{'releases': [], 'live': [], 'collected': []}]

    res = check(doc, mirror, api=Api(), dr='DR-t')
    assert asked == [{'sha256': 'sha256:' + 'c' * 64, 'science_hash': None}]
    assert res['files'][0]['release'] == 'in DR-t, different science (re-reduced before the freeze)'
    assert res['files'][0]['resolved'] is False and res['problems'] == 2   # modified on disk too
    # bytes never on disk: the recorded hash is the registry's, its science hash stands
    doc['files'][0].update({'sha256': None, 'local_path': None})
    res = check(doc, mirror, api=Api())
    assert asked[-1] == {'sha256': H_AAA, 'science_hash': 'sha256:sciA'}


def test_check_offline_counts_a_file_no_store_holds_as_unresolved(mirror):
    doc = _doc(mirror, objects=['OBJ-A'])
    doc['files'][0].update({'sha256': 'sha256:' + 'c' * 64, 'local_science_hash': None, 'local_path': None})
    res = check(doc, mirror)
    r = res['files'][0]
    assert r['local'] == 'missing' and r['resolved'] is False and res['problems'] == 1
    assert r['archive'] == 'not in any store on this machine (the server was not asked)'
    # a superseded download the mirror remembers is not a resolution either
    store = LocalStore(mirror / 'meta' / 'campfire.db')
    store.upsert_storage_objects([{'storage_key': 'spectra/obs_a/obs_a_prism_t1_spec.fits',
                                   'content_hash': 'sha256:' + 'd' * 64, 'size_bytes': 3, 'product_type': 'nirspec_spec',
                                   'status': 'active', 'observation': 'obs_a', 'spectrum_id': 'obs_a_prism_t1'}])
    store.close()
    doc = {'format': FORMAT, 'files': [{'spectrum_id': 'obs_a_prism_t1', 'sha256': H_AAA, 'server_hash': H_AAA,
                                        'science_hash': 'sha256:sciA', 'local_path': None}], 'objects': []}
    res = check(doc, mirror)
    assert res['files'][0]['archive'].startswith('in DR-t')          # the release store still holds that science
    doc['files'][0]['science_hash'] = None
    res = check(doc, mirror)
    assert res['files'][0]['archive'].startswith('a download superseded since') and res['files'][0]['resolved'] is False


def test_load_refuses_another_format(tmp_path):
    p = tmp_path / 'x.json'
    p.write_text('{"format": "other"}')
    with pytest.raises(ProvenanceError):
        load(p)


def test_provenance_commands(mirror):
    runner = CliRunner()
    out = mirror / 'paper.campfire.json'
    res = runner.invoke(main_cli.cli, ['provenance', '--obs', 'obs_a', '-o', str(out)])
    assert res.exit_code == 0, res.output
    assert '2 object(s), 2 file(s) (1 on disk)' in res.output
    doc = json.loads(out.read_text())
    assert doc['format'] == FORMAT and len(doc['files']) == 2
    res = runner.invoke(main_cli.cli, ['provenance', '--object', 'OBJ-A'])
    assert res.exit_code == 0 and json.loads(res.output)['summary']['objects'] == 1
    res = runner.invoke(main_cli.cli, ['provenance'])
    assert res.exit_code == 2 and 'select something' in res.output

    res = runner.invoke(main_cli.cli, ['provenance', 'check', str(out), '--offline', '--dr', 'DR-t'])
    assert res.exit_code == 0, res.output
    assert 'local: ok' in res.output and 'in DR-t, same science' in res.output and 'not in DR-t' in res.output
    assert '2 file(s): 1 unchanged on disk, 0 modified, 0 not on disk; 2 object(s) unchanged, 0 changed.' in res.output
    (mirror / 'products' / 'nirspec' / 'obs_a' / 'obs_a_prism_t1_spec.fits').write_bytes(b'AAB')
    res = runner.invoke(main_cli.cli, ['provenance', 'check', str(out), '--offline'])
    assert res.exit_code == 1 and 'local: modified' in res.output and 'no longer resolve' in res.output
    res = runner.invoke(main_cli.cli, ['provenance', 'check', str(out), '--offline', '--json'])
    assert res.exit_code == 1 and json.loads(res.stdout)['summary']['modified'] == 1
