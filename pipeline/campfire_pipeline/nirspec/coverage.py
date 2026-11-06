"""Slitlet geometry on the detector and the per-pixel count of traces, from the exposure WCS.

Two things every stage that reads "sky" off the detector needs:

* ``slit_geometry_extended`` / ``slitlet_geometry_extended``: one slitlet's slit-frame
  coordinates (``ys`` in shutter pitches from its source shutter, ``lam`` in µm) on every
  detector pixel its trace can reach, not only the cutout jwst's ``assign_wcs`` bounding
  box gives it. The bounding box stops at the calibrated wavelength range, but the sky
  and the source light run past it at both ends (design doc §9, 2026-10-30: on G140M and
  G235M NRS2 the trace carries on from the end of the extended range to the detector
  edge at ≈70 % of its in-band level; on G395M the filter cut-on lets 50 % through 46
  columns before the cutout). The slit's ``detector -> slit_frame`` transform is defined
  there (the bounding box only clips ``wcs(x, y)``), so the geometry is evaluated over a
  row block around the cutout and every column, and is finite wherever the transform is:
  within jwst's slit extent in y (its ``ymin`` / ``ymax`` pads beyond the outer shutters)
  and the transform's own wavelength domain. ``footprint`` is that region cut to the
  ``WAVELENGTH_CUTOFFS`` of the disperser and filter, where the sky light measurably ends.
  The same dict comes back for a stage-2a cutout (a ``SlitModel``, cutout coordinates)
  and for a slitlet of the rate file's MSA configuration (an ``assign_wcs`` slit record,
  detector coordinates), so counters, painting, trace maps and contamination share one
  footprint definition.
* ``coverage_from_wcs``: how many of those footprints fall on each pixel of a rate file,
  over every open slitlet of the MSA configuration plus the failed-open shutters. A counter
  built from processed canonicals only knows the sources stage 2a ran, and on the gratings
  the traces abut with no gap (38 % of a G395M detector lies under more than one footprint),
  so it has to come from the exposure WCS.
"""
import hashlib
import os

import numpy as np

COVERAGE_VERSION = 4          # 2: footprints over the extended geometry (both ends); 3: failed-open margins;
                              # 4: G140M/G235M always on the extended wavelengthrange
AUTO = 'auto'                 # wavelengthrange_override default: ``resolve_wavelengthrange``


def resolve_wavelengthrange(rate_path, override=AUTO):
    """The ``wavelengthrange`` reference a rate file's geometry is built with: ``override``
    when given (None = the CRDS one), else ``extended_wavelength.geometry_wavelengthrange``
    (the extended range for G140M/F100LP and G235M/F170LP whatever the reduction does)."""
    if override != AUTO:
        return override
    from campfire_pipeline.nirspec.extended_wavelength import geometry_wavelengthrange
    return geometry_wavelengthrange(rate_path)
ROW_PAD = 12                 # rows beyond the cutout the block covers: the trace drifts ~6 rows over the extension

# Where the sky light past the calibrated range ends, per (grating, filter), in µm, as
# (blue, red); None at an end means the footprint runs to the detector edge (the slit
# transform itself is finite over the whole detector width, so it bounds nothing).
# Measured 2026-10-30 from the empty shutters' own pixels in the rate frames, the median
# over the cutouts of one exposure per detector, as the wavelength past the calibrated
# end where the light is below 2 % of the in-band level, rounded outward (design doc
# §9): PRISM 0.55 / 5.6 (the light is below 10 % 0.19 µm past 5.31); G140M/F100LP the
# cut-on is sharp at 0.97 and the first-order sky runs to the detector edge at full
# strength; G235M/F170LP 10 % at 1.62 and 2 % at 1.55, and the red end falls from 4.56
# to 2 % at 4.99; G395M/F290LP 50 % at 2.83, 10 % at 2.74, 2 % by 2.63, and the red end
# falls from 5.20 to 10 % at 5.51 and ~2 % by 5.65. A disperser not listed gets no cutoff.
WAVELENGTH_CUTOFFS = {
    ('PRISM', 'CLEAR'): (0.55, 5.6),
    ('G140M', 'F100LP'): (0.96, None),
    ('G235M', 'F170LP'): (1.55, 5.05),
    ('G395M', 'F290LP'): (2.60, 5.7),
}


