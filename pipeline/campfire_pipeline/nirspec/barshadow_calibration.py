"""Empirical NIRSpec MSA bar-shadow calibration from CAMPFIRE stage-2a canonicals.

The CRDS ``barshadow`` reference (one file per disperser, selected on ``GRATING``) holds
two images on a (cross-dispersion, wavelength) grid: ``DATA1X1``, the profile around a
single open shutter whose neighbours are closed, and ``DATA1X3``, the profile around an
open shutter with open neighbours; rows run from one shutter pitch below the shutter
centre to one above (``CRVAL2 = -1``, ``CDELT2 = 0.002``), columns are wavelength.
jwst cuts six half-shutter pieces out of them (``bar_shadow.create_shutter_elements``)
and stitches a slitlet's shadow from its shutter-state string, then evaluates it at each
pixel's slit-frame y and wavelength.

Measured against the empty shutters of our own canonicals, the reference as it stands is
off by a registration shift on the prism (-0.11 px) and by a too-wide open profile on
G395M (docs/design-nirspec-master-background.md §9). Rather than model those, this
module measures the profile itself, in the pipeline's own slit-frame coordinates, from
many observations, and writes it back in the reference's format so ``BarShadowStep``
takes it as an override with no code change:

1. **Samples.** Per canonical (one slit, one nod, one detector), every pixel's
   uniform-calibrated surface brightness (``SCI / PIXAR_SR * PATHLOSS_PS /
   PATHLOSS_UN``, the bar-shadow division undone if it was applied), its slit-frame y in
   shutter pitches, its shutter index and within-shutter coordinate ``f``, its
   wavelength from the ``slit_frame`` transform, and which of the reference's segments
   it measures. Excluded: the source shutter and a pad around it, any other catalogued
   source's shutter in the slitlet, DQ-flagged pixels, and pixels under more than one
   slitlet footprint on the overlap counter (``coverage.coverage_from_wcs``).
2. **The sky.** The spectrum of every empty shutter from its centre pixels alone
   (``|f| < 0.15``, where the shadow is 1 by the reference's own convention), pooled per
   disperser into a median sky on the reference's wavelength columns, with the usual
   scale-and-reject selection of shutters. No bar-shadow model enters here, which is
   what makes the derivation independent of the reference it replaces.
3. **The profile.** For every pixel of a clean shutter, ``SB / sky(λ)`` accumulated in
   cells of (segment, y, wavelength column), as a histogram per cell whose median is the
   cell's value (a clipped mean is biased where the noise is comparable to the value). Four segments are measured: the lower outer
   edge (closed → open, the reference's ``first`` / ``closed_open`` piece, ``DATA1X1``
   rows -1..0), the upper outer edge (open → closed, ``last`` / ``open_closed``,
   ``DATA1X1`` rows 0..1), and the two halves of an interior bar between open shutters
   (``open_open``, ``DATA1X3`` rows -1..0: the lower shutter's upper half and the upper
   shutter's lower half). The upper half of ``DATA1X3`` is the mirror of the lower, which
   is the only part jwst reads anyway. Beyond the measured range the last value is held,
   as the reference does.
4. **Normalisation**: each wavelength column is scaled so the open-shutter centre is 1.
5. **Output**: the reference's layout, header cards and grids, with the provenance (the
   observations, the pixel counts, the code version) in ``HISTORY``, as
   ``empirical_jwst_nirspec_barshadow_<grating>.fits`` next to the extended-wavelength
   photom under ``campfire_pipeline/data`` — campfire products derive the calibration,
   the calibration lives in the repo.

Held-out validation: derive from all observations but one and stack the left-out
observation's empty shutters divided by the new table; a flat line at 1 is the test.
"""
import os
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
from astropy.io import fits

from campfire_pipeline.nirspec.coverage import coverage_from_wcs, cache_path, cutoffs_for

CENTRE_FRAC = 0.15          # |f| below which a pixel is "shutter centre" (shadow == 1)
Y_BIN = 0.02                # accumulation bin in shutter pitches (~0.1 px)
Y_EDGES = np.round(np.arange(-1.0, 1.0 + Y_BIN / 2, Y_BIN), 4)
Y_CENTRES = 0.5 * (Y_EDGES[1:] + Y_EDGES[:-1])
# Each cell keeps the histogram of SB / sky over its pixels, and the cell value is the
# median. A clipped mean is biased wherever the per-pixel noise is comparable to the
# value: beyond the outer shutter edge the true ratio is ~0.04, a faint-sky pixel there
# scatters by ~0.5, and a window of (-0.5, 2.5) dropped the negative tail but kept the
# positive one, so the margin rows came out at 0.1-0.2 (2026-10-30). Pixels outside the
# histogram's range land in the under- and overflow slots and still count for the rank.
RATIO_RANGE = (-1.5, 3.5)
RATIO_BIN = 0.025
HIST_EDGES = np.round(np.arange(RATIO_RANGE[0], RATIO_RANGE[1] + RATIO_BIN / 2, RATIO_BIN), 4)
N_HIST = len(HIST_EDGES) + 1      # underflow, the bins, overflow
SEGMENTS = ('edge_lower', 'edge_upper', 'bar_lower', 'bar_upper')
SEGMENT_DOC = {
    'edge_lower': 'closed -> open outer edge (reference first / closed_open: DATA1X1 rows -1..0)',
    'edge_upper': 'open -> closed outer edge (reference last / open_closed: DATA1X1 rows 0..1)',
    'bar_lower': 'interior bar, lower shutter side (reference open_open: DATA1X3 rows -1..-0.5)',
    'bar_upper': 'interior bar, upper shutter side (reference open_open: DATA1X3 rows -0.5..0)',
}
CANONICAL_RE = re.compile(r'^(jw\d+_\d+)_(\d+)_(nrs[12])_(\d+)\.fits$')


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class Canonical:
    """One slit of one exposure: a stage-2a canonical (``source_id > 0``, one slit per
    file) or a background-only slitlet of the ``bkgslits/`` file (``source_id =
    -slitlet_id``, slit ``extver`` of the file)."""
    path: str
    obs: str
    workspace: str
    root: str
    nod: str
    detector: str
    source_id: int
    grating: str
    filter: str
    msametid: int
    dither: int
    extver: int = 1                  # slit number in a multi-slit file (the bkgslits file)
    key: tuple = field(init=False)

    def __post_init__(self):
        self.key = (self.obs, self.root, self.nod, self.detector, self.source_id)

    @property
    def rate_path(self):
        return os.path.join(self.workspace, f'{self.root}_{self.nod}_{self.detector}_rate.fits')

    @property
    def rate_base(self):
        return f'{self.root}_{self.nod}_{self.detector}'

    @property
    def is_background(self):
        return self.source_id <= 0


