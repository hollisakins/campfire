"""
The first pass by a joint linear solve of local neighbourhoods (extraction v2).

On the gratings a detector pixel often carries light of two or more sources at different
wavelengths. The masked first pass fitted each source with every pixel another source's
shutter lights left out; on G395M that removed most of most sources' own light, and
exactly the wavelengths whose light the contamination prediction needs
(docs/design-nirspec-pipeline-v2.md, section 3.9). Here each source is solved together
with its neighbours: every pixel's model is the sum of the light of every source that
lands on it, each through its fixed shape, so the fit is linear in the spectra and
nothing is masked. Each source also has pixels of its own, which pin it down.

Per disperser (``run``):

1. coupling from the trace maps (``coupling``): which sources' source shutters light
   which other sources' cutouts, from the canonical headers and the maps alone;
2. per source a neighbourhood (``neighbourhood``): the source, its neighbours and theirs
   (two steps, at most ``cap`` sources, strongest first);
3. per member, once per worker (``MemberCache``): the single exposures minus the master
   sky (``first_pass.load_source_sky``), the shape of the chain (the PRISM shape of the
   same source when the PRISM product took its own fit, else its own fit above the
   grating gate, else the default shape; ``shape_for``), the per-exposure pointing
   offsets at that shape, and its frames per unit spectrum (``profile_joint``);
4. one weighted least-squares fit of every member's spectrum at full resolution,
   clipped at ``clip`` sigma twice (``solve_target``); only the target's spectrum is
   kept, each source being the target of its own neighbourhood;
5. the target's errors from its columns of the inverse normal matrix, and the
   wavelengths it cannot be told apart from another member (correlation below
   ``-degenerate``), ``nosep``;
6. a pass-1 file in the format the contamination prediction reads (``why = 'joint'``),
   with the not-separable mask.

The solve covers each member's whole trace, not only its calibrated range (user,
2026-10-31): past the wavelengths a member's fit pixels have, its light is a second set of
unknowns per end in count-rate units (``profile_extend`` geometry), fitted on the rate
files minus the painted master background (``MBKG2D``) together with everything else
(``design_full``). A member's light past its cutout that lands on a neighbour's trace is
then separated like the rest instead of being masked out.
"""

import os
from collections import OrderedDict

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

from campfire_pipeline.nirspec import profile_contam as CT
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_joint as PJ

JOINT_PASS_VERSION = '2'          # 2: the light past the cutouts is among the joint unknowns
SPEC_SRC = 'joint'
CHUNK = 512


# ---------------------------------------------------------------------------
# Coupling and neighbourhoods
# ---------------------------------------------------------------------------
def _cutout_region(path):
    """(x0, y0, nx, ny) of a canonical's cutout on the detector (0-based corner)."""
    from astropy.io import fits
    h = fits.getheader(path, ('SCI', 1))
    return int(h['SLTSTRT1']) - 1, int(h['SLTSTRT2']) - 1, int(h['NAXIS1']), int(h['NAXIS2'])


def coupling(files_by_sid, maps):
    """{sid: {other sid: pixels}}: how many pixels of each source's cutouts (every
    exposure) another source's source shutter lights, from the trace maps.

    files_by_sid : {sid: [canonical paths]}; maps : {exposure key: TraceMap}."""
    out = {}
    for sid, paths in files_by_sid.items():
        n = {}
        for p in paths:
            m = maps.get(CT.exposure_key(p))
            if m is None:
                continue
            iy, ix, idx, flg = m.traces_in(*_cutout_region(p))
            src = m.source_shutter(flg)
            for r in np.unique(idx[src]):
                rec = m.slits[int(r)]
                if rec['kind'] != 'slitlet':
                    continue
                other = int(rec['source_id'])
                if other == int(sid) or other <= 0:
                    continue
                n[other] = n.get(other, 0) + int(np.sum(src & (idx == r)))
        out[int(sid)] = n
    return out


def neighbourhood(coupled, sid, depth=2, cap=25, min_px=100):
    """The target first, then its neighbours and theirs (``depth`` steps), strongest
    coupling first, at most ``cap`` sources. A source couples to another when either
    lights at least ``min_px`` pixels of the other's cutouts."""
    sym = {}
    for a, row in coupled.items():
        for b, n in row.items():
            if n >= min_px:
                sym.setdefault(a, {})[b] = max(sym.get(a, {}).get(b, 0), n)
                sym.setdefault(b, {})[a] = max(sym.get(b, {}).get(a, 0), n)
    chosen, frontier = [int(sid)], [int(sid)]
    for _ in range(depth):
        nxt = []
        for s in frontier:
            for t, _n in sorted(sym.get(s, {}).items(), key=lambda kv: (-kv[1], kv[0])):
                if t not in chosen and t in coupled:
                    chosen.append(t)
                    nxt.append(t)
                    if len(chosen) >= cap:
                        return chosen
        frontier = nxt
    return chosen


