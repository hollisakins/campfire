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


def test_the_kernel_places_points_as_the_table_interpolation_does():
    # Optics.image_kernel + profile_from_kernel (what project uses) against image_profile, on a table
    # of random numbers: the same light, points beyond the table's source positions included
    rng = np.random.default_rng(3)
    o = object.__new__(O.Optics)
    tab = rng.random((O.LAM.size, O.C.size, O.Y.size)).astype(np.float32)
    o.image_table = lambda mask: tab
    npx = 300
    y = rng.uniform(-2.0, 2.0, npx)
    lam = np.exp(rng.uniform(np.log(0.6), np.log(5.3), npx))
    centres = np.vstack([rng.uniform(-2.0, 2.0, (40, npx)),
                         np.full((1, npx), O.C[0] - 0.004), np.full((1, npx), O.C[-1] + 0.004),
                         np.full((1, npx), O.C[0] + 0.003), np.full((1, npx), O.C[-1])])
    weights = rng.normal(size=centres.shape)
    a = o.profile_from_kernel(o.image_kernel(None, y, lam), centres, weights, chunk=16)
    b = o.image_profile(None, y, lam, centres, weights)
    assert np.allclose(a, b, rtol=1e-5, atol=1e-6)


def test_a_components_block_is_kept_for_the_same_values():
    F = FW.ForwardModel(_sky_exposures())
    a = F.component_matrix(FW.Gaussian(sigma=0.05))
    assert F.component_matrix(FW.Gaussian(sigma=FW.Param(0.05))) is a       # same values: the kept block
    assert F.component_matrix(FW.Gaussian(sigma=0.06)) is not a
    s = FW.SlitGaussian(0.1, eoff=np.zeros(3))                              # holds an array: not kept
    assert F.component_matrix(s) is not F.component_matrix(s)


def test_the_nonnegative_solve_is_the_bounded_least_squares():
    from scipy.optimize import lsq_linear
    import scipy.sparse as sp
    from campfire_pipeline.nirspec.forward.model import nonnegative_normal_solve
    rng = np.random.default_rng(5)
    X = rng.normal(size=(200, 40))
    d = X @ rng.normal(size=40) + rng.normal(size=200)
    con = np.arange(40) % 3 != 0                         # every third unknown left free
    x, free, _ = nonnegative_normal_solve(sp.csc_matrix(X.T @ X), X.T @ d, con)
    ref = lsq_linear(X, d, bounds=(np.where(con, 0.0, -np.inf), np.inf), tol=1e-12).x
    assert np.allclose(x, ref, atol=1e-8)
    assert np.all(x[con] >= 0) and np.any(~free) and np.all(free[~con])


def test_nonnegative_spectra():
    # a point and a faint blob whose spectrum is zero below 2.5 um: the free solve puts noise either
    # side of zero there; held non-negative, nothing goes below zero, the held points are marked, and
    # the chi2 is the least it can be within the bound
    comps = [FW.Point(dy=0.0), FW.Gaussian(dy=0.25 * O.SLIT, sigma=0.05)]
    F, truth = _model_with(comps, noise=0.02)
    truth[1] = np.where(F.lam_grid < 2.5, 0.0, truth[1])
    F.pix['d'] = F.design(comps) @ truth.ravel() + 0.02 * np.random.default_rng(0).normal(size=F.npix)
    free = F.solve(comps)
    assert np.any(free.spectra[1][free.good[1]] < 0)
    held = F.solve(comps, covariance=True, nonnegative=[False, True])
    assert np.all(held.spectra[1] >= 0)
    assert held.at_zero[1].any() and not held.at_zero[0].any()
    assert F.chi2(held) >= F.chi2(free)
    assert np.all(np.isfinite(held.errors()[held.good]))
    r = FW.Fit(F, [FW.Point(dy=0.0), FW.Gaussian(dy=0.25 * O.SLIT, sigma=FW.Param(0.04, bounds=(0.01, 0.2)))],
               names=['point', 'blob'], nonnegative=['blob']).run()
    assert np.all(r.spectra[1] >= 0) and r.solution.at_zero[1].any()
    assert r.nfree == 1 + int(r.solution.good.sum()) - int(r.solution.at_zero.sum())