def discover(obs_name, grating, products_dir, filter_name=None):
    """The stage-2a canonicals of one observation for one grating (and filter);
    ``grating=None`` takes every disperser."""
    workspace = os.path.join(products_dir, 'nirspec', obs_name)
    out = []
    for name in sorted(os.listdir(workspace)):
        m = CANONICAL_RE.match(name)
        if not m:
            continue
        path = os.path.join(workspace, name)
        hdr = fits.getheader(path)
        if grating and str(hdr.get('GRATING', '')).upper() != grating.upper():
            continue
        if filter_name and str(hdr.get('FILTER', '')).upper() != filter_name.upper():
            continue
        out.append(Canonical(path=path, obs=obs_name, workspace=workspace, root=m.group(1), nod=m.group(2),
                             detector=m.group(3), source_id=int(m.group(4)), grating=str(hdr['GRATING']).upper(),
                             filter=str(hdr['FILTER']).upper(), msametid=int(hdr.get('MSAMETID', -1)),
                             dither=int(hdr.get('PATT_NUM', 0) or 0)))
    return out


def crds_barshadow_path(canonical_path):
    """The CRDS bar-shadow reference the pipeline would use for this exposure."""
    import crds
    hdr = fits.getheader(canonical_path)
    params = {'INSTRUME': 'NIRSPEC', 'DETECTOR': hdr['DETECTOR'], 'EXP_TYPE': hdr.get('EXP_TYPE', 'NRS_MSASPEC'),
              'FILTER': hdr['FILTER'], 'GRATING': hdr['GRATING'],
              'DATE-OBS': hdr.get('DATE-OBS', '2023-01-01'), 'TIME-OBS': hdr.get('TIME-OBS', '00:00:00')}
    refs = crds.getreferences(params, reftypes=['barshadow'], observatory='jwst')
    return refs['barshadow']


@dataclass
class Grid:
    """The reference file's grids, read from its ``DATA1X1`` header."""
    w0: float            # first wavelength column [m]
    dw: float
    n_w: int
    y0: float            # first row [pitch]
    dy: float
    n_y: int

    @property
    def wavelengths_um(self):
        return (self.w0 + self.dw * np.arange(self.n_w)) * 1e6

    @property
    def rows(self):
        return self.y0 + self.dy * np.arange(self.n_y)

    def column_of(self, lam_um):
        """Nearest wavelength column of each wavelength (microns); -1 outside the grid."""
        col = np.rint((np.asarray(lam_um) * 1e-6 - self.w0) / self.dw).astype(int)
        col[(col < 0) | (col >= self.n_w)] = -1
        return col


def reference_grid(path):
    hd = fits.getheader(path, 'DATA1X1')
    return Grid(w0=float(hd['CRVAL1']), dw=float(hd['CDELT1']), n_w=int(hd['NAXIS1']),
                y0=float(hd['CRVAL2']), dy=float(hd['CDELT2']), n_y=int(hd['NAXIS2']))


# ---------------------------------------------------------------------------
# Per-canonical samples
# ---------------------------------------------------------------------------

def _other_source_columns(c, slitlet_id):
    """Shutter columns of every catalogued source in this slitlet at this nod, and the
    slitlet's own source column, from the original metafile."""
    hdr = fits.getheader(c.rate_path)
    metafile = os.path.join(c.workspace, os.path.basename(hdr.get('OGMETFL') or hdr['MSAMETFL']))
    from astropy.table import Table
    with fits.open(metafile, memmap=False) as mf:
        shut = Table(mf[2].data)
    sel = (shut['msa_metadata_id'] == c.msametid) & (shut['slitlet_id'] == slitlet_id)
    if 'dither_point_index' in shut.colnames and c.dither:
        sel &= shut['dither_point_index'] == c.dither
    rows = shut[sel]
    src = rows[(rows['primary_source'] == 'Y') & (rows['source_id'] > 0)]
    own = src['shutter_column'][src['source_id'] == c.source_id]
    own_col = int(own[0]) if len(own) else None
    cols = sorted(set(int(v) for v in rows['shutter_column']))
    others = sorted({int(v) for v in src['shutter_column'] if own_col is None or int(v) != own_col})
    return own_col, others, cols


def _source_radec(c):
    """The catalogue position of the canonical's source from the original metafile's
    SOURCE_INFO table, or None when it is not listed."""
    hdr = fits.getheader(c.rate_path)
    metafile = os.path.join(c.workspace, os.path.basename(hdr.get('OGMETFL') or hdr['MSAMETFL']))
    from astropy.table import Table
    t = Table.read(metafile, hdu='SOURCE_INFO')
    r = t[t['source_id'] == c.source_id]
    if not len(r):
        return None
    return float(r['ra'][0]), float(r['dec'][0])


def source_slit_position(slit, ra, dec, pitch):
    """Where a sky position falls in a slit's frame, in shutter pitches from the
    'x' shutter's centre, through the slit WCS (``world`` -> ``slit_frame``, divided
    by the slit's y scale as the pixel coordinates are). Exact for any source at any
    dither point, whereas jwst's ``source_ypos`` is the planned position and is 0
    when the metafile lists no primary shutter for the source."""
    _, ys, _ = slit.meta.wcs.get_transform('world', 'slit_frame')(ra, dec, 2.0e-6)
    return float(ys) / pitch


