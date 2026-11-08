"""``campfire identify`` (P5-2): hashing, the local stores, the server's facts
phrased."""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from campfire.db.store import LocalStore
from campfire.identify import describe, hash_file, identify_paths, local_matches, subject

A = 'sha256:' + 'a' * 64
B = 'sha256:' + 'b' * 64


def test_hash_file_gives_the_whole_file_sha256_and_no_science_hash_for_a_non_fits(tmp_path):
    p = tmp_path / 'x.bin'
    p.write_bytes(b'hello')
    ident = hash_file(p)
    assert ident.sha256 == 'sha256:' + hashlib.sha256(b'hello').hexdigest()
    assert ident.science_hash is None


def test_science_hash_follows_the_pipeline_when_it_is_installed(tmp_path):
    pytest.importorskip('campfire_pipeline')
    from astropy.io import fits
    import numpy as np
    p = tmp_path / 'x.fits'
    fits.PrimaryHDU(np.zeros((2, 2))).writeto(p)
    ident = hash_file(p)
    assert ident.science_hash and ident.science_hash.startswith('sha256:')
    # a stamp-like header edit that is not science leaves the science hash alone
    with fits.open(p, mode='update') as h:
        h[0].header['CMPFRTIM'] = '2026-01-01'
    assert hash_file(p).science_hash == ident.science_hash
    assert hash_file(p).sha256 != ident.sha256


@pytest.fixture
def stores(tmp_path):
    live = LocalStore(tmp_path / 'meta' / 'campfire.db')
    live.upsert_spectra([{'id': 1, 'spectrum_id': 'obs_prism_t1', 'grating': 'PRISM', 'science_hash': B,
                          'observation': 'obs', 'program_slug': 'pa'}])
    live.upsert_storage_objects([{'storage_key': 'spectra/obs/obs_prism_t1_spec.fits', 'content_hash': A,
                                  'product_type': 'nirspec_spec', 'status': 'active', 'observation': 'obs',
                                  'spectrum_id': 'obs_prism_t1'}])
    # the bytes downloaded earlier, superseded on the server since (a sync
    # moved content_hash and kept local_file_hash)
    live.mark_object_synced('spectra/obs/obs_prism_t1_spec.fits', 'nirspec/obs/obs_prism_t1_spec.fits',
                            'sha256:' + 'd' * 64, 10, 1.0)
    live.close()
    dr = tmp_path / 'dr' / 'DR-t' / 'meta'
    dr.mkdir(parents=True)
    conn = sqlite3.connect(str(dr / 'campfire.db'))
    conn.executescript("""
        CREATE TABLE release_manifest (storage_key TEXT PRIMARY KEY, component TEXT, program_slug TEXT, field TEXT,
          product_type TEXT, spectrum_id TEXT, backend TEXT, bucket TEXT, content_hash TEXT, science_hash TEXT,
          size_bytes INTEGER, archive_key TEXT, byte_state TEXT, stamped_hash TEXT);
        INSERT INTO release_manifest (storage_key, product_type, spectrum_id, content_hash, science_hash, archive_key, byte_state, stamped_hash)
        VALUES ('spectra/obs/obs_prism_t1_spec.fits', 'nirspec_spec', 'obs_prism_t1', 'sha256:old', 'sha256:oldsci', 'archive/x', 'stamped', 'sha256:""" + 'e' * 64 + """');
    """)
    conn.commit()
    conn.close()
    return tmp_path


def test_local_matches_read_the_live_mirror_and_every_release_store(stores):
    hits = local_matches(stores, A, None)
    assert [(h['source'], h['match'], h['spectrum_id']) for h in hits] == [('live', 'file', 'obs_prism_t1')]
    hits = local_matches(stores, 'sha256:' + 'e' * 64, B)
    assert {(h['source'], h['match']) for h in hits} == {('live', 'science'), ('dr', 'stamped')}
    assert next(h for h in hits if h['source'] == 'dr')['tag'] == 'DR-t'
    assert local_matches(stores, 'sha256:' + 'f' * 64, None) == []
    # the superseded download is told apart from a live match
    stale = local_matches(stores, 'sha256:' + 'd' * 64, None)
    assert [(h['source'], h['match']) for h in stale] == [('live', 'stale-download')]
    assert describe(None, stale).startswith('a download superseded since')


