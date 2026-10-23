"""
Same-slitlet neighbours, found from the data (prototype, extraction v2).

The slitlet is fixed on the detector and the sky moves one shutter between nods. A
second source at a fixed sky offset Delta (slit units) from the target sits at slit
position ``planned + Delta`` in every exposure's own slit frame, so it is visible in
the nods where that position falls in an open shutter. Nod subtraction then puts a
negative copy of it on other nods' differences, and when that copy lands on the
target's rows it is subtracted from the target. Trace maps cannot see this (it is
the same slitlet); the data can.

The scan, run after the shape chain on the same pixels:

1. the target's model at its chosen shape, with its spectrum solved on the native
   grid, is subtracted (so the target's own lines and breaks cannot leak into the
   neighbour); small errors of the target's shape stay free: the change of the
   target model with its centre and with its width, each with a binned spectrum,
   are fitted alongside every neighbour (and in the reference fit without one), so
   a mismatch of the target's wings is not mistaken for a neighbour next to it;
2. a neighbour at Delta is modelled through the same nod bookkeeping as the target
   (positive in the nods where it is in an open shutter, ``-1/n`` copies through the
   partners), with a Gaussian galaxy of the prior median width, and a spectrum that
   is constant within ``nbins`` bins of equal log wavelength (linear amplitudes);
3. Delta runs over every position an open shutter of a science exposure covers,
   except within 0.5 slit units of the target (there a second component is
   degenerate with the target's width; the shape fit's bound flags handle it);
4. the best chi2 drop with a net positive neighbour is the detection statistic. The
   best drop with a net *negative* amplitude is recorded too: a real source is
   never negative, so its distribution over many sources is a built-in null.

For a detected neighbour the light each partner frame puts on each science nod's
main-shutter rows is computed, as a fraction of the target's own light there (or
three times the noise of those rows, if larger: a faint target); a partner above
``drop_frac`` is "dirty" for that nod and the proposal drops it
(A - (B + C)/2 -> A - C). Two-nod sets, and nods whose partners are all dirty, are
reported as unresolved (no proposal: excluding a nod stays a hand decision).

Proposals are relative to the default partners (every other nod of the background
group), whatever override is in force, so a rerun after applying them proposes the
same thing.
"""

from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

INNER = 0.5          # |Delta| below this is not scanned (inside the target's own shutter)


@dataclass
class ScanResult:
    detected: bool = False
    delta: float = np.nan
    dchi2: float = 0.0
    dchi2_null: float = 0.0          # best chi2 drop with a net negative neighbour
    snr: float = 0.0
    curve: tuple = field(default=(), repr=False)     # (deltas, dchi2 positive, dchi2 negative)
    proposals: dict = field(default_factory=dict)   # root -> {nod (str): [nods]}
    unresolved: list = field(default_factory=list)  # 'root:nod'
    dirty: dict = field(default_factory=dict)       # 'root:nod' -> {partner nod: fraction}
    error: str = ''
    suppressed: str = ''             # why proposals were withheld (e.g. other-slitlet light dominates)

    def suppress(self, why):
        """Keep the detection, withhold the partner proposals."""
        if self.proposals or self.unresolved:
            self.suppressed = why
            self.proposals, self.unresolved = {}, []

    def header_cards(self):
        drp = ';'.join(f'{r[-5:]}:{n}>{",".join(str(x) for x in v)}'
                       for r, d in sorted(self.proposals.items()) for n, v in sorted(d.items()))
        return {
            'PFNBR': (int(self.detected), 'Same-slitlet neighbour detected (nod scan)'),
            'PFNBRDEL': (float(self.delta) if np.isfinite(self.delta) else -999.0, 'Neighbour offset from target [slit units]'),
            'PFNBRSIG': (float(self.dchi2), 'Neighbour chi2 drop (best, positive)'),
            'PFNBRNUL': (float(self.dchi2_null), 'Best chi2 drop, negative amplitude (null)'),
            'PFNBRDRP': (drp[:68], 'Proposed partners root:nod>nods'),
            'PFNBRUNS': (len(self.unresolved), 'Contaminated nods with no clean partner'),
            'PFNBRSUP': (self.suppressed[:68], 'Proposals withheld because'),
        }


def _nod(e):
    return int(e.name.split('_')[2])


def _root(e):
    return '_'.join(e.name.split('_')[:2])


