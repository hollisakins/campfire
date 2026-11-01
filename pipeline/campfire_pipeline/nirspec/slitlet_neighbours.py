"""Catalogued neighbours in a source's own slitlet: which nod partners to drop (prototype).

The MSA metadata (``SHUTTER_INFO``) lists, for every shutter of a slitlet at every
dither point, the catalogue source in it. A slitlet can hold two catalogued sources;
when a partner frame puts the other one in the source's main shutter, subtracting that
frame puts a negative copy of it on the source. Stage 2b then drops the partner, as a
hand override would, provided

* the neighbour is bright enough to matter: its light in its own shutter is at least
  ``min_sky_frac`` of the sky there (the frame before subtraction against the master
  background, median over the spectrum), and
* another partner remains (otherwise the nod keeps its partners and is reported).

Overlap with other slitlets' traces is not this module's job (the joint model's
``fnu_contam``). Neighbours the catalogue does not list are the residual scan's
(``nod_scan``).

Coordinates: a slitlet keeps one MSA shutter row and runs along shutter columns; in a
frame's slit coordinates (centred on the source's shutter at that dither) a shutter
``dc`` columns away is centred at ``dc * MSA_SLIT_PITCH``, and the estimated position
in a shutter (0 to 1) is ``y - 0.5`` from its centre.
"""
import os
from dataclasses import dataclass, field

import numpy as np

from .constants import MSA_SLIT_PITCH

OPEN_HALF = 0.5
MIN_SKY_FRAC = 0.02          # default threshold: the neighbour's light / the sky in its shutter


@dataclass
class Placement:
    column: int                  # MSA shutter column
    y: float                     # estimated position in the shutter, 0 to 1 (NaN if not given)


@dataclass
class Neighbour:
    """One catalogued source sharing a slitlet with the target."""
    source_id: int
    where: dict                  # dither -> Placement (dithers where it is in an open shutter)
    sky_frac: float = np.nan     # its light / the sky in its shutter (median over the spectrum)
    sky_frac_hi: float = np.nan  # the same, 90th percentile (a line-dominated neighbour)
    measured_in: str = ''        # frame the brightness was measured in


@dataclass
class NodDecision:
    root: str
    nod: int
    partners: list               # nods subtracted by default
    keep: list                   # nods kept
    dirty: dict = field(default_factory=dict)    # partner nod -> neighbours (ids) in this nod's shutter there
    why: str = ''                # 'dropped' | 'faint' | 'no clean partner' | ''


def slitlet_layout(shutter_table, msa_metadata_id):
    """{slitlet id: {source id: {dither: Placement}}} of one configuration, for the
    slitlets whose open shutters hold more than one real catalogue source."""
    from .metafile import _source_rows
    t = shutter_table[shutter_table['msa_metadata_id'] == int(msa_metadata_id)]
    t = t[_source_rows(t) & (t['background'] == 'N')]
    out = {}
    for s in np.unique(t['slitlet_id']):
        r = t[t['slitlet_id'] == s]
        if np.unique(r['source_id']).size < 2:
            continue
        d = {}
        ys = np.ma.filled(np.ma.asarray(r['estimated_source_in_shutter_y'], dtype=float), np.nan)
        for row, y in zip(r, ys):
            d.setdefault(int(row['source_id']), {})[int(row['dither_point_index'])] = Placement(
                int(row['shutter_column']), float(y))
        out[int(s)] = d
    return out


def neighbours_of(layout, source_id):
    """(the target's {dither: Placement}, [Neighbour]) in its slitlet, or (None, [])."""
    for srcs in layout.values():
        if int(source_id) in srcs:
            return srcs[int(source_id)], [Neighbour(int(o), w) for o, w in sorted(srcs.items()) if o != int(source_id)]
    return None, []


def dirty_partners(target, neighbours, nod_dither, partners):
    """Per nod: {partner nod: [neighbour ids in the target's main shutter of that nod,
    in the partner's frame]}. ``nod_dither`` {nod: dither point}; ``partners`` {nod:
    [nods subtracted by default]}. A nod whose dither does not hold the target has no
    main shutter and is skipped."""
    out = {}
    for nod, parts in partners.items():
        here = target.get(nod_dither[nod])
        if here is None:
            continue
        d = {}
        for j in parts:
            ids = [n.source_id for n in neighbours
                   if (p := n.where.get(nod_dither[j])) is not None and p.column == here.column]
            if ids:
                d[j] = ids
        if d:
            out[nod] = d
    return out


