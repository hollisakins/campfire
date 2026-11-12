"""Source components of the forward model: what each puts on an exposure's pixels per unit of its
total light (``Component.light``). The model multiplies that by the throughput no source changes
(``ForwardModel.throughput_fixed``) and by the component's own spectrum.

Positions on the sky are offsets in arcsec, east and north, from the target's catalog position
(where the MSA planning put it: SRCXPOS / SRCYPOS in each exposure). Each exposure's slit frame
lies on the sky at the position angles its WCS gives (``Exposure.pa_slit_x`` / ``pa_slit_y``), so
one model serves exposures at different roll angles.

Along the slit, light is placed through the optics model's tables: a point source at each slit
position, as stage 3 places its Gaussian (``optics``). Along the dispersion only the light the
shutter passes is modelled: the component's light at each position across the shutter times the
share a point there gets through, relative to a point at the planned position (which jwst's path
loss already carries in the fixed throughput). For a round component the two directions separate
(``SlitGaussian``, ``Point``, ``Gaussian``: the tables directly); for any other shape the
component's image is collapsed onto the slit with that weighting (``project``).

Along the dispersion the position convention is the pipeline's: SRCXPOS times
``optics.X_PITCH`` is the planned position in the arcsec of the optics model's tables.
"""
import numpy as np
from scipy.special import gamma, gammaincinv

from campfire_pipeline.nirspec import optics as O
from campfire_pipeline.nirspec import profile_fit as PF


class Component:
    """Base: a component has named parameters (``names``, ``values``) and its light per pixel."""
    names = ()

    @property
    def values(self):
        return np.array([getattr(self, n) for n in self.names], float)

    def with_values(self, v):
        """A copy with parameters ``v`` (in the order of ``names``)."""
        c = object.__new__(type(self))
        c.__dict__.update(self.__dict__)
        for n, x in zip(self.names, v):
            setattr(c, n, float(x))
        return c

    def light(self, M, k, y, lam, dy):
        """Exposure k's light per pixel at slit positions ``y`` and wavelengths ``lam`` (pixel
        heights ``dy``), per unit total light of the component, before the fixed throughput."""
        raise NotImplementedError

    def __repr__(self):
        args = ', '.join(f'{n}={getattr(self, n):.4g}' for n in self.names)
        return f'{type(self).__name__}({args})'


def _row_scale(dy):
    return np.asarray(dy, float) / O.DY_ROW


def _axes(e):
    """Unit vectors (east, north) of the slit frame's +x and +y in exposure ``e``."""
    if not (np.isfinite(e.pa_slit_x) and np.isfinite(e.pa_slit_y)):
        raise ValueError(f'forward: {e.name} has no slit orientation on the sky (pa_slit_x / pa_slit_y)')
    ax, ay = np.radians(e.pa_slit_x), np.radians(e.pa_slit_y)
    return np.array([np.sin(ax), np.cos(ax)]), np.array([np.sin(ay), np.cos(ay)])


def slit_offsets(e, east, north):
    """(along the dispersion [arcsec], along the slit [slit units]) of sky offsets in exposure e."""
    ux, uy = _axes(e)
    east, north = np.asarray(east, float), np.asarray(north, float)
    return east * ux[0] + north * ux[1], (east * uy[0] + north * uy[1]) / O.SLIT


def _x_planned(e):
    return (float(e.src_x_jwst) if np.isfinite(e.src_x_jwst) else 0.0) * O.X_PITCH


def _x_offset_ratio(o, e, lam, u):
    """The light a point source ``u`` arcsec off the planned position along the dispersion gets
    through the shutter, over a point at the planned position's (one at u = 0)."""
    lam = np.asarray(lam, float)
    if u == 0.0 or lam.size == 0:
        return np.ones(lam.shape)
    x0 = _x_planned(e)
    flat = lam.ravel()
    r = o.x_pass(flat, [x0 + u])[:, 0] / np.maximum(o.x_pass(flat, [x0])[:, 0], 1e-9)
    return r.reshape(lam.shape)


