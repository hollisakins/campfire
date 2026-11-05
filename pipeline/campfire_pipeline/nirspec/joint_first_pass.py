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
3. per member, once per worker (``MemberCache``): its nod differences, the data of its
   final profile fit (``profile_fit.load_source``: each exposure minus the mean of the
   frames stage 2b subtracted; with ``pre_bkgsub``, of the partners the override files
   give, rebuilt from the frames before subtraction, so the solve does not depend on the
   same-slitlet choice stage 2b last made; stage 3 decides that choice from this pass),
   the shape of the chain (the PRISM shape of the same
   source when the PRISM product took its own fit, else its own fit above the grating
   gate, else the default shape; ``shape_for``), the per-exposure pointing offsets at
   that shape, and its frames per unit spectrum (``profile_joint``);
4. one weighted least-squares fit of every member's spectrum at full resolution,
   clipped at ``clip`` sigma twice (``solve_target``); each source is the target of its
   own neighbourhood;
5. the target's errors from its columns of the inverse normal matrix, and the
   wavelengths it cannot be told apart from another member (correlation below
   ``-degenerate``), ``nosep``;
6. a pass-1 file in the format the contamination prediction reads (``why = 'joint'``),
   with the not-separable mask, and beside it the other members' light on the target's
   own pixels from this same solve (``write_joint_light``). The target's product takes
   its neighbours' light from there (``add_joint_light``), not from each neighbour's own
   neighbourhood solve, whose split between two hard-to-separate sources differs.

Nod differences, not single frames minus the master sky: a nod difference subtracts the
same pixels, so the sky cancels exactly, including the part that differs from shutter to
shutter and repeats between nods. On p1 G395M the master sky left about +2.6 % of the sky
in single frames and a per-shutter spread; faint sources came out 0.15-0.2 µJy high and
their shape fits failed (docs/design-nirspec-pipeline-v2.md, 2026-10-31). Each source's
light enters its own difference (+1) and its partners' (-1/n), so other sources' negative
images are modelled like the rest.

