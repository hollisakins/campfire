"""
Light through failed-open shutters on the master-background spectrum (user decision
2026-11-02).

A failed-open shutter is stuck open, so the sky behind it reaches the detector in every
exposure, on the detector rows of every slitlet in the same MSA rows. The master
background paints its background (master background 1.6); what is left is the light of
whatever object is behind it. Nothing in the catalog predicts that light (no source, so
no pass 1), and it is other-source light like any neighbor's: it goes into
``CONTAM2D`` / ``CONTERR2D`` of the master-background product (``mbkg_extraction``).

The model (``solve``), per exposure and per failed-open shutter whose trace reaches the
source's own trace (stored trace map, kind ``failed_open``, ``TO.nod_contamination``):
a spectrum along detector columns, linear between knots every ``knot`` columns (the
pass-1 smoothing length of the disperser, so its spectrum is as smooth as a neighbor's
pass 1), times a flat profile over the shutter's pixels. It is solved together with
the source at the shape the nodded chain chose, on the frames with the other sources'
predicted light removed, so the source's own light is not taken for the shutter's. Each
exposure has its own, since the sky behind the shutter moves with the nod.

Its uncertainty is the solve's statistical one plus ``CT.DPRED_FLOOR`` of the light (the
flat profile is an assumption: an object behind the shutter need not fill it).
"""

from dataclasses import dataclass, field

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

from campfire_pipeline.nirspec import profile_contam as CT
from campfire_pipeline.nirspec import trace_overlap as TO

MIN_PIXELS = 20            # fitted pixels a shutter needs in an exposure to be solved
CLIP, N_CLIP = 5.0, 2      # outlier rejection of the joint solve, as profile_fit.fit_fixed


@dataclass
class Light:
    pred: list                 # per exposure: failed-open light (diff units, the exposure's frame)
    dpred: list                # its uncertainty
    shutters: dict = field(default_factory=dict)   # (exposure, name) -> max |light| / noise on the main shutter


def footprints(exps, sid, maps):
    """{(exposure index, shutter name): (iy, ix)} of every failed-open shutter on the
    source's own trace, per exposure (single frames: no partners)."""
    out = {}
    for g in sorted({e.group for e in exps}):
        ks = [k for k in range(len(exps)) if exps[k].group == g]
        try:
            mlist = [maps[CT.exposure_key(exps[k].name)] for k in ks]
        except KeyError:
            continue
        e0 = exps[ks[0]]
        ny, nx = e0.y.shape
        region = (e0.origin[1] - 1, e0.origin[0] - 1, nx, ny)
        for r in TO.nod_contamination(mlist, int(sid), region, [[] for _ in ks]):
            if r.kind == 'failed_open':
                out[(ks[r.exposure], r.name)] = (r.iy, r.ix)
    return out


def _hats(x, x0, knot):
    """Knot index j and weight t of columns x on knots x0 + j * knot (light =
    (1 - t) a_j + t a_(j+1))."""
    u = (np.asarray(x, float) - x0) / knot
    j = np.floor(u).astype(int)
    return j, u - j