def segment_map(j, f, state):
    """Which reference segment each pixel measures, and its row in that segment's frame.

    ``j`` is the pixel's shutter index in the slitlet's state string (``'0'`` closed,
    ``'1'`` open, ``'x'`` the source; indices outside the string are the cutout's margin
    beyond the outer shutters), ``f`` its within-shutter coordinate in [-0.5, 0.5].
    Returns ``(seg, yseg)``: the index into ``SEGMENTS`` (-1 for none) and the row in
    the reference frame of that segment (``DATA1X1`` rows -1..1 for the outer edges,
    ``DATA1X3`` rows -1..0 for the interior bar).
    """
    j = np.asarray(j, int); f = np.asarray(f, float)
    n_sh = len(state)
    opened = np.array([ch != '0' for ch in state] + [False])     # index n_sh -> closed
    seg = np.full(j.shape, -1, int)
    yseg = np.full(j.shape, np.nan)
    in_range = (j >= 0) & (j < n_sh)
    jj = np.where(in_range, j, 0)
    is_open = in_range & opened[jj]
    low_open = in_range & (j > 0) & opened[np.clip(j - 1, 0, n_sh)]
    up_open = in_range & (j < n_sh - 1) & opened[np.clip(j + 1, 0, n_sh)]
    lower = f < 0
    # open shutter, lower half: closed below -> outer edge (1x1 rows -1..0); open below -> interior bar, upper side
    e_low = is_open & lower & ~low_open
    b_up = is_open & lower & low_open
    seg[e_low] = SEGMENTS.index('edge_lower'); yseg[e_low] = f[e_low]
    seg[b_up] = SEGMENTS.index('bar_upper'); yseg[b_up] = f[b_up]
    # open shutter, upper half: closed above -> outer edge (1x1 rows 0..1); open above -> interior bar, lower side
    e_up = is_open & ~lower & ~up_open
    b_low = is_open & ~lower & up_open
    seg[e_up] = SEGMENTS.index('edge_upper'); yseg[e_up] = f[e_up]
    seg[b_low] = SEGMENTS.index('bar_lower'); yseg[b_low] = f[b_low] - 1.0
    # the margin beyond the outer shutters: the closed neighbour's rows of the 1x1 profile
    below = (j == -1) & opened[0]
    above = (j == n_sh) & opened[n_sh - 1]
    seg[below] = SEGMENTS.index('edge_lower'); yseg[below] = f[below] - 1.0
    seg[above] = SEGMENTS.index('edge_upper'); yseg[above] = f[above] + 1.0
    return seg, yseg



def slit_geometry(s):
    """Slit-frame coordinates of every pixel of one slit's cutout, from its WCS.

    Returns a dict: ``ys`` (slit-frame y in shutter pitches from the 'x' shutter's
    centre), ``lam`` (µm), ``k`` (shutter offset, ``ys`` rounded), ``f``
    (within-shutter coordinate, ``ys - k``), ``j`` (index into the shutter-state
    string; outside ``[0, n)`` in the cutout's margin), ``is_open`` (``j`` names an
    open shutter), ``seg`` / ``yseg`` (``segment_map``), ``pitch``, ``state``, ``kx``
    (the 'x' shutter's index), ``slitlet_id`` and ``quadrant``.
    """
    from gwcs import wcstools
    x, y = wcstools.grid_from_bounding_box(s.meta.wcs.bounding_box, step=(1, 1))
    xs, ys, lam = s.meta.wcs.get_transform('detector', 'slit_frame')(x, y)
    lam = np.asarray(lam, float)
    if np.nanmedian(lam) < 1e-3:
        lam = lam * 1e6
    yscale = float(s.slit_yscale or 1.15)
    pitch = yscale if yscale > 1 else 1 / yscale
    ys = np.asarray(ys, float) / pitch
    state = str(s.shutter_state or '')
    kx = max(state.find('x'), 0)
    n_sh = len(state)
    with np.errstate(invalid='ignore'):
        k = np.rint(ys)
        f = ys - k
        j = np.where(np.isfinite(k), kx + k, -10 ** 6).astype(int)
    seg, yseg = segment_map(j, f, state)
    opened = np.array([ch != '0' for ch in state] + [False])
    is_open = (j >= 0) & (j < n_sh) & opened[np.clip(j, 0, n_sh)]
    return dict(ys=ys, lam=lam, k=k, f=f, j=j, is_open=is_open, seg=seg, yseg=yseg, pitch=pitch,
                state=state, kx=kx, slitlet_id=int(s.slitlet_id or -1), quadrant=int(s.quadrant or 0))


def _hdu(h, name, ver):
    try:
        return h[name, ver]
    except KeyError:
        return None


