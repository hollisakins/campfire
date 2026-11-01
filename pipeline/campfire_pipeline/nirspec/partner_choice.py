"""Nod partners chosen per source from the light each partner frame puts on the
source's shutter (automatic nodded-background overrides; prototype).

A nod difference subtracts the mean of the partner frames. A partner frame in which
another source sits in this source's shutter row subtracts that source's light (a
negative image). Here each partner frame is read beyond the master sky on the
source's main-shutter pixels of the nod, binned along the trace; the trace maps say
which other sources are in the shutter row in that frame. A partner is dropped when
the expected error of the nod difference without it is smaller than with it:

  with both partners j, j':  bias (C_j + C_j') / 2, partner noise (v_j + v_j') / 4
  without j:                 bias C_j',           partner noise v_j'

per bin, summed over the bins where the trace maps put another source in the row in
partner j's frame, leaving out the bins with the largest gains (a feature in a bin or
two is masked, not paid for with a partner). C_j is partner j's light above the
least-lit partner's in that bin, so the master
sky's per-shutter floor (the same shutter in every frame) cancels and only the
ordering of the partners uses the master sky. The result is an override in the
format of ``nodded_background_overrides.toml``: per exposure root, source and nod,
the nods kept as background.
"""
import os
import re
from dataclasses import dataclass, field

import numpy as np
from astropy.io import fits

from . import master_bkg as MB
from . import profile_contam as CT
from . import profile_fit as PF
from . import trace_overlap as TO

IN_ROW = 0.5          # another source is in the shutter row: its source shutter covers this share of the main shutter
BIN_COLS = 20         # detector columns per bin along the trace
MIN_GAIN = 0.25       # drop a partner only when the expected error falls by this share of the partner noise kept
TRIM = 0.1            # share of the bins with the largest gains left out (at least two)


def nod_number(path):
    """Exposure sequence number from a file name (the third underscore segment)."""
    return int(os.path.basename(path).split('_')[2])


def exposure_root(path):
    return '_'.join(os.path.basename(path).split('_')[:2])


@dataclass
class Frame:
    """One exposure's calibrated frame before background subtraction, its variance and
    the master sky, on the source's common frame."""
    sci: np.ndarray
    var: np.ndarray
    sky: np.ndarray


@dataclass
class NodChoice:
    """The partner choice for one nod (one exposure number, both detectors)."""
    root: str
    nod: int
    partners: list                      # nod numbers subtracted by default
    keep: list                          # nod numbers kept
    gain: float = 0.0                   # expected error removed / partner noise kept
    others: dict = field(default_factory=dict)   # partner nod -> other sources in the row
    light: dict = field(default_factory=dict)    # partner nod -> light above the least-lit partner / its error


def place(e, a):
    """A canonical cutout array on the source's common frame."""
    out = np.full(e.y.shape, np.nan)
    r0, c0, ny, nx = e.cutout
    out[r0:r0 + ny, c0:c0 + nx] = a
    return out


def read_frame(e, mbkg_dir, products):
    """``Frame`` of exposure ``e`` from its canonical (``PRE_BKGSUB_*``) and the
    master-background product of its exposure (``sky_for_slit``)."""
    key = CT.exposure_key(e.path)
    if key not in products:
        p = os.path.join(mbkg_dir, f'{key}_mbkg.fits')
        products[key] = MB.load_product(p) if os.path.exists(p) else None
    with fits.open(e.path) as h:
        sci = np.asarray(h['PRE_BKGSUB_SCI'].data, float)
        var = np.asarray(h['PRE_BKGSUB_VAR_POISSON'].data, float) + np.asarray(h['PRE_BKGSUB_VAR_RNOISE'].data, float)
    sky = np.full(sci.shape, np.nan)
    if products[key] is not None:
        sky = np.asarray(MB.sky_for_slit(products[key], e.path)['sky'], float)
    return Frame(place(e, sci), place(e, var), place(e, sky))


