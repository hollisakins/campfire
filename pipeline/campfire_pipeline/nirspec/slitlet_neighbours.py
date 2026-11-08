"""Catalogued neighbours of a source's own slitlet: which nod partners to drop.

The MSA plan's ``SOURCE_INFO`` table gives every catalogue source's sky position and its
``SHUTTER_INFO`` table where the planned sources fall in their shutters. A quadratic map
from sky offsets to MSA shutter coordinates, fitted per configuration and quadrant to
those placements (with one offset per dither point), places every catalogue source
along any slitlet at every dither point (``SkyToMsa``; on p1 it reproduces the planned
placements to 0.003 shutter), including sources just across a bar or past the slitlet's
end, which ``SHUTTER_INFO`` does not list in that slitlet.

When a partner frame puts a neighbour within ``max_distance`` (slit units) of the
target's position in a nod, subtracting that frame puts a negative copy of the
neighbour on the target. Stage 2b then drops that partner, as a hand override would,
provided

* the neighbour is bright enough to matter: its light is at least ``min_sky_frac`` of
  the sky (the frame before subtraction against the master background, over one shutter
  height around it, median over the spectrum), and
* another partner remains. Otherwise the nod keeps its partners and is flagged
  (``no clear background``).

Overlap with other slitlets' traces is not this module's job (the joint model's
``fnu_contam``). Neighbours the catalogue does not list are the residual scan's
(``nod_scan``).

Coordinates: along the slitlet ``u = (column + y - 0.5) * MSA_SLIT_PITCH`` in slit units
(a shutter's open height is 1, the pitch 1.15), with ``y`` the position in the shutter
in pitch units (0 to 1, as the plan's ``estimated_source_in_shutter_y``); across it
``row + x - 0.5`` in shutter pitches. A frame's slit coordinates are centred on the
target's shutter at its dither point.
"""
import os
from dataclasses import dataclass, field

import numpy as np

from . import crds_refs as CR
from .constants import MSA_SLIT_PITCH

OPEN_HALF = 0.5
LIGHT_PAD = 0.25             # light beyond a shutter's open area, slit units (trace_overlap.LIGHT_PAD)
ROW_REACH = 0.75             # across the slitlet: open half-width (~0.37 pitch) plus the PSF, row pitches
MIN_SKY_FRAC = 0.02          # default threshold: the neighbour's light / the sky
MAX_DISTANCE = 0.75          # default: a partner is dirty within this distance of the target, slit units
MEASURE_SEP = 1.0            # the neighbour is measured only this far from the target, slit units


# ---------------------------------------------------------------------------
# Positions: every catalogue source along every slitlet
# ---------------------------------------------------------------------------
def _terms(east, north, quadratic=True):
    e, n = np.asarray(east, float), np.asarray(north, float)
    return np.c_[e, n, e * e, e * n, n * n] if quadratic else np.c_[e, n]


@dataclass
class SkyToMsa:
    """Sky offsets (arcsec east and north of the catalogue's median) to MSA shutter
    coordinates (``row + x - 0.5``, ``column + y - 0.5``) for one configuration: per
    quadrant the shared terms and one offset per dither point."""
    ids: np.ndarray
    east: np.ndarray
    north: np.ndarray
    fits: dict = field(default_factory=dict)     # quadrant -> (quadratic, terms (k, 2), {dither: (2,)})

    @classmethod
    def fit(cls, shutter_table, source_table, msa_metadata_id):
        src = source_table[np.asarray(source_table['source_id']) > 0]
        ra, dec = np.asarray(src['ra'], float), np.asarray(src['dec'], float)
        ra0, dec0 = np.median(ra), np.median(dec)
        m = cls(ids=np.asarray(src['source_id'], np.int64), east=(ra - ra0) * np.cos(np.radians(dec0)) * 3600,
                north=(dec - dec0) * 3600)
        index = {int(s): i for i, s in enumerate(m.ids)}
        t = shutter_table[shutter_table['msa_metadata_id'] == int(msa_metadata_id)]
        x = np.ma.filled(np.ma.asarray(t['estimated_source_in_shutter_x'], dtype=float), np.nan)
        y = np.ma.filled(np.ma.asarray(t['estimated_source_in_shutter_y'], dtype=float), np.nan)
        use = (np.asarray(t['source_id']) > 0) & (t['background'] == 'N') & np.isfinite(x) & np.isfinite(y) \
            & np.isin(np.asarray(t['source_id']), m.ids)
        for q in np.unique(t['shutter_quadrant'][use]):
            sel = use & (t['shutter_quadrant'] == q)
            dith = np.asarray(t['dither_point_index'][sel], int)
            ds = sorted(set(dith.tolist()))
            i = np.array([index[int(s)] for s in t['source_id'][sel]])
            for quadratic in (True, False):
                k = 5 if quadratic else 2
                if sel.sum() >= k + len(ds) + 3:
                    break
            else:
                continue
            A = np.c_[_terms(m.east[i], m.north[i], quadratic), (dith[:, None] == np.array(ds)[None, :]).astype(float)]
            P = np.c_[np.asarray(t['shutter_row'][sel]) + x[sel] - 0.5, np.asarray(t['shutter_column'][sel]) + y[sel] - 0.5]
            coef, *_ = np.linalg.lstsq(A, P, rcond=None)
            m.fits[int(q)] = (quadratic, coef[:k], {d: coef[k + j] for j, d in enumerate(ds)})
        return m

    def place(self, quadrant, dither):
        """(row, column) coordinates of every catalogue source at this dither point in
        this quadrant's frame, or None without a fit."""
        f = self.fits.get(int(quadrant))
        if f is None or int(dither) not in f[2]:
            return None
        quadratic, terms, off = f
        p = _terms(self.east, self.north, quadratic) @ terms + off[int(dither)]
        return p[:, 0], p[:, 1]


