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
"""

import os
import shutil
from dataclasses import dataclass, field

import numpy as np

from campfire_pipeline.nirspec.constants import MSA_SLIT_MARGIN, MSA_SLIT_PITCH

OPEN_HALF = 0.5      # half-height of a shutter's open area, slit_frame units
LIGHT_PAD = 0.25     # PSF spill beyond the open area (~1 detector row), slit_frame units
NONE, SHARED = -1, -2
MAP_VERSION = '1'


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
# Building: jwst WCS for every open slitlet of the configuration
# ---------------------------------------------------------------------------
def full_mask_model(rate_file, workdir, msa_file=None, msa_metadata_id=None, dither_position=None,
                    return_staged=False):
    """jwst model of one exposure with a WCS for every open slitlet of its configuration.

    ``rate_file`` is copied into ``workdir`` (the original is never modified). It is
    the exposure itself, or a template: any rate file with the same detector,
    disperser and filter, pointed at ``msa_file`` / ``msa_metadata_id`` /
    ``dither_position``. Slitlets keep their relative geometry with a template
    because all traces move together with the grating wheel tilt (measured: a
    template moves no trace on the detector and shifts all wavelengths alike by
    ~12 A in G395M). ``return_staged`` also returns the files copied here."""
    from stdatamodels.jwst import datamodels
    from jwst.assign_wcs import AssignWcsStep

    os.makedirs(workdir, exist_ok=True)
    staged = []
    path = os.path.join(workdir, os.path.basename(rate_file))
    if not os.path.exists(path):
        shutil.copy(rate_file, path)
        staged.append(path)
    if msa_file is not None and not os.path.exists(os.path.join(workdir, os.path.basename(msa_file))):
        shutil.copy(msa_file, workdir)
        staged.append(os.path.join(workdir, os.path.basename(msa_file)))
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


def _slit_pixels(model, wcs):
    """Detector pixels inside a slit's bounding box, with their slit-frame y and wavelength [um]."""
    (x0, x1), (y0, y1) = wcs.bounding_box
    ny, nx = model.data.shape[-2:]
    xa = np.arange(max(int(np.ceil(x0)), 0), min(int(np.floor(x1)), nx - 1) + 1)
    ya = np.arange(max(int(np.ceil(y0)), 0), min(int(np.floor(y1)), ny - 1) + 1)
    if xa.size == 0 or ya.size == 0:
        return None
    y, x = np.meshgrid(ya, xa, indexing='ij')
    _, ys, lam = wcs.get_transform('detector', 'slit_frame')(x, y)
    ok = np.isfinite(ys) & np.isfinite(lam)
    if not ok.any():
        return None
    lam = lam[ok]
    if np.nanmax(lam) < 1e-3:      # metres
        lam = lam * 1e6
    return (y[ok] * nx + x[ok]).astype(np.int64), ys[ok], lam


def _slit_record(slit, kind, centres):
    return dict(name=str(slit.name), source_id=int(slit.source_id), kind=kind,
                shutter_state=str(slit.shutter_state), ymin=float(slit.ymin), ymax=float(slit.ymax),
                quadrant=int(slit.quadrant), xcen=int(slit.xcen), ycen=int(slit.ycen),
                src_ypos=float(getattr(slit, 'source_ypos', np.nan)),
                centres=','.join(f'{c:.3f}' for c in centres))


def _slitlet_traces(model):
    from jwst.assign_wcs import nirspec

    for slit in model.meta.wcs.get_transform('gwa', 'slit_frame').slits:
        px = _slit_pixels(model, nirspec.nrs_wcs_set_input(model, slit.name))
        if px is not None:
            centres = open_shutter_centres(slit.shutter_state, slit.ymin, slit.ymax)
            yield _slit_record(slit, 'slitlet', centres), centres, px


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
        px = _slit_pixels(tmp, w)
        if px is not None:
            rec = _slit_record(slit, 'failed_open', np.array([0.0]))
            rec['name'] = f'failed_open_q{slit.quadrant}_{slit.xcen}_{slit.ycen}'
            yield rec, np.array([0.0]), px
    tmp.close()


