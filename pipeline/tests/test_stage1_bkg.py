"""Tests for the stage-1 background-fit helpers and stage-1 ramp config.

Targets the pure-Python building blocks extracted from
``subtract_background_from_rate_file`` (``_fit_picture_frame``,
``_fit_col_1f``, ``_fit_row_1f``, ``_iterate_background``) on synthetic 2D
arrays, plus the ``[nirspec.stage1.ramp]`` config wiring. The jwst-dependent
``_iterate_background`` test is skipped when jwst is unavailable.
"""

import numpy as np
import pytest

from campfire_pipeline.nirspec import stage1


def _gradient_template(ny=2048, nx=64, seed=0):
    """Deterministic template with per-pixel variation (ptp > 0 per quarter)."""
    rng = np.random.default_rng(seed)
    base = np.linspace(0.0, 1.0, ny)[:, None] + np.linspace(0.0, 0.5, nx)[None, :]
    return (base + 0.01 * rng.standard_normal((ny, nx))).astype(np.float64)


class TestFitPictureFrame:
    def test_recovers_linear_template_per_quarter(self):
        template = _gradient_template()
        mask = np.full(template.shape, True)
        # Same coeff/pedestal in every quarter -> exact recovery.
        data = 2.0 * template + 5.0
        model = stage1._fit_picture_frame(data, mask, template, sigma_clip=False)
        np.testing.assert_allclose(model, data, rtol=0, atol=1e-6)

    def test_fits_each_quarter_independently(self):
        template = _gradient_template()
        mask = np.full(template.shape, True)
        data = np.zeros_like(template)
        qh = template.shape[0] // 4
        coeffs = [1.0, 2.0, 3.0, 4.0]
        peds = [0.0, 1.0, -2.0, 10.0]
        for i, (c, p) in enumerate(zip(coeffs, peds)):
            sl = slice(i * qh, (i + 1) * qh)
            data[sl, :] = c * template[sl, :] + p
        model = stage1._fit_picture_frame(data, mask, template, sigma_clip=False)
        np.testing.assert_allclose(model, data, rtol=0, atol=1e-6)


class TestFitCol1f:
    def test_median_method_returns_column_offsets(self):
        ny, nx = 100, 20
        col_offsets = np.arange(nx, dtype=float)
        data = np.broadcast_to(col_offsets, (ny, nx)).copy()
        mask = np.full((ny, nx), True)
        col = stage1._fit_col_1f(data, mask, col_1f_method='median', use_pictureframe=False)
        assert col.shape == (1, nx)
        np.testing.assert_allclose(col[0], col_offsets)

    def test_template_without_pictureframe_falls_back_to_median(self):
        ny, nx = 50, 10
        data = np.zeros((ny, nx)) + np.arange(nx)
        mask = np.full((ny, nx), True)
        # template requested but use_pictureframe=False -> median fallback
        col = stage1._fit_col_1f(data, mask, template=None,
                                 col_1f_method='template', use_pictureframe=False)
        np.testing.assert_allclose(col[0], np.arange(nx))

    def test_masked_pixels_excluded_from_median(self):
        ny, nx = 40, 4
        data = np.zeros((ny, nx))
        data[0, :] = 1000.0  # outlier row
        mask = np.full((ny, nx), True)
        mask[0, :] = False   # exclude the outlier
        col = stage1._fit_col_1f(data, mask, col_1f_method='median', use_pictureframe=False)
        np.testing.assert_allclose(col[0], np.zeros(nx))


class TestFitRow1f:
    def test_returns_row_offsets(self):
        ny, nx = 30, 50
        row_offsets = np.arange(ny, dtype=float)
        data = np.broadcast_to(row_offsets[:, None], (ny, nx)).copy()
        mask = np.full((ny, nx), True)
        row = stage1._fit_row_1f(data, mask)
        assert row.shape == (ny, 1)
        np.testing.assert_allclose(row[:, 0], row_offsets)

    def test_fully_masked_row_is_zeroed_or_finite(self):
        ny, nx = 10, 10
        data = np.zeros((ny, nx))
        mask = np.full((ny, nx), True)
        mask[5, :] = False  # fully masked row
        row = stage1._fit_row_1f(data, mask)
        assert np.all(np.isfinite(row))


class TestIterateBackground:
    def test_removes_injected_background(self):
        pytest.importorskip("jwst")  # _iterate_background uses clip_to_background
        ny, nx = 2048, 64
        template = _gradient_template(ny, nx)
        col = np.linspace(-1, 1, nx)[None, :]
        row = np.linspace(-0.5, 0.5, ny)[:, None]
        # Injected background: PF + column + row patterns
        bkg = 1.5 * template + col + row
        img = bkg.copy()
        slitmask = np.full((ny, nx), True)

        bkg_total, mask, components = stage1._iterate_background(
            img, slitmask, n_iter=3,
            do_picture_frame=True, template=template,
            subtract_2d=False, do_col_1f=True, col_1f_method='template',
            do_row_1f=True, verbose=False,
        )
        residual = img - bkg_total
        # The fit should remove the bulk of the injected structure.
        assert np.std(residual) < 0.1 * np.std(bkg)
        assert components['pictureframe'] is not None
        assert components['col'] is not None
        assert components['row'] is not None
        assert components['bkg2d'] is None

    def test_disabled_components_are_none(self):
        pytest.importorskip("jwst")
        ny, nx = 2048, 32
        img = np.zeros((ny, nx))
        slitmask = np.full((ny, nx), True)
        bkg_total, mask, components = stage1._iterate_background(
            img, slitmask, n_iter=1,
            do_picture_frame=False, template=None,
            subtract_2d=False, do_col_1f=False, do_row_1f=False,
        )
        assert components == {
            'pictureframe': None, 'bkg2d': None, 'col': None, 'row': None,
        }
        np.testing.assert_allclose(bkg_total, 0.0)


