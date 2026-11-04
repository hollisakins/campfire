"""Tests for the calibration-star fits of the diffraction model (``nirspec.optics_calibration``):
a synthetic star through the closed-form stand-in (``optics_standin``) is recovered by the
per-range fit, its stacking samples match the model on noiseless data, and the stack metric
reads a known residual."""

import os

import numpy as np
import pytest

from campfire_pipeline.nirspec import optics_calibration as OC
from campfire_pipeline.nirspec import profile_fit as PF

from test_nirspec_profile_fit import NX, NY, _exposures, _spectrum

STAR = dict(centre=0.1, drift=0.0, width=0.05, width_change=0.0)
OFFSETS = [0.0, 0.02, -0.015]


def _star(noise=1.0, flux=300.0, seed=4):
    """Three nods of a star with its own offset per exposure, no centre law (the centre is
    where the light is, as the calibration fits it)."""
    exps = _exposures(src_y=0.1)
    M = PF.ProfileModel(exps, chromatic=True, centre_law_tables=PF.NO_CENTRE_LAW)
    lam = M.lam_grid
    R = PF.FitResult(params=dict(STAR, pivot=M.pivot, centre_prior=M.c_prior), eoff=np.array(OFFSETS), lam=lam,
                     flux=flux * _spectrum(lam), flux_err=np.zeros_like(lam), chi2=0.0, npix=0, nfree=0,
                     chromatic=True, model=M)
    rng = np.random.default_rng(seed)
    fn = [noise * rng.normal(size=(NY, NX)) for _ in exps]
    for k, e in enumerate(exps):
        e.diff = PF.model_frame(R, k, applied_pathloss=False) + fn[k] - np.mean([fn[j] for j in e.partners], axis=0)
        e.var = np.full((NY, NX), max(noise, 1e-3) ** 2)
    return exps


def test_range_fit_recovers_a_star():
    RF = OC.RangeFit(_star(), (1.0, 4.0))
    s = RF.start()
    g = RF.fit(x0=s['_x'], use=s['_use'])
    assert g['c0'] == pytest.approx(STAR['centre'], abs=0.01)
    assert g['w'] == pytest.approx(STAR['width'], abs=0.01)
    assert np.allclose(np.array(g['offsets']) - g['offsets'][0], OFFSETS, atol=0.01)
    assert 0.8 < g['chi2_raw'] / g['ndof'] < 1.2          # chi2 itself is on the floored variances
    assert g['peak_snr'] > 20


def test_samples_match_the_model_without_noise():
    RF = OC.RangeFit(_star(noise=1e-6), (1.0, 4.0))
    g = RF.fit(x0=[STAR['centre'], 0.0], clip_rounds=0)
    s = RF.samples(g)
    core = np.abs(s['u']) < 1.5
    assert s['u'].size > 100
    assert np.allclose(s['d'][core], s['m'][core], rtol=1e-3, atol=1e-5 * s['m'].max())


def test_clean_keys_and_stack_metric(tmp_path):
    rec = lambda key, snr, c, sat: dict(key=key, detector='NRS1', ranges=[dict(range=2, lam=[1.5, 2.0], peak_snr=snr,  # noqa: E731
                                                                               c_node=c, sat=sat)])
    recs = [rec('a', 50, 0.1, 0.0), rec('b', 40, -0.2, 0.0), rec('c', 30, 0.0, 0.0),
            rec('faint', 5, 0.0, 0.0), rec('edge', 50, 0.4, 0.0), rec('saturated', 50, 0.0, 0.02)]
    keys = OC.clean_keys(recs)
    assert keys == {('NRS1', 2): ['a', 'b', 'c']}
    os.makedirs(tmp_path / 'samples')
    u = np.linspace(-4, 4, 4000)
    m = np.exp(-0.5 * (u / 0.6) ** 2)
    for k in ('a', 'b', 'c'):                          # data 1 % of the peak above the model, everywhere
        np.savez(tmp_path / 'samples' / f'{k}.npz', r2_u=u, r2_d=m + 0.01, r2_m=m)
    met = OC.stack_metric(str(tmp_path / 'samples'), keys)
    assert met[('NRS1', 2)]['n'] == 3
    peak = max(met[('NRS1', 2)]['model'])                # the binned model's peak, just under 1
    assert met[('NRS1', 2)]['max'] == pytest.approx(1.0 / peak, rel=1e-3)