def target_order(coupled, depth=2, cap=25, window=60):
    """Sources ordered so that a worker's member cache is reused: the next target is the
    unvisited source whose neighbourhood shares the most members with the last ``window``
    members used (ties by source id; a fresh start at the lowest id when none shares one).
    A breadth-first walk does not do this: its last sources lie on a ring far apart, and
    on ember_cosmos_p1 G395M they rebuilt 23 of 25 members per target."""
    nbh = {s: neighbourhood(coupled, s, depth=depth, cap=cap) for s in coupled}
    inv = {}
    for s, ids in nbh.items():
        for t in ids:
            inv.setdefault(t, []).append(s)
    left = set(coupled)
    score = {}                                  # unvisited source -> members it shares with recent
    recent = OrderedDict()
    order = []

    def _bump(m, d):
        for s in inv.get(m, ()):
            if s in left:
                score[s] = score.get(s, 0) + d
                if score[s] <= 0:
                    del score[s]

    while left:
        nxt = (min(score, key=lambda s: (-score[s], s)) if score else min(left))
        order.append(nxt)
        left.discard(nxt)
        score.pop(nxt, None)
        for t in nbh[nxt]:
            if t in recent:
                recent.move_to_end(t)
                continue
            recent[t] = None
            _bump(t, 1)
            if len(recent) > window:
                old, _ = recent.popitem(last=False)
                _bump(old, -1)
    return order


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
def shape_for(exps, M, cfg, is_prism, prism_shape):
    """(shape dict, source label) of the chain: the PRISM shape of the same source when
    the PRISM product took its own fit (gratings), else the source's own fit above the
    gate, else the default shape."""
    if not is_prism and prism_shape is not None and prism_shape.shape == 'own':
        return PF.map_shape(prism_shape.params, M.pivot), 'prism'
    snr, _ = PF.detection_snr(M, scale=cfg.gate_noise)
    if snr >= (cfg.gate_prism if is_prism else cfg.gate_grating):
        try:
            Ro = PF.fit(exps, model=M)
            if not Ro.bound_bits and PF.planned_position(exps, cfg.srcypos_tolerance)[1] != 2:
                return dict(centre_offset=Ro.params['centre'] - Ro.params['centre_prior'], drift=Ro.params['drift'],
                            width=Ro.params['width'], width_change=Ro.params['width_change']), 'own'
        except Exception:                                    # noqa: BLE001 - the default shape then
            pass
    return cfg.default_shape(), 'default'


class MemberCache:
    """``profile_joint.Member`` objects of the sources of one disperser, built on first
    use and kept for the most recent ``size`` sources (a worker processes neighbourhoods
    in coupling order, ``target_order``)."""

    def __init__(self, tasks, workspace, sky_for, stage_config, is_prism, size=60):
        from campfire_pipeline.nirspec import profile_chain as PC
        self.tasks = tasks                      # sid -> dict(groups, overrides, science, prism_product)
        self.workspace = workspace
        self.sky_for = sky_for
        self.cfg = PC.ChainConfig.from_stage_config(stage_config)
        self.is_prism = is_prism
        self.size = size
        self._m = OrderedDict()
        self._shape = {}                        # sid -> (shape, label, eoff): kept when a member is evicted
        self._failed = {}                       # sid -> the ProfileFitFallback its build raised
        self._prism = {}                        # sid -> PRISM shape string (signature)

    def get(self, sid):
        """The member of ``sid``; raises the ``ProfileFitFallback`` its build raised (once
        per worker)."""
        sid = int(sid)
        if sid in self._failed:
            raise self._failed[sid]
        if sid in self._m:
            self._m.move_to_end(sid)
            return self._m[sid]
        try:
            m = self._build(sid)
        except PF.ProfileFitFallback as e:
            self._failed[sid] = e
            raise
        self._m[sid] = m
        while len(self._m) > self.size:
            self._m.popitem(last=False)
        return m

    def prism_sig(self, sid):
        """The PRISM shape a grating member takes, as a string: a PRISM rerun that changes
        it invalidates the pass 1 of every neighbourhood it is in."""
        sid = int(sid)
        if sid not in self._prism:
            pp = self.tasks[sid].get('prism_product')
            ps = CT.read_prism_shape(self.workspace, pp) if (not self.is_prism and pp) else None
            self._prism[sid] = 'none' if ps is None else (
                f"{ps.shape}:" + ','.join(f'{k}={ps.params[k]:.4f}' for k in sorted(ps.params)))
        return self._prism[sid]

    def _build(self, sid):
        from campfire_pipeline.nirspec import first_pass as FP
        t = self.tasks[sid]
        exps = FP.load_source_sky(t['groups'], self.sky_for, bkg_overrides=t['overrides'],
                                  science=set(t['science']))
        cfg = self.cfg
        M = PF.ProfileModel(exps, chromatic=True, priors=cfg.priors, bounds=cfg.bounds)
        if sid not in self._shape:
            prism = (CT.read_prism_shape(self.workspace, t['prism_product'])
                     if (not self.is_prism and t.get('prism_product')) else None)
            shape, src = shape_for(exps, M, cfg, self.is_prism, prism)
            R = PF.fit_fixed(model=M, shape=shape, fit_eoff=True)
            self._shape[sid] = (shape, src, np.asarray(R.eoff, float))
        shape, src, eoff = self._shape[sid]
        m = PJ.Member(sid=sid, product=t['product'], exps=exps, model=M,
                      p=PF.shape_vector(shape, M.c_prior, M.priors.width_median),
                      eoff=eoff.copy(), shape_src=src, workspace=self.workspace)
        m.cal = {j: CT.calibration(e, self.workspace) for j, e in enumerate(exps)}
        PJ.basis_frames(m)
        ext_setup(m, self.workspace)
        return m


