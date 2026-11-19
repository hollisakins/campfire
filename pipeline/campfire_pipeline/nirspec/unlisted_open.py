"""Light fixed on the detector that the master background does not predict, and the
MSA shutter it would come through (2026-11-11; PRISM, run by ``master_bkg``).

A shutter that passes light without being in the metafile or in the ``msaoper``
failed-open list (an unlisted failed-open shutter) puts a spectrum on the detector
that nothing paints: the master background leaves it in every exposure, and the nod
differences cancel it, because the MSA does not move on the detector while the sky
moves one shutter per nod. ``zenith_p15`` 549120: one shutter of its slitlet reads
twice the background in every exposure.

Three steps, on the rate files and master-background products of one detector:

1. ``fixed_light``: per exposure, rate minus ``MBKG2D``; per nod position the
   inverse-variance mean over its exposures; per pixel the minimum over nod positions.
   A source in a slitlet moves between nods and drops out of the minimum (unless it is
   larger than the nod step); light fixed on the detector stays.
2. ``find_traces``: the map binned along the dispersion, bins above ``snr`` and above
   ``min_frac`` of the background there, grouped into connected traces of at least
   ``min_cols`` columns.
3. ``MsaMap.scan`` / ``refine``: which shutter's spectrum would land there, and does
   the trace have the background's spectrum? Every shutter of every quadrant is placed
   on the detector through one reference slit per quadrant (its ``slit_frame ->
   detector`` transform at the shutter's offset, slit_frame units scaled by the MSA
   pitch; within a few pixels of the shutter's own WCS); those whose centre row
   follows the trace are fitted as transmission x the exposure's count-rate background
   (step 1's ``MBKG1DR``) along the trace; the best few are painted exactly as the
   master background paints a failed-open shutter (``_rate_sky_through``: their own
   WCS, the quadrant scale, the bar-shadow table) and fitted on the trace's pixels.
   ``grow``: the best one is extended to its neighbors across the dispersion while each
   neighbor passes light of its own, so a leak through a run of adjacent shutters
   (EMBER obs 24 next to slitlet 161: three) is fitted, judged and painted as that run.
"""
import os

import numpy as np

from campfire_pipeline.nirspec import coverage as CV

SHUTTERS_X, SHUTTERS_Y = 365, 171       # MSA shutters per quadrant: along the dispersion, across it
DO_NOT_USE = 1


# ---------------------------------------------------------------------------
# 1. The fixed-light map
# ---------------------------------------------------------------------------

def nod_position(rate_path):
    """The exposure's nod position (``PATT_NUM``, else ``EXPOSURE``)."""
    from astropy.io import fits
    h = fits.getheader(rate_path)
    return int(h.get('PATT_NUM') or h.get('EXPOSURE') or 1)


def mbkg_path_of(rate_path):
    """The ``_mbkg.fits`` beside a rate file."""
    return rate_path[:-len('_rate.fits')] + '_mbkg.fits'


def fixed_light(rate_paths):
    """Rate minus ``MBKG2D`` (DN/s), its inverse-variance mean per nod position and the
    minimum over nod positions, with that position's error. Returns a dict: ``excess``,
    ``err`` (detector shape; NaN where any nod position has no usable value), ``per_nod``
    ({position: (mean, err)}), ``sky`` (the mean ``MBKG2D``)."""
    from astropy.io import fits
    acc = {}
    sky = None
    for p in rate_paths:
        with fits.open(p, memmap=False) as h:
            rate = np.array(h['SCI'].data, float)
            err = np.array(h['ERR'].data, float)
            dq = np.array(h['DQ'].data)
        mb = np.array(fits.getdata(mbkg_path_of(p), 'MBKG2D'), float)
        ok = np.isfinite(rate) & np.isfinite(err) & (err > 0) & ((dq & DO_NOT_USE) == 0)
        d = np.where(ok, rate - np.nan_to_num(mb), 0.0)
        w = np.where(ok, 1.0 / np.where(ok, err, 1.0) ** 2, 0.0)
        n = nod_position(p)
        s, sw = acc.get(n, (0.0, 0.0))
        acc[n] = (s + w * d, sw + w)
        sky = np.nan_to_num(mb) if sky is None else sky + np.nan_to_num(mb)
    per_nod = {}
    for n, (s, sw) in acc.items():
        with np.errstate(invalid='ignore', divide='ignore'):
            per_nod[n] = (np.where(sw > 0, s / sw, np.nan), np.where(sw > 0, 1.0 / np.sqrt(sw), np.nan))
    means = np.array([per_nod[n][0] for n in sorted(per_nod)])
    errs = np.array([per_nod[n][1] for n in sorted(per_nod)])
    bad = ~np.isfinite(means).all(0)
    i = np.argmin(np.where(np.isfinite(means), means, np.inf), axis=0)
    excess = np.take_along_axis(means, i[None], 0)[0]
    err = np.take_along_axis(errs, i[None], 0)[0]
    excess[bad] = np.nan
    err[bad] = np.nan
    return dict(excess=excess, err=err, per_nod=per_nod, sky=sky / len(rate_paths))


# ---------------------------------------------------------------------------
# 2. Traces in the map
# ---------------------------------------------------------------------------

