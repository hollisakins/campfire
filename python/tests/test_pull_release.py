"""``campfire pull --dr`` and ``campfire dr list`` (P5-2)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import campfire.cli as main_cli
from campfire.db.store import LocalStore
from campfire.sync import download_objects


class _PresignApi:
    def __init__(self):
        self.calls = []

    def presign_keys(self, keys, dr=None):
        self.calls.append((list(keys), dr))
        return {}


def test_download_objects_presigns_under_the_release(tmp_path):
    store = LocalStore(tmp_path / 'meta' / 'campfire.db')
    store.upsert_storage_objects([
        {'storage_key': 'spectra/obs/obs_prism_t1_spec.fits', 'content_hash': 'sha256:stamped',
         'product_type': 'nirspec_spec', 'status': 'active', 'observation': 'obs', 'spectrum_id': 'obs_prism_t1'},
    ])
    api = _PresignApi()
    stats = download_objects(api, ['obs'], ['nirspec_spec'], store, tmp_path / 'products', dr='DR-t')
    assert api.calls == [(['spectra/obs/obs_prism_t1_spec.fits'], 'DR-t')]
    assert stats['to_download'] == 1 and stats['unauthorized'] == 1
    store.close()


def test_api_client_presign_carries_the_release_tag():
    from campfire.api.client import APIClient

    class S:
        def __init__(self):
            self.calls = []

        def post(self, path, json=None, timeout=None):
            self.calls.append((path, json))

            class R:
                status_code = 200
                def json(self_inner): return {'urls': {'k': 'u'}}
                def raise_for_status(self_inner): pass
            return R()

    s = S()
    api = APIClient.__new__(APIClient)
    api._session = s
    assert api.presign_keys(['k'], dr='DR2026.1') == {'k': 'u'}
    assert s.calls == [('/storage/presign?dr=DR2026.1', {'keys': ['k']})]
    api.presign_keys(['k'])
    assert s.calls[-1][0] == '/storage/presign'


def test_pull_dr_refuses_intermediate_and_stale():
    runner = CliRunner()
    for flag in ('--intermediate', '--stale'):
        res = runner.invoke(main_cli.cli, ['pull', '--dr', 'DR-t', flag])
        assert res.exit_code == 2 and 'does not apply to a data release' in res.output


def test_pull_dr_loads_the_release_then_lists_it_without_a_selection(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    root = tmp_path / 'dr' / 'DR-t'
    (root / 'meta').mkdir(parents=True)
    (root / 'products').mkdir()
    store = LocalStore(root / 'meta' / 'campfire.db')
    store.upsert_spectra([{'id': 1, 'spectrum_id': 'obs_prism_t1', 'grating': 'PRISM', 'observation': 'obs',
                           'program_slug': 'pa', 'field': 'uds', 'object_id': 'O'}])
    store.upsert_storage_objects([{'storage_key': 'spectra/obs/obs_prism_t1_spec.fits', 'content_hash': 'sha256:s',
                                   'size_bytes': 7, 'product_type': 'nirspec_spec', 'status': 'active',
                                   'observation': 'obs', 'spectrum_id': 'obs_prism_t1'}])
    store.close()
    loaded = []

    class Rep:
        rebuilt, fetched, kept, counts = False, [], [('spectra', 'pa')], {}

    def fake_load(session, tag, data_dir=None, log=None, **kw):
        loaded.append((tag, Path(data_dir)))
        return Rep()

    with patch('campfire.cli.APISession') as MockSession, patch('campfire.cli.APIClient'), \
            patch('campfire.dr_store.load_release', side_effect=fake_load):
        MockSession.return_value.base_url = 'https://t/api/v1'
        res = CliRunner().invoke(main_cli.cli, ['pull', '--dr', 'DR-t'])
        assert res.exit_code == 0, res.output
        assert loaded == [('DR-t', tmp_path)]
        assert 'Release DR-t: 1 observation(s)' in res.output and 'obs' in res.output
        # a dry run of a selection plans against the release store's rows
        res = CliRunner().invoke(main_cli.cli, ['pull', '--dr', 'DR-t', '--obs', 'obs', '--dry-run'])
        assert res.exit_code == 0, res.output
        assert 'would fetch 1 file(s)' in res.output and str(root / 'products') in res.output
        res = CliRunner().invoke(main_cli.cli, ['pull', '--dr', 'DR-t', '--obs', 'nope'])
        assert res.exit_code == 1 and 'not in release DR-t' in res.output


def test_dr_list_prints_the_releases(monkeypatch):
    class Api:
        def __init__(self, *a, **k):
            pass

        def list_releases(self):
            return [{'tag': 'DR2026.1', 'state': 'published', 'frozen_at': '2026-10-01T00:00:00Z',
                     'published_at': '2026-10-05T00:00:00Z', 'counts': {'spectra': 81420}, 'doi': '10.1/x'}]

    with patch('campfire.cli.APISession'), patch('campfire.cli.APIClient', Api):
        res = CliRunner().invoke(main_cli.cli, ['dr', 'list'])
        assert res.exit_code == 0, res.output
        assert 'DR2026.1' in res.output and '2026-10-05' in res.output and '81420' in res.output and '10.1/x' in res.output
        res = CliRunner().invoke(main_cli.cli, ['dr', 'list', '--json'])
        assert '"tag": "DR2026.1"' in res.output


def test_identify_command_prints_one_line_per_file(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    f = tmp_path / 'a.bin'
    f.write_bytes(b'abc')

    class Api:
        def __init__(self, *a, **k):
            pass

        def identify_files(self, files):
            return [{'releases': [{'tag': 'DR2026.1', 'current': True, 'byte_state': 'stamped',
                                   'spectrum_id': 'obs_prism_t1'}], 'live': [], 'collected': []}]

    with patch('campfire.cli.APISession'), patch('campfire.cli.APIClient', Api):
        res = CliRunner().invoke(main_cli.cli, ['identify', str(f)])
        assert res.exit_code == 0, res.output
        assert 'a.bin' in res.output and 'in DR2026.1, still current' in res.output and '[obs_prism_t1]' in res.output
        res = CliRunner().invoke(main_cli.cli, ['identify', '--offline', str(f)])
        assert 'the server was not asked' in res.output
