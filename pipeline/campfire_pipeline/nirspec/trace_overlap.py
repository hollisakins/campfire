"""
Where other slitlets' light lands on a source's slitlet (spectral overlap).

Every open slitlet of an MSA configuration disperses onto the detector. Two slitlets
at nearly the same MSA column but different rows put their spectra on the same
detector rows, offset in wavelength, so light from one lands inside the other's
cutout. Campfire reduces each source with a per-source metafile
(``MetaFile.filter_by_source_id``), so its WCS knows only that source's slitlet.

A ``TraceMap`` records, for one exposure (MSA configuration + dither + detector),
which open slitlets and failed-open shutters illuminate each detector pixel. Master
background step 1 builds it once per exposure from the rate file's geometry, per
shutter, and stores it in the rate file (``ShutterMap``); stage 3 reads it from there
with the stuck-closed shutters removed (``trace_map_from_rate``,
``profile_contam.trace_maps``). Per-source code then looks up its own cutout in it.

The map says WHERE traces share pixels, not whether the shared light matters:
that depends on how bright the other source is at that wavelength, and is measured
from the data (``shared_signal``) or from the other source's own spectrum.

For nod-subtracted data only light that differs between nods matters. A
contaminating slitlet's sky is the same in every nod and cancels; its source moves
from shutter to shutter and does not. So in a given nod the contaminating part of a
slitlet is its source shutter in that nod (``nod_contamination``). Failed-open
shutters carry no catalogue source: their sky cancels too, and they only matter
when a sky object falls in them, which the geometry cannot tell.

Each slitlet is named by the catalogue source it holds at any dither point of the
configuration (``slitlet_sources``). In a nod pattern that moves the source out of a
short slitlet, jwst calls the slitlet background-only at that point and gives it its
slitlet number as source id; the map keeps the source's id there (so the source
never meets its own slitlet as a stranger) and marks no source shutter (the slitlet
holds only sky then, which cancels). A slitlet with no catalogue source at any point
is sky only (``kind = 'sky'``). Shutters listed as stuck closed for the observation
are removed when the map is read: they pass no light.
"""

import hashlib
import os
from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec.constants import MSA_SLIT_PITCH

OPEN_HALF = 0.5      # half-height of a shutter's open area, slit_frame units
LIGHT_PAD = 0.25     # PSF spill beyond the open area (~1 detector row), slit_frame units
NONE = -1
IN_SOURCE = 1        # flag bit: the pixel is in the trace's source shutter (light a nod difference keeps)
PAST_ROWS = 8        # rows added above and below a trace's bounding box where it is followed past its range


# ---------------------------------------------------------------------------
# The configuration: which source each slitlet holds
# ---------------------------------------------------------------------------
def _msa_tables(msa_file):
    from astropy.table import Table
    return Table.read(msa_file, hdu='SHUTTER_INFO'), Table.read(msa_file, hdu='SOURCE_INFO')


def slitlet_sources(msa_file, msa_metadata_id, dither_position):
    """Per slitlet of a configuration: (source id, source in it at this dither point).

    The source id is the catalogue source the slitlet holds at any dither point (the
    primary source where jwst marks one, the most frequent if it changes; never a
    duplicate entry stage 2 drops, ``MetaFile._duplicate_source_ids``), None when it
    holds none at any point (sky only; virtual sources included: stage 2 reduces no such
    source). The flag is True when a shutter of the slitlet holds the source at this
    dither point (jwst's main shutter, ``background = 'N'``): a nod pattern can move the
    source out of a short slitlet, which then holds only sky at that point."""
    from campfire_pipeline.nirspec.metafile import MetaFile, _source_rows

    shutters, sources = _msa_tables(msa_file)
    t = shutters[shutters['msa_metadata_id'] == int(msa_metadata_id)]
    dup = MetaFile(None, msa_file, int(msa_metadata_id), shutter_table=shutters,
                   source_table=sources)._duplicate_source_ids()
    real = _source_rows(t) & ~np.isin(t['source_id'], sorted(dup))
    here = t['dither_point_index'] == int(dither_position)
    out = {}
    for s in np.unique(t['slitlet_id']):
        mine = t['slitlet_id'] == s
        r = t[mine & real]
        sid = None
        if len(r):
            prim = r[r['primary_source'] == 'Y']
            vals, counts = np.unique((prim if len(prim) else r)['source_id'], return_counts=True)
            sid = int(vals[np.argmax(counts)])
        lit = bool(np.any(mine & here & (t['background'] == 'N')))
        out[int(s)] = (sid, lit and sid is not None)
    return out


