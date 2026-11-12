"""The forward model's shape fit (``forward.fit``) and its parameters (``forward.params``): free,
fixed, shared and tied parameters, priors on one parameter and on several, and the pieces a
script uses with its own optimiser (``log_posterior``, ``residuals``).

Synthetic three-nod slitlets (``test_nirspec_forward._sky_exposures``), noiseless unless stated."""
import numpy as np
import pytest

from campfire_pipeline.nirspec import forward as FW
from campfire_pipeline.nirspec import optics as O
from test_nirspec_forward import _sky_exposures, _two_component_truth


def _model_with(comps, truth=None, noise=None, seed=0):
    F = FW.ForwardModel(_sky_exposures())
    resolved = [c.resolved() for c in comps]
    if truth is None:
        truth = _two_component_truth(F)[:len(comps)]
    clean = F.design(resolved) @ truth.ravel()
    F.pix['d'] = clean.copy()
    if noise is not None:
        F.pix['v'] = np.full(F.npix, noise ** 2)
        F.pix['d'] = clean + noise * np.random.default_rng(seed).normal(size=F.npix)
    return F, truth


def test_a_free_width_is_recovered():
    F, truth = _model_with([FW.Gaussian(dy=0.02, sigma=0.08)], noise=1e-3)
    g = FW.Gaussian(dy=FW.Param(0.0, bounds=(-0.2, 0.2)), sigma=FW.Param(0.04, bounds=(0.005, 0.5)))
    r = FW.Fit(F, [g]).run()
    assert r.labels == ['c0.dy', 'c0.sigma']
    assert r.params['c0.sigma'] == pytest.approx(0.08, abs=4 * r.errors['c0.sigma'] + 1e-4)
    assert r.params['c0.dy'] == pytest.approx(0.02, abs=4 * r.errors['c0.dy'] + 1e-4)
    # the synthetic noise is independent per nod-difference pixel, where real nod differences of
    # three nods hold two independent values (FitResult.npix counts those): chi2 per pixel is ~1
    assert 0.9 < r.chi2 / (F.npix - r.nfree) < 1.1


def test_fixed_parameters_are_not_fitted():
    F, _ = _model_with([FW.Gaussian(sigma=0.08)])
    g = FW.Gaussian(dy=0.0, sigma=FW.Param(0.06, fixed=True))
    fit = FW.Fit(F, [g])
    assert fit.labels == [] and fit.start.size == 0
    r = fit.run()                                    # the spectra at the given shape
    assert r.params == {} and np.all(np.isfinite(r.spectra))


def test_a_shared_parameter_is_one_parameter():
    dy = FW.Param(0.0, bounds=(-0.3, 0.3))
    a, b = FW.Point(dy=dy), FW.Gaussian(dy=dy, sigma=FW.Param(0.05, bounds=(0.01, 0.3)))
    fit = FW.Fit(F_dummy := FW.ForwardModel(_sky_exposures()), [a, b], names=['core', 'halo'])
    assert fit.labels == ['core.dy', 'halo.sigma']
    fit.set([0.1, 0.07])
    assert a.resolved().dy == b.resolved().dy == 0.1 and F_dummy is fit.model


def test_a_tied_parameter_follows():
    r_b = FW.Param(0.1, bounds=(0.02, 1.0))
    bulge = FW.Sersic(n=4.0, r_e=r_b)
    disk = FW.Sersic(n=1.0, r_e=FW.Tied(lambda r: 3.0 * r, r_b), q=FW.Param(0.5, bounds=(0.1, 1.0)))
    fit = FW.Fit(FW.ForwardModel(_sky_exposures()), [bulge, disk], names=['bulge', 'disk'])
    assert fit.labels == ['bulge.r_e', 'disk.q']
    fit.set([0.2, 0.4])
    assert disk.resolved().r_e == pytest.approx(0.6)


def test_a_prior_pulls_a_poorly_measured_parameter():
    # pure noise: the width is set by its prior alone
    F, _ = _model_with([FW.Gaussian(sigma=0.08)], truth=np.zeros((1, FW.ForwardModel(_sky_exposures()).lam_grid.size)),
                       noise=1.0)
    g = FW.Gaussian(sigma=FW.Param(0.1, bounds=(0.01, 1.0), prior=FW.LogNormal(0.2, 0.05)))
    r = FW.Fit(F, [g]).run()
    assert r.params['c0.sigma'] == pytest.approx(0.2, rel=0.15)