The solve covers each member's whole trace, not only its calibrated range (user,
2026-10-31): past the wavelengths a member's fit pixels have, its light is a second set of
unknowns per end in count-rate units (``profile_extend`` geometry), fitted on the nod
differences of the rate files together with everything else (``design_full``). A member's
light past its cutout that lands on a neighbour's trace is then separated like the rest
instead of being masked out.
"""

import os
from collections import OrderedDict

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

from campfire_pipeline.nirspec import profile_contam as CT
from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import profile_joint as PJ

JOINT_PASS_VERSION = '4'          # 2: the light past the cutouts is among the joint unknowns
#                                   3: nod differences instead of the master sky; the neighbours'
#                                      light on the target's pixels is written beside its pass 1
#                                   4: no per-exposure pointing offsets fitted (they took neighbours' light)
JOINT_LIGHT_VERSION = '1'
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

    def __init__(self, tasks, workspace, stage_config, is_prism, size=60, pre_bkgsub=False):
        from campfire_pipeline.nirspec import profile_chain as PC
        self.tasks = tasks                      # sid -> dict(groups, overrides, science, prism_product)
        self.workspace = workspace
        self.cfg = PC.ChainConfig.from_stage_config(stage_config)
        self.is_prism = is_prism
        self.size = size
        self.pre_bkgsub = pre_bkgsub            # nod differences of the override files (profile_fit.load_group)
        self.bar_offsets = stage_config.get('bar_offsets')    # bar_offset.table, set by stage 3
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
        t = self.tasks[sid]
        exps = PF.load_source(t['groups'], bkg_overrides=t['overrides'], science=set(t['science']),
                              pre_bkgsub=self.pre_bkgsub, bar_offsets=self.bar_offsets)
        cfg = self.cfg
        M = PF.ProfileModel(exps, chromatic=True, priors=cfg.priors, bounds=cfg.bounds)
        if sid not in self._shape:
            prism = (CT.read_prism_shape(self.workspace, t['prism_product'])
                     if (not self.is_prism and t.get('prism_product')) else None)
            shape, src = shape_for(exps, M, cfg, self.is_prism, prism)
            R = PF.fit_fixed(model=M, shape=shape)
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
def _rate_nod_diff(workspace, key, partner_keys, rows, cols):
    """(data, variance, bad) of one nod difference on a detector band, count-rate units:
    the exposure's rate minus the mean of its partners' (the frames stage 2b subtracts),
    so the sky cancels pixel by pixel. Bad: DO_NOT_USE in any of the rates, or no value.
    None without partners or without a rate file."""
    from campfire_pipeline.nirspec import profile_extend as PE
    paths = [os.path.join(workspace, f'{k}_rate.fits') for k in [key] + list(partner_keys)]
    if not partner_keys or not all(os.path.exists(p) for p in paths):
        return None
    d, v, bad = PE._rate(paths[0], rows, cols)
    n = len(partner_keys)
    for p in paths[1:]:
        s, vj, bj = PE._rate(p, rows, cols)
        d = d - s / n
        v = v + vj / n ** 2
        bad = bad | bj
    bad = bad | ~np.isfinite(d) | ~np.isfinite(v) | (v <= 0)
    return d, v, bad


def _terms(exps, k):
    """The frames of exposure k's nod difference and their weights: [(k, 1), (j, -1/n) for
    each partner j]."""
    e = exps[k]
    part = list(e.partners or [])
    return [(k, 1.0)] + [(j, -1.0 / len(part)) for j in part]


def ext_setup(m, workspace):
    """A member's light past the wavelengths its fit pixels have (``m.ext``): one grid
    per end (``profile_extend.end_rows``), its frames per unit count-rate spectrum on the
    detector there (sparse, one per exposure: the source's own light in that frame), and
    the data there, each science exposure's nod difference of the rate files
    (``_rate_nod_diff``), on the pixels its own trace lights in that exposure. Nothing
    past the cutout (``n = 0``) when it has no such pixels."""
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
        data = _rate_nod_diff(workspace, key, [CT.exposure_key(exps[j].name) for j in (exps[k].partners or [])],
                              rows, cols)
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
    rows for each member's pixels past its cutout (nod differences of the rate files,
    count rate) that no cutout already holds, with every member's in-range light
    (converted back with the giving member's calibration) and extension light on them.

    Every row is a nod difference: a member's light enters through each frame of the
    row's difference with its weight (+1 for the exposure, -1/n for each partner;
    ``_terms``), as in ``profile_joint.design``. A datum is the exposure, its partners
    and the detector pixel; it enters once.

    Column layout per member: its in-range grid (``col0``), then its blue and red end
    grids (``xcol0``). Returns (A, d, v, n_cut) with ``n_cut`` the number of cutout
    rows."""
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

    def add_ext_light(r, det, key, conv_of, w=1.0):
        """Every member's extension light at detector pixels ``det`` (rows ``r``) of frame
        ``key``, times the frame's weight ``w`` in the row's nod difference; ``conv_of(sel)``
        converts count rate to the row's units."""
        for c in members:
            mod = c.ext['model'].get(key) if c.ext['n'] else None
            if mod is None:
                continue
            found, pos = _lookup(mod, det)
            if not found.any():
                continue
            conv = w * conv_of(found)
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
            for j, w in _terms(s.model.exps, k):
                cal = s.calibration(j)[iy, ix]          # count rate -> the member's units in frame j
                add_ext_light(r, det, CT.exposure_key(s.model.exps[j].name), lambda f, cal=cal: cal[f], w)
        row0 += int(own.sum())
    n_cut = row0

    # rows past the cutouts: once per datum, never one a cutout holds
    claimed = np.unique(np.concatenate(cut_keys)) if cut_keys else np.zeros(0, np.int64)
    d, v = [d0], [v0]
    for s in members:
        R = s.ext.get('rows') if s.ext['n'] else None
        if R is None:
            continue
        tag_of = {k: CT.exposure_key(s.exps[k].name) + '|' + ','.join(
            sorted(CT.exposure_key(s.exps[j].name) for j in (s.exps[k].partners or []))) for k in np.unique(R['k'])}
        tid = np.array([tags.setdefault(tag_of[k], len(tags)) for k in R['k']], np.int64)
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
            r = r_all[sk]
            for j, w in _terms(s.exps, k):
                key = CT.exposure_key(s.exps[j].name)
                # in-range light of every member in frame j, from its calibrated units to count rate
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
                        conv = w / b['cal'][cy[ins], cx[ins]]
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
                add_ext_light(r, det[sk], key, lambda f: np.ones(int(f.sum())), w)
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
                nosep_sid=by_sid, nosep_ext=by_ext, others=_others_on_target(members, A, f, gi, s))


