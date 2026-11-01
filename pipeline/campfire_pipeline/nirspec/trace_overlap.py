"""
Where other slitlets' light lands on a source's slitlet (spectral overlap).

Every open slitlet of an MSA configuration disperses onto the detector. Two slitlets
at nearly the same MSA column but different rows put their spectra on the same
detector rows, offset in wavelength, so light from one lands inside the other's
cutout. Campfire reduces each source with a per-source metafile
(``MetaFile.filter_by_source_id``), so its WCS knows only that source's slitlet.

A ``TraceMap`` records, for one exposure (MSA configuration + dither + detector),
which open slitlets and failed-open shutters illuminate each detector pixel. It is
built once per exposure, by running jwst's ``assign_wcs`` with the full metafile
(every open slitlet gets its own WCS) and jwst's ``msaflagopen`` machinery for the
failed-open shutters of the CRDS ``msaoper`` reference file, and cached as FITS.
Per-source code then looks up its own cutout in it.

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
are removed from the configuration before the map is built: they pass no light.
"""

import hashlib
import os
import shutil
from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec.constants import MSA_SLIT_MARGIN, MSA_SLIT_PITCH

OPEN_HALF = 0.5      # half-height of a shutter's open area, slit_frame units
LIGHT_PAD = 0.25     # PSF spill beyond the open area (~1 detector row), slit_frame units
NONE = -1
IN_SOURCE = 1        # flag bit: the pixel is in the trace's source shutter (light a nod difference keeps)
PAST_ROWS = 8        # rows added above and below the bounding box where a trace is followed past its range
MAP_VERSION = '7'


def open_shutter_centres(shutter_state, ymin, ymax, pitch=MSA_SLIT_PITCH, margin=MSA_SLIT_MARGIN):
    """Slit-frame y of the open shutters of a slitlet.

    jwst sets the slit bounds to ``ymin = -(n_below * pitch + margin)`` and
    ``ymax = n_above * pitch + margin`` around the reference (source) shutter, and
    ``shutter_state`` lists the shutters in the same order ('x' = source shutter,
    '1' = open, '0' = closed inside the slitlet). The reference shutter's index in
    the string follows from ``ymin``, so this works for any slitlet length and for
    slitlets with no 'x'."""
    s = str(shutter_state)
    i0 = s.index('x') if 'x' in s else int(round((-ymin - margin) / pitch))
    return np.array([(i - i0) * pitch for i, c in enumerate(s) if c != '0'], float)


# ---------------------------------------------------------------------------
# The configuration: which source each slitlet holds, stuck-closed shutters
# ---------------------------------------------------------------------------
def _msa_tables(msa_file):
    from astropy.table import Table
    return Table.read(msa_file, hdu='SHUTTER_INFO'), Table.read(msa_file, hdu='SOURCE_INFO')


def slitlet_sources(msa_file, msa_metadata_id, dither_position, catalogue_file=None):
    """Per slitlet of a configuration: (source id, source in it at this dither point).

    The source id is the catalogue source the slitlet holds at any dither point (the
    primary source where jwst marks one, the most frequent if it changes; never a
    duplicate entry stage 2 drops, ``MetaFile._duplicate_source_ids``, judged on
    ``catalogue_file``, the metafile before any stuck-closed shutter was removed, as
    stage 2 judges it), None when it holds none at any point (sky only; virtual
    sources included: stage 2 reduces no such source). The flag is True when a shutter
    of the slitlet holds the source at this dither point (jwst's main shutter,
    ``background = 'N'``): a nod pattern can move the source out of a short slitlet,
    which then holds only sky at that point."""
    from campfire_pipeline.nirspec.metafile import MetaFile, _source_rows

    shutters, sources = _msa_tables(msa_file)
    t = shutters[shutters['msa_metadata_id'] == int(msa_metadata_id)]
    cat = (shutters, sources) if catalogue_file is None else _msa_tables(catalogue_file)
    dup = MetaFile(None, msa_file, int(msa_metadata_id), shutter_table=cat[0],
                   source_table=cat[1])._duplicate_source_ids()
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


