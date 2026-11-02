"""Tests for the failed-open shutter light on the master-background spectrum
(``failed_open``).

Synthetic single frames of one 3-shutter slitlet (the source moves one shutter between
exposures, as in ``test_nirspec_profile_fit``), with a failed-open shutter's band of
rows across the slitlet carrying a smooth spectrum, different in each exposure. The
band's pixels are handed to the solve directly (no trace maps).
"""

import numpy as np
import pytest

from campfire_pipeline.nirspec import failed_open as FO
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

DY = 0.227
NY, NX = 30, 160
TRUTH = dict(centre=0.15, drift=0.02, width=0.2, width_change=-0.04)
BAND = (4, 9)          # rows of the failed-open shutter trace: the slitlet edge, off the main shutter
BAND_ON = (14, 18)     # across the source shutter of the first exposure (rows 14-17) only


def _frames(noise, band_light, seed=1, band=BAND):
    rows = np.arange(NY)[:, None] * np.ones((1, NX))
    lam = np.linspace(0.8, 5.0, NX)[None, :] * np.ones((NY, 1)) + 0.002 * (rows - NY / 2)
    r_mid = NY / 2
    shift = MSA_SLIT_PITCH / DY
    setups = [((-1, 0, 1), r_mid), ((0, 1, 2), r_mid - shift), ((-2, -1, 0), r_mid + shift)]
    exps = []
    for i, (open_k, r_src) in enumerate(setups):
        exps.append(PF.Exposure(
            name=f'jw00000001001_03101_0000{i + 1}_nrs1_1.fits', group=0, detector='NRS1', grating='PRISM/CLEAR',
            quadrant=1, diff=np.zeros((NY, NX)), var=np.full((NY, NX), noise ** 2), ok=np.ones((NY, NX), bool),
            wav=lam.copy(), y=(rows - r_src) * DY, pl_applied=np.ones((NY, NX)), pl_point=np.full((NY, NX), 0.8),
            src_y=0.1, open_k=open_k, partners=[], use_data=True, cutout=(0, 0, NY, NX), origin=(1, 1)))
    M = PF.ProfileModel(exps, chromatic=True)
    R = PF.FitResult(params=dict(TRUTH, pivot=M.pivot, centre_prior=M.c_prior), eoff=np.zeros(3), lam=M.lam_grid,
                     flux=20.0 * (1.0 + 0.3 * M.lam_grid), flux_err=np.zeros_like(M.lam_grid), chi2=0.0, npix=0,
                     nfree=0, chromatic=True, model=M)
    rng = np.random.default_rng(seed)
    truth = []
    for k, e in enumerate(exps):
        fo = np.zeros((NY, NX))
        fo[band[0]:band[1], :] = band_light[k][None, :]
        truth.append(fo)
        e.diff = PF.model_frame(R, k, applied_pathloss=False) + fo + noise * rng.normal(size=(NY, NX))
    return exps, truth


def _solve(exps, monkeypatch, knot=10, band=BAND):
    iy, ix = np.mgrid[band[0]:band[1], :NX]
    fps = {(k, 'failed_open_1'): (iy.ravel(), ix.ravel()) for k in range(len(exps))}
    monkeypatch.setattr(FO, 'footprints', lambda exps, sid, maps: fps)
    M = PF.ProfileModel(exps, chromatic=True)
    shape = dict(centre_offset=TRUTH['centre'] - M.c_prior, drift=TRUTH['drift'], width=TRUTH['width'],
                 width_change=TRUTH['width_change'])
    PF.fit_fixed(model=M, shape=shape)
    p = PF.shape_vector(shape, M.c_prior, M.priors.width_median)
    return FO.solve(M, p, exps, 1, {}, knot)


@pytest.mark.parametrize('band,on_main', [(BAND, False), (BAND_ON, True)])
def test_recovers_the_shutter_light(monkeypatch, band, on_main):
    """Off the source or across it: the source's own light is not taken for the shutter's."""
    x = np.arange(NX)
    light = [0.5 + 0.3 * np.sin(x / 30.0), 0.2 + 0.002 * x, np.full(NX, 0.4)]
    exps, truth = _frames(0.05, light, band=band)
    L = _solve(exps, monkeypatch, band=band)
    for k in range(3):
        sel = truth[k] > 0
        assert np.median(np.abs(L.pred[k][sel] - truth[k][sel])) < 0.03
        assert np.all(L.dpred[k][sel] > 0)
        assert np.all(L.pred[k][~sel] == 0)
    # significance on the main shutter: only where the band crosses it (the source moves
    # between exposures; BAND_ON is its shutter in the first one only)
    sig = {k: v for (k, _), v in L.shutters.items()}
    assert (sig[0] > 3) == on_main
    assert sig[1] < 3 and sig[2] < 3


def test_no_light_stays_within_its_errors(monkeypatch):
    exps, truth = _frames(0.05, [np.zeros(NX)] * 3, seed=3)
    L = _solve(exps, monkeypatch)
    for k in range(3):
        band = slice(BAND[0], BAND[1])
        z = L.pred[k][band] / L.dpred[k][band]
        assert np.median(np.abs(z)) < 1.0


def test_no_shutter_on_the_trace(monkeypatch):
    exps, _ = _frames(0.05, [np.zeros(NX)] * 3)
    monkeypatch.setattr(FO, 'footprints', lambda exps, sid, maps: {})
    M = PF.ProfileModel(exps, chromatic=True)
    assert FO.solve(M, np.zeros(4), exps, 1, {}, 10) is None


def test_hats():
    j, t = FO._hats([0, 5, 10, 25], 0.0, 10)
    assert list(j) == [0, 0, 1, 2]
    assert t == pytest.approx([0.0, 0.5, 0.0, 0.5])
