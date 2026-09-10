"""Emission-line fitter for reduced NIRSpec 1-D spectra.

Measures line fluxes, equivalent widths and kinematics at a **known** redshift —
the inspected redshift pulled from the portal (``reference/nirspec/<obs>/
redshifts.toml``, see :mod:`campfire_pipeline.nirspec.redshift_reference`) —
so the catalog never carries fluxes measured at a wrong ``z``. The redshift
is an input, never a fitted quantity beyond a bounded velocity offset.

Model
-----
Lines are grouped into *complexes*: every catalogued line that lands on the
valid wavelength range gets a support window ``±(n_sigma × σ_total +
dv_max/c × λ)`` around ``λ_rest (1+z)``, and overlapping supports merge.
Each complex is fit independently over a window that extends
``cont_pixels`` pixels of continuum beyond the outermost support, with
pixels under any *other* complex's lines masked out. Inside a window the
model is

    F_λ(λ) = Σ_k F_k φ_k(λ; dv, σ_v) + Σ_m c_m x^m

where each free component ``k`` is one line (plus any doublet partners tied to
it at a fixed ratio), ``φ_k`` is a unit-area Gaussian integrated over the pixel
(so ``F_k`` is the integrated line flux in erg s⁻¹ cm⁻²), the width is
``σ² = σ_LSF(λ)² + (σ_v λ / c)²`` with ``σ_LSF`` from the grating's R-curve
scaled by the same ``f_LSF`` calibration the redshift fitter uses, and the
continuum is a low-order polynomial in the scaled window coordinate ``x``.
Lines closer than ``blend_sigma × σ_total`` are unresolvable and are merged:
the heavier line (``Line.weight``) reports the blended flux (flag ``BLEND``),
modelled as one Gaussian at the weight-averaged wavelength of the pair, and
the companion is flagged ``BLENDED`` with no flux of its own.

Close doublets with a free ratio (``linelist.DOUBLETS``: CIII], [OII], [SII],
MgII, CIV, OIII], NV) are additionally reported as a **total** under the
doublet's own name (``component='doublet'``): the covariance-propagated sum
of the two components where the grating resolves them (flag ``RESOLVED``),
the single blended measurement where it does not. The total is the quantity
that means the same thing in every grating — the components' individual
fluxes are strongly anti-correlated near the resolution limit while their
sum stays well constrained — so catalog selections should use it.

Kinematics are fit in two passes. Pass 1 fits every complex with its own
``(dv, σ_v)``, bounded. Complexes with a line detected at ≥
``kinematics_snr_min`` become *anchors*; their inverse-variance-weighted mean
``(dv, σ_v)`` is the spectrum's global kinematics, and every non-anchor
complex is refit in pass 2 with ``(dv, σ_v)`` fixed to it (a linear problem —
stable for the faint lines whose widths a free fit would otherwise wander
on). With no anchor the defaults ``dv = 0``, ``σ_v = sigma_v_default`` are
used and flagged.

Optionally (``fit_broad``) a second, broad Gaussian is tried on permitted
lines where the LSF can resolve it, and kept when it improves χ² by at least
``broad_delta_chi2`` with a detected broad flux. Prism spectra never qualify.

Errors come from the Jacobian of the final least-squares solve (no χ²
rescaling by default). Fluxes are unconstrained in sign so non-detections
carry honest ``flux ± err`` values from which the reader forms limits.

This module is numpy/scipy only and reads nothing from disk except through
:func:`fit_spec_file`; the stage runner lives in :mod:`linefit_stage`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from scipy.optimize import least_squares
from scipy.special import erf

from campfire_pipeline.nirspec.linelist import DOUBLETS, LINES, LINES_BY_NAME, Line, doublet_wave

log = logging.getLogger('nirspec_linefit')

C_KMS = 299792.458
FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))   # 1 / 2.3548

#: Algorithm version, stamped into every product (``LFITVER``) and into
#: ``spectrum_line_fits.fit_version``. Bump on any change to the model, the
#: line list, or the flag semantics so downstream can tell apart re-fits.
LINEFIT_VERSION = '2'

# Per-line flag bits (``flags`` in the LINES table and in the JSON payload).
FLAG_TIED = 1            # flux is ratio-tied to its doublet primary
FLAG_BLENDED = 2         # unresolved: folded into a blend primary, no flux of its own
FLAG_BLEND = 4           # this line's flux includes blended companions
FLAG_EDGE = 8            # window truncated by the valid wavelength range
FLAG_KIN_GLOBAL = 16     # (dv, sigma_v) fixed to the spectrum's global kinematics
FLAG_KIN_DEFAULT = 32    # (dv, sigma_v) fixed to defaults (no anchor complex)
FLAG_BROAD = 64          # a broad component was accepted on this line
FLAG_NO_CONTINUUM = 128  # continuum undetected at the line: no equivalent width
FLAG_FIT_FAILED = 256    # nonlinear refinement failed; grid/linear solution kept
FLAG_MASKED = 512        # > 30 % of the window pixels were masked
FLAG_SIGMA_UNRESOLVED = 1024  # intrinsic width not constrained by the LSF
FLAG_RESOLVED = 2048     # doublet total whose members were fit as separate components

FLAG_NAMES = {
    FLAG_TIED: 'tied', FLAG_BLENDED: 'blended', FLAG_BLEND: 'blend',
    FLAG_EDGE: 'edge', FLAG_KIN_GLOBAL: 'kin_global',
    FLAG_KIN_DEFAULT: 'kin_default', FLAG_BROAD: 'broad',
    FLAG_NO_CONTINUUM: 'no_continuum', FLAG_FIT_FAILED: 'fit_failed',
    FLAG_MASKED: 'masked', FLAG_SIGMA_UNRESOLVED: 'sigma_unresolved',
    FLAG_RESOLVED: 'resolved',
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class LineFitConfig:
    """Tunables for one fit. Defaults mirror ``[nirspec.line_fitting]`` in
    ``config_default.toml``; :meth:`from_options` reads that section."""
    dv_max: float = 1000.0          # km/s, |velocity offset| bound (gratings)
    dv_max_prism: float = 2500.0    # km/s, same for the prism (coarser inspected z)
    sigma_v_min: float = 10.0       # km/s, intrinsic width bounds
    sigma_v_max: float = 600.0
    sigma_v_default: float = 100.0  # km/s, used when no complex anchors kinematics
    kinematics_snr_min: float = 5.0 # a complex needs a line at this S/N to anchor
    support_sigma: float = 4.0      # line support half-width in units of sigma_total
    cont_pixels: int = 8            # continuum pixels beyond the support, each side
    cont_degree: int = 1            # polynomial degree of the local continuum
    blend_sigma: float = 2.0        # lines closer than this × sigma_total are merged
    min_line_pixels: int = 3        # valid pixels within the support to count as covered
    edge_pixels: int = 3            # ignore this many valid pixels at each end
    detect_snr: float = 3.0         # S/N counted as a detection in the summary
    fit_broad: bool = True
    broad_sigma_min: float = 800.0  # km/s
    broad_sigma_max: float = 5000.0
    broad_dv_max: float = 1000.0
    broad_delta_chi2: float = 25.0  # accept a broad component only above this
    broad_snr_min: float = 3.0
    scale_errors_by_chi2: bool = False
    lines: Optional[list[str]] = None   # restrict to these names (None = all)

    @classmethod
    def from_options(cls, options: dict, grating: str | None = None) -> 'LineFitConfig':
        """Build from the ``[nirspec.line_fitting]`` dict (unknown keys ignored)."""
        kw = {}
        for f in cls.__dataclass_fields__:
            if f in options:
                kw[f] = options[f]
        cfg = cls(**kw)
        return cfg

    def dv_bound(self, grating: str | None) -> float:
        if grating and grating.lower() == 'prism':
            return float(self.dv_max_prism)
        return float(self.dv_max)


# ---------------------------------------------------------------------------
# Small numerics
# ---------------------------------------------------------------------------

def fnu_ujy_to_flam(fnu_ujy, wave_um):
    """µJy → erg s⁻¹ cm⁻² Å⁻¹ (same convention as stage 3's ``flam`` column)."""
    return np.asarray(fnu_ujy, dtype=float) * 2.99792458e-19 / np.asarray(wave_um, dtype=float) ** 2


def pixel_edges(wave):
    """Pixel boundaries as midpoints between centers (extrapolated at the ends)."""
    wave = np.asarray(wave, dtype=float)
    mid = 0.5 * (wave[:-1] + wave[1:])
    lo = np.empty_like(wave)
    hi = np.empty_like(wave)
    lo[0] = wave[0] - 0.5 * (wave[1] - wave[0])
    lo[1:] = mid
    hi[:-1] = mid
    hi[-1] = wave[-1] + 0.5 * (wave[-1] - wave[-2])
    return lo, hi


def gaussian_pixint(lo, hi, mu, sigma):
    """Mean flux density (per Å) across each pixel of a unit-flux Gaussian
    centred at ``mu`` with width ``sigma`` (all in µm): the pixel-integrated
    profile, so ``F × gaussian_pixint`` is comparable to a ``flam`` column."""
    s = math.sqrt(2.0) * sigma
    frac = 0.5 * (erf((hi - mu) / s) - erf((lo - mu) / s))
    return frac / ((hi - lo) * 1.0e4)


def make_r_function(r_wav, r_val, f_lsf=1.0) -> Callable[[np.ndarray], np.ndarray]:
    """Closure R(λ) over a tabulated R-curve, linearly extrapolated redward like
    ``common.spectral.r_at_observed`` (imported lazily there: it pulls numba)."""
    r_wav = np.asarray(r_wav, dtype=float)
    r_val = np.asarray(r_val, dtype=float) * float(f_lsf)

    def r_of(w):
        w = np.atleast_1d(np.asarray(w, dtype=float))
        r = np.interp(w, r_wav, r_val)
        red = w > r_wav[-1]
        if np.any(red):
            tail = r_wav >= np.quantile(r_wav, 0.8)
            if np.count_nonzero(tail) >= 2:
                slope, intercept = np.polyfit(r_wav[tail], r_val[tail], 1)
                r[red] = np.clip(slope * w[red] + intercept, 1.0, None)
        return r
    return r_of


# ---------------------------------------------------------------------------
# Complex construction
# ---------------------------------------------------------------------------

@dataclass
class _LineState:
    line: Line
    wave_obs: float                  # µm at the input redshift
    sigma_lsf: float                 # µm
    support: float                   # µm half-width
    wave_fit: float = 0.0            # µm, model centre (= wave_obs unless a blend primary)
    covered: bool = False
    edge: bool = False
    tie_to: Optional[str] = None     # honored tie primary (same complex)
    tie_ratio: float = 1.0
    blend_into: Optional[str] = None  # blend primary this line folded into
    blend_members: list = field(default_factory=list)


@dataclass
class _Complex:
    index: int
    members: list                    # _LineState, wavelength order
    lo_idx: int = 0                  # window pixel range [lo_idx, hi_idx)
    hi_idx: int = 0
    edge: bool = False
    result: Optional[dict] = None

    @property
    def names(self):
        return [m.line.name for m in self.members]


def _select_lines(cfg: LineFitConfig):
    if cfg.lines is None:
        return list(LINES)
    return [LINES_BY_NAME[n] for n in cfg.lines]


def _build_line_states(lines, z, wave, valid, r_of, cfg: LineFitConfig, dv_max):
    """Observed-frame placement, LSF width and coverage for every line."""
    vidx = np.where(valid)[0]
    w_lo, w_hi = wave[vidx[0]], wave[vidx[-1]]
    states = []
    for line in lines:
        w_obs = line.wave * (1.0 + z) * 1.0e-4
        if not (w_lo <= w_obs <= w_hi):
            continue
        r = float(r_of(w_obs)[0])
        sigma_lsf = w_obs / r * FWHM_TO_SIGMA
        sigma_tot = math.hypot(sigma_lsf, cfg.sigma_v_max / C_KMS * w_obs)
        support = cfg.support_sigma * sigma_tot + dv_max / C_KMS * w_obs
        st = _LineState(line=line, wave_obs=w_obs, sigma_lsf=sigma_lsf, support=support,
                        wave_fit=w_obs)
        # Covered = enough valid pixels across the support AND the line core
        # (±2σ at the default width) mostly unmasked, so a bad-pixel hole on
        # the line itself does not get "measured" from its wings.
        inside = valid & (wave >= w_obs - support) & (wave <= w_obs + support)
        core_hw = 2.0 * math.hypot(sigma_lsf, cfg.sigma_v_default / C_KMS * w_obs)
        in_core = (wave >= w_obs - core_hw) & (wave <= w_obs + core_hw)
        n_core = int(np.count_nonzero(in_core))
        core_ok = n_core == 0 or np.count_nonzero(valid & in_core) >= 0.5 * n_core
        st.covered = int(np.count_nonzero(inside)) >= cfg.min_line_pixels and core_ok
        st.edge = (w_obs - support < w_lo) or (w_obs + support > w_hi)
        if st.covered:
            states.append(st)
    states.sort(key=lambda s: s.wave_obs)
    return states


def _group_complexes(states):
    complexes = []
    current = []
    cur_hi = -np.inf
    for st in states:
        lo, hi = st.wave_obs - st.support, st.wave_obs + st.support
        if current and lo <= cur_hi:
            current.append(st)
            cur_hi = max(cur_hi, hi)
        else:
            if current:
                complexes.append(_Complex(index=len(complexes), members=current))
            current = [st]
            cur_hi = hi
    if current:
        complexes.append(_Complex(index=len(complexes), members=current))
    return complexes


def _resolve_ties_and_blends(cx: _Complex, cfg: LineFitConfig):
    """Honor doublet ties inside the complex, then fold unresolvable pairs."""
    names = {m.line.name: m for m in cx.members}
    # ties: only when the primary is in the same complex
    for m in cx.members:
        if m.line.tie and m.line.tie[0] in names:
            m.tie_to, m.tie_ratio = m.line.tie
    # blends: among tie-group heads (a tied line follows its primary)
    heads = [m for m in cx.members if m.tie_to is None]

    def sigma_tot(m):
        return math.hypot(m.sigma_lsf, cfg.sigma_v_default / C_KMS * m.wave_obs)

    def group_weight(head):
        w = head.line.weight
        for m in cx.members:
            if m.tie_to == head.line.name:
                w += m.line.weight
        return w

    # Greedy: heaviest head absorbs any lighter head within blend_sigma.
    heads.sort(key=group_weight, reverse=True)
    absorbed = set()
    for i, primary in enumerate(heads):
        if primary.line.name in absorbed:
            continue
        for other in heads[i + 1:]:
            if other.line.name in absorbed:
                continue
            sep = abs(other.wave_obs - primary.wave_obs)
            thr = cfg.blend_sigma * max(sigma_tot(primary), sigma_tot(other))
            if sep < thr:
                other.blend_into = primary.line.name
                primary.blend_members.append(other.line.name)
                absorbed.add(other.line.name)
                # tied followers of the absorbed head fold in too
                for m in cx.members:
                    if m.tie_to == other.line.name:
                        m.blend_into = primary.line.name
                        m.tie_to = None
                        primary.blend_members.append(m.line.name)
                        absorbed.add(m.line.name)
    # A blend primary is modelled at the weight-averaged wavelength of
    # everything it absorbed, not at its own rest wavelength: negligible for
    # the prism's Hα+[NII], but it keeps the single-Gaussian model honest for
    # an unresolved doublet whose members are comparably strong ([OII]).
    z1 = None
    for primary in heads:
        if primary.blend_members:
            if z1 is None:
                z1 = primary.wave_obs / primary.line.wave
            primary.wave_fit = doublet_wave([primary.line.name] + primary.blend_members) * z1


def _free_components(cx: _Complex):
    """Ordered list of (head_state, [(state, ratio), ...]) fit units."""
    comps = []
    for m in cx.members:
        if m.tie_to is not None or m.blend_into is not None:
            continue
        unit = [(m, 1.0)]
        for f in cx.members:
            if f.tie_to == m.line.name and f.blend_into is None:
                unit.append((f, f.tie_ratio))
        comps.append((m, unit))
    return comps


# ---------------------------------------------------------------------------
# Per-complex model
# ---------------------------------------------------------------------------

class _WindowModel:
    """All the per-window arrays a fit needs, plus the model evaluator.

    Parameter vector layout: ``[F_0..F_{k-1}, c_0..c_d, dv, sigma_v
    (, Fb_0..Fb_{nb-1}, dv_b, sigma_b)]``. The linear block (fluxes +
    continuum) is solved in closed form for a given nonlinear block.
    """

    def __init__(self, cx: _Complex, wave, flam, err, use, lo, hi, cfg: LineFitConfig,
                 r_of, broad_heads=None):
        self.cx = cx
        self.cfg = cfg
        sl = slice(cx.lo_idx, cx.hi_idx)
        self.idx = np.where(use[sl])[0] + cx.lo_idx
        self.w = wave[self.idx]
        self.lo = lo[self.idx]
        self.hi = hi[self.idx]
        self.y = flam[self.idx]
        self.e = err[self.idx]
        self.components = _free_components(cx)
        self.n_flux = len(self.components)
        self.n_cont = cfg.cont_degree + 1
        wc = 0.5 * (wave[cx.lo_idx] + wave[cx.hi_idx - 1])
        half = max(0.5 * (wave[cx.hi_idx - 1] - wave[cx.lo_idx]), 1e-6)
        self.x = (self.w - wc) / half
        self.wc, self.half = wc, half
        self.r_of = r_of
        self.broad_heads = list(broad_heads or [])
        self.n_broad = len(self.broad_heads)

    # --- layout helpers ---------------------------------------------------
    @property
    def n_lin(self):
        return self.n_flux + self.n_cont + self.n_broad

    def profiles(self, dv, sigma_v, dv_b=0.0, sigma_b=None):
        """Column basis for the linear solve at the given nonlinear params."""
        cols = []
        shift = 1.0 + dv / C_KMS
        for head, unit in self.components:
            col = np.zeros_like(self.w)
            for st, ratio in unit:
                mu = st.wave_fit * shift
                sig = math.hypot(st.sigma_lsf, sigma_v / C_KMS * mu)
                col += ratio * gaussian_pixint(self.lo, self.hi, mu, sig)
            cols.append(col)
        for m in range(self.n_cont):
            cols.append(self.x ** m)
        if self.n_broad:
            shift_b = 1.0 + dv_b / C_KMS
            for st in self.broad_heads:
                mu = st.wave_fit * shift_b
                sig = math.hypot(st.sigma_lsf, sigma_b / C_KMS * mu)
                cols.append(gaussian_pixint(self.lo, self.hi, mu, sig))
        return np.column_stack(cols)

    def linear_solve(self, dv, sigma_v, dv_b=0.0, sigma_b=None):
        """Weighted least squares for the linear block; returns (theta, cov, chi2)."""
        A = self.profiles(dv, sigma_v, dv_b, sigma_b)
        Aw = A / self.e[:, None]
        bw = self.y / self.e
        theta, *_ = np.linalg.lstsq(Aw, bw, rcond=None)
        resid = bw - Aw @ theta
        chi2 = float(resid @ resid)
        ata = Aw.T @ Aw
        try:
            cov = np.linalg.inv(ata)
        except np.linalg.LinAlgError:
            cov = np.linalg.pinv(ata)
        return theta, cov, chi2

    def residuals(self, p):
        nl = self.n_flux + self.n_cont
        dv, sigma_v = p[nl], p[nl + 1]
        if self.n_broad:
            dv_b, sigma_b = p[nl + 2], p[nl + 3]
            lin = np.concatenate([p[:nl], p[nl + 4:nl + 4 + self.n_broad]])
        else:
            dv_b, sigma_b = 0.0, None
            lin = p[:nl]
        A = self.profiles(dv, sigma_v, dv_b, sigma_b)
        return (self.y - A @ lin) / self.e

    def evaluate(self, p, wave_full, lo_full, hi_full):
        """Model and continuum on an arbitrary pixel grid (for the MODEL table)."""
        nl = self.n_flux + self.n_cont
        dv, sigma_v = p[nl], p[nl + 1]
        shift = 1.0 + dv / C_KMS
        x = (wave_full - self.wc) / self.half
        cont = np.zeros_like(wave_full)
        for m in range(self.n_cont):
            cont += p[self.n_flux + m] * x ** m
        model = cont.copy()
        for k, (head, unit) in enumerate(self.components):
            for st, ratio in unit:
                mu = st.wave_fit * shift
                sig = math.hypot(st.sigma_lsf, sigma_v / C_KMS * mu)
                model += p[k] * ratio * gaussian_pixint(lo_full, hi_full, mu, sig)
        if self.n_broad:
            dv_b, sigma_b = p[nl + 2], p[nl + 3]
            shift_b = 1.0 + dv_b / C_KMS
            for j, st in enumerate(self.broad_heads):
                mu = st.wave_fit * shift_b
                sig = math.hypot(st.sigma_lsf, sigma_b / C_KMS * mu)
                model += p[nl + 4 + j] * gaussian_pixint(lo_full, hi_full, mu, sig)
        return model, cont


def _grid_init(wm: _WindowModel, dv_max, cfg: LineFitConfig):
    """Coarse (dv, sigma_v) grid search seeding the nonlinear refinement."""
    # dv step: a third of the LSF in velocity, never finer than 10 km/s nor
    # coarser than dv_max/4, so prisms get ~10 steps and gratings ~30.
    sig_kms = min(m.sigma_lsf / m.wave_obs * C_KMS for m in wm.cx.members)
    step = float(np.clip(sig_kms / 3.0, 10.0, max(dv_max / 4.0, 10.0)))
    dv_grid = np.arange(-dv_max, dv_max + 0.5 * step, step)
    sv_grid = np.geomspace(cfg.sigma_v_min, cfg.sigma_v_max, 8)
    best = (np.inf, 0.0, cfg.sigma_v_default)
    for sv in sv_grid:
        for dv in dv_grid:
            _, _, chi2 = wm.linear_solve(dv, sv)
            if chi2 < best[0]:
                best = (chi2, float(dv), float(sv))
    return best


def _refine(wm: _WindowModel, p0, lower, upper, resid=None):
    """Bounded least-squares refinement from ``p0``; returns (p, cov, chi2, ok).

    ``resid`` overrides the residual function (default ``wm.residuals``) for
    fits over a subset of the parameter vector."""
    resid = resid or wm.residuals
    # Start strictly inside the bounds (trf requires it); infinite bounds
    # need no clipping.
    p0 = np.array(p0, dtype=float)
    fin_lo = np.isfinite(lower)
    fin_hi = np.isfinite(upper)
    span = np.where(fin_lo & fin_hi, upper - lower, 1.0)
    p0[fin_lo] = np.maximum(p0[fin_lo], lower[fin_lo] + 1e-6 * span[fin_lo])
    p0[fin_hi] = np.minimum(p0[fin_hi], upper[fin_hi] - 1e-6 * span[fin_hi])
    try:
        res = least_squares(resid, p0, bounds=(lower, upper), x_scale='jac',
                            method='trf', max_nfev=200 * len(p0))
    except Exception as e:  # pragma: no cover - scipy internals
        log.debug(f"least_squares raised: {e}")
        return p0, None, float(np.sum(resid(p0) ** 2)), False
    if not res.success and res.status <= 0:
        return p0, None, float(np.sum(resid(p0) ** 2)), False
    J = res.jac
    jtj = J.T @ J
    try:
        cov = np.linalg.inv(jtj)
    except np.linalg.LinAlgError:
        cov = np.linalg.pinv(jtj)
    chi2 = float(2.0 * res.cost)
    return res.x, cov, chi2, True


def _fit_complex(wm: _WindowModel, dv_max, cfg: LineFitConfig, fixed_kin=None):
    """Fit one window. ``fixed_kin=(dv, sigma_v)`` makes it a linear solve.

    Returns a dict with the parameter vector, covariance, chi2, dof, and the
    kinematics actually used.
    """
    nl = wm.n_flux + wm.n_cont
    if fixed_kin is not None:
        dv, sv = fixed_kin
        theta, cov_lin, chi2 = wm.linear_solve(dv, sv)
        p = np.concatenate([theta, [dv, sv]])
        cov = np.zeros((nl + 2, nl + 2))
        cov[:nl, :nl] = cov_lin
        return dict(p=p, cov=cov, chi2=chi2, dof=len(wm.y) - nl, ok=True,
                    kin_free=False, broad=False)

    chi2_0, dv0, sv0 = _grid_init(wm, dv_max, cfg)
    theta, _, _ = wm.linear_solve(dv0, sv0)
    p0 = np.concatenate([theta, [dv0, sv0]])
    lower = np.concatenate([np.full(nl, -np.inf), [-dv_max, cfg.sigma_v_min]])
    upper = np.concatenate([np.full(nl, np.inf), [dv_max, cfg.sigma_v_max]])
    p, cov, chi2, ok = _refine(wm, p0, lower, upper)
    if not ok or cov is None:
        theta, cov_lin, chi2 = wm.linear_solve(dv0, sv0)
        p = np.concatenate([theta, [dv0, sv0]])
        cov = np.zeros((nl + 2, nl + 2))
        cov[:nl, :nl] = cov_lin
        cov[nl, nl] = cov[nl + 1, nl + 1] = np.nan
        ok = False
    return dict(p=p, cov=cov, chi2=chi2, dof=len(wm.y) - (nl + 2), ok=ok,
                kin_free=True, broad=False)


def _fit_broad_pinned(wm: _WindowModel, prior, dv_fix, sv_fix, cfg: LineFitConfig):
    """Pass-2 refit of a complex whose broad component was accepted in pass 1:
    the narrow ``(dv, sigma_v)`` are pinned to the global values while the
    fluxes, continuum and the broad ``(dv_b, sigma_b)`` stay free, so an
    accepted broad line is never silently dropped because its narrow
    counterpart is too faint to anchor. Returns a fit dict in the full
    parameter layout, or None when the refinement fails."""
    nl = wm.n_flux + wm.n_cont
    nb = wm.n_broad
    p_prev = prior['p']

    def expand(q):
        return np.concatenate([q[:nl], [dv_fix, sv_fix], q[nl:nl + 2], q[nl + 2:]])

    def resid(q):
        return wm.residuals(expand(q))

    q0 = np.concatenate([p_prev[:nl], p_prev[nl + 2:nl + 4], p_prev[nl + 4:]])
    lower = np.concatenate([np.full(nl, -np.inf), [-cfg.broad_dv_max, cfg.broad_sigma_min],
                            np.full(nb, -np.inf)])
    upper = np.concatenate([np.full(nl, np.inf), [cfg.broad_dv_max, cfg.broad_sigma_max],
                            np.full(nb, np.inf)])
    q, cov_q, chi2, ok = _refine(wm, q0, lower, upper, resid=resid)
    if not ok or cov_q is None:
        return None
    # Map the reduced covariance back onto the full layout (pinned entries zero).
    idx = list(range(nl)) + [nl + 2, nl + 3] + [nl + 4 + j for j in range(nb)]
    cov = np.zeros((nl + 4 + nb, nl + 4 + nb))
    cov[np.ix_(idx, idx)] = cov_q
    return dict(p=expand(q), cov=cov, chi2=chi2, dof=len(wm.y) - len(q), ok=True,
                kin_free=False, broad=True, delta_chi2=prior.get('delta_chi2'))


def _try_broad(wm_narrow: _WindowModel, narrow_fit, cx, wave, flam, err, use, lo, hi,
               cfg: LineFitConfig, r_of):
    """Add a broad Gaussian on each permitted line in the complex and keep it
    when χ² improves by ``broad_delta_chi2`` with a detected broad flux."""
    heads = [m for m in cx.members
             if m.line.broad and m.tie_to is None and m.blend_into is None]
    if not heads:
        return None
    # Resolvable only when the LSF is finer than half the narrowest broad width.
    for st in heads:
        if st.sigma_lsf / st.wave_obs * C_KMS > 0.5 * cfg.broad_sigma_min:
            return None
    wm = _WindowModel(cx, wave, flam, err, use, lo, hi, cfg, r_of, broad_heads=heads)
    nl = wm.n_flux + wm.n_cont
    p_n = narrow_fit['p']
    dv0, sv0 = p_n[nl], p_n[nl + 1]
    sb0 = math.sqrt(cfg.broad_sigma_min * cfg.broad_sigma_max)
    theta, _, _ = wm.linear_solve(dv0, sv0, dv0, sb0)
    p0 = np.concatenate([theta[:nl], [dv0, sv0, dv0, sb0], theta[nl:]])
    dv_max = narrow_fit['dv_max']
    lower = np.concatenate([np.full(nl, -np.inf), [-dv_max, cfg.sigma_v_min,
                            -cfg.broad_dv_max, cfg.broad_sigma_min], np.full(wm.n_broad, -np.inf)])
    upper = np.concatenate([np.full(nl, np.inf), [dv_max, cfg.sigma_v_max,
                            cfg.broad_dv_max, cfg.broad_sigma_max], np.full(wm.n_broad, np.inf)])
    p, cov, chi2, ok = _refine(wm, p0, lower, upper)
    if not ok or cov is None:
        return None
    dchi2 = narrow_fit['chi2'] - chi2
    fb = p[nl + 4:]
    eb = np.sqrt(np.clip(np.diag(cov)[nl + 4:], 0, None))
    snr_b = np.where(eb > 0, fb / eb, 0.0)
    if dchi2 >= cfg.broad_delta_chi2 and np.any(snr_b >= cfg.broad_snr_min):
        return dict(p=p, cov=cov, chi2=chi2, dof=len(wm.y) - len(p), ok=True,
                    kin_free=True, broad=True, wm=wm, delta_chi2=float(dchi2))
    return None


# ---------------------------------------------------------------------------
# Result assembly
# ---------------------------------------------------------------------------

def _line_records(cx: _Complex, wm: _WindowModel, fit, z, cfg: LineFitConfig, kin_flag):
    """Per-line dicts for one fitted complex."""
    p, cov = fit['p'], fit['cov']
    nl = wm.n_flux + wm.n_cont
    diag = np.diag(cov)
    dv, sv = float(p[nl]), float(p[nl + 1])
    dv_err = float(np.sqrt(diag[nl])) if np.isfinite(diag[nl]) and diag[nl] > 0 else float('nan')
    sv_err = float(np.sqrt(diag[nl + 1])) if np.isfinite(diag[nl + 1]) and diag[nl + 1] > 0 else float('nan')
    if not fit['kin_free']:
        # kinematics were pinned: report the global values' own uncertainties
        dv_err, sv_err = fit.get('kin_err', (float('nan'), float('nan')))
    n_use = len(wm.y)
    n_win = cx.hi_idx - cx.lo_idx
    masked_frac = 1.0 - n_use / max(n_win, 1)
    chi2, dof = fit['chi2'], max(fit['dof'], 1)

    # continuum + its error at an arbitrary wavelength (polynomial covariance)
    def cont_at(w):
        x = (w - wm.wc) / wm.half
        xs = np.array([x ** m for m in range(wm.n_cont)])
        c = p[wm.n_flux:nl]
        cc = cov[wm.n_flux:nl, wm.n_flux:nl]
        val = float(xs @ c)
        var = float(xs @ cc @ xs)
        return val, math.sqrt(var) if var > 0 else float('nan')

    records = {}
    head_index = {head.line.name: k for k, (head, _unit) in enumerate(wm.components)}
    shift = 1.0 + dv / C_KMS

    for m in cx.members:
        rec = dict(
            label=m.line.label, wave_rest=m.line.wave,
            wave_obs=m.wave_fit * shift, complex=cx.index, component='narrow',
            dv=dv, dv_err=dv_err, sigma_v=sv, sigma_v_err=sv_err,
            sigma_lsf_kms=m.sigma_lsf / m.wave_obs * C_KMS,
            chi2=chi2, dof=dof, npix=n_use, flags=0,
            blend_members=list(m.blend_members) or None, blend_into=m.blend_into,
            tied_to=m.tie_to,
        )
        flags = kin_flag
        if m.edge or cx.edge:
            flags |= FLAG_EDGE
        if masked_frac > 0.3:
            flags |= FLAG_MASKED
        if not fit['ok']:
            flags |= FLAG_FIT_FAILED
        if fit['kin_free'] and (not np.isfinite(sv_err) or sv_err >= sv
                                or m.sigma_lsf / m.wave_obs * C_KMS > 2.0 * sv):
            flags |= FLAG_SIGMA_UNRESOLVED

        if m.blend_into is not None:
            flags |= FLAG_BLENDED
            rec.update(flux=float('nan'), flux_err=float('nan'), snr=float('nan'),
                       ew_rest=float('nan'), ew_rest_err=float('nan'),
                       cont=float('nan'), cont_err=float('nan'))
        else:
            if m.tie_to is not None:
                k = head_index[m.tie_to]
                ratio = m.tie_ratio
                flags |= FLAG_TIED
            else:
                k = head_index[m.line.name]
                ratio = 1.0
                if m.blend_members:
                    flags |= FLAG_BLEND
            f = float(p[k]) * ratio
            fe = float(np.sqrt(diag[k])) * ratio if diag[k] > 0 else float('nan')
            c, ce = cont_at(m.wave_fit * shift)
            snr = f / fe if fe and np.isfinite(fe) and fe > 0 else float('nan')
            if np.isfinite(ce) and ce > 0 and c / ce >= 1.0:
                ew = f / c / (1.0 + z)
                ew_err = abs(ew) * math.sqrt((fe / f) ** 2 + (ce / c) ** 2) if f != 0 else abs(fe / c / (1.0 + z))
            else:
                ew, ew_err = float('nan'), float('nan')
                flags |= FLAG_NO_CONTINUUM
            rec.update(flux=f, flux_err=fe, snr=snr, ew_rest=ew, ew_rest_err=ew_err,
                       cont=c, cont_err=ce)
        rec['flags'] = int(flags)
        records[m.line.name] = rec

    records.update(_doublet_records(cx, wm, fit, records, z, dv, dv_err, sv, sv_err,
                                    chi2, dof, n_use, cont_at))

    if fit.get('broad'):
        dv_b, sb = float(p[nl + 2]), float(p[nl + 3])
        dvb_err = float(np.sqrt(diag[nl + 2])) if diag[nl + 2] > 0 else float('nan')
        sb_err = float(np.sqrt(diag[nl + 3])) if diag[nl + 3] > 0 else float('nan')
        for j, st in enumerate(wm.broad_heads):
            k = nl + 4 + j
            f = float(p[k])
            fe = float(np.sqrt(diag[k])) if diag[k] > 0 else float('nan')
            c, ce = cont_at(st.wave_fit * (1.0 + dv_b / C_KMS))
            ok_c = np.isfinite(ce) and ce > 0 and c / ce >= 1.0
            ew = f / c / (1.0 + z) if ok_c else float('nan')
            ew_err = (abs(ew) * math.sqrt((fe / f) ** 2 + (ce / c) ** 2)
                      if ok_c and f != 0 else float('nan'))
            records[st.line.name]['flags'] |= FLAG_BROAD
            records[st.line.name + '_broad'] = dict(
                label=st.line.label + ' (broad)', wave_rest=st.line.wave,
                wave_obs=st.wave_fit * (1.0 + dv_b / C_KMS), complex=cx.index,
                component='broad', dv=dv_b, dv_err=dvb_err, sigma_v=sb,
                sigma_v_err=sb_err, sigma_lsf_kms=st.sigma_lsf / st.wave_obs * C_KMS,
                chi2=chi2, dof=dof, npix=n_use,
                flags=int(FLAG_BROAD | (FLAG_NO_CONTINUUM if not ok_c else 0)),
                flux=f, flux_err=fe, snr=f / fe if fe and fe > 0 else float('nan'),
                ew_rest=ew, ew_rest_err=ew_err, cont=c, cont_err=ce,
                blend_members=None, blend_into=None, tied_to=None,
                broad_delta_chi2=fit.get('delta_chi2'),
            )
    return records


_INHERITED_FLAGS = (FLAG_EDGE | FLAG_MASKED | FLAG_FIT_FAILED | FLAG_SIGMA_UNRESOLVED
                    | FLAG_KIN_GLOBAL | FLAG_KIN_DEFAULT)


def _doublet_records(cx, wm, fit, records, z, dv, dv_err, sv, sv_err, chi2, dof, n_use, cont_at):
    """Doublet totals (``component='doublet'``) for every catalog doublet whose
    members both sit in this complex.

    The total is the sum of the free components the members' flux lives in,
    with the full covariance: two components when resolved (``RESOLVED``),
    one when the pair was merged. A member folded into a line *outside* the
    doublet leaves the total unmeasurable (``BLENDED``, ``blend_into`` names
    the carrier); a carrier that also absorbed a foreign line makes the total
    a superset, reported like any other blend (``BLEND`` + ``blend_members``).
    """
    p, cov = fit['p'], fit['cov']
    members = {m.line.name: m for m in cx.members}
    head_index = {head.line.name: k for k, (head, _unit) in enumerate(wm.components)}
    shift = 1.0 + dv / C_KMS
    out = {}
    for d in DOUBLETS:
        if not all(n in members for n in d.members):
            continue
        ms = [members[n] for n in d.members]
        wave_rest = d.wave
        w_obs = wave_rest * (1.0 + z) * 1.0e-4
        inherited = 0
        for m in ms:
            inherited |= records[m.line.name]['flags'] & _INHERITED_FLAGS
        rec = dict(
            label=d.label, wave_rest=wave_rest, wave_obs=w_obs * shift, complex=cx.index,
            component='doublet', dv=dv, dv_err=dv_err, sigma_v=sv, sigma_v_err=sv_err,
            sigma_lsf_kms=float(np.mean([m.sigma_lsf / m.wave_obs * C_KMS for m in ms])),
            chi2=chi2, dof=dof, npix=n_use, flags=inherited,
            blend_members=None, blend_into=None, tied_to=None, members=list(d.members),
        )
        carriers = [m.blend_into or m.line.name for m in ms]
        foreign = [c for c in carriers if c not in d.members]
        if foreign or any(m.tie_to is not None for m in ms):
            rec.update(flags=inherited | FLAG_BLENDED, blend_into=foreign[0] if foreign else None,
                       flux=float('nan'), flux_err=float('nan'), snr=float('nan'),
                       ew_rest=float('nan'), ew_rest_err=float('nan'),
                       cont=float('nan'), cont_err=float('nan'))
            out[d.name] = rec
            continue
        ks = sorted({head_index[c] for c in carriers})
        f = float(sum(p[k] for k in ks))
        var = float(sum(cov[i, j] for i in ks for j in ks))
        fe = math.sqrt(var) if np.isfinite(var) and var > 0 else float('nan')
        flags = inherited
        if len(ks) == 2:
            flags |= FLAG_RESOLVED
        extra = [n for k in ks for n in wm.components[k][0].blend_members if n not in d.members]
        if extra:
            flags |= FLAG_BLEND
            rec['blend_members'] = extra
        c, ce = cont_at(w_obs * shift)
        snr = f / fe if np.isfinite(fe) and fe > 0 else float('nan')
        if np.isfinite(ce) and ce > 0 and c / ce >= 1.0:
            ew = f / c / (1.0 + z)
            ew_err = abs(ew) * math.sqrt((fe / f) ** 2 + (ce / c) ** 2) if f != 0 else abs(fe / c / (1.0 + z))
        else:
            ew, ew_err = float('nan'), float('nan')
            flags |= FLAG_NO_CONTINUUM
        rec.update(flux=f, flux_err=fe, snr=snr, ew_rest=ew, ew_rest_err=ew_err,
                   cont=c, cont_err=ce, flags=int(flags))
        out[d.name] = rec
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def fit_lines(wave_um, fnu_ujy, fnu_err_ujy, z, r_of, cfg: Optional[LineFitConfig] = None,
              grating: str | None = None, mask=None) -> dict:
    """Fit the catalogued emission lines of one 1-D spectrum at redshift ``z``.

    Parameters
    ----------
    wave_um, fnu_ujy, fnu_err_ujy : array
        The ``SPEC1D`` columns (µm, µJy, µJy). Non-finite or non-positive
        errors mark bad pixels.
    z : float
        The redshift to fit at (the inspected redshift).
    r_of : callable
        ``R(λ_µm) → R`` — the effective resolving power including ``f_LSF``;
        build with :func:`make_r_function`.
    cfg : LineFitConfig, optional
    grating : str, optional
        Only used to pick the prism's wider ``dv`` bound.
    mask : bool array, optional
        Extra pixels to exclude (True = use).

    Returns
    -------
    dict
        ``lines`` — ``{name: record}`` (see :func:`_line_records`);
        ``summary`` — per-spectrum scalars (``z_used``, ``z_fit``, global
        kinematics, counts, χ²);
        ``model`` — ``(model_flam, cont_flam)`` on the input grid, NaN
        outside fitted windows;
        ``complexes`` — the window bookkeeping (indices, names).
    """
    cfg = cfg or LineFitConfig()
    wave = np.asarray(wave_um, dtype=float)
    fnu = np.asarray(fnu_ujy, dtype=float)
    fnu_err = np.asarray(fnu_err_ujy, dtype=float)
    valid = np.isfinite(wave) & np.isfinite(fnu) & np.isfinite(fnu_err) & (fnu_err > 0)
    if mask is not None:
        valid &= np.asarray(mask, dtype=bool)
    vidx = np.where(valid)[0]
    if cfg.edge_pixels > 0 and len(vidx) > 2 * cfg.edge_pixels + 10:
        valid[vidx[:cfg.edge_pixels]] = False
        valid[vidx[-cfg.edge_pixels:]] = False

    empty = dict(lines={}, summary=_summary(z, None, [], {}, cfg), model=(np.full_like(wave, np.nan),
                 np.full_like(wave, np.nan)), complexes=[])
    if np.count_nonzero(valid) < 10:
        return empty

    flam = fnu_ujy_to_flam(fnu, wave)
    err = fnu_ujy_to_flam(fnu_err, wave)
    lo, hi = pixel_edges(wave)
    dv_max = cfg.dv_bound(grating)

    states = _build_line_states(_select_lines(cfg), z, wave, valid, r_of, cfg, dv_max)
    if not states:
        return empty
    complexes = _group_complexes(states)

    # Windows: support ± cont_pixels, clipped to the array; foreign-line masking.
    n = len(wave)
    for cx in complexes:
        w_lo = min(m.wave_obs - m.support for m in cx.members)
        w_hi = max(m.wave_obs + m.support for m in cx.members)
        i_lo = int(np.searchsorted(wave, w_lo, side='left'))
        i_hi = int(np.searchsorted(wave, w_hi, side='right'))
        cx.lo_idx = max(i_lo - cfg.cont_pixels, 0)
        cx.hi_idx = min(i_hi + cfg.cont_pixels, n)
        cx.edge = (i_lo - cfg.cont_pixels < 0) or (i_hi + cfg.cont_pixels > n)
        _resolve_ties_and_blends(cx, cfg)

    foreign = np.zeros(n, dtype=bool)   # pixels under any line's support
    per_cx_support = []
    for cx in complexes:
        sup = np.zeros(n, dtype=bool)
        for m in cx.members:
            sup |= (wave >= m.wave_obs - m.support) & (wave <= m.wave_obs + m.support)
        per_cx_support.append(sup)
        foreign |= sup

    # Pass 1: free kinematics per complex.
    fits = []
    wms = []
    for cx, sup in zip(complexes, per_cx_support):
        use = valid & ~(foreign & ~sup)
        wm = _WindowModel(cx, wave, flam, err, use, lo, hi, cfg, r_of)
        if len(wm.y) < wm.n_lin + 3:
            fits.append(None)
            wms.append(wm)
            continue
        fit = _fit_complex(wm, dv_max, cfg)
        fit['dv_max'] = dv_max
        if cfg.fit_broad:
            bf = _try_broad(wm, fit, cx, wave, flam, err, use, lo, hi, cfg, r_of)
            if bf is not None:
                bf['dv_max'] = dv_max
                fit, wm = bf, bf['wm']
        fits.append(fit)
        wms.append(wm)

    # Anchors → global kinematics.
    anchors = []
    for cx, wm, fit in zip(complexes, wms, fits):
        if fit is None:
            continue
        nl = wm.n_flux + wm.n_cont
        diag = np.diag(fit['cov'])
        snr = [abs(fit['p'][k]) / math.sqrt(diag[k]) for k in range(wm.n_flux) if diag[k] > 0]
        if snr and max(snr) >= cfg.kinematics_snr_min:
            dv, sv = fit['p'][nl], fit['p'][nl + 1]
            dve = math.sqrt(diag[nl]) if np.isfinite(diag[nl]) and diag[nl] > 0 else float('nan')
            sve = math.sqrt(diag[nl + 1]) if np.isfinite(diag[nl + 1]) and diag[nl + 1] > 0 else float('nan')
            anchors.append((cx.index, dv, dve, sv, sve, max(snr)))

    if anchors:
        kin_source = 'anchor'
        dv_g, dv_g_err = _weighted_mean([a[1] for a in anchors], [a[2] for a in anchors],
                                        [a[5] for a in anchors])
        sv_g, sv_g_err = _weighted_mean([a[3] for a in anchors], [a[4] for a in anchors],
                                        [a[5] for a in anchors])
        sv_g = float(np.clip(sv_g, cfg.sigma_v_min, cfg.sigma_v_max))
        pass2_flag = FLAG_KIN_GLOBAL
    else:
        kin_source = 'default'
        dv_g, dv_g_err, sv_g, sv_g_err = 0.0, float('nan'), cfg.sigma_v_default, float('nan')
        pass2_flag = FLAG_KIN_DEFAULT

    anchor_idx = {a[0] for a in anchors}

    # Pass 2: refit non-anchors with fixed kinematics (linear).
    lines = {}
    model = np.full(n, np.nan)
    cont = np.full(n, np.nan)
    cx_out = []
    for cx, wm, fit in zip(complexes, wms, fits):
        if fit is None:
            continue
        if cx.index in anchor_idx:
            kin_flag = 0
        else:
            pinned = None
            if fit.get('broad'):
                # keep the accepted broad component; pin only the narrow kinematics
                pinned = _fit_broad_pinned(wm, fit, dv_g, sv_g, cfg)
            if pinned is None:
                if fit.get('broad'):
                    wm = _WindowModel(cx, wave, flam, err,
                                      valid & ~(foreign & ~per_cx_support[cx.index]),
                                      lo, hi, cfg, r_of)
                fit = _fit_complex(wm, dv_max, cfg, fixed_kin=(dv_g, sv_g))
            else:
                fit = pinned
            fit['kin_err'] = (dv_g_err, sv_g_err)
            kin_flag = pass2_flag
        if cfg.scale_errors_by_chi2 and fit['dof'] > 0 and fit['chi2'] > fit['dof']:
            fit['cov'] = fit['cov'] * (fit['chi2'] / fit['dof'])
        recs = _line_records(cx, wm, fit, z, cfg, kin_flag)
        lines.update(recs)
        sl = slice(cx.lo_idx, cx.hi_idx)
        m_win, c_win = wm.evaluate(fit['p'], wave[sl], lo[sl], hi[sl])
        model[sl] = m_win
        cont[sl] = c_win
        cx_out.append(dict(index=cx.index, lines=cx.names, lo_idx=cx.lo_idx, hi_idx=cx.hi_idx,
                           chi2=fit['chi2'], dof=fit['dof'], anchor=cx.index in anchor_idx,
                           broad=bool(fit.get('broad'))))

    kin = dict(source=kin_source, dv=dv_g, dv_err=dv_g_err, sigma_v=sv_g, sigma_v_err=sv_g_err,
               n_anchors=len(anchors))
    summary = _summary(z, kin, cx_out, lines, cfg)
    return dict(lines=lines, summary=summary, model=(model, cont), complexes=cx_out)


def _weighted_mean(vals, errs, snrs):
    vals = np.asarray(vals, dtype=float)
    errs = np.asarray(errs, dtype=float)
    snrs = np.asarray(snrs, dtype=float)
    good = np.isfinite(errs) & (errs > 0)
    if np.any(good):
        w = 1.0 / errs[good] ** 2
        return float(np.sum(w * vals[good]) / np.sum(w)), float(1.0 / math.sqrt(np.sum(w)))
    w = snrs ** 2
    return float(np.sum(w * vals) / np.sum(w)), float('nan')


def _summary(z, kin, complexes, lines, cfg: LineFitConfig):
    narrow = [r for r in lines.values() if r['component'] == 'narrow' and np.isfinite(r.get('flux', np.nan))]
    detected = [r for r in narrow if np.isfinite(r['snr']) and r['snr'] >= cfg.detect_snr]
    chi2 = float(sum(c['chi2'] for c in complexes))
    dof = int(sum(c['dof'] for c in complexes))
    s = dict(z_used=float(z), n_lines=len(narrow), n_detected=len(detected),
             n_complexes=len(complexes), chi2=chi2, dof=dof,
             n_broad=sum(1 for r in lines.values() if r['component'] == 'broad'),
             n_doublets=sum(1 for r in lines.values()
                            if r['component'] == 'doublet' and np.isfinite(r.get('flux', np.nan))))
    if kin is None:
        s.update(z_fit=float('nan'), z_fit_err=float('nan'), dv=float('nan'), dv_err=float('nan'),
                 sigma_v=float('nan'), sigma_v_err=float('nan'), kin_source='none', n_anchors=0)
    else:
        z_fit = (1.0 + z) * (1.0 + kin['dv'] / C_KMS) - 1.0
        z_fit_err = (1.0 + z) * kin['dv_err'] / C_KMS if np.isfinite(kin['dv_err']) else float('nan')
        s.update(z_fit=float(z_fit), z_fit_err=float(z_fit_err), dv=kin['dv'], dv_err=kin['dv_err'],
                 sigma_v=kin['sigma_v'], sigma_v_err=kin['sigma_v_err'],
                 kin_source=kin['source'], n_anchors=kin['n_anchors'])
    return s


def decode_flags(flags: int) -> list[str]:
    return [name for bit, name in FLAG_NAMES.items() if flags & bit]