def pixel_samples(c, counter, source_pad_px=2.0):
    """Every usable pixel of one slit (a canonical, or one background slitlet).

    Returns a dict of flat arrays: ``lam`` (µm), ``y`` (slit-frame, pitches), ``k``
    (shutter offset from the source shutter), ``f`` (within-shutter coordinate),
    ``sb`` and ``var`` (uniform calibration, no bar-shadow division), ``seg`` (index into
    ``SEGMENTS``, -1 for pixels that measure no segment), ``yseg`` (the pixel's row in
    that segment's reference frame), ``centre`` (|f| < CENTRE_FRAC), ``is_open``, plus
    ``state``, ``slitlet_id``, ``quadrant``, ``pitch``, ``x0`` / ``y0`` / ``shape`` (the
    cutout on the detector) and ``n_pix``.
    """
    from jwst.datamodels import MultiSlitModel, dqflags

    v = int(c.extver)
    with fits.open(c.path, memmap=False) as h:
        # stage 2b overwrites SCI with the nod-subtracted frame and keeps the originals
        pre = _hdu(h, 'PRE_BKGSUB_SCI', v) is not None
        sci = np.array(h['PRE_BKGSUB_SCI' if pre else 'SCI', v].data, float)
        e = _hdu(h, 'PRE_BKGSUB_ERR', v) if pre else None
        err = np.array((e if e is not None else h['ERR', v]).data, float)
        d = _hdu(h, 'PRE_BKGSUB_DQ', v) if pre else None
        dq = np.array((d if d is not None else h['DQ', v]).data)
        hs = h['SCI', v].header
        ph = h[0].header
        pix_sr = float(hs['PIXAR_SR'])
        pu = _hdu(h, 'PATHLOSS_UN', v); pu = np.array(pu.data, float) if pu is not None else None
        pp = _hdu(h, 'PATHLOSS_PS', v); pp = np.array(pp.data, float) if pp is not None else None
        bar_applied = str(ph.get('S_BARSHA', '')).upper() == 'COMPLETE'
        bar = _hdu(h, 'BARSHADOW', v) if bar_applied else None
        bar = np.array(bar.data, float) if bar is not None else None
        x0, y0 = int(hs['SLTSTRT1']) - 1, int(hs['SLTSTRT2']) - 1

    m = MultiSlitModel(c.path)
    s = m.slits[v - 1]
    g = slit_geometry(s)
    ys, lam, k, f, j, state, pitch = g['ys'], g['lam'], g['k'], g['f'], g['j'], g['state'], g['pitch']
    planned = s.source_ypos is not None and abs(float(s.source_ypos)) > 0

    factor = np.full(sci.shape, 1.0 / pix_sr)
    if pu is not None and pp is not None and str(hs.get('PTHLOSS', 'POINT')).upper() == 'POINT':
        factor *= pp / pu
    if bar is not None:
        factor *= bar                     # undo the division jwst applied
    sb = sci * factor
    var = (err * factor) ** 2

    kx = g['kx']
    n_sh = len(state)

    good = np.isfinite(sb) & np.isfinite(lam) & np.isfinite(ys) & (var > 0)
    good &= (dq & (dqflags.pixel['DO_NOT_USE'] | dqflags.pixel['MSA_FAILED_OPEN'])) == 0
    if counter is not None:
        box = counter[y0:y0 + sci.shape[0], x0:x0 + sci.shape[1]]
        if box.shape == sci.shape:
            good &= box == 1
        else:
            good &= False
    # the source shutter, plus ``source_pad_px`` detector rows beyond each of its edges
    # (one shutter is ~5.3 rows, so 2 px is 0.4 of a pitch into the neighbours; a pad of
    # 5 px or more removes the adjacent shutters' centres altogether and leaves only the
    # shutters two away from the source, which exist only in nods 2 and 3)
    rows_per_pitch = float(np.nanmedian(np.abs(np.diff(ys, axis=0)))) if ys.shape[0] > 1 else 1.0
    px_per_pitch = 1.0 / rows_per_pitch if rows_per_pitch > 0 else 5.0
    pad = source_pad_px / px_per_pitch
    # where the source is, in shutter offsets from the 'x' shutter (the slit-frame origin).
    # Normally 0. When the nod moved the source out of a short slitlet, jwst marks no
    # primary shutter: it names the slitlet background, writes an 'x' on the first
    # shutter with source_ypos 0, and stage 2a keeps the product under the source's id.
    # The real source then sits beyond the slitlet's end (or, for a secondary source,
    # wherever the nod took it); the slit WCS places its catalogue position, so the
    # shutter next to it is padded like any shutter next to a source and the fake 'x'
    # shutter is measured as sky. A background slitlet (``source_id <= 0``) has no
    # source to cut.
    src_offset = 0.0
    if not c.is_background:
        others, own_col, cols = [], None, []
        try:
            own_col, others, cols = _other_source_columns(c, g['slitlet_id'])
        except Exception as e:                # metafile not at hand: keep the pixels
            warnings.warn(f'{os.path.basename(c.path)}: no metafile for other sources ({e})')
        if own_col is None and not planned:
            radec = _source_radec(c)
            if radec is None:
                m.close()
                raise ValueError('source left its slitlet at this dither point and is not in SOURCE_INFO')
            src_offset = source_slit_position(s, *radec, pitch)
        ref_col = own_col if own_col is not None else (cols[kx] if 0 <= kx < len(cols) else None)
        good &= ~(np.abs(ys - src_offset) < 0.5 + pad)
        if src_offset == 0:
            good &= k != 0
        # other catalogued sources in this slitlet (their shutter column -> shutter offset)
        if ref_col is not None:
            for col in others:
                ko = col - ref_col
                good &= ~(np.abs(ys - ko) < 0.5 + pad)
    m.close()

    seg, yseg, is_open = g['seg'], g['yseg'], g['is_open']
    sel = good
    return dict(lam=lam[sel], y=ys[sel], k=k[sel].astype(int), j=j[sel], f=f[sel], sb=sb[sel], var=var[sel],
                seg=seg[sel], yseg=yseg[sel], centre=(np.abs(f[sel]) < CENTRE_FRAC) & is_open[sel],
                is_open=is_open[sel], state=state, slitlet_id=g['slitlet_id'], quadrant=g['quadrant'],
                pitch=pitch, x0=x0, y0=y0, shape=sci.shape, n_pix=int(sel.sum()))


# ---------------------------------------------------------------------------
# Pass 1: shutter-centre spectra and the pooled sky
# ---------------------------------------------------------------------------

def _worker_centre(args):
    c, counter_path, grid, pad = args
    counter = np.load(counter_path) if counter_path else None
    try:
        p = pixel_samples(c, counter, pad)
    except Exception as e:
        return c.key, None, f'{type(e).__name__}: {e}'
    col = grid.column_of(p['lam'])
    out = {}
    for kk in np.unique(p['k']):
        m = (p['k'] == kk) & p['centre'] & (col >= 0)
        if m.sum() < 10:
            continue
        spec = np.full(grid.n_w, np.nan); n = np.zeros(grid.n_w, int)
        cc = col[m]; v = p['sb'][m]
        order = np.argsort(cc); cc = cc[order]; v = v[order]
        starts = np.searchsorted(cc, np.arange(grid.n_w)); ends = np.searchsorted(cc, np.arange(grid.n_w), side='right')
        for i in range(grid.n_w):
            if ends[i] - starts[i] >= 3:
                spec[i] = np.median(v[starts[i]:ends[i]]); n[i] = ends[i] - starts[i]
        out[int(kk)] = (spec, n)
    return c.key, out, None


def centre_spectra(canonicals, counters, grid, pad, processes):
    """Per empty shutter: the median centre-pixel spectrum on the reference columns."""
    from multiprocessing import Pool
    args = [(c, counters.get(c.rate_path), grid, pad) for c in canonicals]
    shutters, failures = {}, []
    with Pool(processes) as pool:
        for key, out, err in pool.imap_unordered(_worker_centre, args, chunksize=4):
            if err:
                failures.append((key, err)); continue
            for kk, (spec, n) in out.items():
                shutters[key + (kk,)] = (spec, n)
    return shutters, failures


def pool_sky(shutters, grid, reject_frac=0.10, n_iter=3, min_shutters=5):
    """Median sky over clean shutters per column, the per-shutter scales, the clean set."""
    keys = list(shutters)
    S = np.array([shutters[k][0] for k in keys])            # (n_shutter, n_w)
    clean = np.ones(len(keys), bool)
    scales = np.full(len(keys), np.nan)
    sky = np.nanmedian(S, axis=0)
    for _ in range(n_iter):
        with np.errstate(invalid='ignore', divide='ignore'):
            ratio = S / sky[None, :]
        scales = np.nanmedian(ratio, axis=1)
        nbins = np.sum(np.isfinite(ratio), axis=1)
        clean = np.isfinite(scales) & (np.abs(scales - 1) < reject_frac) & (nbins >= 10)
        if clean.sum() < min_shutters:
            break
        sky = np.nanmedian(S[clean], axis=0)
    n_per_col = np.sum(np.isfinite(S[clean]), axis=0)
    return sky, n_per_col, {k: (float(a), bool(cl)) for k, a, cl in zip(keys, scales, clean)}