@dataclass
class Slitlet:
    """The target's slitlet in one configuration, along it in slit units."""
    slitlet_id: int
    quadrant: int
    row: int
    open_u: dict                 # dither -> centres of the open shutters
    target_u: dict               # dither -> the target's position (dithers where it is in the slitlet)
    ref_u: dict                  # dither -> centre of the target's shutter (the frame's slit-coordinate origin)


@dataclass
class Neighbour:
    """One catalogue source within reach of the target's slitlet."""
    source_id: int
    where: dict                  # dither -> position along the slitlet (dithers where it is within reach)
    sky_frac: float = np.nan     # its light / the sky around it (median over the spectrum)
    sky_frac_hi: float = np.nan  # the same, 90th percentile (a line-dominated neighbour)
    measured_in: str = ''        # frame the brightness was measured in


def scene(shutter_table, sky, msa_metadata_id, source_id):
    """(``Slitlet``, [``Neighbour``]) of one source in one configuration, or (None, [])
    when the configuration does not hold it. A neighbour is any other catalogue source
    within ``ROW_REACH`` of the slitlet's row and ``OPEN_HALF + LIGHT_PAD`` of one of its
    open shutters at some dither point: placed by ``sky`` (``SkyToMsa``), or, for a
    source the fit cannot place, where ``SHUTTER_INFO`` lists it in the slitlet."""
    from .metafile import _source_rows
    t = shutter_table[shutter_table['msa_metadata_id'] == int(msa_metadata_id)]
    mine = t[_source_rows(t, int(source_id)) & (t['background'] == 'N')]
    if not len(mine):
        return None, []
    prim = mine[mine['primary_source'] == 'Y']
    vals, counts = np.unique((prim if len(prim) else mine)['slitlet_id'], return_counts=True)
    sid_slit = int(vals[np.argmax(counts)])
    mine = mine[mine['slitlet_id'] == sid_slit]
    q, row = int(mine['shutter_quadrant'][0]), int(mine['shutter_row'][0])
    rows = t[t['slitlet_id'] == sid_slit]
    ys = np.ma.filled(np.ma.asarray(rows['estimated_source_in_shutter_y'], dtype=float), np.nan)
    listed = lambda r, y: (int(r['shutter_column']) + (y if np.isfinite(y) else 0.5) - 0.5) * MSA_SLIT_PITCH  # noqa: E731
    dithers = sorted(set(np.asarray(rows['dither_point_index'], int).tolist()))
    sl = Slitlet(slitlet_id=sid_slit, quadrant=q, row=row, open_u={}, target_u={}, ref_u={})
    where = {}
    hit = np.flatnonzero(sky.ids == int(source_id)) if sky is not None else []
    for d in dithers:
        here = rows['dither_point_index'] == d
        sl.open_u[d] = np.unique(np.asarray(rows['shutter_column'][here], int)) * MSA_SLIT_PITCH
        placed = sky.place(q, d) if sky is not None else None
        tr = here & (rows['source_id'] == int(source_id)) & (rows['background'] == 'N')
        if tr.any():
            k = int(np.flatnonzero(tr)[0])
            sl.ref_u[d] = int(rows['shutter_column'][k]) * MSA_SLIT_PITCH
            sl.target_u[d] = (float(placed[1][hit[0]]) * MSA_SLIT_PITCH if placed is not None and len(hit)
                              else listed(rows[k], ys[k]))
        if placed is not None:
            r, c = placed
            u = c * MSA_SLIT_PITCH
            near = np.min(np.abs(u[:, None] - sl.open_u[d][None, :]), axis=1) < OPEN_HALF + LIGHT_PAD
            for i in np.flatnonzero(near & (np.abs(r - row) < ROW_REACH) & (sky.ids != int(source_id))):
                where.setdefault(int(sky.ids[i]), {})[d] = float(u[i])
        for k in np.flatnonzero(here & (rows['background'] == 'N') & (np.asarray(rows['source_id']) > 0)
                                & (rows['source_id'] != int(source_id))):
            s = int(rows['source_id'][k])
            if placed is None or s not in sky.ids:
                where.setdefault(s, {})[d] = listed(rows[k], ys[k])
    return sl, [Neighbour(s, w) for s, w in sorted(where.items())]