# ---------------------------------------------------------------------------
# The light past the cutouts
# ---------------------------------------------------------------------------
def _rate_minus_sky(workspace, key, rows, cols):
    """(data, variance, bad) of one single frame on a detector band, count-rate units:
    the rate minus the painted master background. Bad: DO_NOT_USE in the rate, a
    pixel the master background did not paint, or no value."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import profile_extend as PE
    rp, mp = os.path.join(workspace, f'{key}_rate.fits'), os.path.join(workspace, f'{key}_mbkg.fits')
    if not (os.path.exists(rp) and os.path.exists(mp)):
        return None
    sci, var, bad = PE._rate(rp, rows, cols)
    sl = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
    with fits.open(mp) as h:
        if 'MBKG2D' not in h:
            return None
        sky = np.asarray(h['MBKG2D'].data[sl], float)
        if 'MBKG2DDQ' in h:
            bad = bad | (np.asarray(h['MBKG2DDQ'].data[sl]) != 0)
    d = sci - sky
    bad = bad | ~np.isfinite(d) | ~np.isfinite(var) | (var <= 0)
    return d, var, bad


def ext_setup(m, workspace):
    """A member's light past the wavelengths its fit pixels have (``m.ext``): one grid
    per end (``profile_extend.end_rows``), its frames per unit count-rate spectrum on the
    detector there (sparse, single frames), and the data there, the rate minus the
    painted sky of each science frame, on the pixels its own trace lights. Nothing past
    the cutout (``n = 0``) when it has no such pixels."""
    from campfire_pipeline.nirspec import profile_extend as PE
    M, exps = m.model, m.exps
    lam_in = np.asarray(M.pix['lam'], float)
    lo, hi = float(np.nanmin(lam_in)), float(np.nanmax(lam_in))
    m.ext = dict(lo=lo, hi=hi, grids=[np.zeros(0), np.zeros(0)], n=0, model={}, rows=None)
    _, per_exp = PE.past_geometry(M, m.p, m.eoff, lo, hi)
    if not per_exp:
        return m.ext
    order = [k for k, e in enumerate(exps) if e.use_data] + [k for k, e in enumerate(exps) if not e.use_data]
    grids = [PE.native_grid(r) for r in PE.end_rows(per_exp, exps, order, lo, hi)]
    grids = [g if g is not None else np.zeros(0) for g in grids]
    m.ext['grids'], m.ext['n'] = grids, int(sum(g.size for g in grids))
    if not m.ext['n']:
        return m.ext
    rk, rdet, rd, rv = [], [], [], []
    for k, (rows, cols, ys, lam, gimg, past) in per_exp.items():
        on = past & np.isfinite(gimg) & (gimg != 0) & _on_grids(lam, lo, grids)
        if not on.any():
            continue
        iy, ix = np.nonzero(on)
        det = (rows[iy].astype(np.int64) + 1) * 100000 + (cols[ix] + 1)
        o = np.argsort(det)
        key = CT.exposure_key(exps[k].name)
        m.ext['model'][key] = dict(rows=rows, cols=cols, iy=iy[o], ix=ix[o], det=det[o],
                                   lam=lam[on][o], g=gimg[on][o])
        if not exps[k].use_data:
            continue
        data = _rate_minus_sky(workspace, key, rows, cols)
        if data is None:
            continue
        d, v, bad = data
        keep = ~bad[iy, ix]
        rk.append(np.full(int(keep.sum()), k))
        rdet.append(det[keep])
        rd.append(d[iy, ix][keep])
        rv.append(v[iy, ix][keep])
    if rk:
        m.ext['rows'] = dict(k=np.concatenate(rk), det=np.concatenate(rdet), d=np.concatenate(rd),
                             v=np.concatenate(rv))
    return m.ext


def _on_grids(lam, lo, grids):
    """Wavelengths inside the blue grid (below ``lo``) or the red one (above)."""
    gb, gr = grids
    with np.errstate(invalid='ignore'):
        blue = (lam < lo) & (gb.size >= 2) & (lam >= (gb[0] if gb.size else np.inf)) & (lam <= (gb[-1] if gb.size else -np.inf))
        red = (lam >= lo) & (gr.size >= 2) & (lam >= (gr[0] if gr.size else np.inf)) & (lam <= (gr[-1] if gr.size else -np.inf))
    return blue | red


def _ext_cols(m, lam):
    """Columns (relative to the member's first extension unknown) and weights of linear
    interpolation in its end grids at wavelengths ``lam``: (ok, i, t), column i takes
    1 - t and i + 1 takes t."""
    gb, gr = m.ext['grids']
    ok = _on_grids(lam, m.ext['lo'], m.ext['grids'])
    i = np.zeros(lam.size, int)
    t = np.zeros(lam.size)
    for g, base, sel in ((gb, 0, ok & (lam < m.ext['lo'])), (gr, gb.size, ok & (lam >= m.ext['lo']))):
        if not sel.any():
            continue
        j = np.clip(np.searchsorted(g, lam[sel]) - 1, 0, g.size - 2)
        t[sel] = np.clip((lam[sel] - g[j]) / (g[j + 1] - g[j]), 0.0, 1.0)
        i[sel] = base + j
    return ok, i, t


def _lookup(model, det):
    """Positions of detector keys ``det`` in a member's sorted extension pixels: (found, pos)."""
    pos = np.clip(np.searchsorted(model['det'], det), 0, max(model['det'].size - 1, 0))
    found = model['det'].size > 0
    return (model['det'][pos] == det) if found else np.zeros(det.size, bool), pos


def design_full(members):
    """The joint design over each member's whole trace: ``profile_joint.design`` (the
    cutout pixels and the in-range spectra), plus every member's extension unknowns
    (count rate) on those pixels (converted with the receiving pixel's calibration), and
    rows for each member's pixels past its cutout (single frames, count rate) that no
    cutout already holds, with every member's in-range light (converted back with the
    giving member's calibration) and extension light on them.

    Column layout per member: its in-range grid (``col0``), then its blue and red end
    grids (``xcol0``). Single frames only (the first pass; no partners). Returns (A, d,
    v, n_cut) with ``n_cut`` the number of cutout rows."""
    tags = {}
    cut_keys = [PJ._pixel_keys(m, tags)[3] for m in members]
    PJ.owned_pixels(members)
    A0, d0, v0 = PJ.design(members)
    old0 = [c.col0 for c in members]
    col = 0
    newcol = np.zeros(A0.shape[1], int)
    for c, o in zip(members, old0):
        n = c.model.lam_grid.size
        newcol[o:o + n] = col + np.arange(n)
        c.col0, c.xcol0 = col, col + n
        col = c.xcol0 + c.ext['n']
    A0 = A0.tocoo()
    rows, cols, vals = [A0.row], [newcol[A0.col]], [A0.data]

    def add_ext_light(r, det, key, conv_of):
        """Every member's extension light at detector pixels ``det`` (rows ``r``) of frame
        ``key``; ``conv_of(sel)`` converts count rate to the row's units."""
        for c in members:
            mod = c.ext['model'].get(key) if c.ext['n'] else None
            if mod is None:
                continue
            found, pos = _lookup(mod, det)
            if not found.any():
                continue
            conv = conv_of(found)
            ok, i, t = _ext_cols(c, mod['lam'][pos[found]])
            ok &= np.isfinite(conv)
            if not ok.any():
                continue
            rr = r[found][ok]
            val = mod['g'][pos[found]][ok] * conv[ok]
            rows.extend([rr, rr])
            cols.extend([c.xcol0 + i[ok], c.xcol0 + i[ok] + 1])
            vals.extend([val * (1 - t[ok]), val * t[ok]])

    # the members' extension light on the cutout pixels (each member's owned rows)
    row0 = 0
    for s in members:
        own = s.owned
        loc = np.cumsum(own) - 1
        for k in np.unique(s.pix_k[own]):
            sel = own & (s.pix_k == k)
            iy, ix, r = s.pix_iy[sel], s.pix_ix[sel], row0 + loc[sel]
            e = s.model.exps[k]
            det = (iy + int(e.origin[0])).astype(np.int64) * 100000 + (ix + int(e.origin[1]))
            cal = s.calibration(k)[iy, ix]
            add_ext_light(r, det, CT.exposure_key(e.name), lambda f, cal=cal: cal[f])
        row0 += int(own.sum())
    n_cut = row0

    # rows past the cutouts: once per datum, never one a cutout holds
    claimed = np.unique(np.concatenate(cut_keys)) if cut_keys else np.zeros(0, np.int64)
    d, v = [d0], [v0]
    for s in members:
        R = s.ext.get('rows') if s.ext['n'] else None
        if R is None:
            continue
        names = [CT.exposure_key(s.exps[k].name) for k in R['k']]
        tid = np.array([tags.setdefault(nm + '|', len(tags)) for nm in names], np.int64)
        keys = tid * 10 ** 10 + R['det']
        own = ~np.isin(keys, claimed)
        _, first = np.unique(keys, return_index=True)
        once = np.zeros(keys.size, bool)
        once[first] = True
        own &= once
        claimed = np.union1d(claimed, keys[own])
        if not own.any():
            continue
        kk, det = R['k'][own], R['det'][own]
        r_all = row0 + np.arange(int(own.sum()))
        d.append(R['d'][own])
        v.append(R['v'][own])
        dy, dx = np.divmod(det, 100000)                     # 1-based detector row, column
        for k in np.unique(kk):
            sk = kk == k
            key = CT.exposure_key(s.exps[k].name)
            r = r_all[sk]
            # in-range light of every member, from its calibrated units to count rate
            for c in members:
                b = c.basis.get(key)
                if b is None:
                    continue
                cy = dy[sk] - int(b['origin'][0])
                cx = dx[sk] - int(b['origin'][1])
                ny, nx = b['prof'].shape
                ins = (cy >= 0) & (cy < ny) & (cx >= 0) & (cx < nx)
                if not ins.any():
                    continue
                prof = b['prof'][cy[ins], cx[ins]]
                lam = b['plam'][cy[ins], cx[ins]]
                with np.errstate(divide='ignore', invalid='ignore'):
                    conv = 1.0 / b['cal'][cy[ins], cx[ins]]
                g = c.model.lam_grid
                ok = (np.isfinite(prof) & (prof != 0) & np.isfinite(lam) & (lam >= g[0]) & (lam <= g[-1])
                      & np.isfinite(conv))
                if not ok.any():
                    continue
                lam = lam[ok]
                i = np.clip(np.searchsorted(g, lam) - 1, 0, g.size - 2)
                t = np.clip((lam - g[i]) / (g[i + 1] - g[i]), 0.0, 1.0)
                val = conv[ok] * prof[ok]
                rr = r[ins][ok]
                rows.extend([rr, rr])
                cols.extend([c.col0 + i, c.col0 + i + 1])
                vals.extend([val * (1 - t), val * t])
            add_ext_light(r, det[sk], key, lambda f, n=int(sk.sum()): np.ones(int(f.sum())))
        row0 += int(own.sum())
    A = sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(row0, col))
    return A, np.concatenate(d), np.concatenate(v), n_cut