def bin_columns(a, err, width):
    """Inverse-variance mean of ``a`` over blocks of ``width`` columns: (mean, err), shape
    (ny, nx // width)."""
    ny, nx = a.shape
    nb = nx // width
    aa = a[:, :nb * width].reshape(ny, nb, width)
    ee = err[:, :nb * width].reshape(ny, nb, width)
    ok = np.isfinite(aa) & np.isfinite(ee) & (ee > 0)
    w = np.where(ok, 1.0 / np.where(ok, ee, 1.0) ** 2, 0.0)
    sw = w.sum(2)
    with np.errstate(invalid='ignore', divide='ignore'):
        m = np.where(sw > 0, (w * np.where(ok, aa, 0.0)).sum(2) / sw, np.nan)
        e = np.where(sw > 0, 1.0 / np.sqrt(sw), np.nan)
    return m, e


def find_traces(fixed, width=8, snr=5.0, min_frac=0.2, sky_floor=None, min_cols=48, min_rows=2):
    """Connected regions of fixed light: binned bins with ``excess / err > snr`` and
    ``excess > min_frac x`` the background (the larger of ``MBKG2D`` there and
    ``sky_floor``, the typical background, so light where nothing is painted needs a
    level as well as a significance). Returns a list of dicts: ``mask`` (detector
    pixels), ``cols`` (columns it spans), ``row`` (flux-weighted centre row per column,
    NaN where the trace has no bin), ``profile`` / ``profile_err`` (excess summed over
    its rows per column, DN/s), ``rows`` (its row range), ``total_snr``."""
    from scipy import ndimage
    ex, er, sky = fixed['excess'], fixed['err'], fixed['sky']
    m, e = bin_columns(ex, er, width)
    s, _ = bin_columns(sky, np.where(np.isfinite(er), er, np.nan), width)
    level = np.maximum(np.nan_to_num(s), sky_floor if sky_floor is not None else 0.0)
    with np.errstate(invalid='ignore'):
        hit = (m / e > snr) & (m > min_frac * level)
    hit = ndimage.binary_opening(hit, structure=np.ones((1, 2)))
    lab, n = ndimage.label(hit, structure=np.ones((3, 3)))
    traces = []
    ny, nx = ex.shape
    for k in range(1, n + 1):
        sel = lab == k
        rr, bb = np.nonzero(sel)
        if (bb.max() - bb.min() + 1) * width < min_cols or (rr.max() - rr.min() + 1) < min_rows:
            continue
        mask = np.zeros((ny, nx), bool)
        for r, b in zip(rr, bb):
            mask[r, b * width:(b + 1) * width] = True
        # grow by a row on both sides: the trace's wings fall below the bin threshold
        mask = ndimage.binary_dilation(mask, structure=np.ones((3, 1), bool))
        cols = np.arange(nx)
        v = np.where(mask & np.isfinite(ex), ex, 0.0)
        var = np.where(mask & np.isfinite(er), er ** 2, 0.0)
        prof = v.sum(0)
        perr = np.sqrt(var.sum(0))
        rowsum = (v * np.arange(ny)[:, None]).sum(0)
        with np.errstate(invalid='ignore', divide='ignore'):
            row = np.where(mask.any(0) & (prof > 0), rowsum / prof, np.nan)
        on = mask.any(0)
        tot = prof[on].sum() / np.sqrt(var[:, on].sum()) if on.any() else 0.0
        traces.append(dict(mask=mask, cols=cols[on], row=row, profile=prof, profile_err=perr,
                           rows=(int(rr.min()), int(rr.max())), total_snr=float(tot), label=k))
    traces.sort(key=lambda t: -t['total_snr'])
    return traces


def nod_consistency(trace, per_nod):
    """Is the trace's light the same in every nod position? Per position, the excess
    summed over the trace's mask (pixels usable in every position) and its error;
    ``spread`` = (max - min) / mean and ``chi2`` / ``dof`` of the positions about their
    weighted mean. Light fixed on the detector gives a spread consistent with noise; a
    source that moves (one larger than the nod step survives the minimum) does not."""
    ok = trace['mask'].copy()
    for m, e in per_nod.values():
        ok &= np.isfinite(m) & np.isfinite(e)
    if not ok.any():
        return dict(sums=[], errs=[], spread=np.nan, chi2=np.nan, dof=0)
    sums = np.array([float(np.sum(m[ok])) for m, e in per_nod.values()])
    errs = np.array([float(np.sqrt(np.sum(e[ok] ** 2))) for m, e in per_nod.values()])
    w = 1.0 / errs ** 2
    mean = np.sum(w * sums) / np.sum(w)
    return dict(sums=sums.tolist(), errs=errs.tolist(), spread=float((sums.max() - sums.min()) / mean) if mean > 0 else np.inf,
                chi2=float(np.sum(w * (sums - mean) ** 2)), dof=len(sums) - 1)


# ---------------------------------------------------------------------------
# 3. Which shutter, and is it the background?
# ---------------------------------------------------------------------------

