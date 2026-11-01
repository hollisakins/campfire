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
MIN_SKY_FRAC = 0.05          # default threshold: the neighbour's light / the sky in its shutter


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


def source_decisions(cal_files, source_id, mbkg_dir, msa_dir, min_sky_frac=MIN_SKY_FRAC, products=None, layouts=None):
    """The decisions of one source's background group (canonicals of one root and
    detector, before or after stage 2b: the frames before subtraction are read from
    ``PRE_BKGSUB_SCI`` when present). Returns (list of ``NodDecision``, [Neighbour], the
    target's own light over the sky in its shutter, NaN when not measured)."""
    from astropy.io import fits
    from astropy.table import Table
    from jwst.datamodels import MultiSlitModel
    from . import master_bkg as MB
    from . import profile_contam as CT

    products = {} if products is None else products
    layouts = {} if layouts is None else layouts
    heads = [fits.getheader(f) for f in cal_files]
    msa_file = os.path.join(msa_dir, str(heads[0]['MSAMETFL']))
    key = (msa_file, int(heads[0]['MSAMETID']))
    if key not in layouts:
        layouts[key] = slitlet_layout(Table.read(msa_file, hdu='SHUTTER_INFO'), key[1])
    target, neighbours = neighbours_of(layouts[key], source_id)
    if target is None:
        return [], [], np.nan
    nods = [int(os.path.basename(f).split('_')[2]) for f in cal_files]
    nod_dither = {n: int(h['PATT_NUM']) for n, h in zip(nods, heads)}
    partners = {n: [m for m in nods if m != n] for n in nods}
    if not dirty_partners(target, neighbours, nod_dither, partners):
        return decide('_'.join(os.path.basename(cal_files[0]).split('_')[:2]), target, neighbours, nod_dither,
                      partners, min_sky_frac), neighbours, np.nan
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
        frames[nod_dither[n]] = (np.asarray(s['sci'], float), np.asarray(s['sky'], float), np.asarray(ys, float),
                                 str(h.get('SRCFLUX', 'T')).strip().upper() != 'F')
    for nb in neighbours:
        measure(nb, target, frames)
    own = [light_over_sky(sci, sky, ys, 0.0)[0] for sci, sky, ys, has in frames.values() if has]
    target_frac = float(np.nanmedian(own)) if own else np.nan
    root = '_'.join(os.path.basename(cal_files[0]).split('_')[:2])
    return decide(root, target, neighbours, nod_dither, partners, min_sky_frac), neighbours, target_frac