@dataclass
class TraceMap:
    """Which open slitlets (and failed-open shutters) illuminate each detector pixel of one exposure.

    ``count`` is the number of traces lighting a pixel (an open shutter's area padded
    by ``pad`` for the PSF); where exactly one does, ``index`` / ``ys`` / ``lam`` give
    that trace (``slits`` row), its slit-frame y and wavelength. Shared pixels have
    ``index == SHARED`` and every trace on them is listed in ``shared``."""
    slits: object                 # astropy Table, one row per trace
    count: np.ndarray             # uint8 (ny, nx)
    index: np.ndarray             # int16 (ny, nx): slits row, NONE or SHARED
    ys: np.ndarray                # float32 (ny, nx)
    lam: np.ndarray               # float32 (ny, nx), um
    shared: object                # astropy Table: pix, index, ys, lam
    meta: dict = field(default_factory=dict)

    @classmethod
    def build(cls, rate_file, workdir, msa_file=None, msa_metadata_id=None, dither_position=None,
              failed_open=True, pad=LIGHT_PAD):
        from astropy.table import Table

        model, staged = full_mask_model(rate_file, workdir, msa_file, msa_metadata_id, dither_position,
                                        return_staged=True)
        shape = model.data.shape[-2:]
        traces = list(_slitlet_traces(model))
        if failed_open:
            traces += list(_failed_open_traces(model))
        m = model.meta
        meta = dict(MAPVER=MAP_VERSION, EXPOSURE=os.path.basename(rate_file), DETECTOR=m.instrument.detector,
                    GRATING=m.instrument.grating, FILTER=m.instrument.filter,
                    MSAMETFL=m.instrument.msa_metadata_file, MSAMETID=int(m.instrument.msa_metadata_id),
                    PATT_NUM=int(m.dither.position_number), LITPAD=pad, FAILOPEN=bool(failed_open))
        model.close()
        for f in staged:                                 # the copies made for assign_wcs; the map is all we keep
            if os.path.exists(f):
                os.remove(f)
        return cls.from_traces(traces, shape, pad=pad, meta=meta)

    @classmethod
    def from_traces(cls, traces, shape, pad=LIGHT_PAD, meta=None):
        """Assemble a map from ``(record, open shutter centres, (pix, ys, lam))`` per trace;
        a trace lights the pixels within ``OPEN_HALF + pad`` of one of its open shutters."""
        from astropy.table import Table

        ny, nx = shape
        recs, pix, idx, ys, lam = [], [], [], [], []
        for i, (rec, centres, (p, y, l)) in enumerate(traces):
            near = np.min(np.abs(np.asarray(y)[:, None] - np.asarray(centres)[None, :]), axis=1) < OPEN_HALF + pad
            recs.append(rec)
            pix.append(np.asarray(p, np.int64)[near]); ys.append(np.asarray(y)[near]); lam.append(np.asarray(l)[near])
            idx.append(np.full(near.sum(), i, np.int16))
        pix, idx = np.concatenate(pix), np.concatenate(idx)
        ys, lam = np.concatenate(ys).astype(np.float32), np.concatenate(lam).astype(np.float32)
        count = np.bincount(pix, minlength=ny * nx)
        single = count[pix] == 1
        index = np.full(ny * nx, NONE, np.int16)
        index[count > 1] = SHARED
        index[pix[single]] = idx[single]
        ys_img = np.full(ny * nx, np.nan, np.float32)
        lam_img = np.full(ny * nx, np.nan, np.float32)
        ys_img[pix[single]] = ys[single]
        lam_img[pix[single]] = lam[single]
        order = np.argsort(pix[~single], kind='stable')
        shared = Table(dict(pix=pix[~single][order].astype(np.int32), index=idx[~single][order],
                            ys=ys[~single][order], lam=lam[~single][order]))
        return cls(slits=Table(recs), count=np.minimum(count, 255).astype(np.uint8).reshape(ny, nx),
                   index=index.reshape(ny, nx), ys=ys_img.reshape(ny, nx), lam=lam_img.reshape(ny, nx),
                   shared=shared, meta=dict(meta or {}))

    # -- persistence -------------------------------------------------------
    def write(self, path):
        from astropy.io import fits
        hdr = fits.Header()
        for k, v in self.meta.items():
            hdr[k] = v
        hdus = fits.HDUList([
            fits.PrimaryHDU(header=hdr),
            fits.CompImageHDU(self.count, name='COUNT'),
            fits.CompImageHDU(self.index, name='INDEX'),
            # lossless: float tiles quantised by default (RICE) would round them
            fits.CompImageHDU(self.ys, name='YS', compression_type='GZIP_2', quantize_level=0.0),
            fits.CompImageHDU(self.lam, name='LAM', compression_type='GZIP_2', quantize_level=0.0),
            fits.BinTableHDU(self.shared, name='SHARED'),
            fits.BinTableHDU(self.slits, name='SLITS'),
        ])
        hdus.writeto(path, overwrite=True)

    @classmethod
    def read(cls, path):
        from astropy.io import fits
        from astropy.table import Table
        with fits.open(path) as h:
            meta = {k: h[0].header[k] for k in ('MAPVER', 'EXPOSURE', 'DETECTOR', 'GRATING', 'FILTER', 'MSAMETFL',
                                                'MSAMETID', 'PATT_NUM', 'LITPAD', 'FAILOPEN') if k in h[0].header}
            return cls(slits=Table(h['SLITS'].data), count=np.asarray(h['COUNT'].data),
                       index=np.asarray(h['INDEX'].data), ys=np.asarray(h['YS'].data),
                       lam=np.asarray(h['LAM'].data), shared=Table(h['SHARED'].data), meta=meta)

    # -- lookups -----------------------------------------------------------
    def traces_in(self, x0, y0, nx, ny):
        """Every (pixel, trace) pair in a detector region (0-based corner, size).

        Returns flat arrays ``iy, ix`` (region coordinates), ``index`` (slits row),
        ``ys`` and ``lam`` (that trace's slit-frame y and wavelength)."""
        NY, NX = self.count.shape
        ya, xa = slice(max(y0, 0), min(y0 + ny, NY)), slice(max(x0, 0), min(x0 + nx, NX))
        idx = self.index[ya, xa]
        jy, jx = np.nonzero(idx >= 0)
        out = [(jy + ya.start - y0, jx + xa.start - x0, idx[jy, jx], self.ys[ya, xa][jy, jx], self.lam[ya, xa][jy, jx])]
        if len(self.shared):
            p = np.asarray(self.shared['pix'])
            py, px = p // NX, p % NX
            sel = (py >= ya.start) & (py < ya.stop) & (px >= xa.start) & (px < xa.stop)
            out.append((py[sel] - y0, px[sel] - x0, np.asarray(self.shared['index'])[sel],
                        np.asarray(self.shared['ys'])[sel], np.asarray(self.shared['lam'])[sel]))
        return tuple(np.concatenate(a) for a in zip(*out))

    def rows_of(self, source_id, kind='slitlet'):
        s = self.slits
        return np.nonzero((np.asarray(s['source_id']) == source_id) & (np.asarray(s['kind']) == kind))[0]

    def source_shutter(self, ys, pad=LIGHT_PAD):
        """Pixels of a trace in its source shutter (the light a nod subtraction keeps)."""
        return np.abs(ys) < OPEN_HALF + pad