def test_a_joint_prior_from_samples():
    rng = np.random.default_rng(1)
    s = rng.multivariate_normal([0.1, 0.3], [[0.01 ** 2, 0.5 * 0.01 * 0.02], [0.5 * 0.01 * 0.02, 0.02 ** 2]], 4000)
    a, b = FW.Param(0.1), FW.Param(0.3)
    jp = FW.JointPrior.gaussian(s, [a, b])
    r = jp.residuals([0.12, 0.3])
    assert np.sum(r ** 2) == pytest.approx(-2 * jp.logpdf([0.12, 0.3]))
    # the mean is the most probable point
    assert jp.logpdf(s.mean(axis=0)) == pytest.approx(0.0, abs=1e-12)
    F = FW.ForwardModel(_sky_exposures())
    with pytest.raises(ValueError, match='not a free parameter'):
        FW.Fit(F, [FW.Point()], priors=[jp])


def test_log_posterior_is_minus_half_chi2_plus_priors():
    F, _ = _model_with([FW.Gaussian(sigma=0.08)], noise=1e-3)
    sig = FW.Param(0.06, bounds=(0.01, 0.5), prior=FW.Normal(0.08, 0.01))
    fit = FW.Fit(F, [FW.Gaussian(sigma=sig)])
    theta = np.array([0.07])
    chi2 = fit.chi2(theta)
    assert fit.log_posterior(theta) == pytest.approx(-0.5 * chi2 - 0.5 * 1.0)
    assert np.sum(fit.residuals(theta) ** 2) == pytest.approx(chi2 + 1.0)
    assert fit.log_posterior(np.array([0.6])) == -np.inf            # outside the bounds


def test_a_prior_without_residuals_is_minimised():
    F, _ = _model_with([FW.Gaussian(sigma=0.08)], noise=1e-3)

    class Flat:                                       # a caller's own prior: logpdf only
        def logpdf(self, x):
            return 0.0
    sig = FW.Param(0.05, bounds=(0.01, 0.5), prior=Flat())
    r = FW.Fit(F, [FW.Gaussian(sigma=sig)]).run()
    assert r.params['c0.sigma'] == pytest.approx(0.08, abs=0.01)


def test_stored_prediction_is_applied_as_stage_3_applies_it():
    # stage 3 stores the predicted light and its masking level per exposure in the canonical's units
    # (profile_contam.stored_planes); forward.load reads them back and applies them: the exposures
    # must come out as profile_contam.apply leaves them
    import copy
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec.forward import products as FL
    from test_nirspec_profile_fit import _exposures
    exps = _exposures()
    rng = np.random.default_rng(4)
    for e in exps:
        e.pl_applied = e.pl_applied * (1.0 + 0.1 * rng.random(e.pl_applied.shape))     # varying path loss
    shape = exps[0].y.shape
    pred = [rng.normal(size=shape) * 0.5 for _ in exps]
    dpred = [np.abs(rng.normal(size=shape)) * 0.3 for _ in exps]
    unrel = [np.where(rng.random(shape) < 0.05, 5.0, 0.0) for _ in exps]
    P = CT.Prediction(pred=pred, dpred=dpred, pred_unreliable=unrel)
    a = [copy.copy(e) for e in exps]
    CT.apply(a, P, kappa=1.0)
    stored = CT.stored_planes(exps, P)
    planes = {n: dict(planes={'NOD': p, 'NODMASK': m}, kappa=1.0, state='subtracted') for n, (p, m) in stored.items()}
    b = [copy.copy(e) for e in exps]
    FL._subtract_contam(b, planes, 'NOD', 'NODMASK')
    for x, y in zip(a, b):
        assert np.array_equal(x.ok, y.ok)
        assert np.allclose(x.diff[x.ok], y.diff[x.ok], rtol=1e-12, atol=1e-12)
    # a fit that undid the masking: nothing applied
    c = [copy.copy(e) for e in exps]
    undone = {n: dict(v, state='subtracted_undone') for n, v in planes.items()}
    FL._subtract_contam(c, undone, 'NOD', 'NODMASK')
    assert all(np.array_equal(x.diff, e.diff) and np.array_equal(x.ok, e.ok) for x, e in zip(c, exps))


def test_two_components_with_free_shapes_and_their_own_spectra():
    truth_comps = [FW.Point(dy=0.0), FW.Gaussian(dy=0.25 * O.SLIT, sigma=0.05)]
    F, truth = _model_with(truth_comps, noise=1e-3)
    comps = [FW.Point(dy=FW.Param(0.01, bounds=(-0.2, 0.2))),
             FW.Gaussian(dy=FW.Param(0.25 * O.SLIT + 0.01, bounds=(0.0, 0.3)), sigma=FW.Param(0.07, bounds=(0.01, 0.3)))]
    r = FW.Fit(F, comps, names=['point', 'blob']).run()
    assert r.params['point.dy'] == pytest.approx(0.0, abs=5 * r.errors['point.dy'] + 1e-4)
    assert r.params['blob.sigma'] == pytest.approx(0.05, abs=5 * r.errors['blob.sigma'] + 1e-4)
    good = r.solution.good
    pull = (r.spectra - truth)[good] / r.spectra_err[good]
    assert np.sqrt(np.mean(pull ** 2)) < 2.0