# ---------------------------------------------------------------------------
# The solve
# ---------------------------------------------------------------------------
def _normal(A, d, v, use):
    w = np.where(use, 1.0 / np.where(use, v, 1.0), 0.0)
    AtW = A.T.multiply(w).tocsr()
    return (AtW @ A).tocsc(), AtW @ np.where(use, d, 0.0)


def solve_target(members, clip=5.0, n_clip=2, degenerate=0.9):
    """Joint fit of ``members`` (the target first; ``owned_pixels`` makes it own all its
    pixels) over their whole traces (``design_full``). Returns dict(lam, flux, err,
    nosep, corr, npix, nclip) of the target, NaN where its spectrum is not constrained,
    and ``ext``: its light past its fitted wavelengths, dict(lam, flux, err, nosep) per
    end grid (blue, red), in count-rate units.

    Errors: the target's columns of the inverse normal matrix (marginal, the other
    members free). nosep: wavelengths whose most negative correlation with a point of
    another member's spectrum is below ``-degenerate``; the other member's variance is
    computed only where the bound ``1 / N_jj`` already allows such a correlation."""
    A, d, v, _ = design_full(members)
    blocks = np.concatenate([np.full(m.model.lam_grid.size + m.ext['n'], i) for i, m in enumerate(members)])
    # the in-range and the past-cutout unknowns are in different units (calibrated, count
    # rate): which columns the data constrain is judged within each part
    parts = np.concatenate([np.r_[np.full(m.model.lam_grid.size, 2 * i), np.full(m.ext['n'], 2 * i + 1)]
                            for i, m in enumerate(members)])
    n = A.shape[1]
    use = np.isfinite(d) & np.isfinite(v) & (v > 0)
    for it in range(n_clip + 1):
        N, b = _normal(A, d, v, use)
        diag = N.diagonal()
        good = np.zeros(n, bool)
        for i in np.unique(parts):
            sel = parts == i
            dpos = diag[sel][diag[sel] > 0]
            if dpos.size:
                good[sel] = diag[sel] > 1e-3 * np.median(dpos)
        gi = np.flatnonzero(good)
        # solved for the unknowns scaled to unit diagonal, so the tiny ridge is relative to
        # each column's own constraint (the two parts' diagonals differ by ~20 orders)
        s = 1.0 / np.sqrt(diag[gi])
        S = sp.diags(s)
        Ng = (S @ N[gi][:, gi] @ S + sp.identity(gi.size, format='csc') * 1e-9).tocsc()
        lu = splu(Ng)
        f = np.zeros(n)
        f[gi] = s * lu.solve(s * b[gi])
        if it == n_clip:
            break
        with np.errstate(invalid='ignore', divide='ignore'):
            r = (np.where(use, d, 0.0) - A @ f) / np.sqrt(np.where(use, v, 1.0))
        new = use & (np.abs(r) < clip)
        if np.array_equal(new, use):
            break
        use = new
    tcol = np.flatnonzero(blocks[gi] == 0)              # target columns, in gi indexing
    m0 = members[0].model.lam_grid.size + members[0].ext['n']
    flux = np.full(m0, np.nan)
    err = np.full(m0, np.nan)
    corr = np.full(m0, np.nan)
    partner = np.full(m0, -1)                           # global column of the most negative correlation
    if tcol.size:
        g_n = gi.size
        X = np.zeros((g_n, tcol.size))
        for a in range(0, tcol.size, CHUNK):
            bnd = min(a + CHUNK, tcol.size)
            E = np.zeros((g_n, bnd - a))
            E[tcol[a:bnd], np.arange(bnd - a)] = 1.0
            X[:, a:bnd] = lu.solve(E)
        cii = X[tcol, np.arange(tcol.size)]
        other = blocks[gi] != 0
        njj = np.asarray(Ng.diagonal()).ravel()
        with np.errstate(invalid='ignore', divide='ignore'):
            bound = X / np.sqrt(np.abs(cii)[None, :] / njj[:, None])   # >= |true correlation| in size
        cand = np.flatnonzero(other & np.any(bound < -degenerate, axis=1))
        cjj = np.full(g_n, np.nan)
        for a in range(0, cand.size, CHUNK):
            bnd = min(a + CHUNK, cand.size)
            E = np.zeros((g_n, bnd - a))
            E[cand[a:bnd], np.arange(bnd - a)] = 1.0
            Y = lu.solve(E)
            cjj[cand[a:bnd]] = Y[cand[a:bnd], np.arange(bnd - a)]
        cmin = np.zeros(tcol.size)
        if cand.size:
            with np.errstate(invalid='ignore', divide='ignore'):
                c = X[cand] / np.sqrt(np.abs(cii)[None, :] * np.abs(cjj[cand])[:, None])
            c = np.where(np.isfinite(c), c, 0.0)
            cmin = c.min(axis=0)
            pcol = gi[cand[np.argmin(c, axis=0)]]          # the other unknown each one is closest to
            partner[gi[tcol]] = pcol
        cols = gi[tcol]                                   # global columns of the target
        flux[cols] = f[cols]
        err[cols] = s[tcol] * np.sqrt(np.abs(cii))       # X, cii, cjj are of the scaled unknowns
        corr[cols] = cmin
    nosep = np.isfinite(corr) & (corr < -degenerate)
    n_in = members[0].model.lam_grid.size
    gb, gr = members[0].ext['grids']
    ext = []
    for g, a in ((gb, n_in), (gr, n_in + gb.size)):
        sl = slice(a, a + g.size)
        ext.append(dict(lam=g, flux=flux[sl], err=err[sl], nosep=nosep[sl]))
    # which member, and which part of it (in range or past its cutout), each target point is
    # closest to: the source of a not-separable flag
    pc = partner[:n_in]
    by_sid = np.where(pc >= 0, np.array([m.sid for m in members])[blocks[np.maximum(pc, 0)]], -1)
    by_ext = np.where(pc >= 0, parts[np.maximum(pc, 0)] % 2 == 1, False)
    return dict(lam=members[0].model.lam_grid, flux=flux[:n_in], err=err[:n_in], nosep=nosep[:n_in],
                corr=corr[:n_in], ext=ext, npix=int(use.sum()), nclip=int((~use & np.isfinite(d)).sum()),
                nosep_sid=by_sid, nosep_ext=by_ext)