def offset_in_frame(target, neighbour_at, dither):
    """Centre (slit units) of the neighbour's shutter in the frame of ``dither``, which is
    centred on the target's shutter there."""
    return (neighbour_at.column - target[dither].column) * MSA_SLIT_PITCH


BIN_COLS = 20                # columns per bin along the trace (a single column's sky is too noisy)


def light_over_sky(sci, sky, ys, centre, bin_cols=BIN_COLS):
    """A source's light over the sky in a shutter (``|ys - centre| < 0.5``), summed
    over the shutter's rows and over bins of ``bin_cols`` columns: (median, 90th
    percentile) over the bins where the sky is positive."""
    inside = np.isfinite(ys) & (np.abs(ys - centre) < OPEN_HALF) & np.isfinite(sci) & np.isfinite(sky)
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


def measure(neighbour, target, frames):
    """Fill the neighbour's ``sky_frac`` from the target's own frames before subtraction:
    ``frames`` {dither: (sci, sky, ys, has_target)}. Uses the frame where the
    neighbour is in an open shutter farthest from the target's (the target's wing
    least in it), never one where they share a shutter."""
    best = None
    for d, (sci, sky, ys, has_target) in frames.items():
        at = neighbour.where.get(d)
        if at is None or not has_target or d not in target or at.column == target[d].column:
            continue
        sep = abs(at.column - target[d].column)
        if best is None or sep > best[0]:
            best = (sep, d)
    if best is None:
        return neighbour
    d = best[1]
    sci, sky, ys, _ = frames[d]
    neighbour.sky_frac, neighbour.sky_frac_hi = light_over_sky(sci, sky, ys, offset_in_frame(target, neighbour.where[d], d))
    neighbour.measured_in = f'dither {d}'
    return neighbour


def decide(root, target, neighbours, nod_dither, partners, min_sky_frac=MIN_SKY_FRAC):
    """``NodDecision`` per nod with at least two default partners. A partner is dropped
    when it holds, in the nod's main shutter, a neighbour at or above ``min_sky_frac``
    (or one whose brightness could not be measured) and a partner remains."""
    bright = {n.source_id for n in neighbours if not (n.sky_frac < min_sky_frac)}   # NaN counts as bright
    dirty = dirty_partners(target, neighbours, nod_dither, partners)
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
            keep, why = parts, 'no clean partner'
        elif bad:
            why = 'dropped'
        out.append(NodDecision(root=root, nod=nod, partners=parts, keep=keep, dirty=d, why=why))
    return out


@dataclass
class Group:
    """One background group (a root and detector) of a source, measured."""
    root: str
    target: dict                 # dither -> Placement
    neighbours: list             # [Neighbour], brightness filled where measurable
    nod_dither: dict             # nod -> dither point
    partners: dict               # nod -> nods subtracted by default
    target_frac: float = np.nan  # the target's own light over the sky in its shutter


def layout_for(cal_file, msa_dir, layouts):
    """The shared-slitlet layout of a canonical's MSA configuration (cached in ``layouts``)."""
    from astropy.io import fits
    from astropy.table import Table
    h = fits.getheader(cal_file)
    key = (os.path.join(msa_dir, str(h['MSAMETFL'])), int(h['MSAMETID']))
    if key not in layouts:
        layouts[key] = slitlet_layout(Table.read(key[0], hdu='SHUTTER_INFO'), key[1])
    return layouts[key]


