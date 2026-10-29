"""``campfire cite`` / ``Campfire.cite()`` (P5-3, design §4.5)."""
from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

import campfire.cli as main_cli
from campfire import Campfire
from campfire.cite import LIVE_SENTENCE, cite_sentence, reader_programs, roster, versions_from_store, write_readme
from campfire.db.store import LocalStore

TAG = 'DR2026.1'
ROSTER = {
    'pub': {'is_public': True, 'spectra': 2, 'cfpipe_versions': ['0.7.0'], 'crds_contexts': ['jwst_1535.pmap']},
    'ember': {'is_public': False, 'spectra': 1, 'cfpipe_versions': ['0.7.0'], 'crds_contexts': ['jwst_1535.pmap']},
    'zenith': {'is_public': False, 'spectra': 1, 'cfpipe_versions': ['0.7.1'], 'crds_contexts': ['jwst_1535.pmap']},
}


def release(*held, doi='10.5281/zenodo.1', with_roster=True):
    counts = {'spectra': 4}
    if with_roster:
        counts['programs'] = ROSTER
    files = [{'stream': s, 'scope': p} for p in held for s in ('objects', 'spectra', 'manifest')]
    files.append({'stream': 'manifest', 'scope': 'nircam/uds'})
    return {'tag': TAG, 'state': 'published', 'doi': doi, 'counts': counts, 'files': files,
            'frozen_at': '2026-10-01T00:00:00Z', 'published_at': '2026-10-05T00:00:00Z'}


def test_the_reader_s_programs_are_the_spectra_stream_s_scopes():
    assert reader_programs(release('pub', 'ember')) == ['ember', 'pub']
    assert roster(release())['ember']['is_public'] is False
    assert roster({'tag': TAG}) == {}


def test_public_programs_are_cited_through_the_doi_alone():
    assert cite_sentence(release('pub')) == 'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1).'
    assert cite_sentence(release('pub', 'ember'), ['pub']) == 'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1).'


def test_proprietary_programs_are_described_with_their_pipeline_versions():
    assert cite_sentence(release('pub', 'ember')) == (
        'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1), plus program ember reduced with cfpipe 0.7.0 '
        'and CRDS context jwst_1535.pmap as in that release.')
    assert cite_sentence(release('pub', 'ember', 'zenith')) == (
        'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1), plus programs ember and zenith reduced with '
        'cfpipe 0.7.0 and 0.7.1 and CRDS context jwst_1535.pmap as in that release.')


def test_a_selection_is_intersected_with_what_the_reader_holds():
    # zenith is not held: it cannot be cited through this reader's copy
    assert cite_sentence(release('pub', 'ember'), ['zenith', 'ember']).endswith('plus program ember reduced with cfpipe 0.7.0 and CRDS context jwst_1535.pmap as in that release.')
    # no files list (the record alone): the selection stands
    bare = {'tag': TAG, 'doi': 'd', 'counts': {'programs': ROSTER}}
    assert 'programs ember and zenith' in cite_sentence(bare, ['ember', 'zenith'])


def test_the_store_s_versions_win_over_the_roster_s():
    asked = []
    s = cite_sentence(release('pub', 'ember'), versions_of=lambda progs: (asked.append(progs), {'cfpipe_versions': ['0.7.2'], 'crds_contexts': []})[1])
    assert asked == [['ember']]      # the described programs only, never the public ones
    assert s == 'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1), plus program ember reduced with cfpipe 0.7.2 as in that release.'


def test_without_a_doi_or_a_roster():
    assert cite_sentence({'tag': 'DR-x'}) == 'CAMPFIRE DR-x (unpublished: no DOI yet).'
    # a release frozen before the roster: nothing to describe, the DOI stands
    assert cite_sentence(release('pub', 'ember', with_roster=False)) == 'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1).'