def _holder(processed, slits, bbox=True):
    """An ImageModel carrying the WCS of ``slits`` on the exposure's instrument model
    (as ``coverage._assign_wcs`` does for the failed-open shutters), and the slits it
    holds: with ``bbox``, those off this detector (no bounding box) are left out."""
    from gwcs.wcs import WCS
    from jwst.assign_wcs.nirspec import generate_compound_bbox, slitlets_wcs
    from jwst.datamodels import ImageModel
    from jwst.msaflagopen.msaflagopen_step import create_reference_filename_dictionary
    refs = create_reference_filename_dictionary(processed)

    def build(ss):
        h = ImageModel()
        h.meta.wcs = WCS(slitlets_wcs(processed, refs, ss))
        h.meta.wcsinfo = processed.meta.wcsinfo
        h.meta.exposure.type = 'NRS_MSASPEC'
        return h

    holder = build(slits)
    if not bbox:
        return holder, list(slits)
    try:
        holder.meta.wcs.bounding_box = generate_compound_bbox(holder, slits)
        return holder, list(slits)
    except Exception:                         # noqa: BLE001 - one of them is off the detector
        pass
    keep = []
    for s in slits:
        try:
            generate_compound_bbox(holder, [s])
            keep.append(s)
        except Exception:                     # noqa: BLE001
            continue
    if not keep:
        return None, []
    holder = build(keep)
    holder.meta.wcs.bounding_box = generate_compound_bbox(holder, keep)
    return holder, keep


def one_shutter(name, q, x, y, ny=1):
    """A slit of ``ny`` adjacent shutters starting at MSA shutter (quadrant, x along the
    dispersion, y across it) and running up in y, with a commanded slitlet's margin and
    pitch scales (``coverage.failed_open_slitlets``). A run is one slit, as jwst builds a
    commanded slitlet (``ymax`` one pitch higher per shutter): its interior bars get the
    bar-shadow segments of two open neighbors, not two outer edges."""
    from jwst.assign_wcs.nirspec import MSA_SLIT_SCALES
    from stdatamodels.jwst.transforms.models import Slit
    ny = int(ny)
    ymax = CV.FAILED_OPEN_Y + (ny - 1) * CV._pitch(MSA_SLIT_SCALES[1])
    return Slit(int(name), int(x) + (int(y) - 1) * SHUTTERS_X, 0, int(x), int(y), -CV.FAILED_OPEN_Y, ymax,
                int(q), 0, 'x' + '1' * (ny - 1), slit_id=int(name), slit_xscale=MSA_SLIT_SCALES[0],
                slit_yscale=MSA_SLIT_SCALES[1])


class MsaMap:
    """Where any MSA shutter's spectrum lands on one rate file's detector."""

    XR, YR = 183, 86                    # the reference shutter of each quadrant

    def __init__(self, rate_path):
        from astropy.io import fits
        from jwst.assign_wcs.nirspec import MSA_SLIT_SCALES
        self.rate_path = rate_path
        self.processed = CV._assign_wcs(rate_path, CV.resolve_wavelengthrange(rate_path))[0][0]
        h = fits.getheader(rate_path)
        self.cutoffs = CV.cutoffs_for(h.get('GRATING', ''), h.get('FILTER', ''))
        self.detector = str(h.get('DETECTOR', ''))
        self.sx, self.sy = MSA_SLIT_SCALES
        self.ref = _holder(self.processed, [one_shutter(1000 + q, q, self.XR, self.YR) for q in (1, 2, 3, 4)], bbox=False)[0]

    def fixed_slit_footprints(self, shape):
        """Footprints of the fixed slits (open in every MSA exposure, left out of the
        coverage geometry): {name: detector mask}."""
        from jwst.assign_wcs.nirspec import get_open_fixed_slits, nrs_wcs_set_input
        out = {}
        slits = get_open_fixed_slits(self.processed)
        holder, kept = _holder(self.processed, slits)
        for rec in kept:
            try:
                w = nrs_wcs_set_input(holder, rec.name)
                (_, _), (ylo, yhi) = w.bounding_box
                tr = w.get_transform('detector', 'slit_frame')
                g = CV.geometry_on_block(tr, 0, 0, int(ylo) - CV.ROW_PAD, int(yhi) + 1 + CV.ROW_PAD, 'x', 1.0,
                                         self.cutoffs, ncols=shape[1], nrows=shape[0], with_segments=False)
            except Exception:                 # noqa: BLE001 - off this detector
                continue
            m = np.zeros(shape, bool)
            m[g['row_lo']:g['row_lo'] + g['footprint'].shape[0]] |= g['footprint']
            if m.any():
                out[str(rec.name)] = m
        return out

    def traces(self, q, lam_um, x=None, y=None):
        """Centre traces of shutters (``x``, ``y``: arrays, default every shutter) of
        quadrant ``q`` at wavelengths ``lam_um``: detector (col, row), each (n_shutters,
        n_lam), in this detector's pixels (NaN off the transform)."""
        from jwst.assign_wcs.nirspec import nrs_wcs_set_input
        if x is None:
            x, y = np.meshgrid(np.arange(1, SHUTTERS_X + 1), np.arange(1, SHUTTERS_Y + 1), indexing='ij')
            x, y = x.ravel(), y.ravel()
        tr = nrs_wcs_set_input(self.ref, 1000 + q).get_transform('slit_frame', 'detector')
        xs = ((np.asarray(x, float) - self.XR) * self.sx)[:, None] * np.ones(len(lam_um))
        ys = ((np.asarray(y, float) - self.YR) * self.sy)[:, None] * np.ones(len(lam_um))
        lam = np.broadcast_to(np.asarray(lam_um, float) * 1e-6, xs.shape)
        with np.errstate(invalid='ignore'):
            c, r = tr(xs, ys, lam, with_bounding_box=False)
        return np.asarray(x), np.asarray(y), np.asarray(c, float), np.asarray(r, float)

    def exact(self, candidates):
        """Exact geometry (``coverage.slitlet_geometry_extended``, with bar-shadow segments)
        of candidate shutters or runs [(q, x, y) or (q, x, y, ny), ...]: a list of blocks,
        None where off the detector."""
        slits = [one_shutter(2000 + i, *c) for i, c in enumerate(candidates)]
        holder, kept = _holder(self.processed, slits)
        names = {int(s.name) for s in kept}
        out = []
        for s in slits:
            if int(s.name) not in names:
                out.append(None)
                continue
            try:
                out.append(CV.slitlet_geometry_extended(holder, s, self.cutoffs))
            except Exception:                 # noqa: BLE001 - off the detector
                out.append(None)
        return out


