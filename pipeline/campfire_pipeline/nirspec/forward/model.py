"""The forward model of one target's exposures: the data, where a source lands on them, and the
linear solve for one spectrum per component (``forward``'s package docstring has the model).

``ForwardModel`` holds what does not depend on the source: the fitted pixels and their variances
(nod differences, or single frames with their master background subtracted), each frame's
throughput along the dispersion that the source does not change (jwst's point-source path loss
moved to the shutter centre, divided by the optics model's transmission of a point there), the
spectrum grid, and the placement of a slit position on each exposure (the nod geometry and the
trace-centre law). A component (``forward.components``) adds only its own light per pixel; the
design matrix has one block of columns per component, and the spectra are solved together.
"""
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

from campfire_pipeline.nirspec import crds_refs as CR
from campfire_pipeline.nirspec import profile_fit as PF


def nonnegative_normal_solve(N, b, constrained, max_iter=None):
    """Minimise x.N.x / 2 - b.x with x >= 0 where ``constrained`` (N sparse, symmetric positive
    definite): block principal pivoting on the normal equations (Portugal, Judice & Vicente 1994),
    which swaps every point that breaks the optimality conditions at once, and falls back to one at a
    time (Murty's rule, which always ends) when that stops reducing their number. Returns
    (x, free, lu): the solution, which unknowns are free (the rest held at zero), and the LU of N on
    the free ones."""
    b = np.asarray(b, float)
    con = np.asarray(constrained, bool)
    n = b.size
    max_iter = 20 * n + 100 if max_iter is None else max_iter
    tol_y = 1e-12 * max(float(np.max(np.abs(b))), 1e-300)
    free = np.ones(n, bool)
    best, chances = n + 1, 3
    for _ in range(max_iter):
        x = np.zeros(n)
        fi = np.flatnonzero(free)
        lu = splu(N[fi][:, fi].tocsc()) if fi.size else None
        if fi.size:
            x[fi] = lu.solve(b[fi])
        y = N @ x - b                                 # the gradient; >= 0 for an unknown held at zero
        tol_x = 1e-12 * max(float(np.max(np.abs(x))), 1e-300)
        bad = con & ((free & (x < -tol_x)) | (~free & (y < -tol_y)))
        nb = int(bad.sum())
        if nb == 0:
            x[con] = np.maximum(x[con], 0.0)
            return x, free, lu
        if nb < best:
            best, chances = nb, 3
            free[bad] = ~free[bad]
        elif chances > 0:
            chances -= 1
            free[bad] = ~free[bad]
        else:
            i = np.flatnonzero(bad)[-1]
            free[i] = ~free[i]
    raise RuntimeError('forward: the non-negative solve did not converge')


def _block_key(comp):
    """A hashable identity of a resolved component for the block cache: its type and attributes when
    they are all numbers, strings or None (a ``components.Sum``: its weights and its members'); None
    (not cached) otherwise, e.g. a component holding an array."""
    if isinstance(comp, list):
        keys = [_block_key(c) for c in comp]
        if any(k is None for k in keys):
            return None
        return (type(comp), tuple(float(w) for w in getattr(comp, 'weights', ())), tuple(keys))
    items = getattr(comp, '__dict__', None)
    if items is None or not all(v is None or isinstance(v, (bool, int, float, str)) for v in items.values()):
        return None
    return (type(comp), tuple(sorted(items.items())))


@dataclass
class Solution:
    """The linear solve at fixed component shapes (``ForwardModel.solve``)."""
    spectra: np.ndarray               # (n_components, n_grid): each component's total light; 0 where unconstrained
    cov: np.ndarray                   # (n_components * n_grid) square covariance, component-major; None unless asked
    good: np.ndarray                  # (n_components, n_grid): grid points the data constrain
    A: object                         # sparse design matrix (n_pixels x n_components * n_grid)
    model: np.ndarray                 # the whole model on the fitted pixels
    dchi2: float                      # chi2 improvement over no source (the scale term in both)
    scale: float = 0.0                # master-background scale; 0 without the term
    scale_err: float = np.nan
    at_zero: np.ndarray = None        # (n_components, n_grid): held at zero by ``nonnegative``

    def errors(self):
        """(n_components, n_grid) one-sigma errors from the covariance's diagonal (NaN where
        unconstrained); needs the covariance."""
        if self.cov is None:
            raise ValueError('forward: solve(..., covariance=True) for errors')
        return np.sqrt(np.abs(np.diag(self.cov))).reshape(self.spectra.shape)


