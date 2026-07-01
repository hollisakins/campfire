"""Lock the version-free NIRCam mosaic naming contract (epic #261, N2 / D3).

The `version` axis is retired: one canonical mosaic name per
(field, filter, tile, pixel_scale), no `_<version>_` segment, no `_latest_`
alias. These tests pin that so a regression can't silently reintroduce it.
"""
import pytest

from campfire_pipeline.nircam.manifest import (
    DEFAULT_MOSAIC_NAME, build_mosaic_name, create_manifest,
)


def test_default_template_has_no_version_placeholder():
    assert '[version]' not in DEFAULT_MOSAIC_NAME
    assert DEFAULT_MOSAIC_NAME == \
        'mosaic_nircam_[filter]_[field_name]_[pixel_scale]_[tile]'


def test_build_mosaic_name_is_version_free():
    name = build_mosaic_name('f444w', 'cosmos', '30mas', 'A1')
    assert name == 'mosaic_nircam_f444w_cosmos_30mas_A1'
    assert '_v0_1_' not in name and 'latest' not in name


def test_build_mosaic_name_honors_template_override():
    name = build_mosaic_name('f200w', 'rj0911', '60mas', 'venus',
                             template='m_[filter]_[tile]')
    assert name == 'm_f200w_venus'


def test_build_mosaic_name_multiunderscore_field():
    # A field whose name contains underscores must survive (the builder does a
    # placeholder substitution, not a positional split).
    name = build_mosaic_name('f356w', 'ember_egs_p1', '30mas', 'tile3')
    assert name == 'mosaic_nircam_f356w_ember_egs_p1_30mas_tile3'


class _FakeField:
    name = 'cosmos'


def test_create_manifest_drops_version_key():
    manifest = create_manifest(
        'mosaic_nircam_f444w_cosmos_30mas_A1', _FakeField(), 'f444w', 'A1',
        '30mas', input_files=[], stage_config={'resample': {}})
    assert 'version' not in manifest
    assert manifest['mosaic_name'] == 'mosaic_nircam_f444w_cosmos_30mas_A1'
    assert manifest['tile'] == 'A1'
    assert manifest['pixel_scale'] == '30mas'