def background_through(g, rate_sky, rate_q, table):
    """The count-rate background a fully open shutter of geometry ``g`` passes at every
    pixel of its block (``master_bkg._rate_sky_through``), NaN outside its footprint."""
    from campfire_pipeline.nirspec import master_bkg as MB
    centres = np.asarray(rate_sky['wavelength'], float)
    sky = (np.asarray(rate_sky['rate'], float),)
    floor_bs = float(np.nanmin(table['data1x1']))
    val = MB._rate_sky_through(g['lam'], g['seg'], g['yseg'], g['j'], len(g['state']),
                               rate_q.get(int(g['quadrant']), np.nan), centres, sky, table, floor_bs)
    return np.where(g['footprint'], val, np.nan)


def scan_trace(trace, msamap, rate_sky, row_tol=2.5, n_lam=120, quadrants=(1, 2, 3, 4), keep=20):
    """As ``scan``, but the fit uses the trace's own profile (its mask's rows summed) at
    the trace's columns only: blind to where a candidate would put light beyond the
    trace, and so to the trace's ends, but not misled by other light along a
    candidate's track. ``refine`` decides between the two rankings' candidates."""
    cols = trace['cols']
    sample = cols[np.linspace(0, len(cols) - 1, min(len(cols), 40)).astype(int)]
    sample = sample[np.isfinite(trace['row'][sample])]
    prof, perr = trace['profile'][cols], trace['profile_err'][cols]
    good = np.isfinite(prof) & np.isfinite(perr) & (perr > 0)
    centres = np.asarray(rate_sky['wavelength'], float)
    rsky = np.asarray(rate_sky['rate'], float)
    fin = np.isfinite(rsky)
    lam = np.linspace(centres[fin].min(), centres[fin].max(), n_lam)
    out = []
    for q in quadrants:
        x, y, c, r = msamap.traces(q, lam)
        ok_any = np.isfinite(c).all(1) & np.isfinite(r).all(1)
        mono = np.all(np.diff(c, axis=1) > 0, axis=1)
        idx = np.nonzero(ok_any & mono & (c[:, 0] < cols.max()) & (c[:, -1] > cols.min()))[0]
        for i in idx:
            rs = np.interp(sample, c[i], r[i], left=np.nan, right=np.nan)
            if not np.isfinite(rs).all():
                continue
            dr = rs - trace['row'][sample]
            if np.max(np.abs(dr)) > row_tol:
                continue
            lc = np.interp(cols, c[i], lam, left=np.nan, right=np.nan)
            model = np.interp(lc, centres[fin], rsky[fin], left=np.nan, right=np.nan)
            g = good & np.isfinite(model)
            if g.sum() < 10:
                continue
            w = 1.0 / perr[g] ** 2
            t = np.sum(w * prof[g] * model[g]) / np.sum(w * model[g] ** 2)
            chi2 = float(np.sum(w * (prof[g] - t * model[g]) ** 2))
            out.append(dict(q=q, x=int(x[i]), y=int(y[i]), t=float(t), chi2=chi2, dof=int(g.sum()) - 1,
                            rms_row=float(np.sqrt(np.mean(dr ** 2)))))
    out.sort(key=lambda d: d['chi2'])
    return out[:keep]


def track_profile(fixed, cols, rows, half=3):
    """The map summed over rows ``round(rows) +- half`` at each of ``cols``: (sum, err),
    NaN where any of those pixels is unusable or off the detector."""
    ex, er = fixed['excess'], fixed['err']
    ny = ex.shape[0]
    r0 = np.rint(rows).astype(int)
    off = np.arange(-half, half + 1)
    rr = r0[:, None] + off[None]
    inside = (rr >= 0) & (rr < ny)
    rrc = np.clip(rr, 0, ny - 1)
    v = ex[rrc, cols[:, None]]
    e = er[rrc, cols[:, None]]
    ok = inside & np.isfinite(v) & np.isfinite(e)
    s = np.where(ok.all(1), np.where(ok, v, 0).sum(1), np.nan)
    se = np.where(ok.all(1), np.sqrt((np.where(ok, e, 0) ** 2).sum(1)), np.nan)
    return s, se