def delta_grid(exps, c, step=0.05):
    """Offsets where a neighbour would be in an open shutter of some science exposure."""
    lo, hi = np.inf, -np.inf
    for e in exps:
        if not e.use_data or not e.has_source:
            continue
        ks = np.array(e.open_k)
        lo = min(lo, ks.min() * MSA_SLIT_PITCH - PF.OPEN_HALF - c)
        hi = max(hi, ks.max() * MSA_SLIT_PITCH + PF.OPEN_HALF - c)
    if not np.isfinite(lo):
        return np.array([])
    g = np.arange(np.floor(lo / step) * step, hi + step / 2, step)
    return g[np.abs(g) >= INNER]


def _bins(lam_grid, nbins):
    edges = np.exp(np.linspace(np.log(lam_grid[0]), np.log(lam_grid[-1]) * (1 + 1e-9), nbins + 1))
    idx = np.clip(np.searchsorted(edges, lam_grid, side='right') - 1, 0, nbins - 1)
    B = np.zeros((lam_grid.size, nbins))
    B[np.arange(lam_grid.size), idx] = 1.0
    return B


def _neighbour_p(c, delta, width):
    return np.array([c + delta, 0.0, np.log(max(width, PF.W_FLOOR)), 0.0])


def scan(R, nbins=16, width=None, step=0.05, refine=True):
    """Delta scan on the residuals of a fitted target (``FitResult`` with its model).
    Returns (deltas, dchi2_pos, dchi2_neg, amplitudes at each Delta)."""
    M = R.model
    p, eoff = PF._qvec(R)
    use = R.use if getattr(R, 'use', None) is not None else np.ones(M.npix, bool)
    saved = M.use
    M.use = use
    try:
        f, _, _, A = M.solve(p, eoff)
    finally:
        M.use = saved
    w = np.where(use, 1.0 / M.pix['v'], 0.0)
    r = M.pix['d'] - A @ f
    Bm = _bins(M.lam_grid, nbins)
    width = M.priors.width_median if width is None else width
    c = M.c_prior
    # nuisance: the target model's change with its centre and its (log) width, per bin
    fb = np.nan_to_num(f)[:, None] * Bm
    nuis = []
    for i, h in ((0, 0.02), (2, 0.1)):
        dp = np.zeros(4)
        dp[i] = h
        Ap, Am = M.design(p + dp, eoff), M.design(p - dp, eoff)
        nuis.append(np.asarray((Ap - Am) @ fb) / (2 * h))
    Nu = np.hstack(nuis)
    Nu = Nu[:, np.abs(Nu).sum(axis=0) > 0]

    def _solve(X):
        N = X.T @ (X * w[:, None])
        b = X.T @ (w * r)
        dg = np.diag(N)
        ok = dg > 1e-9 * max(dg.max(), 1e-300)
        Ni = np.linalg.pinv(N[np.ix_(ok, ok)])
        al = Ni @ b[ok]
        return float(al @ b[ok]), al, Ni, ok

    d0 = _solve(Nu)[0] if Nu.shape[1] else 0.0

    def at(delta):
        T = np.asarray(M.design(_neighbour_p(c, delta, width), np.zeros(len(M.exps))) @ Bm)
        live = np.abs(T).sum(axis=0) > 0
        a = np.zeros(nbins)
        if not live.any():
            return 0.0, 0.0, a
        Tl = T[:, live]
        nb = Tl.shape[1]
        d, al, Ni, ok = _solve(np.hstack([Tl, Nu]))
        okn = ok[:nb]
        idx = np.cumsum(ok) - 1                       # positions of the kept columns in al
        an = np.where(okn, al[idx[:nb]], 0.0)
        sn = np.where(okn, np.sqrt(np.maximum(np.diag(Ni)[idx[:nb]], 1e-300)), np.inf)
        z = float(np.sum(an / sn))
        full = np.zeros(nb)
        full[:] = an
        a[live] = full
        return max(d - d0, 0.0), z, a

    grid = delta_grid(M.exps, c, step=2 * step if refine else step)
    res = [at(dd) for dd in grid]
    if refine and grid.size:
        dpos = np.array([d if z > 0 else 0.0 for d, z, _ in res])
        dneg = np.array([d if z <= 0 else 0.0 for d, z, _ in res])
        extra = set()
        for arr in (dpos, dneg):
            if arr.max() > 0:
                i = int(np.argmax(arr))
                for dd in (grid[i] - step, grid[i] + step):
                    if abs(dd) >= INNER:
                        extra.add(round(float(dd), 4))
        extra = sorted(x for x in extra if not np.any(np.isclose(grid, x)))
        if extra:
            res += [at(dd) for dd in extra]
            grid = np.concatenate([grid, extra])
        o = np.argsort(grid)
        grid, res = grid[o], [res[i] for i in o]
    dpos = np.array([d if z > 0 else 0.0 for d, z, _ in res])
    dneg = np.array([d if z <= 0 else 0.0 for d, z, _ in res])
    amps = np.array([a for _, _, a in res]) if res else np.zeros((0, nbins))
    return grid, dpos, dneg, amps