class SlitGaussian(Component):
    """Stage 3's source: a Gaussian along the slit of width ``width`` (slit units) at slit position
    ``centre`` (slit units, the frame of the target's planned position), each linear in
    log2(lambda / pivot) (``drift``, ``width_change``), taken to be round across the shutter
    (``optics.x_extended_ratio``) at the planned SRCXPOS. ``profile_fit.ProfileModel`` is this one
    component."""
    names = ('centre', 'drift', 'width', 'width_change')

    def __init__(self, centre, drift=0.0, width=0.15, width_change=0.0, eoff=None):
        self.centre, self.drift, self.width, self.width_change = float(centre), float(drift), float(width), \
            float(width_change)
        self.eoff = eoff                 # per-exposure pointing offsets (slit units), not fitted; None for none

    def width_at(self, M, lam):
        """Its width on the sky (slit units)."""
        return np.maximum(self.width + self.width_change * M.log2_lam(lam), PF.W_FLOOR)

    def image_width(self, M, k, lam):
        """Its width where it lands in exposure k: stretched as the trace-centre law stretches
        positions along the slit (``ForwardModel.trace_stretch``)."""
        return self.width_at(M, lam) * M.trace_stretch(k, lam)

    def centre_at(self, M, k, lam):
        eoff = 0.0 if self.eoff is None else self.eoff[k]
        return M.trace_centre(k, lam, self.centre + self.drift * M.log2_lam(lam), eoff)

    def image(self, M, k, y, lam, dy):
        """Along the slit: its light per pixel through the tables."""
        e, o = M.exps[k], M.optics[k]
        return o.image(e.open_k, y, lam, self.centre_at(M, k, lam), self.image_width(M, k, lam)) * _row_scale(dy)

    def x_ratio(self, M, k, lam):
        """Across the shutter: the light it gets through over a point source's at the planned SRCXPOS."""
        return M.optics[k].x_extended_ratio(lam, M.exps[k].src_x_jwst, self.width_at(M, lam))

    def light(self, M, k, y, lam, dy):
        return self.image(M, k, y, lam, dy) * self.x_ratio(M, k, lam)


class _SkyComponent(Component):
    """A component placed on the sky (``dx`` east, ``dy`` north, arcsec from the catalog
    position). ``brightness`` is its surface brightness per arcsec^2 per unit total light."""

    def brightness(self, east, north):
        raise NotImplementedError

    def extent(self):
        """Half-size [arcsec] of the box holding essentially all of its light (``project``)."""
        raise NotImplementedError

    def oversample(self):
        """Samples per projection cell along each axis (``project``)."""
        return 1

    def light(self, M, k, y, lam, dy):
        return project(self, M, k, y, lam, dy)


class Point(_SkyComponent):
    """A point source at (``dx``, ``dy``) arcsec east and north of the catalog position."""
    names = ('dx', 'dy')

    def __init__(self, dx=0.0, dy=0.0):
        self.dx, self.dy = float(dx), float(dy)

    def brightness(self, east, north):
        raise ValueError('forward: a point source has no surface brightness; it is placed directly')

    def extent(self):
        return 0.0

    def light(self, M, k, y, lam, dy):
        return _round_light(M, k, y, lam, dy, self.dx, self.dy, PF.W_FLOOR)