def test_a_store_of_another_schema_or_a_missing_table_answers_nothing(tmp_path):
    (tmp_path / 'meta').mkdir()
    conn = sqlite3.connect(str(tmp_path / 'meta' / 'campfire.db'))
    conn.execute("CREATE TABLE _meta (key TEXT, value TEXT)")
    conn.commit()
    conn.close()
    assert local_matches(tmp_path, A, B) == []


class TestDescribe:
    def test_release_member_still_current(self):
        s = {'releases': [{'tag': 'DR2026.1', 'current': True, 'byte_state': 'stamped'}], 'live': [{}], 'collected': []}
        assert describe(s) == 'in DR2026.1, still current'

    def test_release_member_superseded_names_the_live_date_and_the_newest_tag_first(self):
        s = {'releases': [{'tag': 'DR2026.0', 'current': False, 'byte_state': 'copied', 'live_deployed_at': '2026-11-03T10:00:00Z'},
                          {'tag': 'DR2026.1', 'current': False, 'byte_state': 'copied', 'live_deployed_at': '2026-11-03T10:00:00Z'}],
             'live': [], 'collected': []}
        assert describe(s) == 'in DR2026.1 and DR2026.0, superseded in live on 2026-11-03'

    def test_a_field_product_has_no_live_spectrum_to_compare(self):
        s = {'releases': [{'tag': 'DR2026.1', 'current': None, 'byte_state': 'copied'}], 'live': [], 'collected': []}
        assert describe(s) == 'in DR2026.1'

    def test_a_member_not_yet_archived_says_so(self):
        s = {'releases': [{'tag': 'DR2026.1', 'current': True, 'byte_state': 'pending'}], 'live': [], 'collected': []}
        assert describe(s) == 'in DR2026.1, still current (archive copy not yet made)'

    def test_live_only(self):
        s = {'releases': [], 'live': [{'match': 'file', 'deployed_at': '2026-10-02T00:00:00Z'}], 'collected': []}
        assert describe(s) == 'live only, deployed 2026-10-02, not in any DR; cite with a provenance file'
        s['live'][0]['match'] = 'science'
        assert describe(s).endswith('(same science, different bytes)')

    def test_collected(self):
        s = {'releases': [], 'live': [], 'collected': [{'collected_at': '2027-01-05T00:00:00Z'}]}
        assert describe(s) == 'archived, collected on 2027-01-05'

    def test_unknown(self):
        assert describe({'releases': [], 'live': [], 'collected': []}).startswith('unknown to the archive')

    def test_without_the_server_the_local_stores_speak(self):
        assert describe(None, [{'source': 'dr', 'tag': 'DR2026.0'}, {'source': 'dr', 'tag': 'DR2026.1'}]) == \
            'in DR2026.1 and DR2026.0 (local store; the server was not asked)'
        assert describe(None, [{'source': 'live'}]) == 'in the live catalog (local mirror; the server was not asked)'
        assert describe(None, []) == 'not in any store on this machine (the server was not asked)'


class _FakeApi:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def identify_files(self, files):
        self.calls.append(files)
        return [self.answers.get(f['sha256'], {'releases': [], 'live': [], 'collected': []}) for f in files]


def test_identify_paths_hashes_looks_up_locally_and_asks_the_server_once(stores, tmp_path):
    f1 = tmp_path / 'one.bin'
    f1.write_bytes(b'one')
    f2 = tmp_path / 'two.bin'
    f2.write_bytes(b'two')
    h1 = 'sha256:' + hashlib.sha256(b'one').hexdigest()
    api = _FakeApi({h1: {'releases': [{'tag': 'DR-t', 'current': True, 'byte_state': 'stamped', 'spectrum_id': 'obs_prism_t1'}],
                         'live': [], 'collected': []}})
    out = identify_paths([f1, f2, tmp_path / 'missing.bin'], data_dir=stores, api=api)
    assert len(api.calls) == 1 and [f['sha256'] for f in api.calls[0]] == [h1, out[1].sha256]
    assert out[0].sentence == 'in DR-t, still current' and subject(out[0]) == 'obs_prism_t1'
    assert out[1].sentence.startswith('unknown to the archive')
    assert out[2].error and out[2].sha256 == '' and out[2].server is None
    assert out[0].as_dict()['path'].endswith('one.bin')


def test_identify_paths_offline_uses_the_local_stores(stores, tmp_path):
    f = tmp_path / 'x.bin'
    f.write_bytes(b'x')
    out = identify_paths([f], data_dir=stores, api=None)
    assert out[0].server is None and out[0].sentence.endswith('(the server was not asked)')
