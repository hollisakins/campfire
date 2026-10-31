"""
Stuck-closed MSA shutters, measured from the count-rate frame.

A shutter that was commanded open but stayed closed passes neither the sky nor its
source. On the gratings its detector rows are not dark, because other slitlets' traces
land on the same pixels, so a test on the cutout's own pixels cannot see it. This module
asks the question directly: every commanded-open shutter of the exposure contributes a
known sky pattern to the detector (its sky template), and the count-rate frame is fitted
as the sum of all of them plus a constant,

    rate(pixel) = c + sum over shutters s of a_s T_s(pixel),

so overlapping traces are separate terms of one fit. An open shutter has a ~ 1, a stuck
one a ~ 0 (design: docs/design-nirspec-pipeline-v2.md; study:
scratch/claude/stuck_shutters/REPORT.md, ember_cosmos_p1: all 15 hand-vetted PRISM
shutters at a = 0.00-0.02, no other shutter below 0.92).

* Template: the exposure's count-rate sky at the open-shutter centre (MBKG1DR) times the
  quadrant scale times the bar shadow of the commanded shutter state, over the shutter's
  footprint on the detector, past both ends of the calibrated range to the disperser's
  cutoffs (``coverage.slitlet_geometry_extended``). Built from the commanded plan (the
  original metafile), so a shutter a list already removed is still measured. Failed-open
  shutters (msaoper) get templates too, fitted but not reported.
* Fit: weighted by 1/ERR^2 over every good pixel a template covers, clipped at 4 sigma,
  dense normal equations (about a thousand templates), so each error is the marginal one
  and includes the overlap with other shutters. Two fits: 'sky' leaves out the source
  shutters of this dither point (plus a pad), 'all' keeps every pixel. An empty shutter is
  measured by the 'sky' fit, a source shutter by the 'all' fit (a source in an open
  shutter raises a above 1; a stuck shutter passes no source light either).
* Call (``call_shutters``): per physical shutter over all nods and both detectors of one
  configuration, stuck when the median amplitude is below ``a_cut`` from at least
  ``min_n`` measurements and the detectors agree.
"""

import os

import numpy as np
import scipy.sparse as sp

FIXED_SLITS = ('S200A1', 'S200A2', 'S400A1', 'S1600A1', 'S200B1')
DO_NOT_USE = 1
SRC_PAD = 0.15            # shutter pitches beyond a source shutter's edges left out of the 'sky' fit
MIN_TEMPLATE_PIX = 50     # templates with fewer footprint pixels are not built
MIN_FIT_PIX = 200         # a measurement needs this many fitted pixels
DETECTOR_SHAPE = (2048, 2048)
COLUMNS = ('q', 'row', 'col', 'slitlet_id', 'j', 'source_id', 'role', 'detector', 'n_pix', 'lam_lo', 'lam_hi',
           'a', 'a_err')


# ---------------------------------------------------------------------------
# Geometry (until master-background step 1 hands over its assign_wcs cache)
# ---------------------------------------------------------------------------
def rate_geometry(rate_path):
    """The commanded MSA slitlets and the failed-open shutters of a rate file, with the WCS
    that places them on the detector: dict(msa=(model, slits), failed=(model, slits)).

    ``assign_wcs`` on the rate file in memory, with the metafile the rate header names (the
    original one, so every commanded-open shutter is present)."""
    from gwcs.wcs import WCS
    from jwst.assign_wcs import AssignWcsStep
    from jwst.assign_wcs.nirspec import generate_compound_bbox, slitlets_wcs
    from jwst.datamodels import ImageModel
    from jwst.msaflagopen.msaflag_open import create_slitlets
    from jwst.msaflagopen.msaflagopen_step import MSAFlagOpenStep, create_reference_filename_dictionary

    prev = os.getcwd()
    os.chdir(os.path.dirname(os.path.abspath(rate_path)))
    try:
        with ImageModel(os.path.basename(rate_path)) as model:
            step = AssignWcsStep()
            step.input_dir = os.getcwd()
            processed = step.run(model)
        slits = processed.meta.wcs.get_transform('gwa', 'slit_frame').slits
        failed = create_slitlets(MSAFlagOpenStep().get_reference_file(processed, 'msaoper'))
        fo = ImageModel()
        if failed:
            fo.meta.wcs = WCS(slitlets_wcs(processed, create_reference_filename_dictionary(processed), failed))
            fo.meta.wcsinfo = processed.meta.wcsinfo
            fo.meta.exposure.type = 'NRS_MSASPEC'
            fo.meta.wcs.bounding_box = generate_compound_bbox(fo, failed)
    finally:
        os.chdir(prev)
    return dict(msa=(processed, list(slits)), failed=(fo, list(failed or [])))


