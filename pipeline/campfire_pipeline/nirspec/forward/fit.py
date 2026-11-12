"""Fitting the shapes of a set of components (``forward.components``) to a target's exposures.

At any set of shape parameters the spectra are linear: every component's spectrum (and the
master-background scale on single frames) is solved exactly at each step (``ForwardModel.solve``),
so the fit searches only the shape parameters that are free (``forward.params``). ``Fit`` exposes
the pieces separately, so a script can use its own optimiser or sampler:

    fit = Fit(F, [bulge, disk], names=['bulge', 'disk'])
    fit.labels                 # the free parameters, e.g. ['bulge.dx', 'bulge.r_e', 'disk.q']
    fit.start, fit.bounds      # their start values and bounds
    fit.log_posterior(theta)   # -chi2 / 2 at the best spectra for those shapes, plus the log-priors
    fit.residuals(theta)       # the whitened data residuals, and the priors' residuals
    result = fit.run()         # least squares (or minimisation when a prior has no residuals)

The spectra are profiled, not marginalised: at each shape they take their best values. With
hundreds of spectral points per component that is the practical choice; the shape parameters'
errors (``FitResult.errors``) come from the curvature at the minimum, and the spectra's errors
(``FitResult.spectra_err``) are at the best shape.
"""
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import least_squares, minimize

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.forward import params as FP

FIELD_SEP = '.'


@dataclass
class FitResult:
    params: dict                      # every free parameter's best value, by label
    errors: dict                      # their one-sigma errors from the curvature (NaN when not defined)
    covariance: np.ndarray            # of the free parameters, in the order of ``labels``
    labels: list
    spectra: np.ndarray               # (n_components, n_grid) at the best shape; 0 where unconstrained
    spectra_err: np.ndarray           # their errors at that shape (NaN where unconstrained)
    lam: np.ndarray                   # the spectrum grid [um]
    chi2: float                       # data chi2 at the best shape
    npix: int                         # independent data values
    nfree: int                        # free shape parameters + constrained spectral points (+ scale)
    solution: object = field(repr=False, default=None)   # ``forward.model.Solution`` at the best shape
    success: bool = True
    message: str = ''

    @property
    def chi2_red(self):
        return self.chi2 / max(self.npix - self.nfree, 1)


