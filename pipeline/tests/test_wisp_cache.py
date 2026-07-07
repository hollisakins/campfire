"""Tests for the wisp template fetch/cache layer (``nircam/wisp_cache.py``).

No network: downloads are exercised against ``file://`` URLs (urllib handles
them), and the manifest is monkeypatched in-memory. ``CAMPFIRE_ROOT`` is pointed
at a tmp dir so the cache lands under ``<tmp>/cache/wisps/``.
"""

import hashlib
import os
from pathlib import Path

import pytest

from campfire_pipeline.nircam import wisp_cache


def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


@pytest.fixture
def campfire_root(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMPFIRE_ROOT', str(tmp_path))
    return tmp_path


def _publish(srcdir, files):
    """Write ``{name: bytes}`` into srcdir and return (base_url, manifest_dict)."""
    srcdir.mkdir(parents=True, exist_ok=True)
    templates = {}
    for name, content in files.items():
        (srcdir / name).write_bytes(content)
        templates[name] = (_sha256_bytes(content), len(content))
    return Path(srcdir).as_uri(), templates


def _patch_manifest(monkeypatch, base_url, templates):
    monkeypatch.setattr(wisp_cache, '_manifest', lambda: (base_url, templates))


def test_build_names_shape_and_case():
    names = wisp_cache.build_names('nrca3', 'f150w')
    assert names == [
        'WISP_NRCA3_F150W_CLEAR_masked.fits',
        'WISP_NRCA3_F150W_CLEAR_masked_smoothed_1x1.fits',
        'WISP_NRCA3_F150W_CLEAR_masked_smoothed_2x2.fits',
        'WISP_NRCA3_F150W_CLEAR_masked_smoothed_3x3.fits',
    ]


def test_required_templates_gates_on_manifest(monkeypatch):
    present = wisp_cache.build_names('nrca3', 'f150w')
    templates = {n: ('deadbeef', 1) for n in present}
    _patch_manifest(monkeypatch, 'file:///nowhere', templates)

    # Characterized pair -> the full 4-name set.
    assert wisp_cache.required_templates('nrca3', 'f150w') == present
    # Uncharacterized pair -> empty (a legitimate, visible "no template").
    assert wisp_cache.required_templates('nrca3', 'f444w') == []


def test_ensure_downloads_and_verifies(campfire_root, monkeypatch, tmp_path):
    names = wisp_cache.build_names('nrca3', 'f150w')
    files = {n: f'payload-{n}'.encode() for n in names}
    base_url, templates = _publish(tmp_path / 'remote', files)
    _patch_manifest(monkeypatch, base_url, templates)

    fetched = wisp_cache.ensure(names)
    assert fetched == len(names)

    cdir = Path(wisp_cache.cache_dir())
    for n in names:
        assert (cdir / n).read_bytes() == files[n]

    # Second call is a no-op: files already present, nothing re-downloaded.
    assert wisp_cache.ensure(names) == 0
    # No leftover .part temp files.
    assert not list(cdir.glob('*.part'))


def test_ensure_checksum_mismatch_raises(campfire_root, monkeypatch, tmp_path):
    name = 'WISP_NRCA3_F150W_CLEAR_masked.fits'
    base_url, templates = _publish(tmp_path / 'remote', {name: b'real-bytes'})
    # Corrupt the manifest's expected sha so the download can't match.
    templates[name] = ('0' * 64, templates[name][1])
    _patch_manifest(monkeypatch, base_url, templates)

    with pytest.raises(wisp_cache.WispTemplateError, match='checksum mismatch'):
        wisp_cache.ensure([name])
    # A failed download leaves nothing behind — not even a partial file.
    cdir = Path(wisp_cache.cache_dir())
    assert not (cdir / name).exists()
    assert not list(cdir.glob('*.part'))


def test_ensure_size_mismatch_raises(campfire_root, monkeypatch, tmp_path):
    name = 'WISP_NRCA3_F150W_CLEAR_masked.fits'
    base_url, templates = _publish(tmp_path / 'remote', {name: b'real-bytes'})
    templates[name] = (templates[name][0], 999999)  # wrong size
    _patch_manifest(monkeypatch, base_url, templates)

    with pytest.raises(wisp_cache.WispTemplateError, match='size mismatch'):
        wisp_cache.ensure([name])


def test_ensure_no_base_url_raises(campfire_root, monkeypatch):
    name = 'WISP_NRCA3_F150W_CLEAR_masked.fits'
    _patch_manifest(monkeypatch, '', {name: ('a' * 64, 10)})
    with pytest.raises(wisp_cache.WispTemplateError, match='no base_url'):
        wisp_cache.ensure([name])


def test_resolve_prefers_cache_then_legacy(campfire_root, monkeypatch, tmp_path):
    _patch_manifest(monkeypatch, 'file:///nowhere', {})
    name = 'WISP_NRCA3_F150W_CLEAR_masked.fits'
    legacy = tmp_path / 'legacy'
    legacy.mkdir()

    # Nothing anywhere.
    assert wisp_cache.resolve(name, str(legacy)) is None

    # Legacy-only: resolved from the legacy dir.
    (legacy / name).write_bytes(b'legacy')
    assert wisp_cache.resolve(name, str(legacy)) == str(legacy / name)

    # Cache wins over legacy once present.
    cdir = Path(wisp_cache.cache_dir())
    (cdir / name).write_bytes(b'cached')
    assert wisp_cache.resolve(name, str(legacy)) == str(cdir / name)


def test_resolve_legacy_skips_fetch(campfire_root, monkeypatch, tmp_path):
    """A template already in the legacy dir is used as-is, never re-downloaded."""
    names = wisp_cache.build_names('nrcb4', 'f200w')
    # Manifest lists them but the "remote" dir is empty -> a fetch would fail.
    _, templates = _publish(tmp_path / 'remote', {})
    templates = {n: ('x' * 64, 1) for n in names}
    _patch_manifest(monkeypatch, (tmp_path / 'remote').as_uri(), templates)

    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    for n in names:
        (legacy / n).write_bytes(b'legacy')

    # All present in legacy -> ensure fetches nothing and does not raise.
    assert wisp_cache.ensure(names, legacy_dir=str(legacy)) == 0


def test_ensure_for_pairs_expands_and_skips_non_wisp(campfire_root, monkeypatch, tmp_path):
    a3 = wisp_cache.build_names('nrca3', 'f150w')
    files = {n: f'x{n}'.encode() for n in a3}
    base_url, templates = _publish(tmp_path / 'remote', files)
    _patch_manifest(monkeypatch, base_url, templates)

    # nrca1 is not a wisp detector; nrca3/f444w is not in the manifest.
    pairs = {('nrca3', 'f150w'), ('nrca1', 'f150w'), ('nrca3', 'f444w')}
    assert wisp_cache.ensure_for_pairs(pairs) == len(a3)
