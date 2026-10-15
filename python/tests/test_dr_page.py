"""Data releases P6b-3: the release page document (`campfire dr page`). The
database side is supabase/tests/check_dr_page.sql."""
import hashlib
import json

import pytest
from click.testing import CliRunner

import campfire.cli as main_cli
from campfire import dr_page
from campfire.deploy import config as deploy_config
from tests.fakes import FakePostgrest

TAG = 'DR2026.0-rehearsal'
PREFIX = f'dr/{TAG}/page/'


class _S3Error(Exception):
    def __init__(self, code):
        super().__init__(f'http {code}')
        self.response = {'ResponseMetadata': {'HTTPStatusCode': code}}


class FakeS3:
    """An in-memory bucket honouring If-None-Match, HEAD, DELETE and LIST."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})      # key -> bytes
        self.puts = []
        self.deletes = []

    def put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch=None):  # noqa: N803 - boto3 names
        if IfNoneMatch == '*' and Key in self.objects:
            raise _S3Error(412)
        data = Body if isinstance(Body, bytes) else Body.read()
        self.objects[Key] = data
        self.puts.append((Key, data, ContentType))

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _S3Error(404)
        data = self.objects[Key]
        return {'ContentLength': len(data), 'ETag': f'"{hashlib.md5(data).hexdigest()}"'}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)
        self.deletes.append(Key)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {'Contents': [{'Key': k} for k in keys], 'IsTruncated': False}


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


def _ok(params):
    return {'tag': TAG, 'state': 'frozen', 'markdown_bytes': 20, 'assets': len(params['p_assets'])}


def test_collect_page_dir_reads_each_asset_once_and_hashes_the_bytes(tmp_path):
    d = _page_dir(tmp_path)
    doc = dr_page.collect_page_dir(d)
    assert doc.markdown.startswith('# DR2026.0')
    assert [a.name for a in doc.assets] == ['depth.pdf', 'nz.png']
    png = next(a for a in doc.assets if a.name == 'nz.png')
    assert png.data == b'\x89PNGnz.png'
    assert png.content_hash == 'sha256:' + hashlib.sha256(png.data).hexdigest()
    assert png.content_type == 'image/png' and png.size_bytes == 10
    assert png.storage_key(TAG) == PREFIX + 'nz.png'


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
    assert out['assets'] == 2 and out['uploads'] == {'depth.pdf': 'uploaded', 'nz.png': 'uploaded'} and out['swept'] == []
    assert [k for k, _, _ in s3.puts] == [PREFIX + 'depth.pdf', PREFIX + 'nz.png']
    assert s3.puts[1][1] == b'\x89PNGnz.png' and s3.puts[1][2] == 'image/png'
    assert seen['p_tag'] == TAG and seen['p_markdown'].startswith('# DR2026.0')
    assert [a['name'] for a in seen['p_assets']] == ['depth.pdf', 'nz.png']
    assert all(a['storage_key'] == PREFIX + a['name'] and a['backend'] == 'osn' and a['bucket'] == 'data'
               and a['content_hash'] == 'sha256:' + hashlib.sha256(s3.objects[a['storage_key']]).hexdigest()
               for a in seen['p_assets'])


def test_a_412_with_the_same_bytes_is_kept(tmp_path):
    d = _page_dir(tmp_path)
    s3 = FakeS3({PREFIX + 'nz.png': b'\x89PNGnz.png'})
    out = dr_page.push_page(_client(dr_page_set=_ok), s3, 'b', TAG, d)
    assert out['uploads'] == {'depth.pdf': 'uploaded', 'nz.png': 'kept'}
    assert [k for k, _, _ in s3.puts] == [PREFIX + 'depth.pdf'] and s3.deletes == []


def test_a_412_with_different_bytes_is_replaced_on_an_editable_release(tmp_path):
    d = _page_dir(tmp_path)
    s3 = FakeS3({PREFIX + 'nz.png': b'old bytes of the same name'})
    out = dr_page.push_page(_client(dr_page_set=_ok), s3, 'b', TAG, d)
    assert out['uploads']['nz.png'] == 'replaced'
    assert s3.deletes == [PREFIX + 'nz.png'] and s3.objects[PREFIX + 'nz.png'] == b'\x89PNGnz.png'


def test_a_412_with_different_bytes_is_not_replaced_once_the_release_closed(tmp_path):
    d = _page_dir(tmp_path)
    c = _client(dr_page_set=_ok)
    s3 = FakeS3({PREFIX + 'nz.png': b'old bytes'})
    # the release closes between the first check and the conflicting PUT
    original = dr_page.check_editable
    calls = {'n': 0}

    def closing(client, tag):
        calls['n'] += 1
        if calls['n'] > 1:
            c.tables['dr_releases'][0]['state'] = 'published'
        return original(client, tag)
    import campfire.dr_page as mod
    mod_check = mod.check_editable
    mod.check_editable = closing
    try:
        with pytest.raises(dr_page.PageError, match='is published'):
            dr_page.push_page(c, s3, 'b', TAG, d)
    finally:
        mod.check_editable = mod_check
    assert s3.deletes == [] and s3.objects[PREFIX + 'nz.png'] == b'old bytes'


def test_stale_names_under_the_prefix_are_swept_after_the_set(tmp_path):
    d = _page_dir(tmp_path)
    s3 = FakeS3({PREFIX + 'old_figure.png': b'left by an earlier push', 'dr/other/page/x.png': b'x'})
    out = dr_page.push_page(_client(dr_page_set=_ok), s3, 'b', TAG, d)
    assert out['swept'] == [PREFIX + 'old_figure.png']
    assert PREFIX + 'old_figure.png' not in s3.objects and 'dr/other/page/x.png' in s3.objects


def test_rpc_refusal_after_upload_names_the_keys_left_behind(tmp_path):
    d = _page_dir(tmp_path)

    def refuse(params):
        raise RuntimeError('release is published; its page is frozen with it')
    s3 = FakeS3()
    with pytest.raises(dr_page.PageError) as exc:
        dr_page.push_page(_client(dr_page_set=refuse), s3, 'b', TAG, d)
    msg = str(exc.value)
    assert 'refused after the uploads' in msg and PREFIX + 'depth.pdf' in msg and PREFIX + 'nz.png' in msg
    assert set(s3.objects) == {PREFIX + 'depth.pdf', PREFIX + 'nz.png'}    # nothing swept, nothing hidden


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
    assert out['dry_run'] is True and len(out['assets']) == 2
    assert s3.puts == [] and not c.rpcs


def test_fetch_and_format_page():
    c = FakePostgrest(tables={
        'dr_releases': [{'tag': TAG, 'state': 'ready', 'page_md': '# Hi'}],
        'dr_manifest': [{'tag': TAG, 'component': 'page', 'storage_key': PREFIX + 'nz.png',
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
    assert 'Dry run' in res.output and PREFIX + 'nz.png' in res.output
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


def test_cli_page_push_reports_uploads_and_passes_local(tmp_path, monkeypatch):
    d = _page_dir(tmp_path)
    seen = {}
    c = _client(dr_page_set=_ok)
    s3 = FakeS3({PREFIX + 'stale.png': b'x'})
    monkeypatch.setattr(main_cli, '_dr_client', lambda local, what: (seen.__setitem__('local', local), ({}, c))[1])
    monkeypatch.setattr(main_cli, '_dr_osn_s3', lambda config: (s3, 'campfire-data'))
    res = CliRunner().invoke(main_cli.cli, ['dr', 'page', 'push', TAG, str(d), '--local'])
    assert res.exit_code == 0, res.output
    assert seen['local'] is True
    assert '2 asset(s) (2 uploaded)' in res.output and 'deleted 1 stale file(s)' in res.output


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