class Fit:
    """The shape fit of ``components`` (a list; a ``components.Sum`` counts as one, sharing a spectrum)
    to the exposures of ``model`` (a ``ForwardModel``).

    names : a label per component for the results (default ``c0``, ``c1``, ...); a ``Param`` with its
        own ``name`` keeps it. A parameter shared by several components appears once.
    priors : ``params.JointPrior`` objects on several parameters at once (each ``Param``'s own prior is
        taken from it).
    """

    def __init__(self, model, components, names=None, priors=()):
        self.model = model
        self.components = list(components)
        self.names = list(names) if names is not None else [f'c{i}' for i in range(len(self.components))]
        if len(self.names) != len(self.components):
            raise ValueError('Fit: one name per component')
        self.joint = list(priors)
        self.free, self.labels = [], []
        for cname, comp in zip(self.names, self.components):
            for sub, c in self._parts(cname, comp):
                for f in c.names:
                    before = len(self.free)
                    FP.free_params_of(getattr(c, f), self.free)
                    for p in self.free[before:]:
                        self.labels.append(p.name or f'{sub}{FIELD_SEP}{f}')
        for jp in self.joint:
            for p in jp.params:
                if not isinstance(p, FP.Param):
                    raise ValueError('Fit: a joint prior is on Param objects')
                if all(p is not q for q in self.free):
                    raise ValueError(f'Fit: a joint prior names {p!r}, which is not a free parameter of the components')

    @staticmethod
    def _parts(cname, comp):
        if isinstance(comp, list):           # Sum
            return [(f'{cname}[{i}]', c) for i, c in enumerate(comp)]
        return [(cname, comp)]

    # -- the parameter vector ---------------------------------------------------
    @property
    def start(self):
        return np.array([p.value for p in self.free], float)

    @property
    def bounds(self):
        return (np.array([p.bounds[0] for p in self.free], float), np.array([p.bounds[1] for p in self.free], float))

    def set(self, theta):
        """Put the free parameters at ``theta`` (the components then evaluate there)."""
        theta = np.asarray(theta, float)
        if theta.shape != (len(self.free),):
            raise ValueError(f'Fit: {len(self.free)} free parameters, got {theta.shape}')
        for p, v in zip(self.free, theta):
            p.value = float(v)

    def current(self):
        """The components at the current parameter values (numbers only)."""
        return [c.resolved() for c in self.components]

    # -- the objective --------------------------------------------------------------
    def solve(self, theta=None, covariance=False):
        """The spectra at shape ``theta`` (default: the current values): a ``model.Solution``."""
        if theta is not None:
            self.set(theta)
        return self.model.solve(self.current(), covariance=covariance)

    def chi2(self, theta=None):
        return self.model.chi2(self.solve(theta))

    def log_prior(self, theta=None):
        if theta is not None:
            self.set(theta)
        lp = 0.0
        for p in self.free:
            if not p.bounds[0] <= p.value <= p.bounds[1]:
                return -np.inf
            if p.prior is not None:
                lp += float(p.prior.logpdf(p.value))
        for jp in self.joint:
            lp += jp.logpdf([p.value for p in jp.params])
        return lp

    def log_posterior(self, theta):
        """-chi2 / 2 at the best spectra for shape ``theta`` plus the log-priors (-inf outside the
        bounds): what a sampler takes."""
        lp = self.log_prior(theta)
        if not np.isfinite(lp):
            return -np.inf
        return -0.5 * self.chi2(theta) + lp

    def prior_residuals(self):
        """Residuals of the priors at the current values (their squared sum is -2 log-prior up to a
        constant). Raises when a prior has none."""
        r = []
        for p in self.free:
            if p.prior is not None:
                if not hasattr(p.prior, 'residual'):
                    raise TypeError(f'{p!r}: its prior has no residual')
                r.append(float(p.prior.residual(p.value)))
        for jp in self.joint:
            if not jp.has_residuals:
                raise TypeError('a joint prior has no residuals')
            r.extend(jp.residuals([p.value for p in jp.params]))
        return np.array(r, float)

    def data_residuals(self, theta):
        """The data's residuals over their noise on the fitted pixels (0 elsewhere) at the best spectra
        for shape ``theta``."""
        s = self.solve(theta)
        M = self.model
        return np.where(M.use, (M.pix['d'] - s.model) / np.sqrt(M.pix['v']), 0.0)

    def residuals(self, theta):
        """``data_residuals`` followed by the priors' residuals."""
        return np.concatenate([self.data_residuals(theta), self.prior_residuals()])

    def _least_squares_ok(self):
        try:
            self.prior_residuals()
            return True
        except TypeError:
            return False

    # -- the fit ----------------------------------------------------------------
    def run(self, start=None, diff_step=None, dchi2=None, **kw):
        """Fit the free parameters from ``start`` (default: their current values). Least squares
        when every prior has residuals, else minimisation of the negative log-posterior with the
        bounds. ``diff_step`` / ``dchi2``: the derivative step (relative) and the chi2 change it stops
        at (defaults: stage 3's, ``profile_fit.FIT_DIFF_STEP`` / ``FIT_DCHI2``). Leaves the parameters
        at the best values and returns a ``FitResult``. With no free parameter, the spectra at the
        given shape."""
        theta0 = self.start if start is None else np.asarray(start, float)
        lo, hi = self.bounds
        diff_step = PF.FIT_DIFF_STEP if diff_step is None else diff_step
        dchi2 = PF.FIT_DCHI2 if dchi2 is None else dchi2
        success, message, J = True, '', None
        if theta0.size:
            theta0 = np.clip(theta0, lo, hi)
            chi2_start = self.chi2(theta0)
            if self._least_squares_ok():
                opts = dict(x_scale='jac', diff_step=diff_step, ftol=dchi2 / max(chi2_start, 1.0), xtol=1e-10,
                            gtol=1e-10)
                opts.update(kw)
                res = least_squares(self.residuals, theta0, bounds=(lo, hi), **opts)
                theta, J = res.x, res.jac
                success, message = bool(res.success), str(res.message)
            else:
                b = [(None if not np.isfinite(a) else a, None if not np.isfinite(c) else c) for a, c in zip(lo, hi)]
                res = minimize(lambda t: -self.log_posterior(t), theta0, method=kw.pop('method', 'Powell'),
                               bounds=b, **kw)
                theta = res.x
                success, message = bool(res.success), str(res.message)
                J = self._jacobian(theta, diff_step)
            self.set(theta)
        s = self.solve(covariance=True)
        M = self.model
        chi2 = M.chi2(s)
        npix = float(np.sum(M.dof_weight()[M.use]))
        nfree = len(self.free) + int(s.good.sum()) + (1 if M.mbkg_sigma is not None else 0)
        cov = np.full((len(self.free), len(self.free)), np.nan)
        if J is not None and len(self.free):
            try:
                cov = np.linalg.inv(J.T @ J) * max(chi2 / max(npix - nfree, 1.0), 1.0)
            except np.linalg.LinAlgError:
                pass
        err = np.sqrt(np.abs(np.diag(cov))) if len(self.free) else np.zeros(0)
        se = np.where(s.good, s.errors(), np.nan)
        return FitResult(params=dict(zip(self.labels, [p.value for p in self.free])),
                         errors=dict(zip(self.labels, err)), covariance=cov, labels=list(self.labels),
                         spectra=s.spectra, spectra_err=se, lam=M.lam_grid, chi2=chi2, npix=int(round(npix)),
                         nfree=nfree, solution=s, success=success, message=message)

    def _jacobian(self, theta, diff_step):
        """Forward-difference Jacobian of the residuals at ``theta`` (relative steps, as least squares):
        the priors' too when they all have residuals, else the data's alone (the errors then leave
        the priors out)."""
        resid = self.residuals if self._least_squares_ok() else self.data_residuals
        r0 = resid(theta)
        J = np.zeros((r0.size, theta.size))
        for i in range(theta.size):
            t = theta.copy()
            h = diff_step * max(1.0, abs(t[i]))
            lo, hi = self.bounds
            if t[i] + h > hi[i]:
                h = -h
            t[i] += h
            J[:, i] = (resid(t) - r0) / h
        self.set(theta)
        return J