def scan(trace, msamap, rate_sky, fixed=None, row_tol=2.5, n_lam=120, quadrants=(1, 2, 3, 4), keep=20, half=3):
    """Shutters whose spectrum would make ``trace``: those whose centre row is within
    ``row_tol`` of the trace's centre row at every column it spans (sampled), ranked by
    chi-square. The fit is made along each candidate's own track: the map summed over
    ``half`` rows about its centre row at every column where it would put light (its
    wavelengths within step 1's background table), plus the trace's columns it would
    not reach (model 0 there). So a candidate that would put light past the trace's
    end, or stop short of it, is penalized: with a featureless background (G395M) the
    trace's ends are what place the shutter along the dispersion. Model: transmission x
    the count-rate background at the wavelength each column would see. Returns a list
    of dicts (q, x, y, t, chi2, dof, rms_row), best first."""
    ny, nx = (fixed['excess'].shape if fixed is not None else (2048, 2048))
    tcols = trace['cols']
    sample = tcols[np.linspace(0, len(tcols) - 1, min(len(tcols), 40)).astype(int)]
    sample = sample[np.isfinite(trace['row'][sample])]
    centres = np.asarray(rate_sky['wavelength'], float)
    rsky = np.asarray(rate_sky['rate'], float)
    fin = np.isfinite(rsky)
    lam = np.linspace(centres[fin].min(), centres[fin].max(), n_lam)
    allc = np.arange(nx)
    out = []
    for q in quadrants:
        x, y, c, r = msamap.traces(q, lam)
        ok_any = np.isfinite(c).all(1) & np.isfinite(r).all(1)
        mono = np.all(np.diff(c, axis=1) > 0, axis=1)
        idx = np.nonzero(ok_any & mono & (c[:, 0] < tcols.max()) & (c[:, -1] > tcols.min()))[0]
        for i in idx:
            rs = np.interp(sample, c[i], r[i], left=np.nan, right=np.nan)
            dr = rs - trace['row'][sample]
            # the sampled trace columns it reaches must follow the trace's row
            reach = np.isfinite(rs)
            if reach.sum() < max(3, 0.3 * len(sample)) or np.max(np.abs(dr[reach])) > row_tol:
                continue
            if fixed is None:
                continue
            on = (allc >= c[i, 0]) & (allc <= c[i, -1])
            cols_c = allc[on]
            rows_c = np.interp(cols_c, c[i], r[i])
            inrow = (rows_c >= half) & (rows_c < ny - half)
            cols_c, rows_c = cols_c[inrow], rows_c[inrow]
            model = np.interp(np.interp(cols_c, c[i], lam), centres[fin], rsky[fin])
            data, derr = track_profile(fixed, cols_c, rows_c, half)
            # the trace's columns it does not reach: there it predicts nothing
            miss = np.setdiff1d(tcols, cols_c)
            if miss.size:
                dm = trace['profile'][miss]; em = trace['profile_err'][miss]
                cols_c = np.concatenate([cols_c, miss]); model = np.concatenate([model, np.zeros(miss.size)])
                data = np.concatenate([data, dm]); derr = np.concatenate([derr, em])
            g = np.isfinite(data) & np.isfinite(derr) & (derr > 0) & np.isfinite(model)
            if g.sum() < 10 or not np.any(model[g] > 0):
                continue
            # light far above the model (hot pixels, other sources crossing the track) is left
            # out; light the model predicts and the data lack never is
            keep_pts = g.copy()
            for _ in range(2):
                w = 1.0 / derr[keep_pts] ** 2
                den = np.sum(w * model[keep_pts] ** 2)
                t = np.sum(w * data[keep_pts] * model[keep_pts]) / den
                z = np.full(data.shape, -np.inf)
                z[g] = (data[g] - t * model[g]) / derr[g]
                keep_pts = g & (z < 10)
            w = 1.0 / derr[keep_pts] ** 2
            den = np.sum(w * model[keep_pts] ** 2)
            num = np.sum(w * data[keep_pts] * model[keep_pts])
            t = num / den
            chi2 = float(np.sum(w * (data[keep_pts] - t * model[keep_pts]) ** 2))
            # the improvement over no light at all (matched filter): predicted light where
            # the data have none lowers it through the denominator, empty columns add nothing
            gain = float(num ** 2 / den) if t > 0 else 0.0
            out.append(dict(q=q, x=int(x[i]), y=int(y[i]), t=float(t), chi2=chi2, dof=int(keep_pts.sum()) - 1, gain=gain,
                            rms_row=float(np.sqrt(np.nanmean(dr[reach] ** 2)))))
    out.sort(key=lambda d: -d['gain'])
    return out[:keep]