def _others_on_target(members, A, f, gi, s):
    """Each other member's light on the target's own pixels from this solve: per member
    dict(sid, rows, val, err), ``rows`` indexing the target's pixel table (the target is
    first, so it owns all its pixels and they are the first rows of ``A``), in the units
    of the target's nod differences. ``err``: the member's unknowns' errors with the other
    unknowns held (a lower bound; reported only), propagated linearly."""
    t = members[0]
    nT = int(np.sum(t.owned)) if getattr(t, 'owned', None) is not None else 0
    if nT == 0:
        return []
    e_cond = np.zeros(A.shape[1])
    e_cond[gi] = s                                       # 1 / sqrt(N_jj) of the unscaled unknowns
    At = A[:nT].tocsc()
    out = []
    for c in members[1:]:
        cs = slice(c.col0, c.xcol0 + c.ext['n'])
        Ac = At[:, cs]
        if Ac.nnz == 0:
            continue
        rows = np.unique(Ac.nonzero()[0])
        val = np.asarray(Ac @ f[cs]).ravel()[rows]
        err = np.asarray(abs(Ac) @ e_cond[cs]).ravel()[rows]
        keep = (val != 0) | (err != 0)
        if keep.any():
            out.append(dict(sid=int(c.sid), rows=rows[keep], val=val[keep], err=err[keep]))
    return out


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
    rep = np.abs(sol['flux'] * PF.reported_factor(R, R.lam)) * 1e12
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


# ---------------------------------------------------------------------------
# The neighbours' light on the target, from the target's own solve
# ---------------------------------------------------------------------------
def joint_light_path(pass1_file):
    """The file beside a pass-1 file holding the other members' light on its target."""
    return pass1_file[:-len('_pass1.npz')] + '_jointlight.npz'


def _exposure_tags(exps):
    """Per exposure: its key and its nod difference (the exposure and its partners)."""
    return [CT.exposure_key(e.name) + '|' + ','.join(sorted(CT.exposure_key(exps[j].name)
                                                           for j in (e.partners or []))) for e in exps]


KEEP_FRAC = 0.01        # joint-light pixels kept: a member's light or its error above this x the pixel's noise


def write_joint_light(path, target, others, sig):
    """Write the other members' light on the target's pixels (``solve_target``'s
    ``others``) atomically: per pixel its member, exposure, row and column on the target's
    common frame, value and error (single precision), with each exposure's nod difference
    for the check on reading. Pixels where a member's light and its error are both below
    ``KEEP_FRAC`` of the pixel's noise are left out (most of a faint member's pixels;
    3.5 MB per G395M source with them)."""
    k, iy, ix = target.pix_k, target.pix_iy, target.pix_ix
    pv = getattr(target.model, 'pix', {}).get('v') if hasattr(target, 'model') else None
    if pv is not None:
        sig_px = np.sqrt(np.asarray(pv, float))
        kept = []
        for o in others:
            s = sig_px[o['rows']]
            keep = (np.abs(o['val']) > KEEP_FRAC * s) | (np.abs(o['err']) > KEEP_FRAC * s)
            if keep.any():
                kept.append(dict(o, rows=o['rows'][keep], val=o['val'][keep], err=o['err'][keep]))
        others = kept
    sids = np.array([o['sid'] for o in others], np.int64)
    mi = np.concatenate([np.full(o['rows'].size, i) for i, o in enumerate(others)]) if others else np.zeros(0, int)
    rows = np.concatenate([o['rows'] for o in others]) if others else np.zeros(0, int)
    arrays = dict(version=JOINT_LIGHT_VERSION, sig=sig, sid=int(target.sid), members=sids,
                  tags=np.array(_exposure_tags(target.exps)), origin=np.array(target.exps[0].origin),
                  shape=np.array(target.exps[0].y.shape), m=mi.astype(np.int32), k=k[rows].astype(np.int16),
                  iy=iy[rows].astype(np.int32), ix=ix[rows].astype(np.int32),
                  val=(np.concatenate([o['val'] for o in others]) if others else np.zeros(0)).astype(np.float32),
                  err=(np.concatenate([o['err'] for o in others]) if others else np.zeros(0)).astype(np.float32))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path[:-4] + '.tmp.npz'
    np.savez_compressed(tmp, **arrays)
    os.replace(tmp, path)


def read_joint_light(path, sig=None):
    """The neighbours' light file as a dict, or None when missing, of another version or
    (with ``sig``) of another solve."""
    if not os.path.exists(path):
        return None
    z = np.load(path)
    if str(z['version']) != JOINT_LIGHT_VERSION or (sig is not None and str(z['sig']) != str(sig)):
        return None
    return {n: z[n] for n in z.files}