class ForwardModel:
    """Pixels, spectrum grid and placement for a set of exposures (``profile_fit.Exposure``).

    grid : the spectrum grid [um]; None builds it from the exposures (native sampling).
    optics : one diffraction model per exposure (``optics.Optics`` or an object with its
        interface); None takes the shipped model of each exposure's detector.
    centre_law_tables, nod_law : as ``profile_fit.ProfileModel``.
    c_prior : the target's planned slit position (default: ``profile_fit.planned_position``).
    mbkg_sigma : the master-background scale term on single frames (``ProfileModel``)."""

    def __init__(self, exps, grid=None, optics=None, centre_law_tables=None, nod_law=None, c_prior=None,
                 mbkg_sigma=None):
        self.exps = exps
        self.centre_law_tables = centre_law_tables
        self.optics = PF.model_optics(exps, optics)
        self.nod_law = nod_law
        self.nod_shift = PF.nod_shifts(exps, nod_law)
        self.c_prior, self.srcy_flag = PF.planned_position(exps)
        if c_prior is not None:
            self.c_prior = float(c_prior)
        self._build_pixels()
        self._mbkg_term(mbkg_sigma)
        self.lam_grid = self._grid() if grid is None else np.asarray(grid, float)
        lam = self.pix['lam']
        self.pivot = float(np.exp(np.median(np.log(lam))))
        self.lam_rng = (float(np.percentile(lam, 2)), float(np.percentile(lam, 98)))
        self._interp_tables()

    # -- pixels --------------------------------------------------------------
    def _build_pixels(self):
        rows = PF.fit_pixel_masks(self.exps)
        if sum(int(m.sum()) for _, m in rows) == 0:
            raise PF.NoUsablePixels('profile fit: no usable pixels')
        self.pix_masks = rows
        d, v, e_idx, lam = [], [], [], []
        for k, m in rows:
            e = self.exps[k]
            d.append(e.diff[m]); v.append(self.diff_variance(k)[m]); e_idx.append(np.full(m.sum(), k)); lam.append(e.wav[m])
        self.pix = dict(d=np.concatenate(d), v=np.concatenate(v), e=np.concatenate(e_idx), lam=np.concatenate(lam))
        self.npix = self.pix['d'].size
        self.use = np.ones(self.npix, bool)

    def _mbkg_term(self, sigma):
        if sigma is None or sigma == 0:
            self.mbkg_sigma = None
            return
        if not sigma > 0:
            raise ValueError(f'profile fit: mbkg_sigma must be positive, got {sigma!r}')
        fitted = [self.exps[k] for k, _ in self.pix_masks]
        if any(e.partners for e in fitted):
            raise ValueError('profile fit: the master-background scale term is for single frames, '
                             'not nod differences')
        if any(e.mbkg is None for e in fitted):
            raise ValueError('profile fit: the master-background scale term needs every exposure\'s master '
                             'background (Exposure.mbkg)')
        self.mbkg_sigma = float(sigma)
        self.pix['b'] = np.concatenate([np.nan_to_num(np.asarray(self.exps[k].mbkg, float)[m])
                                        for k, m in self.pix_masks])

    def diff_variance(self, k):
        """Variance of exposure k's fitted value (``ProfileModel.diff_variance``)."""
        e = self.exps[k]
        return PF.nod_variance(e.var, [self.exps[j].var for j in e.partners])

    def dof_weight(self):
        """Independent values per fitted pixel, (N-1)/N for a set of N nods, 1 for a single frame."""
        n = np.array([len(self.exps[k].partners) for k in self.pix['e']])
        return np.where(n == 0, 1.0, n / (n + 1.0))

    def _grid(self):
        """Spectrum grid (``ProfileModel._grid``)."""
        lams = []
        for det in sorted({e.detector for e in self.exps if e.use_data}):
            cand = [x for x in self.exps if x.use_data and x.detector == det]
            e = next((x for x in cand if x.has_source), cand[0])
            yy = np.where(np.isfinite(e.y), e.y, np.inf)
            r = np.argmin(np.abs(yy - e.src_y), axis=0)
            lam = e.wav[r, np.arange(e.wav.shape[1])]
            lam = np.unique(lam[np.isfinite(lam)])
            used = [self.exps[j].wav[m] for k, m in self.pix_masks if self.exps[k].detector == det
                    for j, _ in PF.term_weights(self.exps, k)]
            used = np.concatenate([u[np.isfinite(u)] for u in used]) if used else np.zeros(0)
            if lam.size >= 2 and used.size:
                lam = PF.cover(lam, used.min(), used.max())
            lams.append(lam)
        g = np.unique(np.concatenate(lams))
        if g.size < 3:
            raise PF.TooFewWavelengths('profile fit: too few wavelength samples')
        step = np.median(np.diff(g))
        keep = [g[0]]
        for x in g[1:]:
            if x - keep[-1] > 0.5 * step:
                keep.append(x)
        return np.array(keep)

    def _interp_tables(self):
        """Per term (a frame of a fitted value), its pixels' interpolation onto the spectrum grid;
        per exposure, the union of the pixels its frame is needed at (``ProfileModel``)."""
        self.terms = []
        g = self.lam_grid
        n = 0
        term_masks = []
        for k, m in self.pix_masks:
            rows = np.arange(n, n + int(m.sum()))
            n += int(m.sum())
            for j, w in PF.term_weights(self.exps, k):
                p = self.exps[j]
                lam = p.wav[m]
                idx = np.clip(np.searchsorted(g, lam) - 1, 0, g.size - 2)
                t = np.clip((lam - g[idx]) / (g[idx + 1] - g[idx]), 0.0, 1.0)
                inside = (lam >= g[0]) & (lam <= g[-1])
                self.terms.append(dict(k=k, j=j, y=p.y[m], lam=lam, dy=p.dy[m], w=w, idx=idx, t=t, inside=inside,
                                       rows=rows))
                term_masks.append(m)
        union = {}
        for T, m in zip(self.terms, term_masks):
            union[T['j']] = union.get(T['j'], np.zeros(m.shape, bool)) | m
        self.image_pix = {}
        for j, U in union.items():
            e = self.exps[j]
            pos = np.full(U.shape, -1)
            pos[U] = np.arange(U.sum())
            self.image_pix[j] = dict(y=e.y[U], lam=e.wav[U], dy=e.dy[U], pos=pos)
        for T, mask in zip(self.terms, term_masks):
            T['upos'] = self.image_pix[T['j']]['pos'][mask]
        self._pl_curves()
        self._tau_fixed = {}

    def _pl_curves(self):
        self.pl_curve = [PF._row_curve(e, e.pl_point) for e in self.exps]
        good = [c for c in self.pl_curve if c[0].size > 3]
        for i, c in enumerate(self.pl_curve):
            if c[0].size <= 3 and good:
                self.pl_curve[i] = good[0]

    # -- placement -------------------------------------------------------------
    def log2_lam(self, lam):
        """log2(lambda / pivot), clamped to the fitted range: the variable a shape's change with
        wavelength is linear in."""
        lo, hi = np.log2(self.lam_rng[0] / self.pivot), np.log2(self.lam_rng[1] / self.pivot)
        return np.clip(np.log2(lam / self.pivot), lo, hi)

    def trace_centre(self, k, lam, cg, eoff=0.0):
        """Where light from slit position ``cg`` (slit units, in the frame of the target's planned
        position ``c_prior``) lands in exposure k: moved to the target's planned position in k
        (``nod_shift``), plus a pointing offset, then the trace-centre law."""
        e = self.exps[k]
        c = np.asarray(cg, float)
        if self.nod_shift is not None:
            c = c + self.nod_shift[k]           # the position in exposure k's slit, where the law applies
        D, a = PF.centre_law(lam, e.detector, e.quadrant, law=self.centre_law_tables)
        return c + eoff + D + a * c

    def trace_stretch(self, k, lam):
        """d(trace centre) / d(slit position) in exposure k: 1 + alpha of the trace-centre law. A
        source's extent along the slit lands stretched by it (``project`` places every point
        through ``trace_centre``; the round components scale their width by it). Alpha is at
        most about 0.013."""
        e = self.exps[k]
        return 1.0 + PF.centre_law(lam, e.detector, e.quadrant, law=self.centre_law_tables)[1]

    def throughput_fixed(self, k, lam, dy):
        """The part of the throughput along the dispersion that no source changes
        (``ProfileModel.throughput_fixed``)."""
        L, P = self.pl_curve[k]
        pl = np.interp(lam, L, P)
        e = self.exps[k]
        o = self.optics[k]
        sl = e.slit_loss
        if sl.y == 'optics':
            if e.pl_ref and np.isfinite(e.src_y_jwst):
                pl = pl * CR.point_y_ratio(e.pl_ref, lam, e.src_y_jwst)
            c_ref = 0.0
        else:
            c_ref = e.src_y if np.isfinite(e.src_y) else 0.0
        if sl.x == 'corrected':
            pl = pl * o.x_correction(lam, e.src_x_jwst)
        t = o.transmission(PF.REF_APERTURE, lam, np.full(np.shape(lam), c_ref), np.full(np.shape(lam), PF.W_FLOOR))
        return pl / np.maximum(t, 1e-6)

    def image_kernel(self, k, y, lam):
        """Exposure k's optics table at every source position, on the pixels (y, lam)
        (``Optics.image_kernel``), built once per exposure and set of pixels: ``project`` places
        every point of an extended source through it. About 3.5 kB per pixel (16 MB for a
        PRISM exposure's 4500)."""
        cache = self.__dict__.setdefault('_kernels', {})
        y, lam = np.ascontiguousarray(y, float).ravel(), np.ascontiguousarray(lam, float).ravel()
        key = (k, y.size, hash(y.tobytes()), hash(lam.tobytes()))
        if key not in cache:
            cache[key] = self.optics[k].image_kernel(self.exps[k].open_k, y, lam)
        return cache[key]

    # -- the model -------------------------------------------------------------
    def frame_light(self, comp, j, y, lam, dy, fixed=None):
        """Exposure j's frame of component ``comp`` per unit total light: the fixed throughput
        times the component's own light (``Component.light``)."""
        if fixed is None:
            fixed = self.throughput_fixed(j, lam, dy)
        return fixed * comp.light(self, j, y, lam, dy)

    BLOCK_CACHE = 8          # component blocks kept (a fit's derivative steps move one component at a time)

    def component_matrix(self, comp):
        """Sparse (n_pixels x n_grid) block of one component: model = block @ spectrum. The last
        ``BLOCK_CACHE`` blocks are kept, keyed by the component's type and values, for components
        whose attributes are all numbers (``_block_key``)."""
        comp = comp.resolved()                                   # its parameters at their values
        key = _block_key(comp)
        cache = self.__dict__.setdefault('_blocks', {})
        if key is not None and key in cache:
            cache[key] = cache.pop(key)                          # most recently used last
            return cache[key]
        block = self._component_matrix(comp)
        if key is not None:
            cache[key] = block
            while len(cache) > self.BLOCK_CACHE:
                cache.pop(next(iter(cache)))
        return block

    def _component_matrix(self, comp):
        fixed = self.__dict__.setdefault('_tau_fixed', {})       # per exposure, on its image pixels
        G = {}
        for j, q in self.image_pix.items():
            if j not in fixed:
                fixed[j] = self.throughput_fixed(j, q['lam'], q['dy'])
            G[j] = fixed[j] * comp.light(self, j, q['y'], q['lam'], q['dy'])
        rows, cols, vals = [], [], []
        for T in self.terms:
            g = T['w'] * G[T['j']][T['upos']]
            g = np.where(T['inside'], g, 0.0)
            rows += [T['rows'], T['rows']]
            cols += [T['idx'], T['idx'] + 1]
            vals += [g * (1 - T['t']), g * T['t']]
        if not vals:
            return sp.csr_matrix((self.npix, self.lam_grid.size))
        return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                             shape=(self.npix, self.lam_grid.size))

    def design(self, components):
        """Sparse (n_pixels x n_components * n_grid) design matrix, one block per component."""
        return self.design_matrix(components)

    def solve(self, components, covariance=False, nonnegative=None):
        """The spectra of ``components`` solved together (``solve_components``)."""
        return self.solve_components(components, covariance=covariance, nonnegative=nonnegative)

    # an application with its own parametrisation (``profile_fit.ProfileModel``) redefines design
    # and solve in its own terms and builds on these two
    def design_matrix(self, components):
        """``design``."""
        blocks = [self.component_matrix(c) for c in components]
        return blocks[0] if len(blocks) == 1 else sp.hstack(blocks, format='csr')

    def solve_components(self, components, covariance=False, nonnegative=None):
        """Weighted least squares at fixed shapes: every component's spectrum, and with the
        master-background term its scale, solved together. The normal matrix is sparse (each
        pixel touches a few neighbouring grid points of each component), solved by LU; the scale
        adds one row and column, eliminated through the same factorisation, with its allowance
        1 / mbkg_sigma^2 on its diagonal. Where two components land on the same pixels their
        spectra are correlated; the covariance (formed only on request) carries it.

        nonnegative : True, or one flag per component: those spectra are held at or above zero
            (``_solve_nonnegative``), e.g. a bulge and a disk that would otherwise trade light,
            one going negative where the other overshoots. Only when the free solve has a
            negative point; otherwise the free solve is the answer. A faint component's noise is
            then cut at zero, so its spectrum is biased high where it has no light.
        Returns a ``Solution``."""
        A = self.design_matrix(components)
        d = self.pix['d']
        wgt = np.where(self.use, 1.0 / self.pix['v'], 0.0)
        AtW = A.T.multiply(wgt).tocsr()
        N = (AtW @ A).tocsc()
        b = AtW @ d
        diag = N.diagonal()
        good = diag > 1e-3 * np.median(diag[diag > 0])
        gi = np.flatnonzero(good)
        Ng = N[gi][:, gi] + sp.identity(gi.size, format='csc') * 1e-9 * np.median(diag[good])
        lu = splu(Ng)
        x = lu.solve(b[good])
        f = np.zeros(diag.size)
        scale, scale_err = 0.0, np.nan
        if self.mbkg_sigma is None:
            f[good] = x
            model = A @ f
            dchi2 = float(f[good] @ b[good])
        else:
            B = self.pix['b']
            u = np.asarray(AtW @ B)[good]
            y = lu.solve(u)
            bd = float(np.sum(wgt * B * d))
            c = float(np.sum(wgt * B * B)) + 1.0 / self.mbkg_sigma ** 2
            S = c - float(u @ y)
            scale = (bd - float(u @ x)) / S
            scale_err = 1.0 / np.sqrt(S)
            f[good] = x - scale * y
            model = A @ f + scale * B
            dchi2 = float(f[good] @ b[good]) + scale * bd - bd ** 2 / c
        n = len(components)
        con = self._nonnegative_mask(nonnegative, n, diag.size)[good]
        if con.any() and np.any(f[good][con] < 0):
            return self._solve_nonnegative(components, A, AtW, Ng, b[good], good, con, f, covariance)
        cov = None
        if covariance:
            inv = np.linalg.inv(Ng.toarray())
            if self.mbkg_sigma is not None:
                inv = inv + np.outer(y, y) / S
            cov = np.full((diag.size, diag.size), np.nan)
            cov[np.ix_(good, good)] = inv
        return Solution(spectra=f.reshape(n, -1), cov=cov, good=good.reshape(n, -1), A=A, model=model,
                        dchi2=dchi2, scale=scale, scale_err=scale_err, at_zero=np.zeros((n, f.size // n), bool))

    @staticmethod
    def _nonnegative_mask(nonnegative, n, size):
        """Per unknown (component-major), whether its spectrum is held at or above zero."""
        if nonnegative is None or nonnegative is False:
            return np.zeros(size, bool)
        if nonnegative is True:
            flags = np.ones(n, bool)
        else:
            flags = np.asarray(nonnegative, bool)
            if flags.shape != (n,):
                raise ValueError(f'forward: nonnegative takes True, False or one flag per component ({n})')
        return np.repeat(flags, size // n)

    def _solve_nonnegative(self, components, A, AtW, Ng, b, good, con, f_free, covariance):
        """The solve with the flagged spectra held at or above zero (``solve_components``): the same
        weighted least squares (and master-background scale with its allowance, never constrained),
        minimised over the allowed region (``nonnegative_normal_solve``). Grid points held at zero are
        marked in ``Solution.at_zero``; their errors are the unconstrained ones (the spread the data
        allow there, without the bound), their covariances with the other points zero."""
        d = self.pix['d']
        wgt = np.where(self.use, 1.0 / self.pix['v'], 0.0)
        ng = Ng.shape[0]
        term = self.mbkg_sigma is not None
        if term:
            B = self.pix['b']
            u = np.asarray(AtW @ B)[good]
            bd = float(np.sum(wgt * B * d))
            c = float(np.sum(wgt * B * B)) + 1.0 / self.mbkg_sigma ** 2
            N = sp.bmat([[Ng, sp.csc_matrix(u[:, None])], [sp.csc_matrix(u[None, :]), sp.csc_matrix([[c]])]],
                        format='csc')
            rhs, con_all = np.append(b, bd), np.append(con, False)
        else:
            N, rhs, con_all = Ng, b, con
        x, free, lu = nonnegative_normal_solve(N, rhs, con_all)
        f = np.zeros(f_free.size)
        f[good] = x[:ng]
        scale = float(x[ng]) if term else 0.0
        model = A @ f + (scale * B if term else 0.0)
        r2 = float(np.sum(wgt * (d - model) ** 2))
        if term:
            dchi2 = (float(np.sum(wgt * d * d)) - bd ** 2 / c) - (r2 + scale ** 2 / self.mbkg_sigma ** 2)
        else:
            dchi2 = float(np.sum(wgt * d * d)) - r2
        fi = np.flatnonzero(free)
        scale_err = np.nan
        if term:
            e = np.zeros(fi.size)
            e[-1] = 1.0                                  # the scale is always free, and last
            scale_err = float(np.sqrt(lu.solve(e)[-1]))
        cov = None
        if covariance:
            Nd = N.toarray()
            full = np.full((ng + term, ng + term), 0.0)
            full[np.ix_(fi, fi)] = np.linalg.inv(Nd[np.ix_(fi, fi)])
            held = np.flatnonzero(~free)
            if held.size:
                full[held, held] = np.diag(np.linalg.inv(Nd))[held]
            cov = np.full((f.size, f.size), np.nan)
            cov[np.ix_(good, good)] = full[:ng, :ng]
        n = len(components)
        at_zero = np.zeros(f.size, bool)
        at_zero[np.flatnonzero(good)[~free[:ng]]] = True
        return Solution(spectra=f.reshape(n, -1), cov=cov, good=good.reshape(n, -1), A=A, model=model,
                        dchi2=dchi2, scale=scale, scale_err=scale_err, at_zero=at_zero.reshape(n, -1))

    def chi2(self, solution):
        """Data chi2 of a solution on the current pixel mask (no prior or allowance terms)."""
        r = (self.pix['d'] - solution.model) / np.sqrt(self.pix['v'])
        return float(np.sum(r[self.use] ** 2))