def fit_run(trace, fixed, g, rate_sky, rate_q, table):
    """One shutter or run of shutters (geometry ``g`` from ``MsaMap.exact``) painted
    exactly and fitted to the trace's pixels, one transmission per shutter (linear least
    squares over the trace mask and the run's footprint above 10 % of its peak, with the
    map's errors, once more without the pixels beyond 5 sigma of the first fit: the map has
    no outlier rejection of its own). Returns None when a shutter has no pixel to fit,
    else a dict: ``ts`` / ``terrs`` (per shutter, up the slit), ``t`` (their mean weighted
    by each shutter's painted light), chi-square ``chi2`` over ``n`` pixels, ``gain`` (the
    improvement over no light on the same pixels: explaining the trace raises it,
    predicting light where there is none lowers it, empty pixels add nothing; 0 unless
    every shutter passes light), and ``bands``: the ratio excess / painted in wavelength
    bands ((lam_lo, lam_hi, ratio, err), the painted model at each shutter's fitted share)
    to judge the spectral shape. One shutter: its transmission and the ratio to its paint."""
    ex, er = fixed['excess'], fixed['err']
    ny, nx = ex.shape
    ns = len(g['state'])
    val = background_through(g, rate_sky, rate_q, table)
    r0, h = g['row_lo'], val.shape[0]
    model = np.full((ny, nx), np.nan)
    lamf = np.full((ny, nx), np.nan)
    jf = np.full((ny, nx), -1, int)
    model[r0:r0 + h] = val
    lamf[r0:r0 + h] = g['lam']
    jf[r0:r0 + h] = np.where(g['footprint'], g['j'], -1)
    peak = np.nanmax(model) if np.isfinite(model).any() else 0
    if not peak > 0:
        return None
    with np.errstate(invalid='ignore'):
        reg = (trace['mask'] | (model > 0.1 * peak)) & np.isfinite(ex) & np.isfinite(er) & (er > 0)
    m = np.where(np.isfinite(model), model, 0.0)
    if not reg.any():
        return None

    def design(r):
        return np.stack([np.where(jf[r] == i, m[r], 0.0) for i in range(ns)], 1)

    for _ in range(2):
        w = 1.0 / er[reg] ** 2
        a = design(reg)
        nrm = (a * w[:, None]).T @ a
        if not np.all(np.diag(nrm) > 0):
            return None
        ts = np.linalg.solve(nrm, (a * w[:, None]).T @ ex[reg])
        z = np.zeros(reg.shape)
        z[reg] = (ex[reg] - a @ ts) / er[reg]
        reg = reg & (np.abs(z) < 5)
        if not reg.any():
            return None
    terrs = np.sqrt(np.diag(np.linalg.inv(nrm)))
    a = design(reg)
    chi2 = float(np.sum((ex[reg] - a @ ts) ** 2 / er[reg] ** 2))
    gain = float(np.sum(ex[reg] ** 2 / er[reg] ** 2)) - chi2 if np.all(ts > 0) else 0.0
    light = a.sum(0)
    t = float(np.sum(ts * light) / np.sum(light)) if ns > 1 else float(ts[0])
    # the painted model with each shutter at its share of the mean transmission
    share = ts / t if t != 0 else np.ones(ns)
    pm = np.where(jf >= 0, m * share[np.clip(jf, 0, ns - 1)], 0.0) if ns > 1 else m
    ppk = float(np.max(pm)) if ns > 1 else peak
    bands = []
    lam_t = lamf[reg & (pm > 0.3 * ppk)]
    if lam_t.size:
        edges = np.quantile(lam_t, np.linspace(0, 1, 7))
        for lo, hi in zip(edges[:-1], edges[1:]):
            b = reg & (pm > 0.3 * ppk) & (lamf >= lo) & (lamf <= hi)
            if b.sum() < 5:
                continue
            wb = 1.0 / er[b] ** 2
            rb = np.sum(wb * ex[b] * pm[b]) / np.sum(wb * pm[b] ** 2)
            eb = 1.0 / np.sqrt(np.sum(wb * pm[b] ** 2))
            bands.append((float(lo), float(hi), float(rb), float(eb)))
    return dict(ny=ns, t=t, ts=[float(v) for v in ts], terrs=[float(v) for v in terrs], chi2=chi2, gain=gain,
                n=int(reg.sum()), bands=bands, model=pm, region=reg)


def refine(trace, fixed, msamap, candidates, rate_sky, rate_q, table, neighbors=1):
    """The candidates and their neighbors (+-``neighbors`` shutters in x and y), each
    painted alone and fitted to the trace's pixels (``fit_run``). Best (highest gain)
    first; each dict carries its shutter's ``q`` / ``x`` / ``y``."""
    seen = []
    for c in candidates:
        for dx in range(-neighbors, neighbors + 1):
            for dy in range(-neighbors, neighbors + 1):
                k = (c['q'], c['x'] + dx, c['y'] + dy)
                if k not in seen and 1 <= k[1] <= SHUTTERS_X and 1 <= k[2] <= SHUTTERS_Y:
                    seen.append(k)
    res = []
    for k, g in zip(seen, msamap.exact(seen)):
        r = fit_run(trace, fixed, g, rate_sky, rate_q, table) if g is not None else None
        if r is not None:
            res.append(dict(r, q=k[0], x=k[1], y=k[2]))
    res.sort(key=lambda d: -d['gain'])
    return res


# a run grows by a neighbor across the dispersion (same quadrant and x: a neighbor along
# it lands on the same rows a few columns over, which a smooth background cannot tell
# from more transmission) when the neighbor's own transmission is above MIN_RATIO and
# GROW_SNR of its error and the fit's gain rises; at most MAX_RUN shutters
GROW_SNR, MAX_RUN = 5.0, 5