def measure_group(cal_files, source_id, mbkg_dir, msa_dir, products=None, layouts=None):
    """``Group`` of one source's background group (canonicals of one root and detector,
    before or after stage 2b: the frames before subtraction are read from
    ``PRE_BKGSUB_SCI`` when present), or None when the source shares no slitlet. The
    neighbours are measured only when one of them falls in a partner frame."""
    from astropy.io import fits
    from jwst.datamodels import MultiSlitModel
    from . import master_bkg as MB
    from . import profile_contam as CT

    products = {} if products is None else products
    layouts = {} if layouts is None else layouts
    target, neighbours = neighbours_of(layout_for(cal_files[0], msa_dir, layouts), source_id)
    if target is None:
        return None
    heads = [fits.getheader(f) for f in cal_files]
    nods = [int(os.path.basename(f).split('_')[2]) for f in cal_files]
    g = Group(root='_'.join(os.path.basename(cal_files[0]).split('_')[:2]), target=target, neighbours=neighbours,
              nod_dither={n: int(h['PATT_NUM']) for n, h in zip(nods, heads)},
              partners={n: [m for m in nods if m != n] for n in nods})
    if not dirty_partners(target, neighbours, g.nod_dither, g.partners):
        return g
    frames = {}
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
        m.close()
        frames[g.nod_dither[n]] = (np.asarray(s['sci'], float), np.asarray(s['sky'], float), np.asarray(ys, float),
                                   str(h.get('SRCFLUX', 'T')).strip().upper() != 'F')
    for nb in neighbours:
        measure(nb, target, frames)
    own = [v for v in (light_over_sky(sci, sky, ys, 0.0)[0] for sci, sky, ys, has in frames.values() if has)
           if np.isfinite(v)]
    g.target_frac = float(np.median(own)) if own else np.nan
    return g


def source_decisions(cal_files, source_id, mbkg_dir, msa_dir, min_sky_frac=MIN_SKY_FRAC, products=None, layouts=None):
    """The decisions of one background group alone: (list of ``NodDecision``,
    [Neighbour], the target's own light over the sky), or ([], [], NaN)."""
    g = measure_group(cal_files, source_id, mbkg_dir, msa_dir, products, layouts)
    if g is None:
        return [], [], np.nan
    return decide(g.root, g.target, g.neighbours, g.nod_dither, g.partners, min_sky_frac), g.neighbours, g.target_frac


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
    return Group(root=base.root, target=base.target, neighbours=nbs, nod_dither=base.nod_dither,
                 partners=base.partners, target_frac=max(tf) if tf else np.nan)


def layer(effective, scan_nods, slitlet):
    """One source's overrides in one root, layered: a hand entry wins, then the
    catalogued neighbours, then the neighbour scan's file. ``effective`` {nod (str):
    nods} is the hand file over the scan's (``Observation.effective_bkg_overrides``),
    ``scan_nods`` the nods of it that came from the scan, ``slitlet`` {nod (str):
    (nods, reason)} from ``root_overrides``. Where the scan also changed a nod the
    catalogue changed, the partners both keep are used (the catalogue's if none).
    Returns (overrides {nod: nods}, automatic nods {nod}, reasons {nod: str})."""
    out, auto, why = {}, set(), {}
    for nod in sorted(set(effective) | set(slitlet), key=int):
        if nod in effective and nod not in scan_nods:
            out[nod], why[nod] = list(effective[nod]), 'hand'
        elif nod in slitlet:
            keep, reason = slitlet[nod]
            if nod in effective:
                both = [j for j in keep if j in effective[nod]]
                keep, reason = (both, reason + '; scan') if both else (keep, reason)
            out[nod], why[nod] = list(keep), reason
            auto.add(nod)
        else:
            out[nod], why[nod] = list(effective[nod]), 'scan'
            auto.add(nod)
    return out, auto, why


def root_overrides(groups, source_id, workspace, min_sky_frac=MIN_SKY_FRAC):
    """The overrides of one source in one exposure root, over its detector groups
    (lists of canonical paths): (root, {nod (str): (kept nods, reason)} for the nods
    whose partners change, [the nods with a bright neighbour in every partner, as
    'root:nod'], the combined ``Group`` or None). The reason names the neighbours and
    their light over the sky, e.g. ``'slitlet 634547 at 0.031 of sky'``."""
    products, layouts = {}, {}
    g = combine([measure_group(files, source_id, workspace, workspace, products, layouts) for files in groups])
    if g is None:
        return None, {}, [], None
    frac = {n.source_id: n.sky_frac for n in g.neighbours}
    out, unresolved = {}, []
    for d in decide(g.root, g.target, g.neighbours, g.nod_dither, g.partners, min_sky_frac):
        if d.why == 'dropped':
            ids = sorted({i for j in set(d.partners) - set(d.keep) for i in d.dirty[j]})
            out[str(d.nod)] = (d.keep, 'slitlet ' + ','.join(
                f'{i} at {frac[i]:.3f}' if np.isfinite(frac[i]) else f'{i} unmeasured' for i in ids) + ' of sky')
        elif d.why == 'no clean partner':
            unresolved.append(f'{g.root}:{d.nod}')
    return g.root, out, unresolved, g