def test_nonnegative_spectra_with_the_master_background_scale():
    # single frames with the scale term: the bounded solve over spectra and scale (scale free) against
    # the dense bounded least squares with the scale's allowance as one more row
    from scipy.optimize import lsq_linear
    from test_nirspec_mbkg_extraction import _single_frames
    from test_nirspec_profile_fit import TRUTH
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0, mbkg_error=0.01)
    for e in exps:
        e.pa_slit_x, e.pa_slit_y = 90.0, 0.0
    sigma = 0.01
    F = FW.ForwardModel(exps, mbkg_sigma=sigma)
    comps = [FW.SlitGaussian(F.c_prior, width=0.15), FW.Gaussian(dy=-0.3 * O.SLIT, sigma=0.05)]
    s = F.solve(comps, nonnegative=True)
    assert s.at_zero.any()
    g = s.good.ravel()
    w = np.sqrt(1.0 / F.pix['v'])
    X = np.column_stack([F.design(comps).toarray()[:, g], F.pix['b']]) * w[:, None]
    X = np.vstack([X, np.r_[np.zeros(g.sum()), 1.0 / sigma]])
    rhs = np.r_[F.pix['d'] * w, 0.0]
    ref = lsq_linear(X, rhs, bounds=(np.r_[np.zeros(g.sum()), -np.inf], np.inf), tol=1e-12).x
    peak = np.abs(ref[:-1]).max()
    assert np.allclose(s.spectra.ravel()[g], ref[:-1], atol=1e-5 * peak)
    assert s.scale == pytest.approx(ref[-1], abs=1e-5 * abs(ref[-1]) + 1e-9)


def test_stage_3s_fit_is_a_fit_of_one_slit_gaussian():
    # ProfileModel.shape_fit: the parameters in the order of the fit vector, the width as its log,
    # and the residuals' squared sum the data chi2 + the scale's allowance + the shape priors
    from test_nirspec_mbkg_extraction import _single_frames
    from campfire_pipeline.nirspec import profile_fit as PF
    from test_nirspec_profile_fit import TRUTH
    exps, _ = _single_frames(TRUTH, flux_scale=30.0, noise=1.0, mbkg_error=0.01)
    M = PF.ProfileModel(exps, chromatic=True, mbkg_sigma=0.01)
    F = M.shape_fit()
    assert F.labels == ['centre', 'drift', 'ln_width', 'width_change']
    q = np.array([M.c_prior + 0.05, 0.02, np.log(0.2), -0.03])
    p, eoff = M.unpack(q)
    s = M.linear_solve(p, eoff)
    chi2 = np.sum((M.pix['d'] - s.model) ** 2 / M.pix['v'])
    pr = M.priors
    prior = (((q[0] - M.c_prior) / pr.centre_sigma) ** 2 + (q[1] / pr.drift_sigma) ** 2
             + ((q[2] - np.log(pr.width_median)) / pr.width_log_sigma) ** 2 + (q[3] / pr.width_change_sigma) ** 2)
    assert np.sum(F.residuals(q) ** 2) == pytest.approx(chi2 + (s.scale / 0.01) ** 2 + prior, rel=1e-10)
    assert F.log_posterior(q) == pytest.approx(-0.5 * np.sum(F.residuals(q) ** 2), rel=1e-10)
    assert not PF.ProfileModel(exps, chromatic=False, mbkg_sigma=0.01).shape_fit().labels.count('drift')


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


def test_frames_need_the_rate_file(tmp_path):
    """load(frames=True) names what the master background needs and is missing (the _mbkg.fits,
    its MBKG2D, the rate file it was painted on) instead of falling back to the slit's own slitlet."""
    from astropy.io import fits
    from campfire_pipeline.nirspec.forward import products as FPR
    c = tmp_path / 'jw07417015001_03101_00001_nrs1_547856.fits'
    fits.PrimaryHDU().writeto(c)
    assert 'the _mbkg.fits of' in FPR.master_background_inputs_missing([c])[0]
    m = tmp_path / 'jw07417015001_03101_00001_nrs1_mbkg.fits'
    h0 = fits.PrimaryHDU()
    h0.header['MBKGRATE'] = 'jw07417015001_03101_00001_nrs1_rate.fits'
    fits.HDUList([h0]).writeto(m)
    assert 'has no MBKG2D' in FPR.master_background_inputs_missing([c])[0]
    fits.HDUList([h0, fits.ImageHDU(np.zeros((2, 2)), name='MBKG2D')]).writeto(m, overwrite=True)
    assert FPR.master_background_inputs_missing([c]) == ['jw07417015001_03101_00001_nrs1_rate.fits (for '
                                                        'jw07417015001_03101_00001_nrs1_mbkg.fits)']
    fits.PrimaryHDU().writeto(tmp_path / 'jw07417015001_03101_00001_nrs1_rate.fits')
    assert FPR.master_background_inputs_missing([c]) == []