def others_in_row(e, maps, frame_key, main, sid, min_cover=IN_ROW):
    """Other sources whose source shutter covers at least ``min_cover`` of the main-shutter
    pixels of a column in exposure ``frame_key``: {source id: columns}."""
    m = maps[frame_key]
    y0, x0 = int(e.origin[0]) - 1, int(e.origin[1]) - 1
    ny, nx = e.y.shape
    iy, ix, idx, flg = m.traces_in(x0, y0, nx, ny)
    src = m.source_shutter(flg)
    n_main = main.sum(axis=0)
    out = {}
    for r in np.unique(idx[src]):
        rec = m.slits[int(r)]
        if rec['kind'] != 'slitlet' or int(rec['source_id']) in (sid, 0):
            continue
        s = src & (idx == r)
        hit = np.zeros(e.y.shape, bool)
        ok = (iy[s] >= 0) & (iy[s] < ny) & (ix[s] >= 0) & (ix[s] < nx)
        hit[iy[s][ok], ix[s][ok]] = True
        with np.errstate(invalid='ignore', divide='ignore'):
            cover = (hit & main).sum(axis=0) / n_main
        cols = np.flatnonzero(cover >= min_cover)
        if cols.size:
            out[int(rec['source_id'])] = cols
    return out


def binned(a, main, edges):
    """Sum of ``a`` over the main-shutter pixels of each column bin (NaN-free pixels only)."""
    col = np.sum(np.where(main, a, 0.0), axis=0)
    return np.add.reduceat(col, edges[:-1])[:len(edges) - 1]


def choose_nod(frames, k, partners, main, row_cols, bin_cols=BIN_COLS):
    """Partner light for nod ``k`` on one detector (``main``: that nod's main shutter).

    ``frames`` is {exposure index: Frame}; ``partners`` the indices subtracted by default;
    ``row_cols`` {partner index: columns where another source is in the row}. Returns
    per partner the binned light beyond the master sky ``E``, its variance ``V``, and the
    bins where another source is in the row ``N``, and under ``'own'`` the nod's own
    frame's light (target included); the decision is made over both detectors
    (``decide``)."""
    good = main.copy()
    for j in [k] + list(partners):
        good &= np.isfinite(frames[j].sci) & np.isfinite(frames[j].sky) & np.isfinite(frames[j].var)
    nx = main.shape[1]
    edges = np.r_[np.arange(0, nx, bin_cols), nx]
    out = {}
    for j in partners:
        f = frames[j]
        E = binned(np.where(good, f.sci - f.sky, 0.0), good, edges)
        V = binned(np.where(good, f.var, 0.0), good, edges)
        N = np.zeros(len(edges) - 1, bool)
        cols = row_cols.get(j, np.array([], int))
        if cols.size:
            N[np.unique(np.searchsorted(edges, cols, side='right') - 1)] = True
        n_pix = binned(good.astype(float), good, edges)
        N &= n_pix > 0
        out[j] = dict(E=E, V=V, N=N)
    f = frames[k]
    out['own'] = dict(E=binned(np.where(good, f.sci - f.sky, 0.0), good, edges),
                      V=binned(np.where(good, f.var, 0.0), good, edges))
    return out


def decide(parts, min_gain=MIN_GAIN, trim=TRIM):
    """Which partner to drop, from the per-detector ``choose_nod`` outputs of one nod
    (each {partner nod: dict(E, V, N), 'own': dict(E, V)}). Each partner's light is taken
    above both the least-lit partner's and the nod's own frame's in each bin (``C``,
    never negative): light the own frame carries too is light the subtraction should
    remove, and the own frame's target light only makes ``C`` smaller (a bright
    target's contamination is underestimated, never invented). For each partner, over the bins where the
    trace maps put another source in the row in that partner's frame (its ``N``), the
    expected squared error of the nod difference's partner term is compared between
    keeping all partners and dropping it, bin by bin; the largest ``trim`` share of the
    bins' gains (at least two bins) is left out, so a feature in a bin or two (better
    masked) never costs a whole nod a partner. Returns (dropped nod or None, gain, light
    per partner in error units over its ``N``)."""
    nods = sorted(j for j in parts[0] if j != 'own')
    if len(nods) < 2:
        return None, 0.0, {}
    C, V, N = {j: [] for j in nods}, {j: [] for j in nods}, {j: [] for j in nods}
    for p in parts:
        E = np.array([p[j]['E'] for j in nods])
        lo = np.min(E, axis=0)
        own = p['own']['E'] if 'own' in p else lo
        for i, j in enumerate(nods):
            C[j].append(np.clip(np.minimum(E[i] - lo, E[i] - own), 0, None))
            V[j].append(p[j]['V'])
            N[j].append(p[j]['N'])
    C, V, N = ({j: np.concatenate(d[j]) for j in nods} for d in (C, V, N))
    n = len(nods)
    cost_all = (sum(C[j] for j in nods) / n) ** 2 + sum(V[j] for j in nods) / n ** 2
    best, gain, light = None, 0.0, {}
    for d in nods:
        if not N[d].any():
            continue
        keep = [j for j in nods if j != d]
        m = len(keep)
        noise = sum(V[j] for j in keep) / m ** 2
        g = (cost_all - (sum(C[j] for j in keep) / m) ** 2 - noise)[N[d]]
        cut = max(2, int(np.ceil(trim * g.size)))
        kept = np.argsort(g)[:max(g.size - cut, 0)]
        if kept.size == 0:
            continue
        gd = float(np.sum(g[kept]) / np.sum(noise[N[d]][kept]))
        light[d] = float(np.sum(C[d][N[d]]) / np.sqrt(np.sum(V[d][N[d]])))
        if gd > gain:
            best, gain = d, gd
    if gain < min_gain:
        return None, gain, light
    return best, gain, light


