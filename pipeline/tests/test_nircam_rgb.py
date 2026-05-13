"""Tests for the NIRCam RGB compositor (`cfpipe nircam rgb`)."""

import os

import numpy as np
import pytest

from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.rgb import (
    _find_mosaic,
    _resolve_pixel_scale_str,
    _validate_rgb_config,
)
from campfire_pipeline.nircam.trilogy import (
    RGBConfig,
    apply_rgb_stretch,
    compute_rgb_stretch_params,
)


def _make_rgb_config():
    return RGBConfig(
        filter_channels={
            'f444w': {'color': np.array([0.0, 0.4, 0.0])},
            'f356w': {'color': np.array([0.4, 0.2, 0.0])},
            'f150w': {'color': np.array([0.0, 0.4, 1.0])},
        },
        noisesig=2.0,
        noiselum=0.12,
        satpercent=0.01,
    )


def _synthetic_filter_data(rng, shape=(64, 64), bg_rms=1e-3, n_sources=20):
    """Build a positive-definite SCI-like array with a few bright sources."""
    data = rng.normal(loc=0.0, scale=bg_rms, size=shape).astype(np.float32)
    ys = rng.integers(2, shape[0] - 2, size=n_sources)
    xs = rng.integers(2, shape[1] - 2, size=n_sources)
    amps = rng.uniform(0.05, 1.0, size=n_sources).astype(np.float32)
    data[ys, xs] += amps
    return data


class TestApplyRgbStretch:
    def test_shapes_and_dtype(self):
        rng = np.random.default_rng(0)
        cfg = _make_rgb_config()
        per_filter = {f: _synthetic_filter_data(rng) for f in cfg.filter_channels}
        stretch = compute_rgb_stretch_params(per_filter, cfg)

        rgb, alpha = apply_rgb_stretch(per_filter, cfg, stretch)
        assert rgb.shape == (64, 64, 3)
        assert rgb.dtype == np.uint8
        assert alpha.shape == (64, 64)
        assert alpha.dtype == np.uint8
        # Every pixel finite in every filter ⇒ alpha fully opaque.
        assert (alpha == 255).all()

    def test_alpha_zero_where_all_nan(self):
        rng = np.random.default_rng(1)
        cfg = _make_rgb_config()
        per_filter = {f: _synthetic_filter_data(rng) for f in cfg.filter_channels}
        # Punch a NaN hole through every filter at the same patch.
        for arr in per_filter.values():
            arr[10:20, 10:20] = np.nan
        stretch = compute_rgb_stretch_params(per_filter, cfg)
        _, alpha = apply_rgb_stretch(per_filter, cfg, stretch)
        assert (alpha[10:20, 10:20] == 0).all()
        # Outside the hole, pixels are still finite somewhere.
        assert (alpha[:5, :5] == 255).all()

    def test_partial_filter_coverage_keeps_pixel(self):
        """One filter NaN at a pixel ⇒ stretch renormalises across the others."""
        rng = np.random.default_rng(2)
        cfg = _make_rgb_config()
        per_filter = {f: _synthetic_filter_data(rng) for f in cfg.filter_channels}
        per_filter['f444w'][30:40, 30:40] = np.nan
        stretch = compute_rgb_stretch_params(per_filter, cfg)
        rgb, alpha = apply_rgb_stretch(per_filter, cfg, stretch)
        # Pixel still has coverage from f356w/f150w → opaque.
        assert (alpha[30:40, 30:40] == 255).all()
        # And produced *some* RGB signal (not all zero).
        assert rgb[30:40, 30:40].sum() > 0


class TestComputeStretch:
    def test_returns_finite_ordered_params(self):
        rng = np.random.default_rng(3)
        cfg = _make_rgb_config()
        per_filter = {f: _synthetic_filter_data(rng) for f in cfg.filter_channels}
        stretch = compute_rgb_stretch_params(per_filter, cfg)
        assert np.isfinite(stretch.blackpoint)
        assert np.isfinite(stretch.whitepoint)
        assert stretch.blackpoint > 0
        assert stretch.whitepoint > stretch.blackpoint
        assert stretch.noiselum == cfg.noiselum
        assert stretch.rgb_lum_sum.shape == (3,)

    def test_raises_on_all_nan_input(self):
        cfg = _make_rgb_config()
        per_filter = {
            f: np.full((16, 16), np.nan, dtype=np.float32)
            for f in cfg.filter_channels
        }
        with pytest.raises(ValueError, match='No finite pixels'):
            compute_rgb_stretch_params(per_filter, cfg)