def partner_fractions(R, delta, amps, nbins=16, width=None):
    """For each science nod k and every other nod j of its background group: the
    neighbour light frame j puts on nod k's main-shutter pixels, as a fraction of the
    target's own light there. Returns {k: {j: fraction}}."""
    M = R.model
    Bm = _bins(M.lam_grid, nbins)
    width = M.priors.width_median if width is None else width
    pn = _neighbour_p(M.c_prior, delta, width)
    Rn = PF.FitResult(params=dict(centre=pn[0], drift=0.0, width=float(np.exp(pn[2])), width_change=0.0,
                                  pivot=M.pivot, centre_prior=M.c_prior),
                      eoff=np.zeros(len(M.exps)), lam=M.lam_grid, flux=np.clip(Bm @ amps, 0, None),
                      flux_err=np.zeros(M.lam_grid.size), chi2=0.0, npix=0, nfree=0, chromatic=True, model=M)
    out = {}
    for k, e in enumerate(M.exps):
        if not e.use_data or not e.has_source:
            continue
        rows = np.isfinite(e.y) & (np.abs(e.y - M.c_prior) < PF.OPEN_HALF) & e.ok
        tgt = np.nansum(PF.frame_image(R, k)[rows])
        # a faint or undetected target: compare with the noise of its rows instead
        noise = np.sqrt(np.nansum(M.diff_variance(k)[rows]))
        tgt = max(tgt, 3.0 * noise) if np.isfinite(tgt) else 3.0 * noise
        if not tgt > 0:
            continue
        group = [j for j, x in enumerate(M.exps) if x.group == e.group and j != k]
        out[k] = {}
        for j in group:
            nb = np.nansum(PF.frame_image(Rn, j)[rows])
            out[k][j] = float(nb / tgt)
    return out


def propose(R, delta, amps, drop_frac=0.2, nbins=16, width=None):
    """Partner proposals from the fractions (see module doc).
    Returns (proposals {root: {nod: [nods]}}, unresolved ['root:nod'], dirty)."""
    M = R.model
    fr = partner_fractions(R, delta, amps, nbins=nbins, width=width)
    props, unres, dirty = {}, [], {}
    for k, d in fr.items():
        e = M.exps[k]
        bad = [j for j, v in d.items() if v > drop_frac]
        if not bad:
            continue
        key = f'{_root(e)}:{_nod(e)}'
        dirty[key] = {_nod(M.exps[j]): round(v, 3) for j, v in d.items()}
        clean = [j for j in d if j not in bad]
        if len(d) < 2 or not clean:
            unres.append(key)
            continue
        props.setdefault(_root(e), {})[str(_nod(e))] = sorted(_nod(M.exps[j]) for j in clean)
    return props, unres, dirty


def run(R, nbins=16, width=None, dchi2_min=60.0, min_snr=5.0, drop_frac=0.2, null_ratio=1.5):
    """Scan, detect, propose. Never raises (a failure is recorded in ``error``).

    Detected when the best positive chi2 drop exceeds ``dchi2_min``, its S/N
    ``min_snr``, and ``null_ratio`` times the best drop a *negative* neighbour gives
    on the same data (structured residuals of a bright or extended target produce
    both signs; a separate source produces mostly the positive one). The null
    criterion needs three or more nods per group."""
    out = ScanResult()
    try:
        grid, dpos, dneg, amps = scan(R, nbins=nbins, width=width)
        out.curve = (grid, dpos, dneg)
        if grid.size == 0:
            return out
        i = int(np.argmax(dpos))
        out.dchi2, out.delta = float(dpos[i]), float(grid[i])
        out.dchi2_null = float(dneg.max())
        out.snr = float(np.sqrt(max(out.dchi2, 0.0)))
        # with two nods a positive source and a negative one a pitch away give the same
        # differences (D2 = -D1), so the negative null says nothing there
        sizes = [sum(1 for x in R.model.exps if x.group == g) for g in {x.group for x in R.model.exps}]
        null_ok = out.dchi2 > null_ratio * out.dchi2_null if min(sizes) >= 3 else True
        out.detected = bool(out.dchi2 > dchi2_min and out.snr >= min_snr and null_ok)
        if out.detected:
            out.proposals, out.unresolved, out.dirty = propose(R, out.delta, amps[i], drop_frac=drop_frac,
                                                               nbins=nbins, width=width)
    except Exception as e:                       # noqa: BLE001 - a failed scan must not fail the product
        out.error = f'{type(e).__name__}: {e}'
    return out