def _shutter_info(rate_path):
    """The metafile's SHUTTER_INFO rows of this exposure's configuration and dither point."""
    from astropy.io import fits
    from astropy.table import Table
    h = fits.getheader(rate_path)
    name = os.path.basename(h.get('OGMETFL') or h['MSAMETFL'])
    meta = Table.read(os.path.join(os.path.dirname(os.path.abspath(rate_path)), name), hdu='SHUTTER_INFO')
    sel = (meta['msa_metadata_id'] == int(h['MSAMETID'])) & (meta['dither_point_index'] == int(h.get('PATT_NUM', 0) or 0))
    return meta[sel], h


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def build_templates(geometry, rate_sky, qscales, table, shutter_info, cutoffs, shape=DETECTOR_SHAPE):
    """Sparse template matrix (detector pixel x shutter), the parameters of each column, and
    the pixels of this dither point's source shutters (plus ``SRC_PAD``).

    rate_sky : table with ``wavelength`` [um] and ``rate`` (MBKG1DR, count rate at the open
        shutter centre); qscales : {quadrant: scale}; table : the bar-shadow table."""
    from campfire_pipeline.nirspec import barshadow_calibration as B
    from campfire_pipeline.nirspec.coverage import slitlet_geometry_extended

    rlam = np.asarray(rate_sky['wavelength'], float)
    rval = np.asarray(rate_sky['rate'], float)
    fin = np.isfinite(rlam) & np.isfinite(rval)
    rows, cols, vals, params, src = [], [], [], [], []
    for kind in ('msa', 'failed'):
        holder, recs = geometry[kind]
        for rec in recs:
            if str(rec.name) in FIXED_SLITS:
                continue
            try:
                g = slitlet_geometry_extended(holder, rec, cutoffs, nrows=shape[0], ncols=shape[1])
            except Exception:                               # noqa: BLE001 - a slitlet off the detector
                continue
            foot = g['footprint']
            if not foot.any():
                continue
            lam = g['lam']
            sky = np.interp(lam, rlam[fin], rval[fin], left=np.nan, right=np.nan)
            q = qscales.get(int(rec.quadrant), np.nan) if qscales else np.nan
            if np.isfinite(q) and q > 0:
                sky = sky * q
            bs = np.full(lam.shape, np.nan)
            inside = g['seg'] >= 0
            if inside.any():
                bs[inside] = B.evaluate_table(table, g['seg'][inside], g['yseg'][inside], lam[inside])
            val = sky * bs
            state = g['state']
            col_of_j, src_j, row, sid = {}, set(), int(rec.ycen), int(rec.source_id or 0)
            if kind == 'msa':
                mr = shutter_info[shutter_info['slitlet_id'] == int(rec.name)]
                if len(mr):
                    c = np.asarray(mr['shutter_column'], int)
                    c0 = int(c.min())
                    if len(state) == int(c.max()) - c0 + 1:
                        col_of_j = {jj: c0 + jj for jj in range(len(state))}
                    for r in mr:
                        if int(r['source_id']) > 0 and str(r['primary_source']).strip() == 'Y':
                            src_j.add(int(r['shutter_column']) - c0)
                    row = int(mr['shutter_row'][0])
            if sid <= 0:
                sid = -int(rec.name) if str(rec.name).isdigit() else 0
            for jj, ch in enumerate(state):
                if ch == '0':
                    continue
                sel = foot & (g['j'] == jj) & np.isfinite(val) & (val > 0)
                if sel.sum() < MIN_TEMPLATE_PIX:
                    continue
                rr, cc = np.nonzero(sel)
                pix = (rr + g['row_lo']) * shape[1] + cc
                params.append(dict(kind=kind, slitlet_id=int(rec.name) if str(rec.name).isdigit() else -1,
                                   q=int(rec.quadrant), row=row, col=int(col_of_j.get(jj, -1)), j=jj,
                                   source_id=sid, src=jj in src_j,
                                   lam_lo=float(np.nanmin(lam[sel])), lam_hi=float(np.nanmax(lam[sel]))))
                rows.append(pix.astype(np.int64))
                cols.append(np.full(pix.size, len(params) - 1, np.int64))
                vals.append(val[sel])
            if src_j:
                jg, f = g['j'], g['f']
                m = np.zeros(foot.shape, bool)
                for jj in src_j:
                    m |= foot & ((jg == jj) | ((jg == jj - 1) & (f > 0.5 - SRC_PAD)) | ((jg == jj + 1) & (f < -0.5 + SRC_PAD)))
                rr, cc = np.nonzero(m)
                src.append(((rr + g['row_lo']) * shape[1] + cc).astype(np.int64))
    n = shape[0] * shape[1]
    A = sp.csc_matrix((np.concatenate(vals) if vals else np.zeros(0),
                       (np.concatenate(rows) if rows else np.zeros(0, int),
                        np.concatenate(cols) if cols else np.zeros(0, int))), shape=(n, len(params)))
    src_mask = np.unique(np.concatenate(src)) if src else np.zeros(0, np.int64)
    return A, params, src_mask