def default_cache_dir():
    """Where the counters are cached: ``$CAMPFIRE_ROOT/cache/coverage``."""
    root = os.environ.get('CAMPFIRE_ROOT') or os.path.expanduser('~/campfire-data')
    return os.path.join(root, 'cache', 'coverage')


def cutoffs_for(grating, filter_name=None):
    """``(blue_um, red_um)`` for a disperser; ``(None, None)`` when nothing is listed."""
    g = str(grating).upper(); f = str(filter_name).upper() if filter_name else None
    for key in ((g, f), (g, None)):
        if key in WAVELENGTH_CUTOFFS:
            return tuple(WAVELENGTH_CUTOFFS[key])
    return (None, None)


def _cutoff_tag(cutoffs):
    if not cutoffs or all(c is None for c in cutoffs):
        return ''
    return '_cut' + hashlib.sha1(repr(tuple(None if c is None else round(float(c), 4) for c in cutoffs)).encode()).hexdigest()[:6]


def cache_path(rate_path, cache_dir, cutoffs=None, wavelengthrange_override=AUTO):
    """The cache file ``coverage_from_wcs`` writes for ``rate_path``."""
    wavelengthrange_override = resolve_wavelengthrange(rate_path, wavelengthrange_override)
    key = hashlib.sha1(os.path.abspath(rate_path).encode()).hexdigest()[:12]
    tag = f'_v{COVERAGE_VERSION}' + _cutoff_tag(cutoffs)
    if wavelengthrange_override:
        tag += '_wr' + hashlib.sha1(os.path.basename(wavelengthrange_override).encode()).hexdigest()[:8]
    return os.path.join(cache_dir, f'coverage_{os.path.basename(rate_path)[:-5]}_{key}{tag}.npy')


# ---------------------------------------------------------------------------
# One slitlet's geometry on the detector
# ---------------------------------------------------------------------------

def _pitch(slit_yscale):
    """Shutter pitch in slit-frame y units (``slit_yscale`` is the pitch-to-open-height
    ratio, ~1.14; jwst's slit frame counts open-shutter heights)."""
    yscale = float(slit_yscale or 1.15)
    return yscale if yscale > 1 else 1.0 / yscale