@dataclass
class TraceMap:
    """Which open slitlets (and failed-open shutters) illuminate each detector pixel of one exposure.

    A trace lights the pixels within an open shutter's area padded by ``pad`` for the
    PSF. ``index[l]`` is the l-th trace (``slits`` row, ``NONE`` past the last) on each
    pixel and ``flag[l]`` whether the pixel is in that trace's source shutter
    (``IN_SOURCE``); at most a handful of traces share a pixel (6 in a crowded G395M
    mask), so the layers beyond the first are nearly empty and compress away. A
    trace's slit position and wavelength at a pixel are not stored: they are in that
    source's own cutout (WCS and WAVELENGTH), which covers its whole slitlet."""
    slits: object                 # astropy Table, one row per trace
    index: np.ndarray             # int16 (nlayer, ny, nx): slits row or NONE
    flag: np.ndarray              # uint8 (nlayer, ny, nx): IN_SOURCE bit
    meta: dict = field(default_factory=dict)

    @property
    def count(self):
        """Number of traces lighting each pixel."""
        return (self.index >= 0).sum(axis=0).astype(np.uint8)

    @classmethod
    def from_traces(cls, traces, shape, pad=LIGHT_PAD, meta=None):
        """Assemble a map from ``(record, open shutter centres, (pix, ys, lam))`` per
        trace (synthetic maps in the tests; the pipeline's come from ``ShutterMap.trace_map``);
        a trace lights the pixels within ``OPEN_HALF + pad`` of one of its open shutters, and
        those near its source shutter are ``IN_SOURCE`` unless the record says the source is
        not in the slitlet at this point (``lit`` false, or a sky slitlet)."""
        from astropy.table import Table

        ny, nx = shape
        recs, pix, idx, flg = [], [], [], []
        for i, (rec, centres, px) in enumerate(traces):
            p, y = px[0], np.asarray(px[1])
            near = np.min(np.abs(y[:, None] - np.asarray(centres)[None, :]), axis=1) < OPEN_HALF + pad
            src = np.abs(y) < OPEN_HALF + pad          # slit frame is centred on the source shutter
            if not rec.get('lit', True) or rec.get('kind') == 'sky':
                src = np.zeros(y.size, bool)
            recs.append(rec)
            pix.append(np.asarray(p, np.int64)[near])
            flg.append(np.where(src[near], IN_SOURCE, 0).astype(np.uint8))
            idx.append(np.full(near.sum(), i, np.int16))
        pix, idx, flg = np.concatenate(pix), np.concatenate(idx), np.concatenate(flg)
        order = np.lexsort((idx, pix))
        pix, idx, flg = pix[order], idx[order], flg[order]
        first = np.r_[0, np.flatnonzero(np.diff(pix)) + 1]          # layer = rank of the trace on its pixel
        layer = np.arange(pix.size) - np.repeat(first, np.diff(np.r_[first, pix.size]))
        nl = int(layer.max()) + 1 if pix.size else 1
        index = np.full((nl, ny * nx), NONE, np.int16)
        flag = np.zeros((nl, ny * nx), np.uint8)
        index[layer, pix] = idx
        flag[layer, pix] = flg
        return cls(slits=Table(recs), index=index.reshape(nl, ny, nx), flag=flag.reshape(nl, ny, nx),
                   meta=dict(meta or {}))

    # -- lookups -----------------------------------------------------------
    def traces_in(self, x0, y0, nx, ny):
        """Every (pixel, trace) pair in a detector region (0-based corner, size).

        Returns flat arrays ``iy, ix`` (region coordinates), ``index`` (slits row)
        and ``flag`` (``IN_SOURCE``: in that trace's source shutter)."""
        NY, NX = self.index.shape[1:]
        ya, xa = slice(max(y0, 0), min(y0 + ny, NY)), slice(max(x0, 0), min(x0 + nx, NX))
        idx, flg = self.index[:, ya, xa], self.flag[:, ya, xa]
        l, jy, jx = np.nonzero(idx >= 0)
        return jy + ya.start - y0, jx + xa.start - x0, idx[l, jy, jx], flg[l, jy, jx]

    def rows_of(self, source_id, kind='slitlet'):
        """Rows of the traces of ``source_id``: its named slitlet and, in a map read from a
        rate file (``ShutterMap.trace_map``), every slitlet that holds it in any shutter at
        any dither point, primary or not, so a second source shares its slitlet as its own."""
        s = self.slits
        mine = np.asarray(s['source_id']) == source_id
        if 'sources' in s.colnames:
            tag = str(int(source_id))
            mine |= np.array([tag in str(v).split(',') for v in s['sources']], bool)
        return np.nonzero(mine & (np.asarray(s['kind']) == kind))[0]

    @staticmethod
    def source_shutter(flag):
        """Pixels of a trace in its source shutter (the light a nod subtraction keeps)."""
        return (np.asarray(flag) & IN_SOURCE) > 0