class Gaussian(_SkyComponent):
    """A round Gaussian of ``sigma`` arcsec at (``dx``, ``dy``) arcsec east and north of the catalog
    position. Placed through the tables directly (it separates along and across the slit);
    ``project`` gives the same light the general way."""
    names = ('dx', 'dy', 'sigma')

    def __init__(self, dx=0.0, dy=0.0, sigma=0.05):
        self.dx, self.dy, self.sigma = float(dx), float(dy), float(sigma)

    def brightness(self, east, north):
        s = max(self.sigma, 1e-4)
        r2 = (np.asarray(east) - self.dx) ** 2 + (np.asarray(north) - self.dy) ** 2
        return np.exp(-0.5 * r2 / s ** 2) / (2.0 * np.pi * s * s)

    def extent(self):
        return 5.0 * self.sigma + np.hypot(self.dx, self.dy)

    def oversample(self):
        return int(np.clip(np.ceil(2.0 * PROJ_STEP_X / max(self.sigma, 1e-4)), 1, 5))

    def light(self, M, k, y, lam, dy):
        return _round_light(M, k, y, lam, dy, self.dx, self.dy, max(self.sigma / O.SLIT, PF.W_FLOOR))


def _round_light(M, k, y, lam, dy, dx, dn, width):
    """A round Gaussian of ``width`` (slit units) at sky offset (dx, dn): along the slit through the
    tables at its slit position, its width stretched as the trace-centre law stretches positions
    (``ForwardModel.trace_stretch``, as ``project`` places an extended source and ``SlitGaussian``
    its width), across the shutter the round ratio at its own position times the point ratio of
    that position against the planned one."""
    e, o = M.exps[k], M.optics[k]
    u, v = slit_offsets(e, dx, dn)
    c = M.trace_centre(k, lam, M.c_prior + v)
    srcx = (float(e.src_x_jwst) if np.isfinite(e.src_x_jwst) else 0.0) + float(u) / O.X_PITCH
    w = np.full(np.shape(lam), width)
    w_y = w * M.trace_stretch(k, lam)
    return (o.image(e.open_k, y, lam, c, w_y) * _row_scale(dy) * o.x_extended_ratio(lam, srcx, w)
            * _x_offset_ratio(o, e, lam, float(u)))


class Sersic(_SkyComponent):
    """A Sersic profile: index ``n``, half-light radius ``r_e`` (arcsec, along the major axis),
    axis ratio ``q`` (minor / major), major axis at position angle ``pa`` (deg, east of north),
    centred at (``dx``, ``dy``) arcsec east and north of the catalog position. n = 1 is an
    exponential disk, n = 4 a de Vaucouleurs bulge."""
    names = ('dx', 'dy', 'n', 'r_e', 'q', 'pa')

    def __init__(self, dx=0.0, dy=0.0, n=1.0, r_e=0.2, q=1.0, pa=0.0):
        self.dx, self.dy, self.n, self.r_e, self.q, self.pa = (float(dx), float(dy), float(n), float(r_e), float(q),
                                                               float(pa))

    def _b(self):
        return float(gammaincinv(2.0 * self.n, 0.5))

    def brightness(self, east, north):
        n, re, q = max(self.n, 0.2), max(self.r_e, 1e-4), float(np.clip(self.q, 0.05, 1.0))
        t = np.radians(self.pa)
        de, dn = np.asarray(east) - self.dx, np.asarray(north) - self.dy
        a = de * np.sin(t) + dn * np.cos(t)                    # along the major axis
        b = de * np.cos(t) - dn * np.sin(t)
        r = np.sqrt(a * a + (b / q) ** 2)
        bn = self._b()
        total = 2.0 * np.pi * n * q * re * re * np.exp(bn) * gamma(2.0 * n) / bn ** (2.0 * n)
        return np.exp(-bn * ((r / re) ** (1.0 / n) - 1.0)) / total

    def extent(self):
        return float(np.clip(8.0 * self.r_e * (1.0 + 0.25 * max(self.n - 1.0, 0.0)), 0.3, PROJ_MAX)) \
            + np.hypot(self.dx, self.dy)

    def oversample(self):
        return int(np.clip(np.ceil(4.0 * PROJ_STEP_X / max(self.r_e, 1e-4)), 1, 7))