def geometry_on_block(transform, x0, y0, row_lo, row_hi, state, pitch, cutoffs=(None, None),
                      ncols=2048, nrows=2048, with_segments=True):
    """Slit-frame coordinates of every detector pixel in rows ``[row_lo, row_hi)`` and all
    columns, from ``transform`` (``detector -> slit_frame`` of the slit, taking coordinates
    relative to ``(x0, y0)``: the cutout corner for a stage-2a slit, ``(0, 0)`` for a
    full-frame slit WCS).

    Returns a dict of 2-D arrays on the block: ``ys`` (shutter pitches from the source
    shutter's centre), ``lam`` (µm), ``k`` (shutter offset, ``ys`` rounded), ``f``
    (within-shutter coordinate), ``j`` (index into the shutter-state string), ``is_open``,
    ``seg`` / ``yseg`` (the bar-shadow reference segment and row, ``with_segments``),
    ``finite`` (the transform is defined: jwst's slit extent in y and its wavelength
    domain), ``footprint`` (``finite`` cut to ``cutoffs``); plus ``rows`` / ``cols`` (the
    block's detector coordinates), ``row_lo``, ``state``, ``kx``, ``pitch``, ``cutoffs``.
    """
    row_lo = max(int(row_lo), 0); row_hi = min(int(row_hi), nrows)
    rows = np.arange(row_lo, row_hi); cols = np.arange(ncols)
    yy, xx = np.meshgrid(rows, cols, indexing='ij')
    with np.errstate(invalid='ignore', divide='ignore'):
        out = transform(xx.astype(float) - x0, yy.astype(float) - y0)
    xs, ys, lam = (np.asarray(a, float) for a in out[-3:])
    if np.isfinite(lam).any() and np.nanmedian(lam) < 1e-3:
        lam = lam * 1e6
    ys = ys / pitch
    state = str(state or '')
    kx = max(state.find('x'), 0)
    n_sh = len(state)
    with np.errstate(invalid='ignore'):
        k = np.rint(ys)
        f = ys - k
        j = np.where(np.isfinite(k), kx + k, -10 ** 6).astype(int)
    opened = np.array([ch != '0' for ch in state] + [False])
    is_open = (j >= 0) & (j < n_sh) & opened[np.clip(j, 0, n_sh)]
    finite = np.isfinite(ys) & np.isfinite(lam)
    footprint = finite.copy()
    blue, red = cutoffs if cutoffs is not None else (None, None)
    with np.errstate(invalid='ignore'):
        if blue is not None:
            footprint &= lam >= float(blue)
        if red is not None:
            footprint &= lam <= float(red)
    g = dict(rows=rows, cols=cols, row_lo=row_lo, ys=ys, lam=lam, k=k, f=f, j=j, is_open=is_open,
             finite=finite, footprint=footprint, state=state, kx=kx, pitch=pitch, cutoffs=(blue, red))
    if with_segments:
        from campfire_pipeline.nirspec.barshadow_calibration import segment_map
        g['seg'], g['yseg'] = segment_map(j, f, state)
    return g


def slit_geometry_extended(slit, cutoffs=None, row_pad=ROW_PAD, **kw):
    """``geometry_on_block`` for a stage-2a slit (a ``SlitModel`` of a canonical or of
    the ``bkgslits`` file): the cutout's rows ± ``row_pad`` and every column, in detector
    coordinates. ``cutoffs`` default to the disperser's ``WAVELENGTH_CUTOFFS``."""
    x0 = int(slit.xstart) - 1; y0 = int(slit.ystart) - 1
    ny = int(slit.ysize) if slit.ysize else slit.data.shape[0]
    if cutoffs is None:
        try:
            cutoffs = cutoffs_for(slit.meta.instrument.grating, slit.meta.instrument.filter)
        except Exception:
            cutoffs = (None, None)
    tr = slit.meta.wcs.get_transform('detector', 'slit_frame')
    g = geometry_on_block(tr, x0, y0, y0 - row_pad, y0 + ny + row_pad, slit.shutter_state, _pitch(slit.slit_yscale), cutoffs, **kw)
    g.update(x0=x0, y0=y0, cutout_rows=(y0, y0 + ny), cutout_cols=(x0, x0 + int(slit.xsize or slit.data.shape[1])),
             slitlet_id=int(slit.slitlet_id or -1), quadrant=int(slit.quadrant or 0), name=str(slit.name))
    return g


def slitlet_geometry_extended(model, record, cutoffs=(None, None), row_pad=ROW_PAD, **kw):
    """``geometry_on_block`` for one slitlet of a rate file's MSA configuration: ``model``
    carries the exposure's compound WCS (``AssignWcsStep`` output, or the failed-open
    shutters' WCS set on an ``ImageModel``), ``record`` its ``assign_wcs`` slit record
    (``.slits`` of the ``gwa -> slit_frame`` transform). Detector coordinates."""
    from jwst.assign_wcs.nirspec import nrs_wcs_set_input
    w = nrs_wcs_set_input(model, record.name)
    bb = w.bounding_box
    try:
        ylo, yhi = (float(v) for v in bb.intervals['y'])
    except Exception:
        (_, _), (ylo, yhi) = bb
    y0 = int(np.floor(ylo + 0.5)); y1 = int(np.ceil(yhi - 0.5)) + 1
    tr = w.get_transform('detector', 'slit_frame')
    g = geometry_on_block(tr, 0, 0, y0 - row_pad, y1 + row_pad, record.shutter_state, _pitch(getattr(record, 'slit_yscale', None)), cutoffs, **kw)
    g.update(x0=0, y0=0, cutout_rows=(y0, y1), cutout_cols=None, slitlet_id=int(record.name), quadrant=int(record.quadrant), name=str(record.name))
    return g