# ---------------------------------------------------------------------------
# Pass 2: the profile cells
# ---------------------------------------------------------------------------

def profile_selection(p, col, clean_k):
    """Which samples of ``pixel_samples`` enter the profile cells: on the reference grid,
    measuring a segment, and in a clean shutter. A pixel in the margin beyond the outer
    shutters (``j`` of -1 or ``n_sh``) sits in a closed shutter, which has no centre
    spectrum and so is never clean; it is admitted on the open shutter it borders, whose
    outer-edge profile it measures.
    """
    kk = np.asarray(p['k'], int).copy()
    j = np.asarray(p['j'], int)
    n_sh = len(p['state'])
    kk[j == -1] += 1
    kk[j == n_sh] -= 1
    return (col >= 0) & (np.asarray(p['seg']) >= 0) & np.isin(kk, list(clean_k))


def _worker_profile(args):
    c, counter_path, grid, pad, sky, clean_k, table = args
    counter = np.load(counter_path) if counter_path else None
    try:
        p = pixel_samples(c, counter, pad)
    except Exception as e:
        return c.key, None, f'{type(e).__name__}: {e}'
    col = grid.column_of(p['lam'])
    ok = profile_selection(p, col, clean_k)
    if ok.sum() == 0:
        return c.key, None, None
    lam_cols = grid.wavelengths_um
    sky_at = np.interp(p['lam'][ok], lam_cols, sky)
    ratio = p['sb'][ok] / sky_at
    seg = p['seg'][ok]; yb = np.digitize(p['yseg'][ok], Y_EDGES) - 1; cc = col[ok]
    # cosmic rays, hot pixels and faint-sky noise go into the histogram's under- and
    # overflow slots, which count for the median's rank without pulling it
    ok2 = (yb >= 0) & (yb < len(Y_CENTRES)) & np.isfinite(ratio)
    # one flat index into the (segment, y, column, histogram slot) accumulator per pixel;
    # the parent adds them up (a dense accumulator per cutout would be ~33 MB to send back)
    acc = np.ravel_multi_index((seg[ok2], yb[ok2], cc[ok2], hist_slot(ratio[ok2])),
                               (len(SEGMENTS), len(Y_CENTRES), grid.n_w, N_HIST))
    # the "corrected" stack against a table, for the held-out check: ratio / table(yseg, lam)
    corr = None
    if table is not None:
        t = evaluate_table(table, seg[ok2], p['yseg'][ok][ok2], p['lam'][ok][ok2])
        with np.errstate(invalid='ignore', divide='ignore'):
            r2 = ratio[ok2] / t
        fin = np.isfinite(r2)
        corr = np.ravel_multi_index((seg[ok2][fin], yb[ok2][fin], hist_slot(r2[fin])),
                                    (len(SEGMENTS), len(Y_CENTRES), N_HIST))
    return c.key, (acc, corr), None


def exposure_of(key):
    """The pooling unit of a shutter key ``(obs, root, nod, det, sid, k)``: one exposure
    (both detectors), ``(obs, root, nod)``."""
    return key[:3]


def pool_sky_per_observation(shutters, grid, **kw):
    """``pool_sky`` per exposure: different fields, dates and even consecutive nods do
    not share a sky at the per-cent level (ember_uds_p2's second nod sits 17 % above
    the other two at the 3 µm minimum), so the selection and the normalisation are
    made against each exposure's own centre-pixel sky, as the master background does."""
    by_obs = {}
    for key, v in shutters.items():
        by_obs.setdefault(exposure_of(key), {})[key] = v
    skies, n_cols, scales = {}, {}, {}
    for obs, sh in by_obs.items():
        sky, n, sc = pool_sky(sh, grid, **kw)
        skies[obs] = sky; n_cols[obs] = n; scales.update(sc)
    return skies, n_cols, scales


def accumulate_profiles(canonicals, counters, grid, pad, sky, scales, processes, table=None):
    """Sum the profile cells over the clean shutters of every canonical; per observation too.
    ``sky`` is one array for all canonicals or a dict keyed by observation name."""
    from multiprocessing import Pool
    clean_by_canonical = {}
    for key, (a, cl) in scales.items():
        if cl:
            clean_by_canonical.setdefault(key[:-1], set()).add(key[-1])
    def sky_for(c):
        if not isinstance(sky, dict):
            return sky
        return sky.get((c.obs, c.root, c.nod), sky.get(c.obs))
    args = [(c, counters.get(c.rate_path), grid, pad, sky_for(c), clean_by_canonical.get(c.key, set()), table)
            for c in canonicals if clean_by_canonical.get(c.key) and sky_for(c) is not None]
    shape = (len(SEGMENTS), len(Y_CENTRES), grid.n_w, N_HIST)
    cshape = (len(SEGMENTS), len(Y_CENTRES), N_HIST)
    total = np.zeros(shape, np.int32)
    per_obs = {}
    corr_total = np.zeros(cshape, np.int32) if table is not None else None
    corr_obs = {}
    failures = []
    with Pool(processes) as pool:
        for key, out, err in pool.imap_unordered(_worker_profile, args, chunksize=4):
            if err:
                failures.append((key, err)); continue
            if out is None:
                continue
            acc, corr = out
            np.add.at(total.reshape(-1), acc, 1)
            if key[0] not in per_obs:
                per_obs[key[0]] = np.zeros(shape, np.int32)
            np.add.at(per_obs[key[0]].reshape(-1), acc, 1)
            if corr is not None:
                np.add.at(corr_total.reshape(-1), corr, 1)
                if key[0] not in corr_obs:
                    corr_obs[key[0]] = np.zeros(cshape, np.int32)
                np.add.at(corr_obs[key[0]].reshape(-1), corr, 1)
    return total, per_obs, corr_total, corr_obs, failures


def hist_slot(ratio):
    """The histogram slot of each ratio: 0 below ``RATIO_RANGE``, ``N_HIST - 1`` at or
    above it, the bin otherwise."""
    return np.searchsorted(HIST_EDGES, ratio, side='right').astype(np.int64)