def _tree(tmp_path, *held):
    root = tmp_path / 'dr' / TAG
    (root / 'meta').mkdir(parents=True)
    store = LocalStore(root / 'meta' / 'campfire.db')
    store.upsert_spectra([
        {'id': 1, 'spectrum_id': 'obs_p_prism_t1', 'grating': 'PRISM', 'observation': 'obs_p', 'program_slug': 'pub',
         'cfpipe_version': '0.7.0', 'crds_context': 'jwst_1535.pmap'},
        {'id': 2, 'spectrum_id': 'obs_e_prism_t2', 'grating': 'PRISM', 'observation': 'obs_e', 'program_slug': 'ember',
         'cfpipe_version': '0.7.3', 'crds_context': 'jwst_1535.pmap'},
    ])
    store.set_meta('release_tag', TAG)
    store.close()
    (root / 'release.json').write_text(json.dumps(release(*held)))
    return root


def test_versions_from_store_and_the_readme(tmp_path):
    root = _tree(tmp_path, 'pub', 'ember')
    store = LocalStore(root / 'meta' / 'campfire.db')
    assert versions_from_store(store, ['ember']) == {'cfpipe_versions': ['0.7.3'], 'crds_contexts': ['jwst_1535.pmap']}
    assert versions_from_store(store, []) == {'cfpipe_versions': [], 'crds_contexts': []}
    store.close()
    rel = release('pub', 'ember')
    rel['counts_loaded'] = {'spectra': 2}
    rel['loaded_at'] = '2026-10-29T10:00:00Z'
    path = write_readme(root, rel, versions_of=lambda progs: {'cfpipe_versions': ['0.7.3'], 'crds_contexts': ['jwst_1535.pmap']})
    text = path.read_text()
    assert text.startswith('# CAMPFIRE DR2026.1')
    assert 'plus program ember reduced with cfpipe 0.7.3' in text
    assert 'Programs held in this copy: ember, pub.' in text
    assert 'public, on the DOI record: pub; proprietary, described in the sentence: ember.' in text
    assert 'Campfire(dr=\'DR2026.1\')' in text and '2 spectra' in text


def test_a_release_client_cites_from_its_store_and_a_live_client_points_at_provenance(tmp_path):
    _tree(tmp_path, 'pub', 'ember')
    cf = Campfire(dr=TAG, data_dir=tmp_path)
    assert cf.cite() == ('CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1), plus program ember reduced with cfpipe 0.7.3 '
                         'and CRDS context jwst_1535.pmap as in that release.')
    assert cf.cite(['pub']) == 'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1).'
    live = Campfire.__new__(Campfire)
    assert live.cite() == LIVE_SENTENCE


def test_cite_command_reads_the_pulled_release(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    _tree(tmp_path, 'pub', 'ember')
    runner = CliRunner()
    res = runner.invoke(main_cli.cli, ['cite', TAG])
    assert res.exit_code == 0, res.output
    assert res.output.strip() == ('CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1), plus program ember reduced with '
                                  'cfpipe 0.7.3 and CRDS context jwst_1535.pmap as in that release.')
    res = runner.invoke(main_cli.cli, ['cite', TAG, '--obs', 'obs_p'])
    assert res.exit_code == 0 and res.output.strip() == 'CAMPFIRE DR2026.1 (doi:10.5281/zenodo.1).'
    res = runner.invoke(main_cli.cli, ['cite', TAG, '--obs', 'nope'])
    assert res.exit_code == 1 and 'not in release' in res.output
    res = runner.invoke(main_cli.cli, ['cite', TAG, '--program', 'ember', '--json'])
    assert res.exit_code == 0 and json.loads(res.output)['programs'] == ['ember']
    assert json.loads(res.output)['sentence'].startswith('CAMPFIRE DR2026.1 (doi:')


def test_cite_command_asks_the_server_when_the_release_is_not_pulled(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))

    class Api:
        def __init__(self, *a, **k):
            pass

        def release_files(self, tag):
            assert tag == TAG
            return release('pub', 'ember')

    with patch('campfire.cli.APISession'), patch('campfire.cli.APIClient', Api):
        res = CliRunner().invoke(main_cli.cli, ['cite', TAG])
        assert res.exit_code == 0, res.output
        assert 'plus program ember reduced with cfpipe 0.7.0' in res.output
        res = CliRunner().invoke(main_cli.cli, ['cite', TAG, '--obs', 'obs_e'])
        assert res.exit_code == 2 and '--obs needs the pulled release' in res.output