def add_joint_light(P, exps, jl):
    """Add the neighbours' light from the target's own joint solve (``read_joint_light``)
    to a ``profile_contam.Prediction`` that left those members out (``predict`` with
    ``exclude`` = them): subtracted, never masked, like any jointly
    solved light (``pred_joint``; its error in ``dpred_joint``, reported only).

    The target's exposures must be the ones the solve used: the same nod differences on
    the same common frame. Raises ValueError otherwise."""
    tags = _exposure_tags(exps)
    stored = [str(t) for t in jl['tags']]
    if tags != stored or tuple(exps[0].origin) != tuple(int(x) for x in jl['origin']) \
            or tuple(exps[0].y.shape) != tuple(int(x) for x in jl['shape']):
        raise ValueError(f"joint light of source {int(jl['sid'])}: the solve's exposures {stored} are not the "
                         f'product\'s {tags}')
    n = len(exps)
    if P.pred_joint is None:
        P.pred_joint = [np.zeros(e.y.shape) for e in exps]
    if P.dpred_joint is None:
        P.dpred_joint = [np.zeros(e.y.shape) for e in exps]
    c_plan = PF.planned_position(exps)[0]
    for i, csid in enumerate(int(s) for s in jl['members']):
        sel = jl['m'] == i
        if not sel.any():
            continue
        for k in np.unique(jl['k'][sel]):
            sk = sel & (jl['k'] == k)
            img = np.zeros(exps[k].y.shape)
            dimg = np.zeros(exps[k].y.shape)
            np.add.at(img, (jl['iy'][sk], jl['ix'][sk]), jl['val'][sk])
            np.add.at(dimg, (jl['iy'][sk], jl['ix'][sk]), jl['err'][sk])
            P.pred[k] = P.pred[k] + img
            P.pred_joint[k] = P.pred_joint[k] + img
            P.dpred_joint[k] = P.dpred_joint[k] + dimg
            P.by_source.setdefault(csid, [None] * n)
            P.by_source[csid][k] = img if P.by_source[csid][k] is None else P.by_source[csid][k] + img
            e = exps[k]
            if e.use_data and e.partners is not None:
                ms = CT.main_shutter(e, c_plan) & e.ok
                with np.errstate(invalid='ignore', divide='ignore'):
                    v = np.abs(img) / CT.diff_sigma(exps, k)
                v = float(np.nanmax(np.where(ms, v, np.nan))) if ms.any() else 0.0
                P.contributors[csid] = max(P.contributors.get(csid, 0.0), v if np.isfinite(v) else 0.0)
        P.joint.add(csid)
    return P


def signature(stage_config, ids, prism=None, pre_bkgsub=False, overrides=None):
    """The cache signature of a joint pass-1 file: the pass-1 settings, this module's
    version, the neighbourhood's members, the PRISM shapes they take (``prism``, one
    string per member) and, with ``pre_bkgsub``, that the nod differences were the
    override files' (``MemberCache``) and which (``overrides``, one string per member), so
    a pass 1 of the other kind, or of other overrides, is never reused."""
    st = CT.pass1_settings(stage_config)
    s = dict(st, joint=JOINT_PASS_VERSION, members=','.join(str(s) for s in ids))
    if prism:
        s['prism'] = ';'.join(prism)
    if pre_bkgsub:
        s['bkg'] = 'pre'
        if overrides:
            s['overrides'] = ';'.join(overrides)
    return CT._signature(s)


def _paths(workspace, product, pass1_dir):
    """(pass-1 file, missing-reason file) of a product; under ``pass1_dir`` when given
    (validation runs that must not touch the workspace)."""
    if pass1_dir:
        return (os.path.join(pass1_dir, f'{product}_pass1.npz'),
                os.path.join(pass1_dir, f'{product}_pass1_missing.txt'))
    return CT.pass1_path(workspace, product), CT.missing_path(workspace, product)


def _spec3_current(workspace, product, paths):
    """True when the product's Spec3 s2d and x1d exist and are newer than every canonical
    of ``paths``."""
    out = [os.path.join(workspace, f'{product}{s}') for s in ('_s2d.fits', '_x1d.fits')]
    if not all(os.path.exists(p) for p in out):
        return False
    newest = max((os.path.getmtime(p) for p in paths if os.path.exists(p)), default=0.0)
    return min(os.path.getmtime(p) for p in out) > newest


