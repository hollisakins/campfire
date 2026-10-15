"""Data releases P6b-3: the release page document (`campfire dr page`). The
database side is supabase/tests/check_dr_page.sql."""
import hashlib
import json

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire import dr_page
from tests.fakes import FakePostgrest

TAG = 'DR2026.0-rehearsal'


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket, Key, Body, ContentType):  # noqa: N803 - boto3 names
        self.puts.append((Bucket, Key, Body.read(), ContentType))


def _page_dir(tmp_path, *, readme='# DR2026.0\n\nHello.\n', files=('nz.png', 'depth.pdf')):
    d = tmp_path / 'page'
    d.mkdir()
    (d / 'README.md').write_text(readme, encoding='utf-8')
    for name in files:
        (d / name).write_bytes(b'\x89PNG' + name.encode())
    return d


def _client(state='frozen', **handlers):
    return FakePostgrest(tables={'dr_releases': [{'tag': TAG, 'state': state, 'page_md': None}],
                                 'dr_manifest': []},
                         rpc_handlers=handlers)


def test_collect_page_dir_reads_readme_and_assets(tmp_path):
    d = _page_dir(tmp_path)
    doc = dr_page.collect_page_dir(d)
    assert doc.markdown.startswith('# DR2026.0')
    assert [a.name for a in doc.assets] == ['depth.pdf', 'nz.png']
    png = next(a for a in doc.assets if a.name == 'nz.png')
    assert png.content_hash == 'sha256:' + hashlib.sha256(b'\x89PNGnz.png').hexdigest()
    assert png.content_type == 'image/png' and png.size_bytes == 10
    assert png.storage_key(TAG) == f'dr/{TAG}/page/nz.png'


@pytest.mark.parametrize('setup, message', [
    (lambda d: (d / 'README.md').unlink(), 'has no README.md'),
    (lambda d: (d / 'README.md').write_text('  \n'), 'is empty'),
    (lambda d: (d / 'bad name.png').write_bytes(b'x'), 'bad asset name'),
    (lambda d: (d / 'sub').mkdir(), 'not a regular file'),
    (lambda d: (d / 'empty.png').write_bytes(b''), 'between 1 byte and 25 MB'),
])
def test_collect_page_dir_refuses_bad_directories(tmp_path, setup, message):
    d = _page_dir(tmp_path)
    setup(d)
    with pytest.raises(dr_page.PageError, match=message):
        dr_page.collect_page_dir(d)


def test_hidden_files_are_skipped(tmp_path):
    d = _page_dir(tmp_path, files=())
    (d / '.DS_Store').write_bytes(b'x')
    assert dr_page.collect_page_dir(d).assets == []


def test_push_uploads_then_sets_the_page(tmp_path):
    d = _page_dir(tmp_path)
    seen = {}

    def page_set(params):
        seen.update(params)
        return {'tag': TAG, 'state': 'frozen', 'markdown_bytes': 20, 'assets': 2}
    c = _client(dr_page_set=page_set)
    s3 = FakeS3()
    out = dr_page.push_page(c, s3, 'campfire-data', TAG, d, log=lambda m: None)
    assert out['assets'] == 2
    assert [k for _, k, _, _ in s3.puts] == [f'dr/{TAG}/page/depth.pdf', f'dr/{TAG}/page/nz.png']
    assert s3.puts[1][3] == 'image/png'
    assert seen['p_tag'] == TAG and seen['p_markdown'].startswith('# DR2026.0')
    names = [a['name'] for a in seen['p_assets']]
    assert names == ['depth.pdf', 'nz.png']
    assert all(a['storage_key'] == f"dr/{TAG}/page/{a['name']}" and a['backend'] == 'osn' and a['bucket'] == 'data'
               and a['content_hash'].startswith('sha256:') for a in seen['p_assets'])


def test_push_refuses_a_published_release_before_uploading(tmp_path):
    d = _page_dir(tmp_path)
    s3 = FakeS3()
    with pytest.raises(dr_page.PageError, match='is published'):
        dr_page.push_page(_client('published'), s3, 'b', TAG, d)
    assert s3.puts == []


def test_push_refuses_a_freezing_release_and_an_unknown_tag(tmp_path):
    d = _page_dir(tmp_path)
    with pytest.raises(dr_page.PageError, match='is freezing'):
        dr_page.push_page(_client('freezing'), FakeS3(), 'b', TAG, d)
    with pytest.raises(dr_page.PageError, match='no release'):
        dr_page.push_page(FakePostgrest(tables={'dr_releases': []}), FakeS3(), 'b', TAG, d)


def test_dry_run_uploads_nothing(tmp_path):
    d = _page_dir(tmp_path)
    c = _client()
    s3 = FakeS3()
    out = dr_page.push_page(c, s3, 'b', TAG, d, dry_run=True)
    assert out['dry_run'] is True and len(out['assets']) == 2
    assert s3.puts == [] and not c.rpcs


def test_fetch_and_format_page():
    c = FakePostgrest(tables={
        'dr_releases': [{'tag': TAG, 'state': 'ready', 'page_md': '# Hi'}],
        'dr_manifest': [{'tag': TAG, 'component': 'page', 'storage_key': f'dr/{TAG}/page/nz.png',
                         'content_hash': 'sha256:' + 'a' * 64, 'size_bytes': 10, 'updated_at': 'x'},
                        {'tag': TAG, 'component': 'nirspec', 'storage_key': 'data/x.fits',
                         'content_hash': 'sha256:' + 'b' * 64, 'size_bytes': 1, 'updated_at': 'x'}]})
    page = dr_page.fetch_page(c, TAG)
    assert [a['name'] for a in page['assets']] == ['nz.png']
    text = dr_page.format_page(page)
    assert 'nz.png' in text and text.endswith('# Hi')
    assert dr_page.fetch_page(FakePostgrest(tables={'dr_releases': []}), TAG) is None


def test_cli_page_push_dry_run_and_show(tmp_path, monkeypatch):
    d = _page_dir(tmp_path)
    c = FakePostgrest(tables={'dr_releases': [{'tag': TAG, 'state': 'frozen', 'page_md': '# Hi'}],
                              'dr_manifest': []})
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: ({}, c))
    runner = CliRunner()
    res = runner.invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d), '--dry-run'])
    assert res.exit_code == 0, res.output
    assert 'Dry run' in res.output and f'dr/{TAG}/page/nz.png' in res.output
    res = runner.invoke(main_cli.cli, ['dr', 'page', 'show', TAG, '--json'])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)['page_md'] == '# Hi'


def test_cli_page_push_needs_osn_credentials(tmp_path, monkeypatch):
    d = _page_dir(tmp_path)
    c = _client()
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: ({}, c))
    monkeypatch.setattr(main_cli, '_dr_osn_s3', lambda config: (None, 'no [r2_osn] block'))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d)])
    assert res.exit_code == 2 and 'OSN credentials' in res.output