# ---------------------------------------------------------------------------
# The fit
# ---------------------------------------------------------------------------
def solve(A, y, ivar, n_iter=6, clip=4.0):
    """Weighted least squares of y = A a + c with ``n_iter`` rounds of ``clip``-sigma clipping
    (sigma from the median absolute deviation of the normalised residual).

    Returns (a, err, nfit, chi2_red, const): a and err per column of A (err NaN for a column
    left with no pixel), the marginal errors scaled by sqrt(chi2_red), the number of pixels
    each column was fitted on, and the constant."""
    n, p = A.shape
    Ac = sp.hstack([sp.csr_matrix(A), sp.csr_matrix(np.ones((n, 1)))]).tocsr()
    keep = np.ones(n, bool)
    a = np.zeros(p + 1)
    for _ in range(n_iter):
        Aw, iv = Ac[keep], ivar[keep]
        N = (Aw.T @ sp.diags(iv) @ Aw).toarray()
        b = Aw.T @ (iv * y[keep])
        a = np.linalg.solve(N + np.diag((np.diag(N) == 0).astype(float)), b)
        z = (y - Ac @ a) * np.sqrt(ivar)
        s = 1.4826 * np.median(np.abs(z[keep]))
        new = np.abs(z) < clip * s
        if np.array_equal(new, keep):
            break
        keep = new
    Aw, iv = Ac[keep], ivar[keep]
    N = (Aw.T @ sp.diags(iv) @ Aw).toarray()
    empty = np.diag(N) == 0
    Ni = np.linalg.pinv(N + np.diag(empty.astype(float)))
    chi2 = float(np.sum((y[keep] - Aw @ a) ** 2 * iv) / max(int(keep.sum()) - p - 1, 1))
    err = np.sqrt(np.diag(Ni) * chi2)
    err[empty] = np.nan
    nfit = np.asarray((Aw != 0).sum(axis=0)).ravel()
    return a[:p], err[:p], nfit[:p], chi2, float(a[p])


