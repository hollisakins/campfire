"""The forward model (``nirspec.forward``): its components against stage 3's own model, the
general projection against the direct placement of a round source, and the joint solve of
several components with different spectra.

Synthetic three-nod slitlets as in ``test_nirspec_profile_fit`` (the optics stand-in,
``optics_standin``), with the slit's +x pointing east and +y north."""
import numpy as np
import pytest

from campfire_pipeline.nirspec import forward as FW
from campfire_pipeline.nirspec import optics as O
from campfire_pipeline.nirspec import profile_fit as PF
from test_nirspec_profile_fit import _exposures


def _sky_exposures(**kw):
    exps = _exposures(**kw)
    for e in exps:
        e.pa_slit_x, e.pa_slit_y = 90.0, 0.0
    return exps


def _dense(A):
    return np.asarray(A.todense())


def test_slit_gaussian_is_the_profile_model():
    exps = _exposures()
    for e, dy in zip(exps, (0.0, 0.012, -0.009)):     # nods planned at slightly different positions
        e.src_y += dy
    P = PF.ProfileModel(exps, chromatic=True)
    F = FW.ForwardModel(exps)
    p = np.array([0.15, 0.02, np.log(0.2), -0.04])
    a = _dense(P.design(p, np.zeros(len(exps))))
    b = _dense(F.design([FW.SlitGaussian(0.15, 0.02, 0.2, -0.04)]))
    assert np.allclose(a, b, rtol=1e-12, atol=1e-15 * np.abs(a).max())


def test_point_at_the_catalog_position_is_the_narrowest_slit_gaussian():
    exps = _sky_exposures()
    F = FW.ForwardModel(exps)
    a = _dense(F.design([FW.Point()]))
    b = _dense(F.design([FW.SlitGaussian(F.c_prior, width=PF.W_FLOOR)]))
    assert np.allclose(a, b, rtol=1e-12, atol=1e-15 * np.abs(a).max())


def test_a_sky_offset_north_moves_the_source_along_the_slit():
    # +y points north here, so 0.3 slit units north is +0.3 along the slit
    exps = _sky_exposures()
    F = FW.ForwardModel(exps)
    a = _dense(F.design([FW.Point(dx=0.0, dy=0.3 * O.SLIT)]))
    b = _dense(F.design([FW.SlitGaussian(F.c_prior + 0.3, width=PF.W_FLOOR)]))
    assert np.allclose(a, b, rtol=1e-10, atol=1e-12 * np.abs(a).max())


@pytest.mark.parametrize('sigma', [0.02, 0.08])
def test_projection_of_a_round_gaussian_is_its_direct_placement(sigma):
    # the stand-in passes all light across the shutter, so a source much wider than the
    # projection's +-1 arcsec there (PROJ_X_HALF) would lose light the real shutter never passes
    exps = _sky_exposures()
    F = FW.ForwardModel(exps)
    g = FW.Gaussian(dx=0.02, dy=-0.03, sigma=sigma)
    q = F.image_pix[0]
    direct = g.light(F, 0, q['y'], q['lam'], q['dy'])
    general = FW.project(g, F, 0, q['y'], q['lam'], q['dy'])
    assert np.max(np.abs(general - direct)) < 1e-6 * np.max(direct)


@pytest.mark.parametrize('sigma', [0.03, 0.08])
def test_stage3s_gaussian_is_the_projection_of_a_round_source(sigma):
    # the projection places every point of the source through the trace-centre law, so its width
    # lands stretched as positions are; stage 3's Gaussian must land the same way
    exps = _sky_exposures()
    for e, dy in zip(exps, (0.0, 0.012, -0.009)):
        e.src_y += dy
    F = FW.ForwardModel(exps)
    for k, q in F.image_pix.items():
        assert np.ptp(F.trace_stretch(k, q['lam'])) > 1e-3        # the law stretches here
        general = FW.project(FW.Gaussian(sigma=sigma), F, k, q['y'], q['lam'], q['dy'])
        stage3 = FW.SlitGaussian(F.c_prior, width=sigma / O.SLIT).light(F, k, q['y'], q['lam'], q['dy'])
        assert np.max(np.abs(general - stage3)) < 1e-6 * np.max(stage3)


def test_projection_needs_the_slit_orientation():
    exps = _exposures()
    F = FW.ForwardModel(exps)
    q = F.image_pix[0]
    with pytest.raises(ValueError, match='orientation'):
        FW.project(FW.Gaussian(), F, 0, q['y'], q['lam'], q['dy'])


@pytest.mark.parametrize('n,q', [(1.0, 1.0), (4.0, 0.5), (0.5, 0.3)])
def test_sersic_brightness_integrates_to_one(n, q):
    s = FW.Sersic(n=n, r_e=0.1, q=q, pa=30.0)
    h = 0.002
    x = np.arange(-4.0, 4.0, h) + h / 2
    E, N = np.meshgrid(x, x)
    tot = float(np.sum(s.brightness(E, N)) * h * h)
    assert tot == pytest.approx(1.0, abs=0.02 if n == 4.0 else 0.005)   # n = 4 keeps light beyond 4 arcsec


def test_a_sum_shares_one_spectrum_by_its_weights():
    exps = _sky_exposures()
    F = FW.ForwardModel(exps)
    a, b = FW.Point(dy=0.1), FW.Point(dy=-0.1)
    q = F.image_pix[0]
    s = FW.Sum([a, b], weights=[3.0, 1.0]).light(F, 0, q['y'], q['lam'], q['dy'])
    assert np.allclose(s, 0.75 * a.light(F, 0, q['y'], q['lam'], q['dy'])
                       + 0.25 * b.light(F, 0, q['y'], q['lam'], q['dy']))


def _two_component_truth(F):
    lam = F.lam_grid
    return np.vstack([1.0 + 0.3 * lam, 2.0 * np.exp(-0.5 * ((lam - 2.5) / 0.6) ** 2) + 0.2])


def test_two_components_with_different_spectra_are_solved_together():
    exps = _sky_exposures()
    F = FW.ForwardModel(exps)
    comps = [FW.Point(dy=0.0), FW.Gaussian(dy=0.25 * O.SLIT, sigma=0.05)]
    truth = _two_component_truth(F)
    F.pix['d'] = F.design(comps) @ truth.ravel()
    s = F.solve(comps)
    good = s.good
    assert good.all()
    assert np.allclose(s.spectra[good], truth[good], rtol=1e-6, atol=1e-8)


def test_joint_solve_errors_describe_the_scatter():
    exps = _sky_exposures()
    F = FW.ForwardModel(exps)
    comps = [FW.Point(dy=0.0), FW.Gaussian(dy=0.25 * O.SLIT, sigma=0.05)]
    truth = _two_component_truth(F)
    clean = F.design(comps) @ truth.ravel()
    rng = np.random.default_rng(3)
    noise = 0.02
    F.pix['v'] = np.full(F.npix, noise ** 2)
    pulls = []
    for _ in range(20):
        F.pix['d'] = clean + noise * rng.normal(size=F.npix)
        s = F.solve(comps, covariance=True)
        pulls.append(((s.spectra - truth) / s.errors())[s.good])
    rms = float(np.sqrt(np.mean(np.concatenate(pulls) ** 2)))
    assert 0.9 < rms < 1.1