class TestPixelScaleResolution:
    def test_string_passthrough(self):
        assert _resolve_pixel_scale_str('30mas') == '30mas'

    def test_arcsec_float_under_one(self):
        assert _resolve_pixel_scale_str(0.06) == '60mas'

    def test_mas_int(self):
        assert _resolve_pixel_scale_str(60) == '60mas'

    def test_bad_string_rejected(self):
        with pytest.raises(ValueError):
            _resolve_pixel_scale_str('60as')


class TestFindMosaic:
    def test_returns_none_when_missing(self, tmp_path):
        assert _find_mosaic(str(tmp_path), 'cosmos', 'f444w', '60mas', 'A1') is None

    def test_picks_latest_version(self, tmp_path):
        for ver in ('v0_1', 'v0_2', 'v0_3'):
            (tmp_path / f'mosaic_nircam_f444w_cosmos_60mas_{ver}_A1_i2d.fits').touch()
        # Add an unrelated file the glob should ignore.
        (tmp_path / 'mosaic_nircam_f444w_cosmos_60mas_v0_3_A1_i2d_thumb.png').touch()
        path = _find_mosaic(str(tmp_path), 'cosmos', 'f444w', '60mas', 'A1')
        assert path is not None
        assert os.path.basename(path) == 'mosaic_nircam_f444w_cosmos_60mas_v0_3_A1_i2d.fits'


class TestFieldRgbParsing:
    def _write_fields_toml(self, tmp_path, body):
        fp = tmp_path / 'fields.toml'
        fp.write_text(body)
        return fp

    def test_loads_rgb_block(self, tmp_path):
        body = """
[ftest]
    filters = ['f444w', 'f356w', 'f150w']
    files = ['jw01727*']
    tangent_point = [150.0, 2.0]

    [ftest.A1]
        rotation = 0
        [ftest.A1.30mas]
            crpix = [100, 100]
            naxis = [200, 200]

    [ftest.rgb]
        noisesig = 2.5
        noiselum = 0.1
        satpercent = 0.05
        [ftest.rgb.channels]
            f444w = [0.0, 0.4, 0.0]
            f356w = [0.4, 0.2, 0.0]
            f150w = [0.0, 0.4, 1.0]
"""
        fp = self._write_fields_toml(tmp_path, body)
        f = Field.load('ftest', fields_file=str(fp))
        assert f.rgb is not None
        assert f.rgb['noisesig'] == 2.5
        assert f.rgb['channels']['f444w'] == [0.0, 0.4, 0.0]
        # tile parsing must not have absorbed `rgb` as a tile
        assert list(f.tiles.keys()) == ['A1']

    def test_missing_rgb_block_is_none(self, tmp_path):
        body = """
[ftest]
    filters = ['f444w']
    files = ['jw01727*']
    tangent_point = [150.0, 2.0]
    [ftest.A1]
        rotation = 0
        [ftest.A1.30mas]
            crpix = [100, 100]
            naxis = [200, 200]
"""
        fp = self._write_fields_toml(tmp_path, body)
        f = Field.load('ftest', fields_file=str(fp))
        assert f.rgb is None

    def test_validate_rejects_non_field_filters(self, tmp_path):
        body = """
[ftest]
    filters = ['f444w']
    files = ['jw01727*']
    tangent_point = [150.0, 2.0]
    [ftest.A1]
        rotation = 0
        [ftest.A1.30mas]
            crpix = [100, 100]
            naxis = [200, 200]
    [ftest.rgb]
        [ftest.rgb.channels]
            f444w = [1, 0, 0]
            f150w = [0, 0, 1]
"""
        fp = self._write_fields_toml(tmp_path, body)
        f = Field.load('ftest', fields_file=str(fp))
        with pytest.raises(ValueError, match='filters not declared'):
            _validate_rgb_config(f)

    def test_validate_rejects_bad_weight_shape(self, tmp_path):
        body = """
[ftest]
    filters = ['f444w']
    files = ['jw01727*']
    tangent_point = [150.0, 2.0]
    [ftest.A1]
        rotation = 0
        [ftest.A1.30mas]
            crpix = [100, 100]
            naxis = [200, 200]
    [ftest.rgb]
        [ftest.rgb.channels]
            f444w = [1, 0]
"""
        fp = self._write_fields_toml(tmp_path, body)
        f = Field.load('ftest', fields_file=str(fp))
        with pytest.raises(ValueError, match='3-element'):
            _validate_rgb_config(f)