def trace_map_path(cache_dir, exposure_name):
    root = os.path.basename(exposure_name).replace('_rate.fits', '').replace('.fits', '')
    return os.path.join(cache_dir, f'{root}_tracemap.fits')


def get_trace_map(rate_file, cache_dir, workdir=None, exposure_name=None, **build_kw):
    """Cached ``TraceMap`` of one exposure: read it if present, else build and write it.

    ``exposure_name`` names the cache file when ``rate_file`` is a template."""
    path = trace_map_path(cache_dir, exposure_name or rate_file)
    if os.path.exists(path):
        return TraceMap.read(path)
    os.makedirs(cache_dir, exist_ok=True)
    tm = TraceMap.build(rate_file, workdir or os.path.join(cache_dir, 'work'), **build_kw)
    tm.write(path)
    return tm


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
    target_ys: np.ndarray = field(repr=False)
    target_lam: np.ndarray = field(repr=False)
    other_lam: np.ndarray = field(repr=False)

    @property
    def n_cols(self):
        return int(np.unique(self.ix).size)

    def in_target_shutter(self, pad=LIGHT_PAD):
        return np.abs(self.target_ys) < OPEN_HALF + pad


def nod_contamination(maps, source_id, region, partners=None, pad=LIGHT_PAD):
    """Other sources' light in each nod difference of ``source_id``.

    ``maps`` are the trace maps of the nods of one configuration and detector, in
    nod order; ``region`` is the source's cutout ``(x0, y0, nx, ny)`` (0-based, e.g.
    SLTSTRT1 - 1, SLTSTRT2 - 1, SLTSIZE1, SLTSIZE2); ``partners[k]`` lists the nods
    subtracted from nod k (default all others, as stage 2b). A contaminant's source
    shutter in nod k adds light to nod k's difference; its source shutter in a
    partner nod subtracts. Failed-open shutters are returned whole (their light is
    unknown). Pixels are those the target's own trace covers."""
    n = len(maps)
    if partners is None:
        partners = [[j for j in range(n) if j != k] for k in range(n)]
    lut = [m.traces_in(*region) for m in maps]
    out = []
    for k in range(n):
        iy, ix, idx, ys, lam = lut[k]
        own = np.isin(idx, maps[k].rows_of(source_id))
        if not own.any():
            continue
        t_key = iy[own].astype(np.int64) * 100000 + ix[own]
        t_ys, t_lam = ys[own], lam[own]
        for j in [k] + list(partners[k] or []):
            jy, jx, jidx, jys, jlam = lut[j]
            slits = maps[j].slits
            for r in np.unique(jidx):
                rec = slits[int(r)]
                if rec['kind'] == 'slitlet' and int(rec['source_id']) == source_id:
                    continue
                sel = jidx == r
                if rec['kind'] == 'slitlet':
                    sel &= maps[j].source_shutter(jys, pad)
                if not sel.any():
                    continue
                key = jy[sel].astype(np.int64) * 100000 + jx[sel]
                common, it, ic = np.intersect1d(t_key, key, return_indices=True)
                if common.size == 0:
                    continue
                out.append(Shared(exposure=k, from_exposure=j, sign=1 if j == k else -1,
                                  name=str(rec['name']), source_id=int(rec['source_id']), kind=str(rec['kind']),
                                  iy=(common // 100000).astype(int), ix=(common % 100000).astype(int),
                                  target_ys=t_ys[it], target_lam=t_lam[it], other_lam=jlam[sel][ic]))
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