class Sum(list):
    """Several components with one shared spectrum: their light per unit total light of the sum is
    the sum of each one's light times its share (``weights``, normalised to one)."""

    def __init__(self, components, weights=None):
        super().__init__(components)
        w = np.ones(len(components)) if weights is None else np.asarray(weights, float)
        self.weights = w / w.sum()

    def light(self, M, k, y, lam, dy):
        return sum(w * c.light(M, k, y, lam, dy) for w, c in zip(self.weights, self))


# ---------------------------------------------------------------------------
# The general projection
# ---------------------------------------------------------------------------
PROJ_STEP_X = 0.01        # arcsec across the shutter
PROJ_STEP_Y = O.C[1] - O.C[0]   # slit units along the slit: the tables' step in source position
PROJ_X_HALF = 1.0         # arcsec either side of the planned position across the shutter (the shutter passes
                          # nothing beyond; x_transmission_table)
PROJ_MAX = 2.5            # arcsec: the largest half-size along the slit (the tables span +-5.5 slit units)
PROJ_MIN_WEIGHT = 1e-6    # points carrying less than this share of the brightest are skipped


def project(comp, M, k, y, lam, dy):
    """Exposure k's light per pixel of any component on the sky: its image sampled on a grid in the
    slit frame (across the shutter in arcsec about the planned position, along the slit in steps of
    the tables' source positions about the catalog position), each sample weighted by the share a
    point there gets through the shutter along the dispersion relative to the planned position
    (``Optics.x_pass``, at the tables' wavelengths), summed across the shutter into one point per
    slit position, and the points placed through the tables (``Optics.image_profile``), their
    weights interpolated in ln(lambda) to each pixel's wavelength."""
    e, o = M.exps[k], M.optics[k]
    y, lam, dy = (np.asarray(a, float).ravel() for a in (y, lam, dy))
    if y.size == 0:
        return np.zeros(0)
    ux, uy = _axes(e)
    half_y = min(comp.extent(), PROJ_MAX) / O.SLIT
    vs = np.arange(-np.ceil(half_y / PROJ_STEP_Y), np.ceil(half_y / PROJ_STEP_Y) + 1) * PROJ_STEP_Y
    us = np.arange(-PROJ_X_HALF, PROJ_X_HALF + 1e-9, PROJ_STEP_X)
    m = max(int(comp.oversample()), 1)
    sub_u = (np.arange(m) + 0.5) / m - 0.5
    img = np.zeros((us.size, vs.size))
    for su in sub_u:
        for sv in sub_u:
            U = (us + su * PROJ_STEP_X)[:, None]
            V = ((vs + sv * PROJ_STEP_Y) * O.SLIT)[None, :]
            img += comp.brightness(U * ux[0] + V * uy[0], U * ux[1] + V * uy[1])
    img *= PROJ_STEP_X * PROJ_STEP_Y * O.SLIT / (m * m)        # light per cell
    lo, hi = float(np.nanmin(lam)), float(np.nanmax(lam))
    nodes = O.LAM[(O.LAM >= lo / 1.2) & (O.LAM <= hi * 1.2)]
    if nodes.size < 2:
        nodes = np.array([lo, hi]) if hi > lo else np.array([lo, lo * 1.01])
    x0 = _x_planned(e)
    t = o.x_pass(nodes, np.concatenate([x0 + us, [x0]]))
    ratio = t[:, :-1] / np.maximum(t[:, -1:], 1e-9)          # (n_nodes, n_u)
    S = ratio @ img                                            # (n_nodes, n_v): light per slit position
    keep = np.flatnonzero(np.max(np.abs(S), axis=0) > PROJ_MIN_WEIGHT * np.max(np.abs(S)))
    if keep.size == 0:
        return np.zeros(y.size)
    ln_nodes, ln_lam = np.log(nodes), np.log(lam)
    weights = np.array([np.interp(ln_lam, ln_nodes, S[:, j]) for j in keep])
    centres = np.array([M.trace_centre(k, lam, M.c_prior + vs[j]) for j in keep])
    return o.image_profile(e.open_k, y, lam, centres, weights) * _row_scale(dy)
