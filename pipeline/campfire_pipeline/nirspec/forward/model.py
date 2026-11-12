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

    # -- the model -------------------------------------------------------------
    def frame_light(self, comp, j, y, lam, dy, fixed=None):
        """Exposure j's frame of component ``comp`` per unit total light: the fixed throughput
        times the component's own light (``Component.light``)."""
        if fixed is None:
            fixed = self.throughput_fixed(j, lam, dy)
        return fixed * comp.light(self, j, y, lam, dy)

    def component_matrix(self, comp):
        """Sparse (n_pixels x n_grid) block of one component: model = block @ spectrum."""
        comp = comp.resolved()                                   # its parameters at their values
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

    def solve(self, components, covariance=False):
        """The spectra of ``components`` solved together (``solve_components``)."""
        return self.solve_components(components, covariance=covariance)

    # an application with its own parametrisation (``profile_fit.ProfileModel``) redefines design
    # and solve in its own terms and builds on these two
    def design_matrix(self, components):
        """``design``."""
        blocks = [self.component_matrix(c) for c in components]
        return blocks[0] if len(blocks) == 1 else sp.hstack(blocks, format='csr')

    def solve_components(self, components, covariance=False):
        """Weighted least squares at fixed shapes: every component's spectrum, and with the
        master-background term its scale, solved together. The normal matrix is sparse (each
        pixel touches a few neighbouring grid points of each component), solved by LU; the scale
        adds one row and column, eliminated through the same factorisation, with its allowance
        1 / mbkg_sigma^2 on its diagonal. Where two components land on the same pixels their
        spectra are correlated; the covariance (formed only on request) carries it. Returns a
        ``Solution``."""
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
        cov = None
        if covariance:
            inv = np.linalg.inv(Ng.toarray())
            if self.mbkg_sigma is not None:
                inv = inv + np.outer(y, y) / S
            cov = np.full((diag.size, diag.size), np.nan)
            cov[np.ix_(good, good)] = inv
        n = len(components)
        return Solution(spectra=f.reshape(n, -1), cov=cov, good=good.reshape(n, -1), A=A, model=model,
                        dchi2=dchi2, scale=scale, scale_err=scale_err)

    def chi2(self, solution):
        """Data chi2 of a solution on the current pixel mask (no prior or allowance terms)."""
        r = (self.pix['d'] - solution.model) / np.sqrt(self.pix['v'])
        return float(np.sum(r[self.use] ** 2))
