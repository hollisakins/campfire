"""Parameters of the forward model's components: what is free, fixed, tied to another parameter,
bounded, or given a prior.

Any parameter of a component (``Point(dx=...)``, ``Sersic(r_e=...)``, ...) takes a plain number
(fixed at it), a ``Param`` (free unless ``fixed=True``, with optional bounds and prior), or a
``Tied`` value (a function of other parameters). The same ``Param`` given to several components
is one parameter: a bulge and a disk sharing ``dx`` and ``dy`` have one centre.

    centre_e, centre_n = Param(0.0, bounds=(-0.3, 0.3)), Param(0.0, bounds=(-0.3, 0.3))
    r_bulge = Param(0.15, bounds=(0.02, 1.0), prior=LogNormal(0.15, 0.3))   # e.g. from imaging
    bulge = Sersic(dx=centre_e, dy=centre_n, n=4.0, r_e=r_bulge, q=0.8, pa=30.0)
    disk = Sersic(dx=centre_e, dy=centre_n, n=1.0, r_e=Tied(lambda r: 3.0 * r, r_bulge),
                  q=Param(0.5, bounds=(0.1, 1.0)), pa=30.0)

A prior on several parameters at once (``JointPrior``) goes to the fit (``Fit(..., priors=[...])``):
for example a Gaussian approximation of posterior samples from a fit to imaging
(``JointPrior.gaussian(samples, [r_bulge, ...])``), or any log-density the caller writes.
"""
import numpy as np


class Param:
    """One parameter. ``value``: its value (the start of a fit, the value used when fixed);
    ``fixed``: held at ``value``; ``bounds``: (low, high), either may be None; ``prior``: an object
    with ``logpdf(x)`` (``Normal``, ``LogNormal``, ``Uniform`` or the caller's own); ``name``: a
    label for results (default: the component and field it first appears in)."""

    def __init__(self, value, fixed=False, bounds=None, prior=None, name=None):
        self.value = float(value)
        self.fixed = bool(fixed)
        lo, hi = bounds if bounds is not None else (None, None)
        self.bounds = (-np.inf if lo is None else float(lo), np.inf if hi is None else float(hi))
        if not self.bounds[0] <= self.value <= self.bounds[1]:
            raise ValueError(f'Param: value {self.value} outside its bounds {self.bounds}')
        self.prior = prior
        self.name = name

    def __repr__(self):
        tag = 'fixed' if self.fixed else 'free'
        return f'Param({self.value:.4g}, {tag}{", " + self.name if self.name else ""})'


class Tied:
    """A value computed from other parameters: ``fn(*values of params)``. Not fitted itself."""

    def __init__(self, fn, *params):
        self.fn = fn
        self.params = params

    @property
    def value(self):
        return float(self.fn(*[value_of(p) for p in self.params]))

    def __repr__(self):
        return f'Tied({getattr(self.fn, "__name__", "fn")}, {len(self.params)} parameters)'


def value_of(x):
    """The number a field holds: a ``Param``'s or ``Tied``'s value, or the number itself."""
    return x.value if isinstance(x, (Param, Tied)) else float(x)


def is_parameter(x):
    return isinstance(x, (Param, Tied))


def keep(x):
    """A component field as given: a ``Param`` or ``Tied`` kept as it is, anything else as a float."""
    return x if is_parameter(x) else float(x)


def free_params_of(x, out):
    """Append (in order, once each) the free ``Param`` objects a field depends on."""
    if isinstance(x, Param):
        if not x.fixed and all(x is not p for p in out):
            out.append(x)
    elif isinstance(x, Tied):
        for p in x.params:
            free_params_of(p, out)


# ---------------------------------------------------------------------------
# Priors on one parameter: logpdf (up to a constant) and, for the least-squares fit, a residual
# whose square is -2 logpdf (up to a constant)
# ---------------------------------------------------------------------------
class Normal:
    def __init__(self, mean, sigma):
        self.mean, self.sigma = float(mean), float(sigma)

    def residual(self, x):
        return (x - self.mean) / self.sigma

    def logpdf(self, x):
        return -0.5 * self.residual(x) ** 2


class LogNormal:
    """ln(x) normal about ln(``median``) with scatter ``sigma`` (natural log): for sizes."""
    def __init__(self, median, sigma):
        self.median, self.sigma = float(median), float(sigma)

    def residual(self, x):
        return np.log(max(x, 1e-300) / self.median) / self.sigma

    def logpdf(self, x):
        return -0.5 * self.residual(x) ** 2 - np.log(max(x, 1e-300))


class Uniform:
    """Flat between ``low`` and ``high``: the same as bounds (give the ``Param`` the bounds too, so
    the optimiser stays inside)."""
    def __init__(self, low, high):
        self.low, self.high = float(low), float(high)

    def residual(self, x):
        return 0.0

    def logpdf(self, x):
        return 0.0 if self.low <= x <= self.high else -np.inf


class JointPrior:
    """A prior on several parameters together. ``logpdf(values)`` takes their values in the order of
    ``params``; ``residuals(values)`` (optional) gives a vector whose squared sum is -2 logpdf up to a
    constant, which lets the least-squares fit use it (otherwise ``Fit.run`` minimises the negative
    log-posterior)."""

    def __init__(self, params, logpdf, residuals=None):
        self.params = list(params)
        self._logpdf = logpdf
        self._residuals = residuals

    def logpdf(self, values):
        return float(self._logpdf(np.asarray(values, float)))

    @property
    def has_residuals(self):
        return self._residuals is not None

    def residuals(self, values):
        return np.atleast_1d(np.asarray(self._residuals(np.asarray(values, float)), float))

    @classmethod
    def gaussian(cls, samples, params, inflate=1.0):
        """A multivariate normal with the mean and covariance of ``samples`` (n_samples x
        n_params, columns in the order of ``params``), the covariance times ``inflate`` ** 2: for
        example posterior samples from a fit to imaging, where the spectrum's light may sit
        differently from the image's (a size measured in another band)."""
        s = np.asarray(samples, float)
        if s.ndim != 2 or s.shape[1] != len(params):
            raise ValueError(f'JointPrior.gaussian: samples must be n x {len(params)}, got {s.shape}')
        mu = s.mean(axis=0)
        C = np.atleast_2d(np.cov(s, rowvar=False)) * inflate ** 2
        L = np.linalg.cholesky(C)

        def res(v):
            return np.linalg.solve(L, v - mu)
        return cls(params, lambda v: -0.5 * float(np.sum(res(v) ** 2)), residuals=res)