# ---------------------------------------------------------------------------
# Pass-1 file of one target
# ---------------------------------------------------------------------------
def pass1_of(target, sol, maps, stage_config):
    """A ``profile_contam.Pass1`` of the target from its joint solution: frames from its
    own spectrum (never smoothed), the light past its fitted wavelengths from the joint
    solution too (count rate, merged as ``ext`` pixels, ``ext_frames``), and the
    not-separable mask."""
    from campfire_pipeline.nirspec import profile_extend as PE
    st = CT.pass1_settings(stage_config)
    M, exps = target.model, target.exps
    R = PJ.fixed_result(M, target.p, target.eoff, flux=np.nan_to_num(sol['flux']),
                        flux_err=np.nan_to_num(sol['err']))
    R.flux = sol['flux']
    frames, nosrc, _ = CT._frames(R, exps, target.sid, None, target.workspace, 0, st['lines'])
    ext = ext_frames(target, sol.get('ext'))
    if ext is not None:
        frames = PE.merge_frames(frames, ext)
        nosrc = [k for k in nosrc if k not in ext['frames']]
    P1 = CT.Pass1(sid=int(target.sid), lam=sol['lam'], flux=sol['flux'], flux_err=sol['err'], frames=frames,
                  reliable=True, nosrc=set(nosrc))
    P1.ext, P1.nband, P1.nosep = ext, None, sol['nosep']
    P1.n_smooth, P1.keep_lines = 0, bool(st['lines'])
    P1.clean, P1.why, P1.shape_src, P1.spec_src = 1.0, 'joint', target.shape_src, SPEC_SRC
    P1.snr_det = np.nan
    P1.flux_raw, P1.err_raw = sol['flux'], sol['err']
    rep = np.abs(sol['flux'] * PF.main_shutter_factor(R, R.lam)) * 1e12
    rep = rep[np.isfinite(rep)]
    P1.rep_p95 = float(np.percentile(rep, 95)) if rep.size > 10 else np.nan
    P1.ref_p95, P1.n_lines = np.nan, 0
    return P1