class _DummyObs:
    """Minimal stand-in for an Observation with no per-obs stage overrides."""
    stage_overrides = {}


class TestRampConfig:
    def test_ramp_table_resolves_via_get_stage_config(self):
        from campfire_pipeline.config import load_config, get_stage_config
        cfg = load_config()
        stage_config = get_stage_config('stage1', cfg, _DummyObs())
        assert 'ramp' in stage_config
        ramp = stage_config['ramp']
        for key in ('do_picture_frame', 'do_col_1f', 'do_row_1f',
                    'col_1f_method', 'n_iter', 'plot'):
            assert key in ramp
        assert ramp['n_iter'] == 1

    def test_obsolete_keys_removed(self):
        from campfire_pipeline.config import load_config
        cfg = load_config()
        stage1_cfg = cfg['nirspec']['stage1']
        assert 'do_clean_flicker_noise' not in stage1_cfg
        assert 'mask_science_regions' not in stage1_cfg

    def test_partial_ramp_override_preserves_global_keys(self):
        """A per-obs override of one ramp key must not clobber the rest of the
        ramp table (deep merge in get_stage_config)."""
        from campfire_pipeline.config import get_stage_config

        config = {'nirspec': {'stage1': {
            'plot': True,
            'ramp': {'do_picture_frame': True, 'box_size': 128,
                     'bkg_estimator': 'mean', 'n_iter': 1,
                     'col_1f_method': 'template'},
        }}}

        class Obs:
            stage_overrides = {'stage1': {'ramp': {'n_iter': 3}}}

        merged = get_stage_config('stage1', config, Obs())
        assert merged['ramp']['n_iter'] == 3          # override applied
        assert merged['ramp']['box_size'] == 128      # global preserved
        assert merged['ramp']['bkg_estimator'] == 'mean'
        assert merged['ramp']['do_picture_frame'] is True
        # the shared config dict must not be mutated
        assert config['nirspec']['stage1']['ramp']['n_iter'] == 1

    def test_partial_wavelength_override_preserves_other_gratings(self):
        from campfire_pipeline.config import get_stage_config

        config = {'nirspec': {'stage1': {
            'override_wavelength_range': {'G140M': [0.7, 2.5], 'G395H': [2.87, 5.5]},
        }}}

        class Obs:
            stage_overrides = {'stage1': {'override_wavelength_range': {'G140M': [0.8, 2.4]}}}

        merged = get_stage_config('stage1', config, Obs())
        assert merged['override_wavelength_range']['G140M'] == [0.8, 2.4]
        assert merged['override_wavelength_range']['G395H'] == [2.87, 5.5]


class TestDiagnosticSlopeWeights:
    """The ramp diagnostic accumulates with OLS slope weights so the result is
    a true per-group rate (not a uniform mean, which overstates by ~ngroup/2)."""

    def test_slope_weights_recover_linear_rate(self):
        ng = 20
        t = np.arange(ng, dtype=float)
        wnum = t - t.mean()
        wden = float(np.sum(wnum * wnum))
        slope_w = wnum / wden

        b = 3.7  # true per-group rate
        img = b * t  # zero-subtracted accumulated signal at each group
        rate = float(np.sum(slope_w * img))
        np.testing.assert_allclose(rate, b, rtol=1e-12)

        # The old uniform-mean estimator overstates the rate by ~ng/2.
        uniform_mean = float(np.mean(img[1:]))
        assert uniform_mean > 5 * b


class TestRampStepGuards:
    """CampfireRampBkgStep self-guards: it only corrects NIRSpec full-frame
    multi-group ramps and otherwise returns the model untouched."""

    def _ramp(self, shape, instrument='NIRSPEC', subarray='FULL'):
        RampModel = pytest.importorskip(
            "stdatamodels.jwst.datamodels", reason="jwst datamodels required"
        ).RampModel
        m = RampModel(np.zeros(shape, dtype=np.float32))
        m.meta.instrument.name = instrument
        m.meta.subarray.name = subarray
        return m

    def _step(self):
        pytest.importorskip("jwst")
        from campfire_pipeline.nirspec.detector1 import CampfireRampBkgStep
        return CampfireRampBkgStep()

    def test_non_nirspec_unchanged(self):
        step = self._step()
        m = self._ramp((1, 3, 8, 8), instrument='MIRI')
        out = step.process(m)
        np.testing.assert_array_equal(out.data, np.zeros((1, 3, 8, 8), np.float32))

    def test_non_full_subarray_unchanged(self):
        step = self._step()
        m = self._ramp((1, 3, 8, 8), subarray='SUB512')
        out = step.process(m)
        np.testing.assert_array_equal(out.data, np.zeros((1, 3, 8, 8), np.float32))

    def test_single_group_unchanged(self):
        step = self._step()
        m = self._ramp((1, 1, 8, 8))
        out = step.process(m)
        np.testing.assert_array_equal(out.data, np.zeros((1, 1, 8, 8), np.float32))