def drop_stuck_shutters(msa_file, msa_metadata_id, stuck, out_file=None):
    """Remove stuck-closed shutters from a metafile's configuration (in ``out_file``,
    default in place: only ever called on a staged copy).

    stuck : {source id: [shutter numbers]}, the observation's stuck-closed list for this
        configuration's root (``Observation.stuck_closed_shutters``). The numbers count
        the source's slitlet shutters in column order from 1, as stage 2 counts them
        (its per-source metafile: the source's slitlets, detached background shutters
        pruned). Those shutters' rows go at every dither point.
    Returns the number of rows removed."""
    from astropy.io import fits
    from astropy.table import Table
    from campfire_pipeline.nirspec.metafile import _prune_detached_shutters, _source_rows

    import copy

    mid = int(msa_metadata_id)
    with fits.open(msa_file) as h:
        hdus = copy.deepcopy(h)
    t = Table(hdus['SHUTTER_INFO'].data)
    conf = t['msa_metadata_id'] == mid
    drop = np.zeros(len(t), bool)
    for sid, numbers in (stuck or {}).items():
        slits = np.unique(t['slitlet_id'][conf & _source_rows(t, int(sid))])
        if slits.size == 0:
            continue
        rows = _prune_detached_shutters(t[conf & np.isin(t['slitlet_id'], slits)], int(sid))
        cols = np.sort(np.unique(rows['shutter_column']))
        gone = [cols[int(n) - 1] for n in numbers if 1 <= int(n) <= cols.size]
        drop |= conf & np.isin(t['slitlet_id'], slits) & np.isin(t['shutter_column'], gone)
    if drop.any():
        touched = np.unique(t['slitlet_id'][drop])
        t = t[~drop]
        # a dither point whose primary shutter is gone holds no named source: its other
        # main shutters (a second catalogue source, ember_cosmos_p9 slitlet 52) become
        # background, as jwst needs a primary for any main shutter (and the map names a
        # slitlet by one source)
        conf = (t['msa_metadata_id'] == mid) & np.isin(t['slitlet_id'], touched)
        for s, d in {(int(a), int(b)) for a, b in zip(t['slitlet_id'][conf], t['dither_point_index'][conf])}:
            g = conf & (t['slitlet_id'] == s) & (t['dither_point_index'] == d)
            if not np.any(t['primary_source'][g] == 'Y'):
                t['background'][g] = 'Y'
        new = fits.BinTableHDU(t, name='SHUTTER_INFO', header=hdus['SHUTTER_INFO'].header)
        hdus[hdus.index_of('SHUTTER_INFO')] = new
    hdus.writeto(out_file or msa_file, overwrite=True)
    return int(drop.sum())