# ---------------------------------------------------------------------------
# Which partners hold a neighbour on the target
# ---------------------------------------------------------------------------
@dataclass
class NodDecision:
    root: str
    nod: int
    partners: list               # nods subtracted by default
    keep: list                   # nods kept
    dirty: dict = field(default_factory=dict)    # partner nod -> neighbours (ids) near the target there
    why: str = ''                # 'dropped' | 'faint' | 'no clear background' | ''


def dirty_partners(slit, neighbours, nod_dither, partners, max_distance=MAX_DISTANCE):
    """Per nod: {partner nod: [neighbours that the partner's frame puts within
    ``max_distance`` of the target's position in the nod]}. ``nod_dither`` {nod: dither
    point}; ``partners`` {nod: [nods subtracted by default]}. A nod whose dither does not
    hold the target is skipped."""
    out = {}
    for nod, parts in partners.items():
        t = slit.target_u.get(nod_dither[nod])
        if t is None:
            continue
        d = {}
        for j in parts:
            ids = [n.source_id for n in neighbours
                   if (u := n.where.get(nod_dither[j])) is not None and abs(u - t) < max_distance]
            if ids:
                d[j] = ids
        if d:
            out[nod] = d
    return out


BIN_COLS = 20                # columns per bin along the trace (a single column's sky is too noisy)