def _boxcar(sid, t, cache, maps, stage_config, err):
    """Pass 1 of a target whose own member cannot be built (a named fallback): its
    stage-3 3-pixel boxcar (``profile_contam.boxcar_pass1``), as the masked first pass
    does, so the targets its trace crosses are still predicted. Stage 3 runs Spec3 after
    this pass (``stage3.run_profile_fit_phase``), so a product of this run (``t['spec3']``,
    the ``stage3.run_stage3_single_source`` arguments) whose s2d is missing or older than
    its canonicals gets its Spec3 here first. Raises ``err`` when there is no boxcar
    either."""
    from campfire_pipeline.nirspec.stage3 import spec3_boxcar3, run_stage3_single_source
    s3 = t.get('spec3')
    if s3 is not None and not _spec3_current(cache.workspace, t['product'], [f for g in t['groups'] for f in g]):
        args, kw = s3
        run_stage3_single_source(*args, **kw)
    box = spec3_boxcar3(cache.workspace, t['product'])
    if box is None:
        raise type(err)(f'{err}; no stage-3 s2d or spectrum to take its 3-pixel boxcar from '
                        f'(no science exposure reached stage 3)') from err
    exps = PF.load_source(t['groups'], bkg_overrides=t['overrides'], science=set(t['science']),
                          pre_bkgsub=getattr(cache, 'pre_bkgsub', False),
                          bar_offsets=stage_config.get('bar_offsets'))
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
    pre = getattr(cache, 'pre_bkgsub', False)
    sig = signature(stage_config, ids, [cache.prism_sig(s) for s in ids], pre,
                    [repr(cache.tasks[int(s)].get('overrides')) for s in ids] if pre else None)
    jpath = joint_light_path(out)
    prev = None if overwrite else CT._pass1_valid(out, list(groups) + [mpaths], len(mpaths), sig)
    if prev is not None and not CT._boxcar_stale(prev, out, cache.workspace, t['product']):
        # a joint pass 1 needs its neighbours' light beside it; a boxcar one has none
        if str(prev['why'] if 'why' in prev else '') != 'joint' or read_joint_light(jpath, sig) is not None:
            return out
    if os.path.exists(jpath):
        os.remove(jpath)
    tmaps = {k: maps[k] for k in keys if k in maps}
    try:
        try:
            target = cache.get(sid)
        except PF.ProfileFitFallback as e:
            P1, src = _boxcar(sid, t, cache, tmaps, stage_config, e)
            log(f"{t['product']}: pass 1 from the stage-3 3-pixel boxcar ({src}), its own fell back "
                f"({e.reason}: {e})")
            CT.write_pass1(out, P1, sig, len(mpaths), SPEC_SRC)
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
    CT.write_pass1(out, P1, sig, len(mpaths), SPEC_SRC)
    write_joint_light(jpath, target, sol.get('others', []), sig)
    if os.path.exists(missing):
        os.remove(missing)
    return out


def run_batch(targets, tasks, workspace, stage_config, is_prism, coupled, maps_keys=None, overwrite=False,
              depth=2, cap=25, degenerate=0.9, pass1_dir=None, cache_size=60, pre_bkgsub=False):
    """One worker's share of a disperser: the joint first pass of ``targets`` (in
    coupling order), with one member cache of ``cache_size`` members. Returns the
    written paths."""
    cache = MemberCache(tasks, workspace, stage_config, is_prism, size=cache_size, pre_bkgsub=pre_bkgsub)
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
        degenerate=0.9, pass1_dir=None, cache_size=60, pre_bkgsub=False, targets=None):
    """The joint first pass of every source of one disperser.

    tasks : {sid: dict(product, groups, overrides, science, prism_product[, spec3])} for
    every source of the disperser (its light reaches the others; ``spec3``: ``_boxcar``).
    overwrite : bool, or the set of source ids whose pass 1 is redone whatever its cache
    says (the others are redone only when stale). pre_bkgsub : every member's nod
    differences those of the override files (``MemberCache``). targets : solve only these
    sources' neighbourhoods (default every source). Returns the written paths."""
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
    if targets is not None:
        want = {int(t) for t in targets}
        order = [s for s in order if s in want]
    if not order:
        return []
    nb = max(1, int(n_processes))
    size = int(np.ceil(len(order) / nb))
    batches = [order[i:i + size] for i in range(0, len(order), size)]
    args = [(b, tasks, workspace, stage_config, is_prism, coupled, keys, overwrite, depth, cap, degenerate,
             pass1_dir, cache_size, pre_bkgsub) for b in batches]
    res = dispatch(run_batch, args, n_processes=n_processes, use_starmap=True) or []
    return [p for r in res for p in (r or [])]