def ext_frames(target, ends):
    """The target's light past its fitted wavelengths as ``profile_extend.merge_frames``
    takes it: dict(lam, flux, err, flux_raw, frames, npix, nmask) with the frames on
    each exposure's extension canvas, or None when it has none."""
    if not ends or not target.ext['n'] or not any(np.isfinite(x['flux']).any() for x in ends):
        return None
    frames = {}
    for key, mod in target.ext['model'].items():
        rows, cols = mod['rows'], mod['cols']
        img = np.zeros((rows.size, cols.size))
        err = np.zeros((rows.size, cols.size))
        for x in ends:
            g = x['lam']
            if g.size < 2:
                continue
            with np.errstate(invalid='ignore'):
                on = (mod['lam'] >= g[0]) & (mod['lam'] <= g[-1]) & ((mod['lam'] < target.ext['lo']) == (g[-1] < target.ext['lo']))
            f = np.interp(mod['lam'][on], g, np.nan_to_num(x['flux']))
            e = np.interp(mod['lam'][on], g, np.nan_to_num(x['err']))
            img[mod['iy'][on], mod['ix'][on]] += mod['g'][on] * f
            err[mod['iy'][on], mod['ix'][on]] += np.abs(mod['g'][on]) * e
        keep = np.isfinite(img) & (img != 0)
        if keep.any():
            frames[key] = dict(rows=rows, cols=cols, img=np.where(keep, img, 0.0), err=np.where(keep, err, 0.0),
                               keep=keep)
    if not frames:
        return None
    cat = lambda n: np.concatenate([np.asarray(x[n], float) for x in ends])     # noqa: E731
    return dict(lam=cat('lam'), flux=cat('flux'), err=cat('err'), flux_raw=cat('flux'), frames=frames,
                npix=0, nmask=0)