def add_block(frame, g, values=None):
    """Add a block's ``footprint`` (or ``values``, same shape) into a full-frame array."""
    sl = slice(g['row_lo'], g['row_lo'] + g['footprint'].shape[0])
    frame[sl] += (g['footprint'] if values is None else values)
    return frame


# ---------------------------------------------------------------------------
# One rate file's slitlets: the geometry cache and the counter
# ---------------------------------------------------------------------------

GEOMETRY_VERSION = 3          # 2: failed-open shutters carry the margin of a commanded slitlet; 3: G140M/G235M on the extended wavelengthrange
FIXED_SLITS = ('S200A1', 'S200A2', 'S400A1', 'S1600A1', 'S200B1')
FAILED_OPEN_Y = 1.0           # slit-frame half extent of a failed-open shutter (open heights)


def failed_open_slitlets(msaoper):
    """The ``msaoper`` failed-open shutters as one-shutter slits (jwst's
    ``msaflagopen.create_slitlets``), each given what ``assign_wcs`` gives a commanded
    slitlet's record:

    * the slit extent: its open area (``ymin`` / ``ymax`` = ±0.5) plus jwst's 0.5 margin
      beyond it. jwst's own records stop at the open area, so the rows just past the
      shutter, which carry 5-50 % of its core level (PRISM, measured 2026-11-02), were
      outside every footprint and never painted; with the margin they are painted from
      the closed-neighbor rows of the bar-shadow profile like any slitlet's;
    * the shutter scales ``MSA_SLIT_SCALES`` (pitch / open size, ``slit_yscale`` 1.15).
      jwst leaves them at 1.0 on these records (its transforms do not read them), which
      made ``_pitch`` count the open height as the pitch: the bar-shadow profile came out
      1.15x too narrow across the shutter, short of the data at both edges."""
    from jwst.assign_wcs.nirspec import MSA_SLIT_SCALES
    from jwst.msaflagopen.msaflag_open import create_slitlets
    return [s._replace(ymin=-FAILED_OPEN_Y, ymax=FAILED_OPEN_Y, slit_xscale=MSA_SLIT_SCALES[0],
                       slit_yscale=MSA_SLIT_SCALES[1]) for s in create_slitlets(msaoper)]


def geometry_cache_path(rate_path, cache_dir, cutoffs=None, wavelengthrange_override=AUTO):
    """The ``RateGeometry`` cache of ``rate_path``, keyed like the counter's."""
    p = cache_path(rate_path, cache_dir, cutoffs, wavelengthrange_override)
    head, tail = os.path.split(p)
    return os.path.join(head, 'geometry' + tail[len('coverage'):-len('.npy')] + f'_g{GEOMETRY_VERSION}.npz')


def _assign_wcs(rate_path, wavelengthrange_override=None):
    """``assign_wcs`` on the rate file in memory with the metafile its header names (the
    original one, so every commanded-open shutter is present), plus the failed-open
    shutters of the ``msaoper`` reference on a WCS of their own (``failed_open_slitlets``):
    ``[(holder, slits, kind), ...]`` with kind ``'msa'`` / ``'failed'``."""
    from gwcs.wcs import WCS
    from jwst.assign_wcs import AssignWcsStep
    from jwst.assign_wcs.nirspec import generate_compound_bbox, slitlets_wcs
    from jwst.datamodels import ImageModel
    from jwst.msaflagopen.msaflagopen_step import MSAFlagOpenStep, create_reference_filename_dictionary

    prev = os.getcwd()
    os.chdir(os.path.dirname(os.path.abspath(rate_path)))
    try:
        with ImageModel(os.path.basename(rate_path)) as model:
            step = AssignWcsStep()
            step.input_dir = os.getcwd()
            if wavelengthrange_override:
                step.override_wavelengthrange = wavelengthrange_override
            processed = step.run(model)
        out = [(processed, list(processed.meta.wcs.get_transform('gwa', 'slit_frame').slits), 'msa')]
        failed = failed_open_slitlets(MSAFlagOpenStep().get_reference_file(processed, 'msaoper'))
        if failed:
            holder = ImageModel()
            holder.meta.wcs = WCS(slitlets_wcs(processed, create_reference_filename_dictionary(processed), failed))
            holder.meta.wcsinfo = processed.meta.wcsinfo
            holder.meta.exposure.type = 'NRS_MSASPEC'
            holder.meta.wcs.bounding_box = generate_compound_bbox(holder, failed)
            out.append((holder, list(failed), 'failed'))
    finally:
        os.chdir(prev)
    return out