def fit_shutters(rate_path, geometry=None, rate_sky=None, qscales=None, table=None, msaoper=None):
    """Amplitude of every commanded-open shutter of one rate file (one exposure x detector).

    geometry : ``rate_geometry`` output (master-background step 1 will hand over its own);
        built from ``rate_path`` when None. msaoper is part of the geometry here (the
        failed-open shutters come with it) and is accepted for the agreed signature.
    rate_sky : MBKG1DR table (``wavelength``, ``rate``); qscales : {quadrant: scale};
    table : bar-shadow table.

    Returns an astropy Table, one row per commanded-open shutter with a template:
    ``COLUMNS`` (role 'src' = source shutter of this dither point, measured on every pixel;
    'sky' = empty shutter, measured with the source shutters left out; 'all' = empty
    shutter with too few pixels left for the 'sky' fit)."""
    from astropy.io import fits
    from astropy.table import Table
    from campfire_pipeline.nirspec.coverage import cutoffs_for

    info, h = _shutter_info(rate_path)
    cutoffs = cutoffs_for(str(h['GRATING']).upper(), str(h['FILTER']).upper())
    if geometry is None:
        geometry = rate_geometry(rate_path)
    A, params, src_mask = build_templates(geometry, rate_sky, qscales, table, info, cutoffs)
    with fits.open(rate_path, memmap=False) as f:
        rate = np.asarray(f['SCI'].data, float).ravel()
        err = np.asarray(f['ERR'].data, float).ravel()
        dq = np.asarray(f['DQ'].data).ravel()
    covered = np.asarray((A != 0).sum(axis=1)).ravel() > 0
    good = np.isfinite(rate) & np.isfinite(err) & (err > 0) & ((dq & DO_NOT_USE) == 0) & covered
    srcm = np.zeros(rate.size, bool)
    srcm[src_mask] = True
    res = {}
    Acsr = A.tocsr()
    for tag, sel in (('sky', good & ~srcm), ('all', good)):
        idx = np.flatnonzero(sel)
        res[tag] = solve(Acsr[idx], rate[idx], 1.0 / err[idx] ** 2)
    det = str(h.get('DETECTOR', '')).lower()
    out = []
    for i, p in enumerate(params):
        if p['kind'] != 'msa' or p['col'] < 0:
            continue
        n_sky = int(res['sky'][2][i])
        if p['src']:
            role, tag = 'src', 'all'
        elif n_sky >= MIN_FIT_PIX:
            role, tag = 'sky', 'sky'
        else:
            role, tag = 'all', 'all'
        out.append((p['q'], p['row'], p['col'], p['slitlet_id'], p['j'], p['source_id'], role, det,
                    int(res[tag][2][i]), p['lam_lo'], p['lam_hi'], float(res[tag][0][i]), float(res[tag][1][i])))
    t = Table(rows=out, names=COLUMNS) if out else Table(names=COLUMNS)
    t.meta.update(chi2_sky=res['sky'][3], chi2_all=res['all'][3], const_sky=res['sky'][4], const_all=res['all'][4])
    return t


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------
def call_shutters(tables, a_cut=0.3, min_n=2, guard=0.5, min_pix=MIN_FIT_PIX):
    """Per physical shutter of one configuration (all nods x both detectors of a root):
    {(q, row, col): dict(stuck, a, n, note)}.

    Measurements with fewer than ``min_pix`` fitted pixels are not used. A shutter is stuck
    when the median of its measurements is below ``a_cut``, it has at least ``min_n`` of
    them, and the two detectors' medians agree within ``guard`` (the light past the
    calibrated range can give one detector a spurious negative amplitude). Every shutter
    with a measurement appears; undetermined ones carry stuck=False and a note."""
    by = {}
    for t in tables:
        for r in t:
            if int(r['n_pix']) < min_pix or not np.isfinite(r['a']):
                continue
            by.setdefault((int(r['q']), int(r['row']), int(r['col'])), []).append((str(r['detector']), float(r['a'])))
    out = {}
    for key, ms in by.items():
        a = np.array([m[1] for m in ms])
        med = float(np.median(a))
        per_det = {}
        for d, v in ms:
            per_det.setdefault(d, []).append(v)
        dmed = [float(np.median(v)) for v in per_det.values()]
        disagree = len(dmed) > 1 and (max(dmed) - min(dmed)) > guard
        if disagree:
            stuck, note = False, 'guard: detectors disagree'
        elif med < a_cut and a.size < min_n:
            stuck, note = False, f'n<{min_n}'
        elif med < a_cut:
            stuck, note = True, ''
        elif med < 0.8:
            stuck, note = False, 'review: partly open or unmodelled light'
        else:
            stuck, note = False, ''
        out[key] = dict(stuck=stuck, a=med, n=int(a.size), note=note)
    return out