# ---------------------------------------------------------------------------
# The stored map: per shutter, built once from the rate file's geometry
# ---------------------------------------------------------------------------
CORE = 2             # flag bit: inside the open area of the pixel's nearest open shutter (unpadded)
SHUTTER_MAP_VERSION = '3'   # 2: failed-open shutters padded like any open shutter;
                            # 3: G140M/G235M geometry always on the extended wavelengthrange
SHUTTER_MAP_EXTENSIONS = ('TMSH1', 'TMSH2', 'TMFLAG', 'TMSLITS', 'TMSHUT')


@dataclass
class ShutterMap:
    """The trace map as stored in the rate file: which open shutters light each pixel,
    built from every commanded shutter (no stuck shutter removed), so a stuck-closed list
    applies when it is read (``trace_map``) and editing the list never rebuilds it.

    Per pixel one layer per slitlet whose light reaches it: ``sh1`` the nearest open
    shutter of that slitlet within ``OPEN_HALF + pad`` (a ``shutters`` row), ``sh2`` the
    other one within reach (the padded areas of two neighbouring shutters overlap over the
    bar between them; at most two, since the pad is under half a pitch) or ``NONE``;
    ``flag`` carries ``CORE`` (inside ``sh1``'s open area, unpadded: the footprint a
    count of traces uses, where ``sh1``/``sh2`` padded is the one contamination uses).
    ``slits``: name, kind (``slitlet`` / ``sky`` / ``failed_open``), source_id and lit
    as ``slitlet_sources`` gives them before any stuck shutter is removed. ``shutters``:
    per open shutter its slitlet row, index ``j`` in the shutter state, physical key
    ``q`` / ``row`` / ``col`` (-1 unknown), the SHUTTER_INFO source id, background and
    primary flags at this dither point, ``is_source`` (jwst's source shutter here),
    ``holds_source`` (holds the slitlet's source at any dither point) and ``sources``
    (every catalogue source it holds at any dither point, primary or not, comma
    separated)."""
    slits: object
    shutters: object
    sh1: np.ndarray               # int16 (nlayer, ny, nx)
    sh2: np.ndarray               # int16 (nlayer, ny, nx)
    flag: np.ndarray              # uint8 (nlayer, ny, nx): CORE bit
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_geometry(cls, geo, shutter_info, msa_metadata_id, dither_position, sources=None,
                      pad=LIGHT_PAD, meta=None):
        """The map of one rate file from its ``coverage.RateGeometry`` (every commanded
        slitlet's footprint, past the calibrated range to the cutoffs, and the failed-open
        shutters). ``shutter_info``: the metafile's SHUTTER_INFO rows of the configuration
        (every dither point); ``sources``: ``slitlet_sources`` (None: as jwst names them)."""
        from astropy.table import Table

        ny, nx = geo.shape
        conf = shutter_info[shutter_info['msa_metadata_id'] == int(msa_metadata_id)]
        here = conf[conf['dither_point_index'] == int(dither_position)]
        reach = (OPEN_HALF + pad) / MSA_SLIT_PITCH            # geometry ys counts pitches
        core_half = OPEN_HALF / MSA_SLIT_PITCH
        slits, shut = [], []
        pix_l, s1_l, s2_l, fl_l, sl_l = [], [], [], [], []
        for b in geo.blocks(with_segments=False):
            state, kx = b['state'], b['kx']
            n = len(state)
            opened = np.array([c != '0' for c in state], bool)
            i_slit = len(slits)
            if b['kind'] == 'failed':
                slits.append(dict(name=f'failed_open_{b["name"]}', source_id=0, kind='failed_open', lit=False))
                cols = {}
            else:
                sid_slit = int(b['name']) if str(b['name']).isdigit() else -1
                sid, lit = (int(b['source_id']), True) if sources is None else sources.get(sid_slit, (None, False))
                if sid is None:
                    slits.append(dict(name=str(b['name']), source_id=0, kind='sky', lit=False))
                else:
                    slits.append(dict(name=str(b['name']), source_id=int(sid), kind='slitlet', lit=bool(lit)))
                mr = here[here['slitlet_id'] == sid_slit]
                cols = {}
                if len(mr):
                    c = np.asarray(mr['shutter_column'], int)
                    if n == int(c.max()) - int(c.min()) + 1:
                        cols = {jj: int(c.min()) + jj for jj in range(n)}
            g_of_j = np.full(n + 1, NONE, np.int32)
            for jj in np.flatnonzero(opened):
                jj = int(jj)
                rec = dict(slit=i_slit, j=jj, q=int(b['quadrant']), row=-1, col=-1, source_id=0, background='',
                           primary='', is_source=bool(state[jj] == 'x' and slits[i_slit]['kind'] == 'slitlet'),
                           holds_source=False, sources='')
                if jj in cols:
                    allr = conf[(conf['slitlet_id'] == int(b['name'])) & (conf['shutter_column'] == cols[jj])]
                    rr = here[(here['slitlet_id'] == int(b['name'])) & (here['shutter_column'] == cols[jj])]
                    rec['col'] = cols[jj]
                    if len(rr):
                        r0 = rr[0]
                        rec.update(row=int(r0['shutter_row']), q=int(r0['shutter_quadrant']),
                                   source_id=int(r0['source_id']), background=str(r0['background']).strip(),
                                   primary=str(r0['primary_source']).strip())
                    held = allr[np.char.strip(np.asarray(allr['background'], str)) == 'N']
                    ids = sorted({int(s) for s in held['source_id'] if int(s) > 0})
                    rec['sources'] = ','.join(str(s) for s in ids)
                    rec['holds_source'] = slits[i_slit]['source_id'] in ids
                g_of_j[jj] = len(shut)
                shut.append(rec)
            ys = np.asarray(b['ys'], float)
            k = np.rint(ys); f = ys - k
            ja = (kx + k).astype(int)
            jb = ja + np.where(f >= 0, 1, -1)
            ok_a = (ja >= 0) & (ja < n)
            ok_b = (jb >= 0) & (jb < n)
            va = ok_a & opened[np.clip(ja, 0, n - 1)] & (np.abs(f) < reach)
            vb = ok_b & opened[np.clip(jb, 0, n - 1)] & ((1 - np.abs(f)) < reach)
            ga, gb = g_of_j[np.where(ok_a, ja, n)], g_of_j[np.where(ok_b, jb, n)]
            s1 = np.where(va, ga, np.where(vb, gb, NONE))
            s2 = np.where(va & vb, gb, NONE)
            keep = s1 >= 0
            pix_l.append(np.asarray(b['pix'], np.int64)[keep])
            s1_l.append(s1[keep]); s2_l.append(s2[keep])
            fl_l.append(np.where(va & (np.abs(f) < core_half), CORE, 0)[keep].astype(np.uint8))
            sl_l.append(np.full(int(keep.sum()), i_slit, np.int32))
        cat = lambda xs, dt: np.concatenate(xs).astype(dt) if xs else np.zeros(0, dt)    # noqa: E731
        pix, s1, s2, flg, slt = (cat(pix_l, np.int64), cat(s1_l, np.int32), cat(s2_l, np.int32),
                                 cat(fl_l, np.uint8), cat(sl_l, np.int32))
        order = np.lexsort((slt, pix))
        pix, s1, s2, flg = pix[order], s1[order], s2[order], flg[order]
        first = np.r_[0, np.flatnonzero(np.diff(pix)) + 1] if pix.size else np.zeros(0, int)
        layer = np.arange(pix.size) - np.repeat(first, np.diff(np.r_[first, pix.size]))
        nl = int(layer.max()) + 1 if pix.size else 1
        a1 = np.full((nl, ny * nx), NONE, np.int16); a2 = np.full((nl, ny * nx), NONE, np.int16)
        af = np.zeros((nl, ny * nx), np.uint8)
        a1[layer, pix] = s1; a2[layer, pix] = s2; af[layer, pix] = flg
        names = ('slit', 'j', 'q', 'row', 'col', 'source_id', 'background', 'primary', 'is_source',
                 'holds_source', 'sources')
        # 'sources' as wide as its longest list (a fixed width would cut ids off)
        wsrc = max([64] + [len(r['sources']) for r in shut])
        dtypes = (np.int16, np.int16, np.int16, np.int16, np.int16, np.int64, 'U1', 'U1', bool, bool, f'U{wsrc}')
        st = Table(rows=[tuple(r[c] for c in names) for r in shut], names=names, dtype=dtypes) if shut \
            else Table(names=names, dtype=dtypes)
        sl = Table(rows=[(r['name'], r['source_id'], r['kind'], r['lit']) for r in slits],
                   names=('name', 'source_id', 'kind', 'lit'), dtype=('U32', np.int64, 'U11', bool)) if slits \
            else Table(names=('name', 'source_id', 'kind', 'lit'), dtype=('U32', np.int64, 'U11', bool))
        m = dict(SHMAPVER=SHUTTER_MAP_VERSION, LITPAD=float(pad))
        m.update(meta or {})
        return cls(slits=sl, shutters=st, sh1=a1.reshape(nl, ny, nx), sh2=a2.reshape(nl, ny, nx),
                   flag=af.reshape(nl, ny, nx), meta=m)

    @classmethod
    def from_rate(cls, rate_file, geo, pad=LIGHT_PAD):
        """``from_geometry`` with the configuration the rate file's header names (its
        original metafile, beside it, as ``coverage.RateGeometry`` reads it)."""
        from astropy.io import fits
        from astropy.table import Table
        h = fits.getheader(rate_file)
        msa = os.path.join(os.path.dirname(os.path.abspath(rate_file)), os.path.basename(h.get('OGMETFL') or h['MSAMETFL']))
        mid, dither = int(h['MSAMETID']), int(h.get('PATT_NUM', 0) or 0)
        sources = slitlet_sources(msa, mid, dither)
        meta = dict(EXPOSURE=os.path.basename(rate_file), DETECTOR=h.get('DETECTOR'), GRATING=h.get('GRATING'),
                    FILTER=h.get('FILTER'), MSAMETFL=os.path.basename(msa), MSAMETID=mid, PATT_NUM=dither)
        return cls.from_geometry(geo, Table.read(msa, hdu='SHUTTER_INFO'), mid, dither, sources=sources,
                                 pad=pad, meta=meta)

    # -- persistence: extensions of the rate file -----------------------------
    def hdus(self):
        from astropy.io import fits
        sl = fits.BinTableHDU(self.slits, name='TMSLITS')
        for k, v in self.meta.items():
            sl.header[k] = v
        return [fits.CompImageHDU(self.sh1, name='TMSH1', compression_type='GZIP_2'),
                fits.CompImageHDU(self.sh2, name='TMSH2', compression_type='GZIP_2'),
                fits.CompImageHDU(self.flag, name='TMFLAG', compression_type='GZIP_2'),
                sl, fits.BinTableHDU(self.shutters, name='TMSHUT')]

    @staticmethod
    def empty_hdus():
        """The extensions with nothing in them (a rate file with no map), so every name
        a jwst save may have recorded is always present."""
        from astropy.io import fits
        return [fits.ImageHDU(name='TMSH1'), fits.ImageHDU(name='TMSH2'), fits.ImageHDU(name='TMFLAG'),
                fits.BinTableHDU(name='TMSLITS'), fits.BinTableHDU(name='TMSHUT')]

    @staticmethod
    def present(rate_file):
        """``rate_file`` holds a stored map (headers only)."""
        from astropy.io import fits
        try:
            with fits.open(rate_file, memmap=False) as h:
                return 'TMSLITS' in h and bool(h['TMSLITS'].header.get('NAXIS2', 0))
        except OSError:
            return False

    @classmethod
    def read(cls, rate_file):
        """The map stored in ``rate_file``; None when it holds none."""
        from astropy.io import fits
        from astropy.table import Table
        with fits.open(rate_file, memmap=False) as h:
            if 'TMSLITS' not in h or not h['TMSLITS'].header.get('NAXIS2', 0):
                return None
            hdr = h['TMSLITS'].header
            meta = {k: hdr[k] for k in ('SHMAPVER', 'LITPAD', 'EXPOSURE', 'DETECTOR', 'GRATING', 'FILTER',
                                        'MSAMETFL', 'MSAMETID', 'PATT_NUM') if k in hdr}
            return cls(slits=Table(h['TMSLITS'].data), shutters=Table(h['TMSHUT'].data),
                       sh1=np.asarray(h['TMSH1'].data), sh2=np.asarray(h['TMSH2'].data),
                       flag=np.asarray(h['TMFLAG'].data), meta=meta)

    # -- the map as the consumers read it ---------------------------------------
    def trace_map(self, stuck=None):
        """The ``TraceMap`` with the stuck-closed shutters ``stuck`` ({(q, row, col)},
        e.g. ``stuck_list.stuck_set``) removed: a (pixel, slitlet) survives while one of
        its shutters does; ``IN_SOURCE`` where a surviving shutter is the slitlet's source
        shutter at this point and that shutter is not stuck; a slitlet whose shutters
        holding its source are all stuck becomes ``sky`` (the slitlet keeps its pre-stuck
        source otherwise); ``sources`` lists the catalogue sources its surviving shutters
        hold at any point."""
        sh = self.shutters
        nsh = len(sh)
        keys = list(zip(np.asarray(sh['q'], int), np.asarray(sh['row'], int), np.asarray(sh['col'], int)))
        alive = np.array([k not in (stuck or ()) for k in keys], bool) if nsh else np.zeros(0, bool)
        slit_of = np.asarray(sh['slit'], int)
        slits = self.slits.copy()
        kind = np.asarray(slits['kind']).astype('U11')
        sid = np.asarray(slits['source_id'], np.int64).copy()
        lit = np.asarray(slits['lit'], bool).copy()
        holds = np.asarray(sh['holds_source'], bool) & alive
        is_src = np.asarray(sh['is_source'], bool)
        srcs = []
        for i in range(len(slits)):
            mine = slit_of == i
            if kind[i] == 'slitlet' and np.any(np.asarray(sh['holds_source'], bool)[mine]) and not holds[mine].any():
                kind[i], sid[i], lit[i] = 'sky', 0, False
            if np.any(mine & is_src) and not np.any(mine & is_src & alive):
                lit[i] = False
            ids = sorted({int(s) for row in np.asarray(sh['sources'])[mine & alive] for s in str(row).split(',') if s})
            if kind[i] == 'slitlet' and sid[i] > 0 and sid[i] not in ids:
                ids = [int(sid[i])] + ids
            srcs.append(','.join(str(s) for s in ids))
        slits['kind'] = kind; slits['source_id'] = sid; slits['lit'] = lit
        slits['sources'] = np.array(srcs, dtype='U256') if srcs else np.zeros(0, 'U256')
        src_here = is_src & alive & lit[slit_of] & (kind[slit_of] == 'slitlet') if nsh else np.zeros(0, bool)

        def take(a, idx):
            return np.where(idx >= 0, a[np.clip(idx, 0, max(nsh - 1, 0))], False) if nsh else np.zeros(idx.shape, bool)

        s1, s2 = self.sh1.astype(np.int32), self.sh2.astype(np.int32)
        a1, a2 = take(alive, s1), take(alive, s2)
        first = np.where(a1, s1, np.where(a2, s2, NONE))
        other = np.where(a1 & a2, s2, NONE)
        index = np.where(first >= 0, slit_of[np.clip(first, 0, max(nsh - 1, 0))] if nsh else NONE, NONE).astype(np.int16)
        insrc = take(src_here, first) | take(src_here, other)
        core = ((self.flag & CORE) > 0) & a1
        flag = (np.where(insrc, IN_SOURCE, 0) | np.where(core, CORE, 0)).astype(np.uint8)
        meta = dict(self.meta, STUCK=stuck_signature_keys(stuck))
        return TraceMap(slits=slits, index=index, flag=flag, meta=meta)