def signature(stage_config, ids, prism=None):
    """The cache signature of a joint pass-1 file: the pass-1 settings, this module's
    version, the neighbourhood's members and the PRISM shapes they take (``prism``, one
    string per member)."""
    st = CT.pass1_settings(stage_config)
    s = dict(st, joint=JOINT_PASS_VERSION, members=','.join(str(s) for s in ids))
    if prism:
        s['prism'] = ';'.join(prism)
    return CT._signature(s)


def _paths(workspace, product, pass1_dir):
    """(pass-1 file, missing-reason file) of a product; under ``pass1_dir`` when given
    (validation runs that must not touch the workspace)."""
    if pass1_dir:
        return (os.path.join(pass1_dir, f'{product}_pass1.npz'),
                os.path.join(pass1_dir, f'{product}_pass1_missing.txt'))
    return CT.pass1_path(workspace, product), CT.missing_path(workspace, product)


def _boxcar(sid, t, cache, maps, stage_config, err):
    """Pass 1 of a target whose own member cannot be built (a named fallback): its
    stage-3 3-pixel boxcar (``profile_contam.boxcar_pass1``), as the masked first pass
    does, so the targets its trace crosses are still predicted. Raises ``err`` when there
    is no boxcar either."""
    from campfire_pipeline.nirspec.stage3 import spec3_boxcar3
    box = spec3_boxcar3(cache.workspace, t['product'])
    if box is None:
        raise type(err)(f'{err}; no stage-3 s2d or spectrum to take its 3-pixel boxcar from '
                        f'(no science exposure reached stage 3)') from err
    exps = PF.load_source(t['groups'], bkg_overrides=t['overrides'], science=set(t['science']))
    return CT.boxcar_pass1(exps, sid, maps, cache.workspace, cache.is_prism, stage_config, box[:3]), box[3]