class RateGeometry:
    """Every slitlet footprint of one rate file, compactly: the footprint pixels of each
    slitlet (flat detector index), their wavelength and slit-frame ``ys``, and per slitlet
    its kind (``msa`` / ``failed``), name, quadrant, ``ycen``, source id and shutter state.
    ``j``, ``f``, ``is_open`` and the bar-shadow segment follow from ``ys`` and the state
    (``blocks``). One ``assign_wcs`` per rate file feeds the coverage counter, the
    master background's rate-unit sky and the stuck-shutter fit."""

    def __init__(self, pix, lam, ys, offsets, meta, shape):
        self.pix, self.lam, self.ys = pix, lam, ys
        self.offsets, self.meta, self.shape = offsets, meta, tuple(shape)

    @classmethod
    def from_rate(cls, rate_path, cutoffs=None, wavelengthrange_override=AUTO):
        from astropy.io import fits
        h = fits.getheader(rate_path)
        if cutoffs is None:
            cutoffs = cutoffs_for(h.get('GRATING', ''), h.get('FILTER', ''))
        shape = fits.getheader(rate_path, 'SCI')
        shape = (int(shape['NAXIS2']), int(shape['NAXIS1']))
        pix, lam, ys, meta = [], [], [], []
        for holder, slits, kind in _assign_wcs(rate_path, resolve_wavelengthrange(rate_path, wavelengthrange_override)):
            for rec in slits:
                if str(rec.name) in FIXED_SLITS:
                    continue
                try:
                    g = slitlet_geometry_extended(holder, rec, cutoffs, nrows=shape[0], ncols=shape[1], with_segments=False)
                except Exception:                     # noqa: BLE001 - a slitlet off the detector
                    continue
                foot = g['footprint']
                if not foot.any():
                    continue
                rr, cc = np.nonzero(foot)
                pix.append(((rr + g['row_lo']) * shape[1] + cc).astype(np.int32))
                lam.append(g['lam'][foot].astype(np.float32)); ys.append(g['ys'][foot].astype(np.float32))
                meta.append(dict(kind=kind, name=str(rec.name), quadrant=int(rec.quadrant), ycen=float(rec.ycen),
                                 source_id=int(rec.source_id or 0), state=str(rec.shutter_state or '')))
        offsets = np.cumsum([0] + [p.size for p in pix]).astype(np.int64)
        cat = lambda xs, dt: np.concatenate(xs) if xs else np.zeros(0, dt)        # noqa: E731
        return cls(cat(pix, np.int32), cat(lam, np.float32), cat(ys, np.float32), offsets, meta, shape)

    def save(self, path):
        import json
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp.npz'
        np.savez(tmp, pix=self.pix, lam=self.lam, ys=self.ys, offsets=self.offsets,
                 shape=np.array(self.shape), meta=np.array(json.dumps(self.meta)))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path):
        import json
        with np.load(path) as z:
            return cls(z['pix'], z['lam'], z['ys'], z['offsets'], json.loads(str(z['meta'])), tuple(z['shape']))

    def counter(self):
        """Footprints per pixel (int16, detector shape): what ``coverage_from_wcs`` returns."""
        return np.bincount(self.pix, minlength=self.shape[0] * self.shape[1]).astype(np.int16).reshape(self.shape)

    def blocks(self, kind=None, with_segments=True):
        """Per slitlet: its meta plus flat arrays over its footprint pixels: ``pix``,
        ``lam``, ``ys``, ``k``, ``f``, ``j`` (index into ``state``), ``is_open`` and, with
        ``with_segments``, ``seg`` / ``yseg`` (as ``geometry_on_block``)."""
        from campfire_pipeline.nirspec.barshadow_calibration import segment_map
        for i, m in enumerate(self.meta):
            if kind is not None and m['kind'] != kind:
                continue
            sl = slice(self.offsets[i], self.offsets[i + 1])
            ys = self.ys[sl].astype(float)
            state = m['state']; kx = max(state.find('x'), 0); n_sh = len(state)
            k = np.rint(ys); f = ys - k; j = (kx + k).astype(int)
            opened = np.array([ch != '0' for ch in state] + [False])
            b = dict(m, pix=self.pix[sl].astype(np.int64), lam=self.lam[sl].astype(float), ys=ys, k=k, f=f, j=j,
                     is_open=(j >= 0) & (j < n_sh) & opened[np.clip(j, 0, n_sh)], kx=kx)
            if with_segments:
                b['seg'], b['yseg'] = segment_map(j, f, state)
            yield b