def stuck_signature(stuck):
    """Short identity of a stuck-closed list (the map's STUCK card: a changed list rebuilds it)."""
    if not stuck:
        return ''
    s = ';'.join(f"{int(k)}:{','.join(str(int(n)) for n in sorted(v))}" for k, v in sorted(stuck.items()))
    return hashlib.sha1(s.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Building: jwst WCS for every open slitlet of the configuration
# ---------------------------------------------------------------------------
def full_mask_model(rate_file, workdir, msa_file=None, msa_metadata_id=None, dither_position=None,
                    return_staged=False, stuck=None):
    """jwst model of one exposure with a WCS for every open slitlet of its configuration.

    ``rate_file`` is copied into ``workdir`` (the original is never modified). It is
    the exposure itself, or a template: any rate file with the same detector,
    disperser and filter, pointed at ``msa_file`` / ``msa_metadata_id`` /
    ``dither_position``. Slitlets keep their relative geometry with a template
    because all traces move together with the grating wheel tilt (measured: a
    template moves no trace on the detector and shifts all wavelengths alike by
    ~12 A in G395M). ``stuck`` (``drop_stuck_shutters``) is applied to the staged
    copy of ``msa_file``. ``return_staged`` also returns the files copied here."""
    from stdatamodels.jwst import datamodels
    from jwst.assign_wcs import AssignWcsStep

    os.makedirs(workdir, exist_ok=True)
    staged = []
    path = os.path.join(workdir, os.path.basename(rate_file))
    if not os.path.exists(path):
        shutil.copy(rate_file, path)
        staged.append(path)
    if msa_file is not None:
        # always a fresh copy: a stuck-closed list edits it
        msa_copy = os.path.join(workdir, os.path.basename(msa_file))
        shutil.copy(msa_file, msa_copy)
        staged.append(msa_copy)
        if stuck:
            drop_stuck_shutters(msa_copy, msa_metadata_id, stuck)
    model = datamodels.open(path)
    if msa_file is not None:
        model.meta.instrument.msa_metadata_file = os.path.basename(msa_file)
    if msa_metadata_id is not None:
        model.meta.instrument.msa_metadata_id = int(msa_metadata_id)
    if dither_position is not None:
        model.meta.dither.position_number = int(dither_position)
    cwd = os.getcwd()
    try:
        os.chdir(workdir)          # assign_wcs resolves the metafile next to the input
        out = AssignWcsStep.call(model)
    finally:
        os.chdir(cwd)
    model.close()
    return (out, staged) if return_staged else out


def _slit_pixels(model, wcs, past_range=False):
    """Detector pixels inside a slit's bounding box, with their slit-frame y and wavelength [um].

    ``past_range``: also the pixels past both ends of the bounding box. jwst's bounding
    box stops at the calibrated wavelength range, but the light does not stop there, at
    the blue end (the filter cut-on is gradual) or the red end; the trace's slit-frame y
    is still defined (the transform extrapolates), so the trace is followed over every
    column, ``PAST_ROWS`` rows wider for its curvature, out to the disperser's
    ``coverage.WAVELENGTH_CUTOFFS`` (the footprint the master background paints, from
    ``coverage.geometry_on_block``). The source's light there is predicted from its
    pass 1 extended in count-rate units (``profile_extend``). Returns (pixel index, y,
    wavelength)."""
    from campfire_pipeline.nirspec import coverage as CV
    (x0, x1), (y0, y1) = wcs.bounding_box
    ny, nx = model.data.shape[-2:]
    tr = wcs.get_transform('detector', 'slit_frame')

    def at(xa, ya):
        if xa.size == 0 or ya.size == 0:
            return None
        y, x = np.meshgrid(ya, xa, indexing='ij')
        _, ys, lam = tr(x, y)
        ok = np.isfinite(ys) & np.isfinite(lam)
        if not ok.any():
            return None
        lam = lam[ok]
        if np.nanmax(lam) < 1e-3:      # metres
            lam = lam * 1e6
        return (y[ok] * nx + x[ok]).astype(np.int64), ys[ok], lam

    xi0, xi1 = max(int(np.ceil(x0)), 0), min(int(np.floor(x1)), nx - 1)
    ya = np.arange(max(int(np.ceil(y0)), 0), min(int(np.floor(y1)), ny - 1) + 1)
    inside = at(np.arange(xi0, xi1 + 1), ya)
    if inside is None:
        return None
    parts = [inside]
    if past_range:
        try:
            cutoffs = CV.cutoffs_for(model.meta.instrument.grating, model.meta.instrument.filter)
        except Exception:                                    # noqa: BLE001 - no disperser: no cutoff
            cutoffs = (None, None)
        g = CV.geometry_on_block(tr, 0, 0, int(np.ceil(y0)) - PAST_ROWS, int(np.floor(y1)) + PAST_ROWS + 1,
                                 '', 1.0, cutoffs, ncols=nx, nrows=ny, with_segments=False)
        px = inside[0] % nx
        yb, xb = np.meshgrid(g['rows'], g['cols'], indexing='ij')
        keep = g['footprint'] & ((xb < px.min()) | (xb > px.max()))
        if keep.any():
            parts.append(((yb[keep] * nx + xb[keep]).astype(np.int64), g['ys'][keep], g['lam'][keep]))
    return tuple(np.concatenate([p[i] for p in parts]) for i in range(3))


def _slit_record(slit, kind, centres, source_id=None, lit=True):
    return dict(name=str(slit.name), source_id=int(slit.source_id if source_id is None else source_id), kind=kind,
                lit=bool(lit), shutter_state=str(slit.shutter_state), ymin=float(slit.ymin), ymax=float(slit.ymax),
                quadrant=int(slit.quadrant), xcen=int(slit.xcen), ycen=int(slit.ycen),
                src_ypos=float(getattr(slit, 'source_ypos', np.nan)),
                centres=','.join(f'{c:.3f}' for c in centres))


def _slitlet_traces(model, sources=None):
    """Every open slitlet's trace, named by ``sources`` (``slitlet_sources``: slitlet id ->
    (source id or None, source in it at this point)) when given, else as jwst names it."""
    from jwst.assign_wcs import nirspec

    for slit in model.meta.wcs.get_transform('gwa', 'slit_frame').slits:
        px = _slit_pixels(model, nirspec.nrs_wcs_set_input(model, slit.name), past_range=True)
        if px is None:
            continue
        centres = open_shutter_centres(slit.shutter_state, slit.ymin, slit.ymax)
        sid, lit = None, True
        if sources is not None:
            try:
                sid, lit = sources[int(slit.name)]
            except (KeyError, ValueError):
                sid, lit = int(slit.source_id), True
        if sources is not None and sid is None:
            yield _slit_record(slit, 'sky', centres, source_id=0, lit=False), centres, px
        else:
            yield _slit_record(slit, 'slitlet', centres, source_id=sid, lit=lit), centres, px


def _failed_open_traces(model):
    """Failed-open shutters as one-shutter slits, via jwst's msaflagopen machinery."""
    from gwcs.wcs import WCS
    from jwst.assign_wcs import nirspec
    from jwst.msaflagopen import msaflag_open
    from jwst.msaflagopen.msaflagopen_step import MSAFlagOpenStep, create_reference_filename_dictionary

    msaoper = MSAFlagOpenStep().get_reference_file(model, 'msaoper')
    if msaoper in (None, 'N/A'):
        return
    slits = msaflag_open.create_slitlets(msaoper)
    tmp = model.copy()
    tmp.meta.exposure.type = 'NRS_MSASPEC'
    tmp.meta.wcs = WCS(nirspec.slitlets_wcs(tmp, create_reference_filename_dictionary(model), slits))
    tmp.meta.wcs.bounding_box = nirspec.generate_compound_bbox(tmp, slits)
    for slit in slits:
        try:
            w = nirspec.nrs_wcs_set_input(tmp, slit.name)
        except (ValueError, KeyError):
            continue
        px = _slit_pixels(tmp, w)            # one shutter, sky only: its light cancels (not followed past the range)
        if px is not None:
            rec = _slit_record(slit, 'failed_open', np.array([0.0]))
            rec['name'] = f'failed_open_q{slit.quadrant}_{slit.xcen}_{slit.ycen}'
            yield rec, np.array([0.0]), px
    tmp.close()


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
    def build(cls, rate_file, workdir, msa_file=None, msa_metadata_id=None, dither_position=None,
              failed_open=True, pad=LIGHT_PAD, stuck=None):
        """The map of one exposure (``full_mask_model``). With ``msa_file``, slitlets are
        named by their source (``slitlet_sources``) and ``stuck`` (the stuck-closed
        shutters of the configuration's root, ``drop_stuck_shutters``) is applied first."""
        model, staged = full_mask_model(rate_file, workdir, msa_file, msa_metadata_id, dither_position,
                                        return_staged=True, stuck=stuck)
        shape = model.data.shape[-2:]
        m = model.meta
        sources = None
        if msa_file is not None:
            sources = slitlet_sources(os.path.join(workdir, os.path.basename(msa_file)),
                                      int(m.instrument.msa_metadata_id), int(m.dither.position_number),
                                      catalogue_file=msa_file)
        traces = list(_slitlet_traces(model, sources))
        if failed_open:
            traces += list(_failed_open_traces(model))
        meta = dict(MAPVER=MAP_VERSION, EXPOSURE=os.path.basename(rate_file), DETECTOR=m.instrument.detector,
                    GRATING=m.instrument.grating, FILTER=m.instrument.filter,
                    MSAMETFL=m.instrument.msa_metadata_file, MSAMETID=int(m.instrument.msa_metadata_id),
                    PATT_NUM=int(m.dither.position_number), LITPAD=pad, FAILOPEN=bool(failed_open),
                    STUCK=stuck_signature(stuck))
        model.close()
        for f in staged:                                 # the copies made for assign_wcs; the map is all we keep
            if os.path.exists(f):
                os.remove(f)
        return cls.from_traces(traces, shape, pad=pad, meta=meta)

    @classmethod
    def from_traces(cls, traces, shape, pad=LIGHT_PAD, meta=None):
        """Assemble a map from ``(record, open shutter centres, (pix, ys, lam))`` per
        trace; a trace lights the pixels within ``OPEN_HALF + pad`` of one of its open
        shutters, and those near its source shutter are ``IN_SOURCE`` unless the record
        says the source is not in the slitlet at this point (``lit`` false, or a sky
        slitlet)."""
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

    # -- persistence -------------------------------------------------------
    def write(self, path):
        from astropy.io import fits
        hdr = fits.Header()
        for k, v in self.meta.items():
            hdr[k] = v
        hdus = fits.HDUList([
            fits.PrimaryHDU(header=hdr),
            fits.CompImageHDU(self.index, name='INDEX', compression_type='GZIP_2'),
            fits.CompImageHDU(self.flag, name='FLAG', compression_type='GZIP_2'),
            fits.BinTableHDU(self.slits, name='SLITS'),
        ])
        hdus.writeto(path, overwrite=True)

    @classmethod
    def read(cls, path):
        from astropy.io import fits
        from astropy.table import Table
        with fits.open(path) as h:
            meta = {k: h[0].header[k] for k in ('MAPVER', 'EXPOSURE', 'DETECTOR', 'GRATING', 'FILTER', 'MSAMETFL',
                                                'MSAMETID', 'PATT_NUM', 'LITPAD', 'FAILOPEN', 'STUCK')
                    if k in h[0].header}
            return cls(slits=Table(h['SLITS'].data), index=np.asarray(h['INDEX'].data),
                       flag=np.asarray(h['FLAG'].data), meta=meta)

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


def trace_map_path(cache_dir, exposure_name):
    root = os.path.basename(exposure_name).replace('_rate.fits', '').replace('.fits', '')
    return os.path.join(cache_dir, f'{root}_tracemap.fits')


def map_is_current(path, stuck=None):
    """A cached map exists, was built by this map version and, when ``stuck`` is given,
    with that stuck-closed list (``stuck_signature``)."""
    from astropy.io import fits
    if not os.path.exists(path):
        return False
    try:
        h = fits.getheader(path)
    except OSError:
        return False
    if str(h.get('MAPVER', '')) != MAP_VERSION:
        return False
    return stuck is None or str(h.get('STUCK', '')) == stuck_signature(stuck)


def get_trace_map(rate_file, cache_dir, workdir=None, exposure_name=None, **build_kw):
    """Cached ``TraceMap`` of one exposure: read it if present, else build and write it.

    ``exposure_name`` names the cache file when ``rate_file`` is a template."""
    path = trace_map_path(cache_dir, exposure_name or rate_file)
    if map_is_current(path, stuck=build_kw.get('stuck') or {}):
        return TraceMap.read(path)
    os.makedirs(cache_dir, exist_ok=True)
    tm = TraceMap.build(rate_file, workdir or os.path.join(cache_dir, 'work'), **build_kw)
    tm.write(path)
    return tm


# ---------------------------------------------------------------------------
# The stored map: per shutter, built once from the rate file's geometry
# ---------------------------------------------------------------------------
CORE = 2             # flag bit: inside the open area of the pixel's nearest open shutter (unpadded)
SHUTTER_MAP_VERSION = '1'
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
        dtypes = (np.int16, np.int16, np.int16, np.int16, np.int16, np.int64, 'U1', 'U1', bool, bool, 'U64')
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
        holding its source are all stuck becomes ``sky``, as ``drop_stuck_shutters`` +
        ``slitlet_sources`` make it (the slitlet keeps its pre-stuck source otherwise);
        ``sources`` lists the catalogue sources its surviving shutters hold at any point."""
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
