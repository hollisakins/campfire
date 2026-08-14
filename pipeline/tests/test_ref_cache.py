"""Tests for the generic manifest fetch/cache engine (``common/ref_cache.py``).

Engine-level: a throwaway dataset bound to the ``spike_models`` cache kind
(which doubles as coverage for the new layout entry). Dataset-policy behavior
(wisp naming, required-set gating) stays in ``test_wisp_cache.py``, which also
pins the wrapper's back-compat surface. No network: ``file://`` URLs.
"""

import hashlib
from pathlib import Path

import pytest

from campfire_pipeline.common import ref_cache


@pytest.fixture
def campfire_root(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    return tmp_path


def _publish(srcdir, files):
    """Write ``{name: bytes}`` into srcdir; return (base_url, entries)."""
    srcdir.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name, content in files.items():
        (srcdir / name).write_bytes(content)
        entries[name] = (hashlib.sha256(content).hexdigest(), len(content))
    return Path(srcdir).as_uri(), entries


def _engine(base_url, entries, **kw):
    return ref_cache.RefCache('spike_models', lambda: (base_url, entries), **kw)


def test_load_manifest_reads_toml(tmp_path):
    m = tmp_path / 'm.toml'
    m.write_text(
        'base_url = "https://example.org/set/v1/"\n'
        'version = "v1"\n\n'
        '[[model]]\nname = "A.fits"\nsha256 = "aa"\nbytes = 3\n\n'
        '[[model]]\nname = "B.fits"\nsha256 = "bb"\nbytes = 4\n'
    )
    base_url, entries = ref_cache.load_manifest(m, 'model')
    assert base_url == 'https://example.org/set/v1'  # trailing / stripped
    assert entries == {'A.fits': ('aa', 3), 'B.fits': ('bb', 4)}


def test_load_manifest_unconfigured_is_lazy(tmp_path):
    m = tmp_path / 'm.toml'
    m.write_text('base_url = ""\n')
    assert ref_cache.load_manifest(m, 'model') == ('', {})


def test_ensure_downloads_verifies_and_is_idempotent(campfire_root, tmp_path):
    files = {'A.fits': b'payload-a', 'B.fits': b'payload-b'}
    eng = _engine(*_publish(tmp_path / 'remote', files))

    assert eng.ensure(list(files)) == 2
    cdir = Path(eng.cache_dir())
    assert cdir == campfire_root / 'cache' / 'spike_models'
    for n, content in files.items():
        assert (cdir / n).read_bytes() == content

    # Present files are trusted: second call fetches nothing.
    assert eng.ensure(list(files)) == 0
    assert not list(cdir.glob('*.part'))


def test_ensure_checksum_mismatch_raises_and_leaves_nothing(campfire_root, tmp_path):
    base_url, entries = _publish(tmp_path / 'remote', {'A.fits': b'real'})
    entries['A.fits'] = ('0' * 64, entries['A.fits'][1])
    eng = _engine(base_url, entries)

    with pytest.raises(ref_cache.RefCacheError, match='checksum mismatch'):
        eng.ensure(['A.fits'])
    cdir = Path(eng.cache_dir())
    assert not (cdir / 'A.fits').exists()
    assert not list(cdir.glob('*.part'))


def test_ensure_size_mismatch_raises(campfire_root, tmp_path):
    base_url, entries = _publish(tmp_path / 'remote', {'A.fits': b'real'})
    entries['A.fits'] = (entries['A.fits'][0], 999999)
    with pytest.raises(ref_cache.RefCacheError, match='size mismatch'):
        _engine(base_url, entries).ensure(['A.fits'])


def test_ensure_no_base_url_raises_with_hint(campfire_root):
    eng = _engine('', {'A.fits': ('a' * 64, 4)},
                  no_base_url_hint='Regenerate with build_thing.py.')
    with pytest.raises(ref_cache.RefCacheError, match='no base_url') as ei:
        eng.ensure(['A.fits'])
    assert 'build_thing.py' in str(ei.value)


def test_ensure_unlisted_name_skips(campfire_root, tmp_path):
    eng = _engine(*_publish(tmp_path / 'remote', {'A.fits': b'a'}))
    # Unlisted name is skipped (caller gates "required"), listed one fetches.
    assert eng.ensure(['NOT_LISTED.fits', 'A.fits']) == 1


def test_custom_error_class(campfire_root):
    class MyError(ref_cache.RefCacheError):
        pass
    eng = _engine('', {'A.fits': ('a' * 64, 4)}, error_cls=MyError)
    with pytest.raises(MyError):
        eng.ensure(['A.fits'])


def test_resolve_prefers_cache_then_legacy(campfire_root, tmp_path):
    eng = _engine('file:///nowhere', {})
    legacy = tmp_path / 'legacy'
    legacy.mkdir()

    assert eng.resolve('A.fits', str(legacy)) is None
    (legacy / 'A.fits').write_bytes(b'legacy')
    assert eng.resolve('A.fits', str(legacy)) == str(legacy / 'A.fits')
    cdir = Path(eng.cache_dir())
    (cdir / 'A.fits').write_bytes(b'cached')
    assert eng.resolve('A.fits', str(legacy)) == str(cdir / 'A.fits')


def test_legacy_dir_skips_fetch(campfire_root, tmp_path):
    # Manifest lists the file but the remote doesn't have it: a fetch would
    # fail, so passing ensure() proves the legacy copy short-circuited it.
    eng = _engine((tmp_path / 'empty-remote').as_uri(), {'A.fits': ('x' * 64, 1)})
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    (legacy / 'A.fits').write_bytes(b'legacy')
    assert eng.ensure(['A.fits'], legacy_dir=str(legacy)) == 0


def test_manifest_fn_called_lazily(campfire_root, tmp_path):
    """The manifest callable is consulted per ensure() call, not at bind time."""
    calls = []

    def manifest():
        calls.append(1)
        return _publish(tmp_path / 'remote', {'A.fits': b'a'})

    eng = ref_cache.RefCache('spike_models', manifest)
    assert not calls
    eng.ensure(['A.fits'])
    assert calls