def source_choice(groups, sid, maps, mbkg_dir, science=None, products=None,
                  bin_cols=BIN_COLS, min_gain=MIN_GAIN, min_cover=IN_ROW):
    """The partner choice of every nod of one source: a list of ``NodChoice``.

    ``groups`` as ``profile_fit.load_source`` takes them (stage 2b's partners, no
    overrides); ``maps`` {exposure key: TraceMap}; ``mbkg_dir`` holds the
    ``<exposure>_mbkg.fits`` products."""
    products = {} if products is None else products
    exps = PF.load_source(groups, bkg_overrides=None, science=science)
    c_plan = PF.planned_position(exps)[0]
    frames = {i: read_frame(e, mbkg_dir, products) for i, e in enumerate(exps)}
    by_nod = {}
    for k, e in enumerate(exps):
        if not e.use_data or not e.partners or len(e.partners) < 2:
            continue
        main = CT.main_shutter(e, c_plan)
        found = {j: others_in_row(e, maps, CT.exposure_key(exps[j].path), main, sid, min_cover) for j in e.partners}
        row_cols = {j: np.unique(np.concatenate(list(o.values()))) for j, o in found.items() if o}
        named = {nod_number(exps[j].path): sorted(o) for j, o in found.items() if o}
        part = choose_nod(frames, k, e.partners, main, row_cols, bin_cols)
        part = {(j if j == 'own' else nod_number(exps[j].path)): v for j, v in part.items()}
        key = (exposure_root(e.path), nod_number(e.path))
        by_nod.setdefault(key, dict(parts=[], others={}))
        by_nod[key]['parts'].append(part)
        for n, s in named.items():
            by_nod[key]['others'].setdefault(n, set()).update(s)
    out = []
    for (root, nod), d in sorted(by_nod.items()):
        partners = sorted(j for j in d['parts'][0] if j != 'own')
        drop, gain, light = decide(d['parts'], min_gain)
        out.append(NodChoice(root=root, nod=nod, partners=partners,
                             keep=[j for j in partners if j != drop], gain=gain,
                             others={n: sorted(s) for n, s in d['others'].items()}, light=light))
    return out


def write_overrides(choices, path, header=''):
    """``{sid: [NodChoice]}`` to an override file in the hand-override format; only nods
    whose partners changed are written, each with the sources that decided it."""
    lines = [f'# {h}' for h in header.splitlines()] if header else []
    roots = {}
    for sid, cs in choices.items():
        for c in cs:
            if c.keep != c.partners:
                roots.setdefault(c.root, {}).setdefault(sid, []).append(c)
    for root in sorted(roots):
        lines.append(f'\n[{root}]')
        for sid in sorted(roots[root]):
            cs = roots[root][sid]
            body = ', '.join(f'{c.nod} = [{", ".join(str(j) for j in c.keep)}]' for c in cs)
            note = '; '.join(f'nod {c.nod}: drop {sorted(set(c.partners) - set(c.keep))[0]} '
                             f'({",".join(str(s) for s in c.others.get(sorted(set(c.partners) - set(c.keep))[0], [])) or "?"}) '
                             f'gain {c.gain:.2f}' for c in cs)
            lines.append(f'    {sid} = {{{body}}}   # {note}')
    with open(path, 'w') as f:
        f.write('\n'.join(lines) + '\n')