def cell_counts(acc):
    """Pixels per cell of a histogram accumulator (the last axis is the histogram)."""
    return np.asarray(acc).sum(axis=-1)


def cell_quantile(acc, q):
    """The ``q`` quantile per cell of a histogram accumulator, linear inside the bin; NaN
    where the cell is empty or the quantile falls in the under- or overflow slot."""
    acc = np.asarray(acc)
    cum = np.cumsum(acc, axis=-1, dtype=np.int64)
    n = cum[..., -1]
    target = q * n
    k = np.clip((cum < target[..., None]).sum(axis=-1), 0, N_HIST - 1)   # the slot holding it
    before = np.take_along_axis(cum, np.clip(k - 1, 0, None)[..., None], axis=-1)[..., 0]
    before = np.where(k > 0, before, 0)
    inside = np.take_along_axis(acc, k[..., None], axis=-1)[..., 0].astype(float)
    lo = HIST_EDGES[np.clip(k - 1, 0, len(HIST_EDGES) - 1)]
    with np.errstate(invalid='ignore', divide='ignore'):
        val = lo + np.clip((target - before) / inside, 0.0, 1.0) * RATIO_BIN
    return np.where((k == 0) | (k == N_HIST - 1) | (n == 0), np.nan, val)


def cell_stats(acc, n_min=20):
    """Median and variance of the median per cell of a histogram accumulator; NaN below
    ``n_min`` pixels. The spread is the interquartile range (sigma = IQR / 1.349) and the
    variance of the median (pi / 2) sigma^2 / n."""
    n = cell_counts(acc).astype(float)
    med = cell_quantile(acc, 0.5)
    q25, q75 = cell_quantile(acc, 0.25), cell_quantile(acc, 0.75)
    with np.errstate(invalid='ignore', divide='ignore'):
        var = 0.5 * np.pi * ((q75 - q25) / 1.349) ** 2 / n
    bad = (n < n_min) | ~np.isfinite(med)
    # a quartile in the under- or overflow slot: the spread exceeds the histogram, which
    # makes the cell as uncertain as the range allows
    wide = ~bad & ~np.isfinite(var)
    with np.errstate(invalid='ignore', divide='ignore'):
        var = np.where(wide, 0.5 * np.pi * ((RATIO_RANGE[1] - RATIO_RANGE[0]) / 1.349) ** 2 / n, var)
    med = np.where(bad, np.nan, med); var = np.where(bad, np.nan, var)
    return med, np.clip(var, 0, None)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

def _fill_and_smooth(prof, var, smooth_cols, floor_rows=None, collapsed=None):
    """Fill the unmeasured cells of one segment's (y, wavelength) array and smooth it.

    Inside each column, gaps between measured rows are interpolated; rows outside the
    measured range stay empty. Across wavelength, each row's empty columns are
    interpolated from the measured ones (held at the ends). Rows never measured at any
    wavelength take the wavelength-collapsed profile (``collapsed``, the same rows
    measured over every column at once: the rows beyond the outer shutter edge, which a
    cutout covers with one or two pixel rows at most, are too sparse per column but
    well measured together; a row the collapse does not reach either takes the last
    collapsed value outward from the measured range), else the floor: the median of
    the measured cells beyond the bar (``floor_rows``), or the lowest measured value.
    Then a Gaussian along wavelength.
    """
    from scipy import ndimage
    out = prof.copy(); vout = var.copy()
    for i in range(out.shape[1]):
        fin = np.isfinite(out[:, i])
        if fin.sum() < 2:
            continue
        idx = np.arange(out.shape[0]); lo, hi = idx[fin][0], idx[fin][-1]
        out[lo:hi + 1, i] = np.interp(idx[lo:hi + 1], idx[fin], out[fin, i])
        vout[lo:hi + 1, i] = np.interp(idx[lo:hi + 1], idx[fin], vout[fin, i])
    for arr in (out, vout):
        for r in range(arr.shape[0]):
            fin = np.isfinite(arr[r])
            if 0 < fin.sum() < arr.shape[1]:
                idx = np.arange(arr.shape[1]); arr[r] = np.interp(idx, idx[fin], arr[r, fin])
    if floor_rows is not None and np.isfinite(out[floor_rows]).any():
        floor = np.nanmedian(out[floor_rows])
    else:
        floor = np.nanmin(out) if np.isfinite(out).any() else 0.4
    never = ~np.isfinite(out).any(axis=1)
    fill = np.full(out.shape[0], floor)
    if collapsed is not None and np.isfinite(collapsed).any():
        c = np.asarray(collapsed, float).copy()
        measured_rows = np.flatnonzero(~never)
        if measured_rows.size:
            # outward from the measured range, hold the last collapsed value
            lo, hi = measured_rows[0], measured_rows[-1]
            fin = np.flatnonzero(np.isfinite(c))
            below = fin[fin < lo]; above = fin[fin > hi]
            if below.size:
                c[:below[0]] = c[below[0]]
            if above.size:
                c[above[-1] + 1:] = c[above[-1]]
        fill = np.where(np.isfinite(c), c, floor)
    out[never] = fill[never, None]; vout[never] = np.nanmax(vout) if np.isfinite(vout).any() else 0.0
    out[~np.isfinite(out)] = floor; vout[~np.isfinite(vout)] = 0.0
    if smooth_cols and smooth_cols > 0:
        out = ndimage.gaussian_filter1d(out, smooth_cols, axis=1, mode='nearest')
        vout = ndimage.gaussian_filter1d(vout, smooth_cols, axis=1, mode='nearest') / max(1.0, smooth_cols)
    return np.clip(out, 0.0, 1.5), vout