def solve(M, p, exps, sid, maps, knot, minus=None):
    """Failed-open light on the source's single frames (module doc).

    M : the source's ``profile_fit.ProfileModel`` on its single frames (its outlier mask
        is not used: the joint solve clips on its own residuals); p : the shape vector
        it is solved at.
    minus : per exposure, light removed from the data first (other sources' predicted
        light, diff units), or None.
    Returns ``Light``; None when no failed-open shutter reaches the source's trace."""
    fps = footprints(exps, sid, maps)
    if not fps:
        return None
    row_of = {}
    start = 0
    for k, m in M.pix_masks:
        idx = np.full(m.shape, -1, np.int64)
        idx[m] = start + np.arange(int(m.sum()))
        row_of[k] = idx
        start += int(m.sum())
    d = M.pix['d'].copy()
    if minus is not None:
        for k, m in M.pix_masks:
            sub = np.nan_to_num(np.asarray(minus[k], float)[m])
            d[row_of[k][m]] -= sub
    ngrid = M.lam_grid.size
    rows, cols, vals, blocks = [], [], [], []
    ncol = ngrid
    for (k, name), (iy, ix) in sorted(fps.items()):
        if k not in row_of:
            continue
        r = row_of[k][iy, ix]
        fit = r >= 0
        if fit.sum() < MIN_PIXELS:
            continue
        x0 = float(ix.min())
        j, t = _hats(ix, x0, knot)
        nk = int(j.max()) + 2
        rows += [r[fit], r[fit]]
        cols += [ncol + j[fit], ncol + j[fit] + 1]
        vals += [1.0 - t[fit], t[fit]]
        blocks.append((k, name, iy, ix, j, t, ncol, nk))
        ncol += nk
    if not blocks:
        return None
    A = M.design(p, np.zeros(len(M.exps)))
    E = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                      shape=(M.npix, ncol))
    B = sp.hstack([A, E[:, ngrid:]]).tocsr()
    # its own outlier rejection: the source-only fit's (M.use) clipped the shutter's light
    use = np.ones(M.npix, bool)
    for it in range(N_CLIP + 1):
        wgt = np.where(use, 1.0 / M.pix['v'], 0.0)
        BtW = B.T.multiply(wgt).tocsr()
        N = (BtW @ B).tocsc()
        b = BtW @ d
        diag = N.diagonal()
        good = diag > 1e-3 * np.median(diag[diag > 0])
        gi = np.flatnonzero(good)
        Ng = N[gi][:, gi] + sp.identity(gi.size, format='csc') * 1e-9 * np.median(diag[good])
        lu = splu(Ng)
        f = np.zeros(ncol)
        f[good] = lu.solve(b[good])
        if it == N_CLIP:
            break
        new = np.abs(d - B @ f) < CLIP * np.sqrt(M.pix['v'])
        if np.array_equal(new, use):
            break
        use = new
    # covariance of the shutters' knots only: columns of the inverse at their positions
    pos = np.full(ncol, -1)
    pos[gi] = np.arange(gi.size)
    want = np.flatnonzero(good & (np.arange(ncol) >= ngrid))
    unit = np.zeros((gi.size, want.size))
    unit[pos[want], np.arange(want.size)] = 1.0
    inv_cols = lu.solve(unit) if want.size else np.zeros((gi.size, 0))
    cov = np.zeros((ncol, ncol))
    cov[np.ix_(want, want)] = inv_cols[pos[want]]

    pred = [np.zeros(e.y.shape) for e in exps]
    dpred = [np.zeros(e.y.shape) for e in exps]
    c_plan = M.c_prior
    out = Light(pred=pred, dpred=dpred)
    for k, name, iy, ix, j, t, c0, nk in blocks:
        a = np.where(good[c0:c0 + nk], f[c0:c0 + nk], 0.0)
        C = cov[c0:c0 + nk, c0:c0 + nk]
        light = (1.0 - t) * a[j] + t * a[j + 1]
        var = (1.0 - t) ** 2 * C[j, j] + t ** 2 * C[j + 1, j + 1] + 2 * t * (1.0 - t) * C[j, j + 1]
        err = np.sqrt(np.maximum(var, 0.0)) + CT.DPRED_FLOOR * np.abs(light)
        np.add.at(pred[k], (iy, ix), light)
        np.add.at(dpred[k], (iy, ix), err)
        e = exps[k]
        ms = CT.main_shutter(e, c_plan) & e.ok
        sig = CT.diff_sigma(exps, k)
        on = ms[iy, ix]
        with np.errstate(invalid='ignore', divide='ignore'):
            s = np.abs(light[on]) / sig[iy[on], ix[on]]
        s = s[np.isfinite(s)]
        out.shutters[(k, name)] = float(s.max()) if s.size else 0.0
    return out