def grow(trace, fixed, msamap, best, rate_sky, rate_q, table):
    """``best`` (a ``refine`` result) extended one shutter at a time, up or down the slit,
    whichever passes the rule above with the larger gain, until neither does. Returns the
    run's ``fit_run`` dict with ``q`` / ``x`` / ``y`` (its lowest shutter) / ``ny``."""
    cur = best
    while cur['ny'] < MAX_RUN:
        q, x, y, n = cur['q'], cur['x'], cur['y'], cur['ny']
        trials = []
        if y > 1:
            trials.append(((q, x, y - 1, n + 1), 0))          # the new shutter is the run's first
        if y + n <= SHUTTERS_Y:
            trials.append(((q, x, y, n + 1), n))              # ... or its last
        ok = []
        for (key, i), g in zip(trials, msamap.exact([k for k, i in trials])):
            r = fit_run(trace, fixed, g, rate_sky, rate_q, table) if g is not None else None
            if r is None:
                continue
            tn, en = r['ts'][i], r['terrs'][i]
            if min(r['ts']) > 0 and tn > MIN_RATIO and tn > GROW_SNR * en and r['gain'] > cur['gain']:
                ok.append(dict(r, q=key[0], x=key[1], y=key[2]))
        if not ok:
            break
        cur = max(ok, key=lambda d: d['gain'])
    return cur


# ---------------------------------------------------------------------------
# 4. Detection on one configuration x detector, and the shutters' footprints
# ---------------------------------------------------------------------------

# what counts as "has the background's spectrum": the excess / painted ratio in at least
# MIN_BANDS wavelength bands, above MIN_RATIO, every band within FLAT_TOL of their median,
# and the painted shutter fits the trace's pixels to CHI2_MAX per pixel. Calibrated on
# zenith_p15 (549120's shutter) and ember_cosmos_p1 v1.5 (the msaoper failed-open shutters
# rediscovered at their listed positions), PRISM, 2026-11-11.
MIN_BANDS, MIN_RATIO, FLAT_TOL, CHI2_MAX = 3, 0.05, 0.15, 2.0
LISTED_OVERLAP = 0.3     # a trace this much on a painted msaoper failed-open shutter is that shutter's
GRATINGS = ('PRISM',)    # dispersers searched (G395M: a featureless background places x to +-10 shutters,
                         # and failed-open shutters there pass a chromatic fraction; not yet)


def group_key(rate_path):
    """Exposures that share an MSA configuration, disperser and detector: the same
    shutters are open and land on the same pixels, so the same fixed light."""
    from astropy.io import fits
    h = fits.getheader(rate_path)
    return (str(h.get('MSAMETFL', '')), int(h.get('MSACONID', 0) or 0), str(h.get('GRATING', '')),
            str(h.get('FILTER', '')), str(h.get('DETECTOR', '')))


def verdict(best):
    """Whether a ``refine`` fit is a shutter passing the background."""
    rb = np.array([r for lo, hi, r, e in best['bands']])
    if rb.size < MIN_BANDS:
        return False
    med = np.median(rb)
    return bool(med > MIN_RATIO and np.max(np.abs(rb / med - 1)) < FLAT_TOL and best['chi2'] / best['n'] < CHI2_MAX)