def stuck_signature_keys(stuck):
    """Short identity of a set of stuck physical shutters ``{(q, row, col)}``."""
    if not stuck:
        return ''
    s = ';'.join(f'{q},{r},{c}' for q, r, c in sorted(stuck))
    return hashlib.sha1(s.encode()).hexdigest()[:16]


def trace_map_from_rate(rate_file, stuck=None):
    """The trace map stored in ``rate_file`` (master background step 1) with ``stuck``
    removed; None when the rate file holds none."""
    m = ShutterMap.read(rate_file)
    return None if m is None else m.trace_map(stuck)


# ---------------------------------------------------------------------------
# Per-source use
# ---------------------------------------------------------------------------
@dataclass
class Shared:
    """One other trace's light inside a source's nod difference."""
    exposure: int                 # nod (index into the list of maps)
    from_exposure: int            # nod whose light it is (== exposure: added; else subtracted)
    sign: int
    name: str
    source_id: int
    kind: str
    iy: np.ndarray = field(repr=False)        # cutout pixels (region coordinates)
    ix: np.ndarray = field(repr=False)
    target_src: np.ndarray = field(repr=False)   # pixel is in the target's source shutter

    @property
    def n_cols(self):
        return int(np.unique(self.ix).size)

    def in_target_shutter(self):
        return self.target_src


