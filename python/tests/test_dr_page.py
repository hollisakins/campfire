"""Data releases P6b-3: the release page document (`campfire dr page`). The
database side is supabase/tests/check_dr_page.sql."""
import hashlib
import io
import json

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire import dr_page
from campfire.deploy import config as deploy_config
from tests.fakes import FakePostgrest

TAG = 'DR2026.0-rehearsal'
PREFIX = f'dr/{TAG}/page/'


def _sha(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _key(name: str, data: bytes) -> str:
    return f"{PREFIX}{hashlib.sha256(data).hexdigest()[:16]}/{name}"


NZ = b'\x89PNGnz.png'
DEPTH = b'\x89PNGdepth.pdf'


class _S3Error(Exception):
    def __init__(self, code):
        super().__init__(f'http {code}')
        self.response = {'ResponseMetadata': {'HTTPStatusCode': code}}


class FakeS3:
    """An in-memory bucket honouring If-None-Match, GET and DELETE (no HEAD, no LIST:
    the push must not need them)."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})      # key -> bytes
        self.puts = []
        self.deletes = []

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch=None):  # noqa: N803 - boto3 names
        if IfNoneMatch != '*':
            raise AssertionError('every page PUT must be conditional')
        if Key in self.objects:
            raise _S3Error(412)
        data = Body if isinstance(Body, bytes) else Body.read()
        self.objects[Key] = data
        self.puts.append((Key, data, ContentType))

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _S3Error(404)
        return {'Body': io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)
        self.deletes.append(Key)


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


def _ok(params, removed=()):
    return {'tag': TAG, 'state': 'frozen', 'markdown_bytes': 20, 'assets': len(params['p_assets']),
            'removed_keys': list(removed)}


def test_collect_page_dir_reads_each_asset_once_and_hashes_the_bytes(tmp_path):
    d = _page_dir(tmp_path)
    doc = dr_page.collect_page_dir(d)
    assert doc.markdown.startswith('# DR2026.0')
    assert [a.name for a in doc.assets] == ['depth.pdf', 'nz.png']
    png = next(a for a in doc.assets if a.name == 'nz.png')
    assert png.data == NZ and png.content_hash == _sha(NZ)
    assert png.content_type == 'image/png' and png.size_bytes == 10
    assert png.storage_key(TAG) == _key('nz.png', NZ) == f"{PREFIX}{png.sha16}/nz.png"


def test_page_key_is_content_addressed():
    assert dr_page.page_key('DR1', 'sha256:' + 'a' * 64, 'f.png') == 'dr/DR1/page/aaaaaaaaaaaaaaaa/f.png'
    assert dr_page.asset_name('DR1', 'dr/DR1/page/aaaaaaaaaaaaaaaa/f.png') == 'f.png'
    with pytest.raises(dr_page.PageError, match='sha256'):
        dr_page.page_key('DR1', 'etag:abc', 'f.png')


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


def test_push_uploads_the_hashed_bytes_conditionally_then_sets_the_page(tmp_path):
    d = _page_dir(tmp_path)
    seen = {}

    def page_set(params):
        seen.update(params)
        return _ok(params)
    c = _client(dr_page_set=page_set)
    s3 = FakeS3()
    out = dr_page.push_page(c, s3, 'campfire-data', TAG, d, log=lambda m: None)
    assert out['assets'] == 2 and out['uploads'] == {'depth.pdf': 'uploaded', 'nz.png': 'uploaded'} and out['deleted'] == []
    assert [k for k, _, _ in s3.puts] == [_key('depth.pdf', DEPTH), _key('nz.png', NZ)]
    assert s3.puts[1][1] == NZ and s3.puts[1][2] == 'image/png'
    assert seen['p_tag'] == TAG and seen['p_markdown'].startswith('# DR2026.0')
    assert [a['name'] for a in seen['p_assets']] == ['depth.pdf', 'nz.png']
    assert all(a['storage_key'] == dr_page.page_key(TAG, a['content_hash'], a['name']) and a['backend'] == 'osn'
               and a['bucket'] == 'data' and a['content_hash'] == _sha(s3.objects[a['storage_key']])
               for a in seen['p_assets'])


def test_a_412_with_the_same_bytes_is_reused_after_reading_them_back(tmp_path):
    d = _page_dir(tmp_path)
    s3 = FakeS3({_key('nz.png', NZ): NZ})
    out = dr_page.push_page(_client(dr_page_set=_ok), s3, 'b', TAG, d)
    assert out['uploads'] == {'depth.pdf': 'uploaded', 'nz.png': 'reused'}
    assert [k for k, _, _ in s3.puts] == [_key('depth.pdf', DEPTH)] and s3.deletes == []


def test_a_412_with_different_bytes_fails_the_push_and_touches_nothing(tmp_path):
    d = _page_dir(tmp_path)
    c = _client(dr_page_set=_ok)
    s3 = FakeS3({_key('nz.png', NZ): b'other bytes under the same content-addressed key'})
    with pytest.raises(dr_page.PageError, match='already holds different bytes.*never overwritten'):
        dr_page.push_page(c, s3, 'b', TAG, d)
    assert s3.deletes == [] and s3.objects[_key('nz.png', NZ)] == b'other bytes under the same content-addressed key'
    assert not c.rpcs                                   # the page was not set


def test_only_the_keys_the_database_retired_are_deleted(tmp_path):
    d = _page_dir(tmp_path)
    old_key = f'{PREFIX}0123456789abcdef/old_figure.png'
    stale_unreferenced = f'{PREFIX}fedcba9876543210/orphan.png'
    s3 = FakeS3({old_key: b'old', stale_unreferenced: b'orphan', 'dr/other/page/aaaaaaaaaaaaaaaa/x.png': b'x'})
    out = dr_page.push_page(_client(dr_page_set=lambda p: _ok(p, removed=[old_key, 'dr/other/page/aaaaaaaaaaaaaaaa/x.png'])),
                            s3, 'b', TAG, d)
    assert out['deleted'] == [old_key]                  # another tag's key is never ours to delete
    assert s3.deletes == [old_key]
    assert stale_unreferenced in s3.objects and 'dr/other/page/aaaaaaaaaaaaaaaa/x.png' in s3.objects


def test_rpc_refusal_after_upload_names_the_orphans(tmp_path):
    d = _page_dir(tmp_path)

    def refuse(params):
        raise RuntimeError('release is published; its page is frozen with it')
    s3 = FakeS3()
    with pytest.raises(dr_page.PageError) as exc:
        dr_page.push_page(_client(dr_page_set=refuse), s3, 'b', TAG, d)
    msg = str(exc.value)
    assert 'refused after the uploads' in msg and 'swept only by `dr purge`' in msg
    assert _key('depth.pdf', DEPTH) in msg and _key('nz.png', NZ) in msg
    assert set(s3.objects) == {_key('depth.pdf', DEPTH), _key('nz.png', NZ)} and s3.deletes == []


def test_push_refuses_a_published_release_before_uploading(tmp_path):
    d = _page_dir(tmp_path)
    s3 = FakeS3()
    with pytest.raises(dr_page.PageError, match='is published'):
        dr_page.push_page(_client('published'), s3, 'b', TAG, d)
    assert s3.puts == []


def test_push_refuses_a_freezing_release_and_an_unknown_tag_and_accepts_failed(tmp_path):
    d = _page_dir(tmp_path)
    with pytest.raises(dr_page.PageError, match='is freezing'):
        dr_page.push_page(_client('freezing'), FakeS3(), 'b', TAG, d)
    with pytest.raises(dr_page.PageError, match='no release'):
        dr_page.push_page(FakePostgrest(tables={'dr_releases': []}), FakeS3(), 'b', TAG, d)
    assert dr_page.push_page(_client('failed', dr_page_set=_ok), FakeS3(), 'b', TAG, d)['assets'] == 2


def test_dry_run_uploads_nothing(tmp_path):
    d = _page_dir(tmp_path)
    c = _client()
    s3 = FakeS3()
    out = dr_page.push_page(c, s3, 'b', TAG, d, dry_run=True)
    assert out['dry_run'] is True and [a['storage_key'] for a in out['assets']] == [_key('depth.pdf', DEPTH), _key('nz.png', NZ)]
    assert s3.puts == [] and not c.rpcs


def test_fetch_and_format_page():
    c = FakePostgrest(tables={
        'dr_releases': [{'tag': TAG, 'state': 'ready', 'page_md': '# Hi'}],
        'dr_manifest': [{'tag': TAG, 'component': 'page', 'storage_key': _key('nz.png', NZ),
                         'content_hash': _sha(NZ), 'size_bytes': 10, 'updated_at': 'x'},
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
    assert 'Dry run' in res.output and _key('nz.png', NZ) in res.output
    res = runner.invoke(main_cli.cli, ['dr', 'page', 'show', TAG, '--json'])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)['page_md'] == '# Hi'


def test_cli_page_push_checks_the_release_before_the_credentials(tmp_path, monkeypatch):
    d = _page_dir(tmp_path)
    asked = []
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: ({}, _client('published')))
    monkeypatch.setattr(main_cli, '_dr_osn_s3', lambda config: asked.append(1) or (None, 'no [r2_osn] block'))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d)])
    assert res.exit_code == 2 and 'is published' in res.output and asked == []
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: ({}, _client()))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d)])
    assert res.exit_code == 2 and 'OSN credentials' in res.output and asked == [1]


def test_cli_page_push_reports_uploads_and_deletions(tmp_path, monkeypatch):
    d = _page_dir(tmp_path)
    old_key = f'{PREFIX}0123456789abcdef/old.png'
    c = _client(dr_page_set=lambda p: _ok(p, removed=[old_key]))
    s3 = FakeS3({old_key: b'x'})
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: ({}, c))
    monkeypatch.setattr(main_cli, '_dr_osn_s3', lambda config: (s3, 'campfire-data'))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d)])
    assert res.exit_code == 0, res.output
    assert '2 asset(s) (2 uploaded)' in res.output and 'deleted 1 retired file(s)' in res.output
    assert s3.deletes == [old_key]


def test_cli_page_push_local_is_refused_without_dry_run(tmp_path, monkeypatch):
    d = _page_dir(tmp_path)
    seen = {}
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: (seen.__setitem__('local', local), ({}, _client()))[1])
    res = CliRunner().invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d), '--local'])
    assert res.exit_code == 2 and '--local refused without --dry-run (the bucket is the shared one)' in res.output
    assert seen == {}                                   # refused before any client is built
    res = CliRunner().invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d), '--local', '--dry-run'])
    assert res.exit_code == 0, res.output
    assert seen['local'] is True and 'Dry run' in res.output


def test_local_mode_honours_the_native_stack_env(monkeypatch, tmp_path):
    monkeypatch.setenv('CAMPFIRE_LOCAL_SUPABASE_URL', 'http://127.0.0.1:23603/')
    monkeypatch.setenv('CAMPFIRE_LOCAL_SUPABASE_SERVICE_ROLE_KEY', 'sk-native')
    monkeypatch.delenv('CAMPFIRE_DEPLOY_MODE', raising=False)
    cfg = deploy_config.load_config(None, local=True)
    assert cfg['supabase']['url'] == 'http://127.0.0.1:23603'
    assert cfg['supabase']['service_role_key'] == 'sk-native'
    monkeypatch.delenv('CAMPFIRE_LOCAL_SUPABASE_URL')
    monkeypatch.delenv('CAMPFIRE_LOCAL_SUPABASE_SERVICE_ROLE_KEY')
    assert deploy_config.load_config(None, local=True)['supabase']['url'] == 'http://127.0.0.1:54321'