def detect(rate_paths, snr=5.0, min_frac=0.2, min_rows=4, max_traces=40, logger=None):
    """Unlisted shutters passing the background in one configuration x detector
    (``rate_paths``: its rate files, each with its ``_mbkg.fits`` beside it).

    Fixed light (``fixed_light``) in traces (``find_traces``); a trace on a fixed slit, one
    whose light moves between nod positions (a source larger than the nod step) and one on
    a failed-open shutter the master background painted are left alone; for the others the
    candidate shutters of both scans are refined and the best one kept when it passes
    ``verdict``. Needs at least two nod positions. Returns a list of dicts (``q``, ``x``,
    ``y``, ``t``: transmission fitted on the map, ``chi2n``, ``bands``, ``trace``: its
    detector rows and columns)."""
    from astropy.io import fits
    from campfire_pipeline.nirspec import master_bkg as MB
    from campfire_pipeline.nirspec import master_bkg_rate as R
    say = logger or (lambda s: None)
    rate_paths = [p for p in rate_paths if os.path.exists(mbkg_path_of(p))]
    if not rate_paths:
        return []
    fx = fixed_light(rate_paths)
    if len(fx['per_nod']) < 2:
        say(f'{len(rate_paths)} exposure(s) at one nod position: fixed light cannot be told from sources, not searched')
        return []
    painted = fx['sky'] > 0
    if not painted.any():
        return []
    traces = find_traces(fx, snr=snr, min_frac=min_frac, sky_floor=float(np.nanmedian(fx['sky'][painted])), min_rows=min_rows)
    if not traces:
        return []
    rate0 = rate_paths[0]
    rate_sky, rate_q = R.read_rate_sky(rate0)
    if rate_sky is None:
        return []
    mm = MsaMap(rate0)
    fs = mm.fixed_slit_footprints(fx['excess'].shape)
    table = MB.resolve_barshadow_table(fits.getheader(mbkg_path_of(rate0))['MBKGBSRF'], rate0)
    listed = (fits.getdata(mbkg_path_of(rate0), 'MBKG2DDQ') & MB.DQ_FAILED_OPEN) > 0
    found = []
    for i, t in enumerate(traces[:max_traces]):
        where = f'trace rows {t["rows"][0]}-{t["rows"][1]} cols {t["cols"].min()}-{t["cols"].max()}'
        n_mask = t['mask'].sum()
        if any((m & t['mask']).sum() > 0.5 * n_mask for m in fs.values()):
            continue                                          # a fixed slit: open in every exposure
        nc = nod_consistency(t, fx['per_nod'])
        if nc['spread'] > 0.2 and nc['chi2'] / max(nc['dof'], 1) > 9:
            continue                                          # moves between nods: a source
        if (listed & t['mask']).sum() > LISTED_OVERLAP * n_mask:
            continue                                          # an msaoper failed-open shutter, painted already
        cands, seen = [], set()
        for tol in (2.5, 5.0):      # the reference-slit placement can be a few pixels off near the detector edges
            for c in scan(t, mm, rate_sky, fixed=fx, keep=3, row_tol=tol) + scan_trace(t, mm, rate_sky, keep=3, row_tol=tol):
                if (c['q'], c['x'], c['y']) not in seen:
                    seen.add((c['q'], c['x'], c['y']))
                    cands.append(c)
        if not cands:
            continue
        best = refine(t, fx, mm, cands, rate_sky, rate_q, table, neighbors=2)
        if not best:
            continue
        b = grow(t, fx, mm, best[0], rate_sky, rate_q, table)
        ok = verdict(b)
        rb = [r for lo, hi, r, e in b['bands']]
        each = f' (each {", ".join(f"{v:.2f}" for v in b["ts"])})' if b['ny'] > 1 else ''
        say(f'{where}: {run_label(b["q"], b["x"], b["y"], b["ny"])} transmission {b["t"]:.2f}{each} '
            f'chi2/n {b["chi2"] / b["n"]:.2f} '
            f'excess/background {min(rb, default=np.nan):.2f}-{max(rb, default=np.nan):.2f} -> '
            + ('passes the background: painted' if ok else 'not the background\'s spectrum: left'))
        taken = {(f['q'], f['x'], y) for f in found for y in range(f['y'], f['y'] + f['ny'])}
        if ok and not taken & {(b['q'], b['x'], y) for y in range(b['y'], b['y'] + b['ny'])}:
            found.append(dict(q=int(b['q']), x=int(b['x']), y=int(b['y']), ny=int(b['ny']), t=float(b['t']),
                              ts=list(b['ts']), chi2n=float(b['chi2'] / b['n']),
                              ratio_lo=float(min(rb)), ratio_hi=float(max(rb)),
                              rows=t['rows'], cols=(int(t['cols'].min()), int(t['cols'].max()))))
    return found


def run_label(q, x, y, ny=1):
    """``q4 x188 y24-26`` (a run) or ``q4 x188 y26`` (one shutter)."""
    return f'q{int(q)} x{int(x)} y{int(y)}' + (f'-{int(y) + int(ny) - 1}' if int(ny) > 1 else '')


def shutter_name(q, x, y, ny=1):
    """The painter's name of an unlisted shutter or run (never an msaoper index or a
    slitlet id): ``U4_86_43``, ``U4_188_24-26``."""
    return f'U{int(q)}_{int(x)}_{int(y)}' + (f'-{int(y) + int(ny) - 1}' if int(ny) > 1 else '')


def failed_blocks(rate_path, shutters, shape=(2048, 2048)):
    """The footprints of unlisted shutters or runs [(q, x, y) or (q, x, y, ny), ...] on one
    rate file, in the form ``coverage.RateGeometry.blocks`` gives the msaoper failed-open
    shutters (kind ``failed``, flat ``pix``, ``lam``, ``f``, ``j``, ``is_open``, ``seg`` /
    ``yseg``, a commanded slitlet's margin; a run is one slit, so one block with one
    transmission), on the rate file's own WCS. Shutters off the detector are left out."""
    from astropy.io import fits
    processed = CV._assign_wcs(rate_path, CV.resolve_wavelengthrange(rate_path))[0][0]
    h = fits.getheader(rate_path)
    cutoffs = CV.cutoffs_for(h.get('GRATING', ''), h.get('FILTER', ''))
    shutters = [tuple(s) + (1,) * (4 - len(s)) for s in shutters]
    slits = [one_shutter(3000 + i, *s) for i, s in enumerate(shutters)]
    holder, kept = _holder(processed, slits)
    names = {int(s.name) for s in kept}
    out = []
    for (q, x, y, ny), s in zip(shutters, slits):
        if int(s.name) not in names:
            continue
        try:
            g = CV.slitlet_geometry_extended(holder, s, cutoffs, nrows=shape[0], ncols=shape[1])
        except Exception:                     # noqa: BLE001 - off the detector
            continue
        foot = g['footprint']
        if not foot.any():
            continue
        rr, cc = np.nonzero(foot)
        out.append(dict(kind='failed', name=shutter_name(q, x, y, ny), quadrant=int(q), ycen=0.0, source_id=0,
                        state=str(s.shutter_state),
                        pix=((rr + g['row_lo']) * shape[1] + cc).astype(np.int64), lam=g['lam'][foot].astype(float),
                        ys=g['ys'][foot].astype(float), k=g['k'][foot], f=g['f'][foot], j=g['j'][foot],
                        is_open=g['is_open'][foot], kx=0, seg=g['seg'][foot], yseg=g['yseg'][foot]))
    return out