def nod_contamination(maps, source_id, region, partners=None):
    """Other sources' light in each nod difference of ``source_id``.

    ``maps`` are the trace maps of the nods of one configuration and detector, in
    nod order; ``region`` is the source's cutout ``(x0, y0, nx, ny)`` (0-based, e.g.
    SLTSTRT1 - 1, SLTSTRT2 - 1, SLTSIZE1, SLTSIZE2); ``partners[k]`` lists the nods
    subtracted from nod k (default all others, as stage 2b). A contaminant's source
    shutter in nod k adds light to nod k's difference; its source shutter in a
    partner nod subtracts. Failed-open shutters are returned whole (their light is
    unknown); sky slitlets not at all (their sky cancels). Pixels are those the
    target's own trace covers."""
    n = len(maps)
    if partners is None:
        partners = [[j for j in range(n) if j != k] for k in range(n)]
    lut = [m.traces_in(*region) for m in maps]
    out = []
    for k in range(n):
        iy, ix, idx, flg = lut[k]
        own = np.isin(idx, maps[k].rows_of(source_id))
        if not own.any():
            continue
        t_key = iy[own].astype(np.int64) * 100000 + ix[own]
        t_src = maps[k].source_shutter(flg[own])
        for j in [k] + list(partners[k] or []):
            jy, jx, jidx, jflg = lut[j]
            slits = maps[j].slits
            for r in np.unique(jidx):
                rec = slits[int(r)]
                if rec['kind'] == 'sky' or (rec['kind'] == 'slitlet' and int(rec['source_id']) == source_id):
                    continue
                sel = jidx == r
                if rec['kind'] == 'slitlet':
                    sel &= maps[j].source_shutter(jflg)
                if not sel.any():
                    continue
                key = jy[sel].astype(np.int64) * 100000 + jx[sel]
                common, it, _ = np.intersect1d(t_key, key, return_indices=True)
                if common.size == 0:
                    continue
                out.append(Shared(exposure=k, from_exposure=j, sign=1 if j == k else -1,
                                  name=str(rec['name']), source_id=int(rec['source_id']), kind=str(rec['kind']),
                                  iy=(common // 100000).astype(int), ix=(common % 100000).astype(int),
                                  target_src=t_src[it]))
    return out


def shared_signal(data, err, shared, exclude=None):
    """Signal of one contaminant in a nod difference: the error-weighted sum of
    ``sign * data`` over its shared pixels, its error, and the number of pixels.

    ``exclude`` (bool mask on the cutout) drops pixels, e.g. the target's own source
    rows, so the measurement sees only the contaminant. A significant positive value
    means the contaminant's light is really there."""
    m = np.ones(shared.iy.size, bool)
    if exclude is not None:
        m &= ~exclude[shared.iy, shared.ix]
    d, e = data[shared.iy[m], shared.ix[m]], err[shared.iy[m], shared.ix[m]]
    ok = np.isfinite(d) & np.isfinite(e) & (e > 0)
    if not ok.any():
        return np.nan, np.nan, 0
    w = 1 / e[ok] ** 2
    val = np.sum(w * shared.sign * d[ok]) / np.sum(w)
    return float(val), float(1 / np.sqrt(np.sum(w))), int(ok.sum())