def build_tables(total, grid, smooth_cols=2.0, n_min=20):
    """The ``DATA1X1`` / ``DATA1X3`` images (and variances) on the reference grid from the
    accumulated cells. Returns a dict with the arrays and the per-segment measured
    profiles on the Y_CENTRES bins (for the QA)."""
    mean, var = cell_stats(total, n_min)          # (seg, ybin, col)
    seg_i = {name: i for i, name in enumerate(SEGMENTS)}
    rows = grid.rows
    # measured profiles per segment on the coarse bins, filled and smoothed
    filled, vfilled = {}, {}
    beyond = {'edge_lower': Y_CENTRES < -0.55, 'edge_upper': Y_CENTRES > 0.55,
              'bar_lower': np.abs(Y_CENTRES + 0.5) < 0.05, 'bar_upper': np.abs(Y_CENTRES + 0.5) < 0.05}
    # the wavelength-collapsed profile per segment (sum over the columns), for the rows
    # no single column measures: the margin beyond the outer shutters
    coll = total.sum(axis=2)                      # (seg, ybin, 3)
    cmean, _ = cell_stats(coll, n_min)
    for name in SEGMENTS:
        filled[name], vfilled[name] = _fill_and_smooth(mean[seg_i[name]], var[seg_i[name]], smooth_cols,
                                                       floor_rows=beyond[name], collapsed=cmean[seg_i[name]])

    def onto_rows(prof, vprof, lo, hi):
        """Interpolate a coarse-bin profile onto the rows in [lo, hi], from the bins of
        that range only (held at its ends, so the segment's edge rows never see the
        floor the neighbouring, unmeasured bins were set to)."""
        sel_rows = (rows >= lo - 1e-9) & (rows <= hi + 1e-9)
        sel_bins = (Y_CENTRES >= lo - 1e-9) & (Y_CENTRES <= hi + 1e-9)
        out = np.full((sel_rows.sum(), grid.n_w), np.nan); vout = out.copy()
        for i in range(grid.n_w):
            out[:, i] = np.interp(rows[sel_rows], Y_CENTRES[sel_bins], prof[sel_bins, i])
            vout[:, i] = np.interp(rows[sel_rows], Y_CENTRES[sel_bins], vprof[sel_bins, i])
        return sel_rows, out, vout

    d1 = np.full((grid.n_y, grid.n_w), np.nan); v1 = d1.copy()
    d3 = np.full((grid.n_y, grid.n_w), np.nan); v3 = d3.copy()
    # 1x1: rows -1..0 from the lower outer edge, 0..1 from the upper outer edge
    sel, a, va = onto_rows(filled['edge_lower'], vfilled['edge_lower'], -1.0, 0.0); d1[sel] = a; v1[sel] = va
    sel, a, va = onto_rows(filled['edge_upper'], vfilled['edge_upper'], 0.0, 1.0)
    d1[sel] = np.where(np.isnan(d1[sel]), a, 0.5 * (d1[sel] + a)); v1[sel] = np.where(np.isnan(v1[sel]), va, 0.5 * (v1[sel] + va))
    # 1x3: rows -1..-0.5 from the lower shutter's upper half, -0.5..0 from the upper shutter's lower half,
    #      0..1 the mirror (jwst reads rows -1..0 only)
    sel, a, va = onto_rows(filled['bar_lower'], vfilled['bar_lower'], -1.0, -0.5); d3[sel] = a; v3[sel] = va
    sel, a, va = onto_rows(filled['bar_upper'], vfilled['bar_upper'], -0.5, 0.0)
    d3[sel] = np.where(np.isnan(d3[sel]), a, 0.5 * (d3[sel] + a)); v3[sel] = np.where(np.isnan(v3[sel]), va, 0.5 * (v3[sel] + va))
    lower_half = rows <= 1e-9
    n_up = int((~lower_half).sum())
    d3[~lower_half] = d3[lower_half][::-1][1:1 + n_up]       # row +dy mirrors row -dy (row 0 is shared)
    v3[~lower_half] = v3[lower_half][::-1][1:1 + n_up]
    # normalise every column to 1 at the open-shutter centre (|row| < CENTRE_FRAC of the 1x1;
    # for the 1x3 the centres are at rows -1 and 0)
    c1 = np.nanmean(d1[np.abs(rows) < CENTRE_FRAC], axis=0)
    c3 = np.nanmean(d3[(np.abs(rows) < CENTRE_FRAC) | (np.abs(rows + 1) < CENTRE_FRAC)], axis=0)
    with np.errstate(invalid='ignore'):
        d1 /= c1[None, :]; v1 /= c1[None, :] ** 2
        d3 /= c3[None, :]; v3 /= c3[None, :] ** 2
    for arr in (d1, d3):
        arr[np.isnan(arr)] = np.nanmin(arr) if np.isfinite(np.nanmin(arr)) else 1.0
    for arr in (v1, v3):
        arr[np.isnan(arr)] = np.nanmax(arr) if np.isfinite(np.nanmax(arr)) else 0.0
    return dict(data1x1=d1, var1x1=v1, data1x3=d3, var1x3=v3, measured=mean, measured_var=var,
                filled=filled, n_cells=cell_counts(total))


def load_table(path):
    """A reference file (CRDS or ours) as the dict ``evaluate_table`` takes."""
    with fits.open(path) as h:
        return dict(data1x1=np.array(h['DATA1X1'].data, float), data1x3=np.array(h['DATA1X3'].data, float),
                    grid=reference_grid(path))


