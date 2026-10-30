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

COVERAGE_VERSION = 2          # 2: footprints over the extended geometry (both ends)
ROW_PAD = 12                  # rows beyond the cutout the block covers: the trace drifts ~6 rows over the extension

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


def cache_path(rate_path, cache_dir, cutoffs=None, wavelengthrange_override=None):
    """The cache file ``coverage_from_wcs`` writes for ``rate_path``."""
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
# The counter
# ---------------------------------------------------------------------------

def coverage_from_wcs(rate_path, cache_dir=None, cutoffs=None, wavelengthrange_override=None):
    """Count of slitlet footprints per pixel of ``rate_path`` (int16, detector shape).

    Every open slitlet in the metafile the rate file points at, plus the failed-open
    shutters of the ``msaoper`` reference; fixed slits are skipped (always masked
    elsewhere). Each footprint is the slitlet's extended geometry cut to ``cutoffs``
    (default: the disperser's ``WAVELENGTH_CUTOFFS``). ``wavelengthrange_override`` is the
    extended ``wavelengthrange`` reference of the extended-wavelength reduction, so the
    slit transforms' wavelength domain matches that reduction's. Cached as
    ``coverage_<rate>_<hash>_v2[_cut<hash>][_wr<hash>].npy`` under ``cache_dir``.
    """
    from astropy.io import fits
    if cutoffs is None:
        ph = fits.getheader(rate_path)
        cutoffs = cutoffs_for(ph.get('GRATING', ''), ph.get('FILTER', ''))
    cache = cache_path(rate_path, cache_dir, cutoffs, wavelengthrange_override) if cache_dir else None
    if cache and os.path.exists(cache):
        return np.load(cache)

    from jwst.datamodels import ImageModel
    from jwst.assign_wcs import AssignWcsStep
    from jwst.assign_wcs.nirspec import generate_compound_bbox, slitlets_wcs
    from jwst.msaflagopen.msaflagopen_step import create_reference_filename_dictionary, MSAFlagOpenStep
    from jwst.msaflagopen.msaflag_open import create_slitlets
    from gwcs.wcs import WCS

    prev = os.getcwd()
    os.chdir(os.path.dirname(os.path.abspath(rate_path)))
    try:
        with ImageModel(os.path.basename(rate_path)) as model:
            step = AssignWcsStep()
            step.input_dir = os.getcwd()
            if wavelengthrange_override:
                step.override_wavelengthrange = wavelengthrange_override
            processed = step.run(model)
            source_slits = processed.meta.wcs.get_transform('gwa', 'slit_frame').slits
            failed = create_slitlets(MSAFlagOpenStep().get_reference_file(processed, 'msaoper'))
            refnames = create_reference_filename_dictionary(processed)
            counter = np.zeros(model.data.shape, np.int16)
            for slits, holder in ((source_slits, processed), (failed, None)):
                if not slits:
                    continue
                if holder is None:
                    holder = ImageModel()
                    holder.meta.wcs = WCS(slitlets_wcs(processed, refnames, slits))
                    holder.meta.wcsinfo = processed.meta.wcsinfo
                    holder.meta.exposure.type = 'NRS_MSASPEC'
                    holder.meta.wcs.bounding_box = generate_compound_bbox(holder, slits)
                for slit in slits:
                    if str(slit.name) in ('S200A1', 'S200A2', 'S400A1', 'S1600A1', 'S200B1'):
                        continue
                    g = slitlet_geometry_extended(holder, slit, cutoffs, nrows=counter.shape[0], ncols=counter.shape[1], with_segments=False)
                    add_block(counter, g)
    finally:
        os.chdir(prev)
    if cache:
        os.makedirs(cache_dir, exist_ok=True)
        np.save(cache, counter)
    return counter