def light_over_sky(sci, sky, ys, centre, bin_cols=BIN_COLS, use=None):
    """A source's light over the background in one shutter height (``|ys - centre| < 0.5``),
    summed over those rows and over bins of ``bin_cols`` columns: (median, 90th
    percentile) over the bins where the background is positive. ``use`` (bool, the
    cutout's shape) limits the pixels, e.g. ``usable_pixels``."""
    inside = np.isfinite(ys) & (np.abs(ys - centre) < OPEN_HALF) & np.isfinite(sci) & np.isfinite(sky)
    if use is not None:
        inside &= use
    light = np.sum(np.where(inside, sci - sky, 0.0), axis=0)
    base = np.sum(np.where(inside, sky, 0.0), axis=0)
    edges = np.arange(0, light.size, bin_cols)
    lb, bb = np.add.reduceat(light, edges), np.add.reduceat(base, edges)
    nb = np.add.reduceat((inside.sum(axis=0) > 0).astype(int), edges)
    ok = (nb >= bin_cols // 2) & (bb > 0)
    if ok.sum() < 3:
        return np.nan, np.nan
    r = lb[ok] / bb[ok]
    return float(np.median(r)), float(np.percentile(r, 90))


def usable_pixels(ys, open_centres):
    """Pixels a brightness is measured on: inside one of the slitlet's open shutters
    (``|ys - centre| < OPEN_HALF``). The cutout's margin beyond them has almost no
    background, so a ratio there is unstable (634547 on p1 read 0.28 of the background
    against 0.004 in its own exposures). Leaving out the pixels other slitlets' sources
    light was tried and dropped: on p1 G395M it left 49 of 115 neighbors unmeasured
    (18 before) without better agreement."""
    ys = np.asarray(ys, float)
    return np.min(np.abs(np.where(np.isfinite(ys), ys, np.inf)[..., None]
                         - np.asarray(open_centres, float)[None, None, :]), axis=-1) < OPEN_HALF


def model_light(pass1, key, y0, x0, shape, pathloss):
    """A source's modeled light on a cutout: its pass-1 frame of exposure ``key``
    (``profile_contam.Pass1``, the joint first pass's spectrum at its shape, other
    slitlets' light separated) placed on the cutout whose 0-based detector corner is
    (``y0``, ``x0``), in the canonical's units (divided by the point-source ``pathloss``
    the frame image has removed). Pixels past the cutouts (``ext``, count-rate units) are
    left out. None when the pass 1 has no frame of that exposure."""
    fr = None if pass1 is None else pass1.frames.get(key)
    if fr is None:
        return None
    out = np.zeros(shape)
    iy, ix = np.unravel_index(np.asarray(fr['idx']), tuple(int(v) for v in fr['shape']))
    ty, tx = int(fr['origin'][0]) - 1 + iy - y0, int(fr['origin'][1]) - 1 + ix - x0
    ok = (ty >= 0) & (ty < shape[0]) & (tx >= 0) & (tx < shape[1])
    if 'ext' in fr:
        ok &= ~np.asarray(fr['ext'], bool)
    np.add.at(out, (ty[ok], tx[ok]), np.asarray(fr['img'], float)[ok])
    pl = np.asarray(pathloss, float)
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(np.isfinite(pl) & (pl > 0), out / pl, 0.0)


def measure(neighbour, slit, frames, model_of=None):
    """Fill the neighbour's ``sky_frac`` from the target's own frames before subtraction:
    ``frames`` {dither: (sci, sky, ys, has_target)}.

    With ``model_of`` (dither -> the neighbour's modeled light on that frame, or None;
    ``model_light``), the light is the model's: other slitlets' light is not counted
    and the target's is not in it, so any frame holding the target serves. On p1 G395M
    the data measure read 3x the model where another slitlet lights the band, while the
    model agreed with the neighbours' PRISM spectra (2 of 17 beyond 3 sigma, against 10
    of 17 for the shipped G395M spectra). Without a model the data measure is used: the
    frames that put the neighbour at least ``MEASURE_SEP`` from the target. Either way
    the frames with the neighbour inside an open shutter come first, then the farthest,
    on ``usable_pixels`` only; the first that gives a value is used."""
    cands = []
    for d, (sci, sky, ys, has_target) in frames.items():
        u = neighbour.where.get(d)
        if u is None or not has_target or d not in slit.target_u or d not in slit.ref_u:
            continue
        model = model_of(d) if model_of is not None else None
        sep = abs(u - slit.target_u[d])
        if model is None and sep < MEASURE_SEP:
            continue
        inside = bool(np.min(np.abs(u - slit.open_u[d])) < OPEN_HALF)
        cands.append(((model is not None, inside, sep), d, model))
    for _, d, model in sorted(cands, key=lambda c: c[0], reverse=True):
        sci, sky, ys, _ = frames[d]
        use = usable_pixels(ys, slit.open_u[d] - slit.ref_u[d])
        light = sci if model is None else sky + model
        med, hi = light_over_sky(light, sky, ys, neighbour.where[d] - slit.ref_u[d], use=use)
        if np.isfinite(med):
            neighbour.sky_frac, neighbour.sky_frac_hi = med, hi
            neighbour.measured_in = f'dither {d}' + (' (model)' if model is not None else '')
            break
    return neighbour


def decide(root, slit, neighbours, nod_dither, partners, min_sky_frac=MIN_SKY_FRAC, max_distance=MAX_DISTANCE):
    """``NodDecision`` per nod. A partner is dropped when it puts on the target a
    neighbour at or above ``min_sky_frac`` (or one whose brightness could not be
    measured) and a partner remains; when none would, the nod keeps its partners
    (``no clear background``)."""
    bright = {n.source_id for n in neighbours if not (n.sky_frac < min_sky_frac)}   # NaN counts as bright
    dirty = dirty_partners(slit, neighbours, nod_dither, partners, max_distance)
    out = []
    for nod in sorted(partners):
        parts = sorted(partners[nod])
        d = dirty.get(nod, {})
        bad = [j for j, ids in d.items() if set(ids) & bright]
        keep = [j for j in parts if j not in bad]
        why = ''
        if d and not bad:
            why = 'faint'
        elif bad and not keep:
            keep, why = parts, 'no clear background'
        elif bad:
            why = 'dropped'
        out.append(NodDecision(root=root, nod=nod, partners=parts, keep=keep, dirty=d, why=why))
    return out


# ---------------------------------------------------------------------------
# One source's background groups
# ---------------------------------------------------------------------------
@dataclass
class Group:
    """One background group (a root and detector) of a source, measured."""
    root: str
    slit: Slitlet
    neighbours: list             # [Neighbour], brightness filled where measurable
    nod_dither: dict             # nod -> dither point
    partners: dict               # nod -> nods subtracted by default
    target_frac: float = np.nan  # the target's own light over the background in its shutter


def scene_for(cal_file, msa_dir, source_id, cache):
    """``scene`` of a canonical's source in its MSA configuration (tables and fits cached
    in ``cache`` per metafile and configuration)."""
    from astropy.io import fits
    from astropy.table import Table
    h = fits.getheader(cal_file)
    key = (os.path.join(msa_dir, str(h['MSAMETFL'])), int(h['MSAMETID']))
    if key not in cache:
        shut = Table.read(key[0], hdu='SHUTTER_INFO')
        try:
            src = Table.read(key[0], hdu='SOURCE_INFO')
            sky = SkyToMsa.fit(shut, src, key[1])
        except (KeyError, ValueError):
            sky = None
        cache[key] = (shut, sky)
    shut, sky = cache[key]
    return scene(shut, sky, key[1], source_id)


def measure_group(cal_files, source_id, mbkg_dir, msa_dir, products=None, cache=None, max_distance=MAX_DISTANCE,
                  pass1_for=None):
    """``Group`` of one source's background group (canonicals of one root and detector,
    before or after stage 2b: the frames before subtraction are read from
    ``PRE_BKGSUB_SCI`` when present), or None when its configuration does not hold it.
    The neighbours are measured only when one of them is near the target in a partner
    frame; with ``pass1_for`` (source id -> ``profile_contam.Pass1`` or None, the joint
    first pass of this disperser) from their modeled light (``measure``)."""
    from astropy.io import fits
    from jwst.datamodels import MultiSlitModel
    from . import master_bkg as MB
    from . import profile_contam as CT

    products = {} if products is None else products
    cache = {} if cache is None else cache
    slit, neighbours = scene_for(cal_files[0], msa_dir, source_id, cache)
    if slit is None:
        return None
    heads = [fits.getheader(f) for f in cal_files]
    nods = [int(os.path.basename(f).split('_')[2]) for f in cal_files]
    g = Group(root='_'.join(os.path.basename(cal_files[0]).split('_')[:2]), slit=slit, neighbours=neighbours,
              nod_dither={n: int(h['PATT_NUM']) for n, h in zip(nods, heads)},
              partners={n: [m for m in nods if m != n] for n in nods})
    if not dirty_partners(slit, neighbours, g.nod_dither, g.partners, max_distance):
        return g
    frames, geo = {}, {}
    for f, h, n in zip(cal_files, heads, nods):
        ek = CT.exposure_key(f)
        if ek not in products:
            p = os.path.join(mbkg_dir, f'{ek}_mbkg.fits')
            products[ek] = MB.load_product(p) if os.path.exists(p) else None
        if products[ek] is None:
            continue
        s = MB.sky_for_slit(products[ek], f)
        m = MultiSlitModel(f)
        sl = m.slits[0]
        yy, xx = np.mgrid[:sl.data.shape[0], :sl.data.shape[1]]
        _, ys, _ = sl.meta.wcs.get_transform('detector', 'slit_frame')(xx, yy)
        geo[g.nod_dither[n]] = (ek, int(sl.ystart) - 1, int(sl.xstart) - 1, sl.data.shape, CR.point_pathloss(sl))
        m.close()
        frames[g.nod_dither[n]] = (np.asarray(s['sci'], float), np.asarray(s['sky'], float), np.asarray(ys, float),
                                   str(h.get('SRCFLUX', 'T')).strip().upper() != 'F')
    for nb in neighbours:
        P1 = pass1_for(nb.source_id) if pass1_for is not None else None
        model_of = None if P1 is None else (lambda d, P1=P1: model_light(P1, *geo[d]) if d in geo else None)
        measure(nb, slit, frames, model_of)
    own = [v for v in (light_over_sky(sci, sky, ys, 0.0, use=usable_pixels(ys, slit.open_u[d] - slit.ref_u[d]))[0]
                       for d, (sci, sky, ys, has) in frames.items() if has and d in slit.ref_u)
           if np.isfinite(v)]
    g.target_frac = float(np.median(own)) if own else np.nan
    return g


def combine(groups):
    """One root's detector groups as one: each neighbour's brightness the larger of the
    detectors' (a detector that could not measure it does not count), the nods and
    partners of the group with the most nods. None when no group is given."""
    groups = [g for g in groups if g is not None]
    if not groups:
        return None
    base = max(groups, key=lambda g: len(g.nod_dither))
    nbs = []
    for n in base.neighbours:
        same = [x for g in groups for x in g.neighbours if x.source_id == n.source_id]
        fr = [x.sky_frac for x in same if np.isfinite(x.sky_frac)]
        hi = [x.sky_frac_hi for x in same if np.isfinite(x.sky_frac_hi)]
        nbs.append(Neighbour(n.source_id, n.where, max(fr) if fr else np.nan, max(hi) if hi else np.nan,
                             next((x.measured_in for x in same if x.measured_in), '')))
    tf = [g.target_frac for g in groups if np.isfinite(g.target_frac)]
    return Group(root=base.root, slit=base.slit, neighbours=nbs, nod_dither=base.nod_dither,
                 partners=base.partners, target_frac=max(tf) if tf else np.nan)


def layer(hand, slitlet):
    """One source's overrides in one root, layered: a hand entry wins, then the
    catalogued neighbours. ``hand`` {nod (str): nods} is the source's entry of the hand
    file, ``slitlet`` {nod (str): (nods, reason)} from ``root_overrides``. Returns
    (overrides {nod: nods}, automatic nods {nod}, reasons {nod: str})."""
    out, auto, why = {}, set(), {}
    for nod in sorted(set(hand) | set(slitlet), key=int):
        if nod in hand:
            out[nod], why[nod] = list(hand[nod]), 'manual'
        else:
            keep, reason = slitlet[nod]
            out[nod], why[nod] = list(keep), reason
            auto.add(nod)
    return out, auto, why


def _names(ids, frac):
    return 'neighbor ' + ','.join(f'{i} at {frac[i]:.3f}' if np.isfinite(frac[i]) else f'{i} unmeasured'
                                   for i in ids) + ' of background'


def pass1_reader(workspace, prefix):
    """Source id -> its joint-first-pass ``Pass1`` (``{prefix}_{sid}``, e.g.
    ``ember_cosmos_p1_g395m_f290lp``) or None when it has none; cached."""
    from . import profile_contam as CT
    seen = {}

    def read(sid):
        if sid not in seen:
            path = CT.pass1_path(workspace, f'{prefix}_{int(sid)}')
            seen[sid] = CT.Pass1.read(path) if os.path.exists(path) else None
        return seen[sid]
    return read


def root_overrides(groups, source_id, workspace, min_sky_frac=MIN_SKY_FRAC, max_distance=MAX_DISTANCE,
                   pass1_prefix=None):
    """The overrides of one source in one exposure root, over its detector groups
    (lists of canonical paths): (root, {nod (str): (kept nods, reason)} for the nods
    whose partners change, {nod (str): flag} for the nods with a bright neighbour from
    every partner, the combined ``Group`` or None). The reason names the neighbours and
    their light over the background, e.g. ``'neighbor 634547 at 0.031 of background'``; the flag
    starts ``'every background exposure: '``. With ``pass1_prefix`` the neighbours'
    brightness comes from the joint first pass (``pass1_reader``, stage 3)."""
    products, cache = {}, {}
    pass1_for = pass1_reader(workspace, pass1_prefix) if pass1_prefix else None
    g = combine([measure_group(files, source_id, workspace, workspace, products, cache, max_distance, pass1_for)
                 for files in groups])
    if g is None:
        return None, {}, {}, None
    frac = {n.source_id: n.sky_frac for n in g.neighbours}
    out, flags = {}, {}
    for d in decide(g.root, g.slit, g.neighbours, g.nod_dither, g.partners, min_sky_frac, max_distance):
        if d.why == 'dropped':
            out[str(d.nod)] = (d.keep, _names(sorted({i for j in set(d.partners) - set(d.keep) for i in d.dirty[j]}), frac))
        elif d.why == 'no clear background':
            flags[str(d.nod)] = 'every background exposure: ' + _names(sorted({i for ids in d.dirty.values() for i in ids
                                                                      if not (frac[i] < min_sky_frac)}), frac)
    return g.root, out, flags, g