def run_target(sid, ids, cache, maps, stage_config, overwrite=False, degenerate=0.9, pass1_dir=None):
    """Solve one target's neighbourhood ``ids`` (target first) and write its pass-1 file.

    A neighbour whose member cannot be built (``ProfileFitFallback``) is left out of the
    solve, logged; its light then stays in the target's spectrum where they share pixels.
    When the target's own member cannot be built its pass 1 is its stage-3 3-pixel
    boxcar (``_boxcar``). Returns the path, or None (logged, reason in the missing file)
    when there is no boxcar either."""
    from campfire_pipeline.common.io import log
    from campfire_pipeline.nirspec import trace_overlap as TO

    t = cache.tasks[int(sid)]
    out, missing = _paths(cache.workspace, t['product'], pass1_dir)
    groups = t['groups']
    keys = {CT.exposure_key(f) for g in groups for f in g}
    mpaths = [TO.trace_map_path(os.path.join(cache.workspace, CT.MAPS_SUBDIR), f'{k}_rate.fits') for k in sorted(keys)]
    mpaths = [p for p in mpaths if os.path.exists(p)]
    sig = signature(stage_config, ids, [cache.prism_sig(s) for s in ids])
    # a rebuilt master background (the sky every member is fitted on) redoes it too
    mbkg = [p for p in (os.path.join(cache.workspace, f'{k}_mbkg.fits') for k in sorted(keys)) if os.path.exists(p)]
    prev = None if overwrite else CT._pass1_valid(out, list(groups) + [mpaths, mbkg], len(mpaths), sig)
    if prev is not None and not CT._boxcar_stale(prev, out, cache.workspace, t['product']):
        return out
    tmaps = {k: maps[k] for k in keys if k in maps}
    try:
        try:
            target = cache.get(sid)
        except PF.ProfileFitFallback as e:
            P1, src = _boxcar(sid, t, cache, tmaps, stage_config, e)
            log(f"{t['product']}: pass 1 from the stage-3 3-pixel boxcar ({src}), its own fell back "
                f"({e.reason}: {e})")
            CT.write_pass1(out, P1, sig, len(mpaths), SPEC_SRC, '1')
            if os.path.exists(missing):
                os.remove(missing)
            return out
    except PF.ProfileFitFallback as e:
        log(f"{t['product']}: no pass 1 ({e.reason}: {e})")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(missing, 'w') as f:
            f.write(f'{e.reason}: {e}\n')
        return None
    members = [target]
    for s in ids[1:]:
        try:
            members.append(cache.get(s))
        except PF.ProfileFitFallback as e:
            log(f"{t['product']}: neighbour {s} left out of the joint solve ({e.reason})")
    sol = solve_target(members, degenerate=degenerate)
    P1 = pass1_of(target, sol, tmaps, stage_config)
    CT.write_pass1(out, P1, sig, len(mpaths), SPEC_SRC, '1')
    if os.path.exists(missing):
        os.remove(missing)
    return out


def run_batch(targets, tasks, workspace, stage_config, is_prism, coupled, maps_keys=None, overwrite=False,
              depth=2, cap=25, degenerate=0.9, pass1_dir=None):
    """One worker's share of a disperser: the joint first pass of ``targets`` (in
    coupling order), with one member cache. Returns the written paths."""
    from campfire_pipeline.nirspec import first_pass as FP
    sky_for = FP.master_sky(workspace)
    cache = MemberCache(tasks, workspace, sky_for, stage_config, is_prism)
    keys = maps_keys or sorted({CT.exposure_key(f) for t in tasks.values() for g in t['groups'] for f in g})
    from campfire_pipeline.nirspec import trace_overlap as TO
    maps = {}
    for k in keys:
        p = TO.trace_map_path(os.path.join(workspace, CT.MAPS_SUBDIR), f'{k}_rate.fits')
        if TO.map_is_current(p):
            maps[k] = TO.TraceMap.read(p)
    out = []
    for sid in targets:
        ids = [s for s in neighbourhood(coupled, sid, depth=depth, cap=cap) if s in tasks]
        ow = overwrite if isinstance(overwrite, bool) else int(sid) in overwrite
        out.append(run_target(sid, ids, cache, maps, stage_config, overwrite=ow, degenerate=degenerate,
                              pass1_dir=pass1_dir))
    return out


def run(tasks, workspace, stage_config, is_prism, n_processes=1, overwrite=False, depth=2, cap=25,
        degenerate=0.9, pass1_dir=None):
    """The joint first pass of every source of one disperser.

    tasks : {sid: dict(product, groups, overrides, science, prism_product)} for every
    source of the disperser (its light reaches the others). overwrite : bool, or the set
    of source ids whose pass 1 is redone whatever its cache says (the others are redone
    only when stale). Returns the written paths."""
    from campfire_pipeline.common.parallel import dispatch
    from campfire_pipeline.nirspec import trace_overlap as TO

    files = {sid: [f for g in t['groups'] for f in g] for sid, t in tasks.items()}
    keys = sorted({CT.exposure_key(f) for fs in files.values() for f in fs})
    maps = {}
    for k in keys:
        p = TO.trace_map_path(os.path.join(workspace, CT.MAPS_SUBDIR), f'{k}_rate.fits')
        if TO.map_is_current(p):
            maps[k] = TO.TraceMap.read(p)
    coupled = coupling(files, maps)
    order = target_order(coupled, depth=depth, cap=cap)
    nb = max(1, int(n_processes))
    size = int(np.ceil(len(order) / nb))
    batches = [order[i:i + size] for i in range(0, len(order), size)]
    args = [(b, tasks, workspace, stage_config, is_prism, coupled, keys, overwrite, depth, cap, degenerate,
             pass1_dir) for b in batches]
    res = dispatch(run_batch, args, n_processes=n_processes, use_starmap=True) or []
    return [p for r in res for p in (r or [])]