def evaluate_table(table, seg, yseg, lam_um):
    """The table's value for pixels measuring segment ``seg`` at row ``yseg`` and
    wavelength ``lam_um`` — the same rows jwst would read for those pixels."""
    from scipy import ndimage
    g = table['grid']
    rows = (np.asarray(yseg) - g.y0) / g.dy
    cols = (np.asarray(lam_um) * 1e-6 - g.w0) / g.dw
    out = np.full(len(rows), np.nan)
    seg = np.asarray(seg)
    is1 = np.isin(seg, [SEGMENTS.index('edge_lower'), SEGMENTS.index('edge_upper')])
    if is1.any():
        out[is1] = ndimage.map_coordinates(table['data1x1'], [rows[is1], cols[is1]], order=1, mode='nearest')
    if (~is1).any():
        out[~is1] = ndimage.map_coordinates(table['data1x3'], [rows[~is1], cols[~is1]], order=1, mode='nearest')
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_reference(tables, template_path, out_path, provenance):
    """Write the empirical reference in the CRDS layout, copying the template's cards."""
    with fits.open(template_path) as t:
        phdr = t[0].header.copy()
        ext_hdrs = {name: t[name].header.copy() for name in ('DATA1X1', 'VAR1X1', 'DATA1X3', 'VAR1X3')}
    phdr['DESCRIP'] = 'CAMPFIRE empirical NIRSpec MSA bar-shadow profile from empty shutters'
    phdr['AUTHOR'] = 'CAMPFIRE (campfire_pipeline.nirspec.barshadow_calibration)'
    phdr['PEDIGREE'] = provenance.get('pedigree', phdr.get('PEDIGREE', 'INFLIGHT'))
    phdr['CFBSTMPL'] = (os.path.basename(template_path), 'CRDS reference the grids and cards come from')
    phdr['CFBSDATE'] = (datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'), 'derivation date (UTC)')
    phdr['CFBSVERS'] = (provenance.get('version', 'unknown'), 'campfire-pipeline version')
    phdr['CFBSNOBS'] = (len(provenance.get('observations', [])), 'observations stacked')
    phdr['CFBSNPIX'] = (int(provenance.get('n_pixels', 0)), 'empty-shutter pixels in the stack')
    phdr['CFBSNSHU'] = (int(provenance.get('n_shutters', 0)), 'clean empty shutters in the stack')
    if 'HISTORY' in phdr:
        del phdr['HISTORY']               # removes every HISTORY card of the template
    phdr['HISTORY'] = 'Derived by campfire_pipeline/data/Generate_Barshadow_Cals.py from CAMPFIRE stage-2a'
    phdr['HISTORY'] = 'canonicals: the empty shutters of every slitlet, pixels divided by the pooled sky'
    phdr['HISTORY'] = 'measured at the shutter centres, binned per reference segment, row and column;'
    phdr['HISTORY'] = 'see docs/design-nirspec-master-background.md section 9.'
    for obs in provenance.get('observations', []):
        phdr['HISTORY'] = f'observation: {obs}'
    for k, v in provenance.get('options', {}).items():
        phdr['HISTORY'] = f'option: {k} = {v}'
    hdus = [fits.PrimaryHDU(header=phdr)]
    for name, arr in (('DATA1X1', tables['data1x1']), ('VAR1X1', tables['var1x1']),
                      ('DATA1X3', tables['data1x3']), ('VAR1X3', tables['var1x3'])):
        hdu = fits.ImageHDU(data=np.asarray(arr, np.float32), header=ext_hdrs[name], name=name)
        hdus.append(hdu)
    fits.HDUList(hdus).writeto(out_path, overwrite=True)
    return out_path


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def derive(obs_names, grating, products_dir, cache_dir, filter_name=None, source_pad_px=2.0,
           processes=4, smooth_cols=2.0, n_min=20, held_out=None, log=print):
    """Run the derivation for one grating over several observations.

    Returns a dict with the tables, the sky, the per-observation cells, the CRDS
    template path, the provenance, and (if ``held_out``) the held-out stacks against
    the CRDS table and against the new one.
    """
    canonicals = []
    for obs in obs_names:
        found = discover(obs, grating, products_dir, filter_name)
        log(f'{obs}: {len(found)} {grating} canonicals')
        canonicals += found
    if not canonicals:
        raise SystemExit('no canonicals found')
    template = crds_barshadow_path(canonicals[0].path)
    grid = reference_grid(template)
    cutoffs = cutoffs_for(grating, filter_name or canonicals[0].filter)
    log(f'template {os.path.basename(template)}: {grid.n_w} columns {grid.wavelengths_um[0]:.3f}-{grid.wavelengths_um[-1]:.3f} um, '
        f'{grid.n_y} rows; footprint cutoffs {cutoffs} um')

    counters = {}
    for rate in sorted({c.rate_path for c in canonicals}):
        if os.path.exists(rate):
            coverage_from_wcs(rate, cache_dir=cache_dir, cutoffs=cutoffs)      # build the cache
            counters[rate] = cache_path(rate, cache_dir, cutoffs)
    log(f'{len(counters)} overlap counters')

    train = [c for c in canonicals if c.obs != held_out]
    test = [c for c in canonicals if c.obs == held_out]

    shutters, fails = centre_spectra(train, counters, grid, source_pad_px, processes)
    if fails:
        log(f'{len(fails)} canonicals failed in pass 1, e.g. {fails[0]}')
    skies, n_cols, scales = pool_sky_per_observation(shutters, grid)
    n_clean = sum(1 for a, cl in scales.values() if cl)
    for exp in sorted(skies):
        n_e = sum(1 for k in shutters if exposure_of(k) == exp); c_e = sum(1 for k, v in scales.items() if exposure_of(k) == exp and v[1])
        log(f'pass 1: {exp[0]} {exp[1]} nod {exp[2]}: {n_e} empty shutters, {c_e} clean; sky {np.nanmin(skies[exp]):.3g}-{np.nanmax(skies[exp]):.3g} MJy/sr')
    # the report's reference sky: the median over observations (each is already normalised per column)
    sky = np.nanmedian(np.array(list(skies.values())), axis=0)
    n_per_col = np.sum(np.array(list(n_cols.values())), axis=0)

    total, per_obs, _, _, fails = accumulate_profiles(train, counters, grid, source_pad_px, skies, scales, processes)
    if fails:
        log(f'{len(fails)} canonicals failed in pass 2, e.g. {fails[0]}')
    n_pixels = int(total.sum())
    log(f'pass 2: {n_pixels} pixels in the profile cells')
    tables = build_tables(total, grid, smooth_cols=smooth_cols, n_min=n_min)

    result = dict(tables=tables, grid=grid, sky=sky, skies=skies, n_per_col=n_per_col, scales=scales, per_obs=per_obs, shutters=shutters,
                  template=template, canonicals=canonicals, total=total,
                  provenance=dict(observations=sorted({c.obs for c in train}), n_pixels=n_pixels, n_shutters=n_clean,
                                  options=dict(grating=grating, filter=filter_name or 'any', source_pad_px=source_pad_px,
                                               cutoffs_um=f'{cutoffs[0]}-{cutoffs[1]}', smooth_cols=smooth_cols, n_min=n_min, held_out=held_out or 'none')))
    if test:
        # the held-out observation: its own sky and selection, its pixels against both tables
        sh_t, _ = centre_spectra(test, counters, grid, source_pad_px, processes)
        sky_t, _, scales_t = pool_sky(sh_t, grid)
        crds_table = load_table(template)
        new_table = dict(data1x1=tables['data1x1'], data1x3=tables['data1x3'], grid=grid)
        _, _, corr_crds, _, _ = accumulate_profiles(test, counters, grid, source_pad_px, sky_t, scales_t, processes, table=crds_table)
        tot_t, _, corr_new, _, _ = accumulate_profiles(test, counters, grid, source_pad_px, sky_t, scales_t, processes, table=new_table)
        result['held_out'] = dict(obs=held_out, raw=tot_t, crds=corr_crds, new=corr_new)
    return result