def rate_geometry(rate_path, cache_dir=None, cutoffs=None, wavelengthrange_override=AUTO):
    """The ``RateGeometry`` of ``rate_path``, from its cache under ``cache_dir`` when there
    is one (built and written otherwise, with the coverage counter beside it)."""
    from astropy.io import fits
    if cutoffs is None:
        ph = fits.getheader(rate_path)
        cutoffs = cutoffs_for(ph.get('GRATING', ''), ph.get('FILTER', ''))
    wavelengthrange_override = resolve_wavelengthrange(rate_path, wavelengthrange_override)
    path = geometry_cache_path(rate_path, cache_dir, cutoffs, wavelengthrange_override) if cache_dir else None
    if path and os.path.exists(path):
        return RateGeometry.load(path)
    geo = RateGeometry.from_rate(rate_path, cutoffs, wavelengthrange_override)
    if path:
        geo.save(path)
        np.save(cache_path(rate_path, cache_dir, cutoffs, wavelengthrange_override), geo.counter())
    return geo


def coverage_from_wcs(rate_path, cache_dir=None, cutoffs=None, wavelengthrange_override=AUTO):
    """Count of slitlet footprints per pixel of ``rate_path`` (int16, detector shape).

    Every open slitlet in the metafile the rate file points at, plus the failed-open
    shutters of the ``msaoper`` reference; fixed slits are skipped (always masked
    elsewhere). Each footprint is the slitlet's extended geometry cut to ``cutoffs``
    (default: the disperser's ``WAVELENGTH_CUTOFFS``). The slit transforms' wavelength
    domain comes from ``wavelengthrange_override`` (default ``resolve_wavelengthrange``:
    the extended range for G140M/F100LP and G235M/F170LP whether or not the reduction
    extends them, since their first-order light reaches the detector either way). Cached
    as ``coverage_<rate>_<hash>_v<N>[_cut<hash>][_wr<hash>].npy`` under ``cache_dir``, from
    the same pass as the ``rate_geometry`` cache.
    """
    from astropy.io import fits
    if cutoffs is None:
        ph = fits.getheader(rate_path)
        cutoffs = cutoffs_for(ph.get('GRATING', ''), ph.get('FILTER', ''))
    wavelengthrange_override = resolve_wavelengthrange(rate_path, wavelengthrange_override)
    cache =cache_path(rate_path, cache_dir, cutoffs, wavelengthrange_override) if cache_dir else None
    if cache and os.path.exists(cache):
        return np.load(cache)
    return rate_geometry(rate_path, cache_dir, cutoffs, wavelengthrange_override).counter()
