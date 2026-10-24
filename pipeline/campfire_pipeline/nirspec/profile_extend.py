"""
A source's light past the long-wavelength end of its cutout, for predicting it on
other sources (prototype, extraction v2; ``contam_pass1_extend``).

jwst cuts each slitlet at the end of the disperser's nominal wavelength range
(``wavelengthrange``: G395M/F290LP 2.87-5.27 um), but the long-pass filter does not
stop the light there: a bright source's trace continues towards the detector's
cut-off (~5.5 um). The trace maps follow it (``trace_overlap``, ``BEYOND_RANGE``, to
5.6 um), so the pixels where it lands on another source's trace are known, but pass 1
had no spectrum for them and they were masked as unpredicted (2976 on 5215: 4.42-4.72
um of 5215's trace).

There is no calibration there: the F290LP/G395M fast flats stop at 5.27-5.285 um, the
photom response at 5.20 um. None is needed. The prediction moves a contaminant's light
onto the target's pixels and converts it with the target's own calibration of those
pixels (``profile_contam.predict``: target calibration over the contaminant's). So past
its cutout the contaminant's light is fitted directly in detector units (count rate):

* pixels: the source's slitlet past its cutout's red end, the cutout's rows plus
  ``EXT_ROWS`` above and below (the trace curves), up to ``EXT_MAX_WAVE`` of
  extrapolated wavelength, on the detector; each exposure's own slit WCS (jwst's
  transform extrapolates past the bounding box) gives the slit position and the
  wavelength of every pixel;
* data: the nod differences of the count-rate files with the source's own partners;
  pixels lit by another source's source shutter in any term are masked (as in pass 1);
* model: the source's fixed pass-1 shape (its cross-dispersion image and along-slit
  throughput, the path loss held at its last value), times a spectrum on the native
  wavelength grid past the cutout, in count-rate units;
* the spectrum is clipped and smoothed like the pass-1 spectrum.

The result is added to the source's pass-1 frames with calibration 1 (rate units),
flagged ``ext``. Nothing else changes.
"""

import os

import numpy as np
import scipy.sparse as sp
from scipy.sparse.linalg import splu

from campfire_pipeline.nirspec import profile_fit as PF
from campfire_pipeline.nirspec import trace_overlap as TO

EXT_ROWS = TO.BEYOND_ROWS
EXT_MAX_WAVE = TO.BEYOND_MAX_WAVE
DETECTOR = 2048
MIN_PIXELS = 30
BACK = 80            # columns inside the cutout the region starts at (G395M: ~0.14 um)


def _wcs_on(path, rows, cols):
    """Slit position and wavelength [um] of exposure ``path``'s slit at detector rows /
    columns (0-based), from its own cutout WCS (extrapolated past its bounding box), and
    the cutout's (ystart, xstart, ny, nx)."""
    from jwst.datamodels import MultiSlitModel
    with MultiSlitModel(path) as m:
        s = m.slits[0]
        tr = s.meta.wcs.get_transform('detector', 'slit_frame')
        Y, X = np.meshgrid(rows - (s.ystart - 1), cols - (s.xstart - 1), indexing='ij')
        _, ys, lam = tr(X.astype(float), Y.astype(float))
        box = (int(s.ystart), int(s.xstart), int(s.data.shape[0]), int(s.data.shape[1]))
    lam = np.asarray(lam, float)
    if np.nanmax(np.abs(lam)) < 1e-3:
        lam = lam * 1e6
    return np.asarray(ys, float), lam, box


def canvas(exps):
    """The detector region past the red end of a source's cutouts, per background group:
    {group: dict(rows, cols)} (0-based detector indices), or {} when no group has one.

    The red end is the side of the cutout where the wavelength grows; the region runs
    from the column after the cutout's last one to the detector edge, rows widened by
    ``EXT_ROWS``; columns whose wavelength exceeds ``EXT_MAX_WAVE`` in every row and
    exposure are dropped later."""
    out = {}
    for g in sorted({e.group for e in exps}):
        ks = [k for k, e in enumerate(exps) if e.group == g and e.path]
        if not ks:
            continue
        e = exps[ks[0]]
        r0, c0, ny, nx = e.cutout
        y0, x0 = int(e.origin[0]) - 1 + r0, int(e.origin[1]) - 1 + c0          # 0-based detector corner
        mid = ny // 2
        red_right = np.nanmedian(e.wav[r0 + mid, c0 + nx - 5:c0 + nx]) > np.nanmedian(e.wav[r0 + mid, c0:c0 + 5])
        # from BACK columns inside the cutout (its last samples carry no flat field: the pass-1
        # spectrum stops short of the cutout's end) to the detector edge
        if red_right:
            cols = np.arange(max(x0 + nx - BACK, 0), DETECTOR)
        else:
            cols = np.arange(0, min(x0 + BACK, DETECTOR))
        rows = np.arange(max(y0 - EXT_ROWS, 0), min(y0 + ny + EXT_ROWS, DETECTOR))
        if cols.size and rows.size:
            out[g] = dict(rows=rows, cols=np.sort(cols), red_right=bool(red_right))
    return out


def _rate(path_rate, rows, cols):
    from astropy.io import fits
    with fits.open(path_rate) as h:
        sl = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
        sci = np.asarray(h['SCI'].data[sl], float)
        var = np.zeros_like(sci)
        for n in ('VAR_POISSON', 'VAR_RNOISE'):
            if n in h:
                var = var + np.asarray(h[n].data[sl], float)
        if not (var > 0).any() and 'ERR' in h:
            var = np.asarray(h['ERR'].data[sl], float) ** 2
        dq = np.asarray(h['DQ'].data[sl]) if 'DQ' in h else np.zeros(sci.shape, int)
    return sci, var, (dq & PF.DO_NOT_USE) > 0


def _others_mask(exps, k_list, maps, sid, rows, cols):
    """Pixels of the canvas lit by another source's source shutter in exposure k's own
    frame or a partner's (geometry of the trace maps, beyond-range light included)."""
    from campfire_pipeline.nirspec import profile_contam as CT
    mask = {k: np.zeros((rows.size, cols.size), bool) for k in k_list}
    region = (int(cols[0]), int(rows[0]), int(cols.size), int(rows.size))
    try:
        mlist = [maps[CT.exposure_key(exps[k].name)] for k in k_list]
    except KeyError:
        return mask, False
    partners = [[k_list.index(j) for j in exps[k].partners] if exps[k].partners is not None else [] for k in k_list]
    for k in k_list:
        n = k_list.index(k)
        lut = [m.traces_in(*region) for m in mlist]
        for j in [n] + partners[n]:
            jy, jx, jidx, jflg = lut[j]
            slits = mlist[j].slits
            for r in np.unique(jidx):
                rec = slits[int(r)]
                if rec['kind'] == 'slitlet' and int(rec['source_id']) == int(sid):
                    continue
                sel = jidx == r
                if rec['kind'] == 'slitlet':
                    sel &= mlist[j].source_shutter(jflg)
                mask[k][jy[sel], jx[sel]] = True
    return mask, True


def solve_extension(R, sid, maps, workspace, n_smooth=31, keep_lines=True, clip=5.0, n_clip=2, mask_others=True):
    """Spectrum past the cutouts' red end, in count-rate units, at the fixed shape of
    ``R`` (a pass-1 ``FitResult``; its model carries the exposures), and the frames it
    puts on each exposure. Returns None when there is no such region or too few pixels.

    Returns dict(lam, flux, err, flux_raw, frames={exposure key: dict(rows, cols, img,
    err)}, npix, nmask)."""
    from campfire_pipeline.nirspec import profile_contam as CT
    from campfire_pipeline.nirspec.profile_contam import smooth_spectrum

    M = R.model
    exps = M.exps
    p, eoff = PF._qvec(R)
    # past the last wavelength the pass-1 spectrum has (in the cutout's last columns the data
    # have no flat field, F290LP/G395M above ~5.25 um)
    fin = np.isfinite(R.flux) & (R.flux != 0)
    lam_max_in = float(R.lam[fin].max()) if fin.any() else np.nanmax([np.nanmax(e.wav) for e in exps])
    regions = canvas(exps)
    if not regions:
        return None
    blocks = []          # per science exposure: (k, rows, cols, D, V, ok, terms -> (w, g, lam))
    per_exp = {}         # exposure index -> (rows, cols, y, lam, g) for frames
    for g, reg in regions.items():
        rows, cols = reg['rows'], reg['cols']
        ks = [k for k, e in enumerate(exps) if e.group == g]
        geo = {}
        for k in ks:
            e = exps[k]
            try:
                ys, lam, _ = _wcs_on(e.path, rows, cols)
            except Exception:                        # noqa: BLE001 - no WCS, no extension for this exposure
                continue
            with np.errstate(invalid='ignore'):
                beyond = np.isfinite(ys) & np.isfinite(lam) & (lam > lam_max_in) & (lam <= EXT_MAX_WAVE)
            dy = np.abs(np.gradient(np.where(np.isfinite(ys), ys, 0.0), axis=0))
            gimg = np.zeros(ys.shape)
            if e.has_source and beyond.any():
                yv, lv, dv = ys[beyond], lam[beyond], dy[beyond]
                gimg[beyond] = M.throughput(p, eoff, k, lv, dv) * M.image(p, eoff, k, yv, lv, dv)
            geo[k] = (ys, lam, gimg, beyond)
            per_exp[k] = (rows, cols, ys, lam, gimg, beyond)
        if not geo:
            continue
        keys = {k: CT.exposure_key(exps[k].name) for k in ks}
        rates = {}
        for k in ks:
            path = os.path.join(workspace, f'{keys[k]}_rate.fits')
            if os.path.exists(path):
                rates[k] = _rate(path, rows, cols)
        omask, have = _others_mask(exps, ks, maps, sid, rows, cols) if mask_others else ({k: 0 for k in ks}, True)
        for k in ks:
            e = exps[k]
            if not e.use_data or e.partners is None or k not in rates or k not in geo:
                continue
            terms = [(k, 1.0)] + [(j, -1.0 / len(e.partners)) for j in e.partners]
            if any(j not in rates or j not in geo for j, _ in terms):
                continue
            n = len(e.partners)
            D = rates[k][0] - np.mean([rates[j][0] for j in e.partners], axis=0)
            s2 = (rates[k][1] + sum(rates[j][1] for j in e.partners)) / (n + 1)
            V = ((n + 1) / n) ** 2 * s2
            bad = np.zeros(D.shape, bool)
            for j, _ in terms:
                bad |= rates[j][2]
            lit = np.zeros(D.shape, bool)
            for j, _ in terms:
                lit |= geo[j][3] & (np.abs(geo[j][2]) > 0)
            ok = lit & np.isfinite(D) & np.isfinite(V) & (V > 0) & ~bad & ~np.asarray(omask[k], bool)
            blocks.append((k, D, V, ok, [(w, geo[j][2], geo[j][1]) for j, w in terms]))
    if not blocks:
        return None
    # spectrum grid: native sampling at the source row of the first science exposure's canvas
    k0 = blocks[0][0]
    ys0, lam0 = per_exp[k0][2], per_exp[k0][3]
    yy = np.where(np.isfinite(ys0), ys0, np.inf)
    r = np.argmin(np.abs(yy - exps[k0].src_y), axis=0)
    lrow = lam0[r, np.arange(lam0.shape[1])]
    lrow = np.sort(lrow[np.isfinite(lrow) & (lrow > lam_max_in) & (lrow <= EXT_MAX_WAVE)])
    if lrow.size < 5:
        return None
    step = np.median(np.diff(lrow))
    grid = [lrow[0]]
    for x in lrow[1:]:
        if x - grid[-1] > 0.5 * step:
            grid.append(x)
    grid = np.array(grid)
    # design
    rows_, cols_, vals_, d_, v_ = [], [], [], [], []
    nrow = 0
    for k, D, V, ok, terms in blocks:
        idx = np.flatnonzero(ok.ravel())
        if idx.size == 0:
            continue
        rr = nrow + np.arange(idx.size)
        for w, gimg, lam in terms:
            gv = gimg.ravel()[idx]
            lv = lam.ravel()[idx]
            use = (gv != 0) & np.isfinite(lv) & (lv >= grid[0]) & (lv <= grid[-1])
            if not use.any():
                continue
            i = np.clip(np.searchsorted(grid, lv[use]) - 1, 0, grid.size - 2)
            t = np.clip((lv[use] - grid[i]) / (grid[i + 1] - grid[i]), 0.0, 1.0)
            rows_ += [rr[use], rr[use]]
            cols_ += [i, i + 1]
            vals_ += [w * gv[use] * (1 - t), w * gv[use] * t]
        d_.append(D.ravel()[idx])
        v_.append(V.ravel()[idx])
        nrow += idx.size
    if nrow < MIN_PIXELS or not vals_:
        return None
    A = sp.csr_matrix((np.concatenate(vals_), (np.concatenate(rows_), np.concatenate(cols_))), shape=(nrow, grid.size))
    d, v = np.concatenate(d_), np.concatenate(v_)
    use = np.ones(nrow, bool)
    for it in range(n_clip + 1):
        wgt = np.where(use, 1.0 / v, 0.0)
        AtW = A.T.multiply(wgt).tocsr()
        N = (AtW @ A).tocsc()
        b = AtW @ d
        diag = N.diagonal()
        if not (diag > 0).any():
            return None
        good = diag > 1e-3 * np.median(diag[diag > 0])
        gi = np.flatnonzero(good)
        Ng = N[gi][:, gi] + sp.identity(gi.size, format='csc') * 1e-9 * np.median(diag[good])
        lu = splu(Ng.tocsc())
        f = np.zeros(grid.size)
        f[gi] = lu.solve(b[gi])
        res = (d - A @ f) / np.sqrt(v)
        if it == n_clip:
            break
        new = np.abs(res) < clip
        if np.array_equal(new, use):
            break
        use = new
    err = np.full(grid.size, np.nan)
    E = np.zeros((gi.size, gi.size))
    E[np.arange(gi.size), np.arange(gi.size)] = 1.0
    err[gi] = np.sqrt(np.abs(np.diag(lu.solve(E))))
    flux_raw = np.where(good, f, np.nan)
    if n_smooth and n_smooth > 1:
        fs, es, _ = smooth_spectrum(flux_raw, err, n=n_smooth, keep_lines=keep_lines)
    else:
        fs, es = flux_raw, err
    frames = {}
    for k, (rows, cols, ys, lam, gimg, beyond) in per_exp.items():
        if not exps[k].has_source:
            continue
        with np.errstate(invalid='ignore'):
            img = gimg * np.interp(np.nan_to_num(lam), grid, np.nan_to_num(fs), left=0.0, right=0.0)
            eim = np.abs(gimg) * np.interp(np.nan_to_num(lam), grid, np.nan_to_num(es), left=0.0, right=0.0)
        keep = beyond & np.isfinite(img) & (np.abs(img) > 0)
        if keep.any():
            frames[CT.exposure_key(exps[k].name)] = dict(rows=rows, cols=cols, img=np.where(keep, img, 0.0),
                                                         err=np.where(keep, eim, 0.0), keep=keep)
    return dict(lam=grid, flux=fs, err=es, flux_raw=flux_raw, frames=frames, npix=int(use.sum()),
                nmask=int(nrow - use.sum()))


def ext_part(fr):
    """The pixels of a merged pass-1 frame that lie past the cutout (``ext``), as a sparse
    frame of their own, or None."""
    if 'ext' not in fr or not np.any(fr['ext']):
        return None
    x = np.asarray(fr['ext'], bool)
    return dict(origin=fr['origin'], shape=fr['shape'], idx=fr['idx'][x], img=fr['img'][x], err=fr['err'][x],
                cal=fr['cal'][x], ext=np.ones(int(x.sum()), bool))


def merge_sparse(a, b):
    """Two sparse frames (origin, shape, idx, img, err, cal) on one canvas covering both;
    ``a`` wins where both have a pixel. ``b``'s pixels are flagged ``ext``."""
    def det(fr):
        ny, nx = (int(v) for v in fr['shape'])
        iy, ix = np.divmod(np.asarray(fr['idx'], np.int64), nx)
        return iy + int(fr['origin'][0]), ix + int(fr['origin'][1])
    ay, ax = det(a)
    by, bx = det(b)
    allx, ally = np.concatenate([ax, bx]), np.concatenate([ay, by])
    o = (int(ally.min()), int(allx.min()))
    shape = (int(ally.max() - o[0] + 1), int(allx.max() - o[1] + 1))
    ia = (ay - o[0]) * shape[1] + (ax - o[1])
    ib = (by - o[0]) * shape[1] + (bx - o[1])
    # ``b`` replaces ``a``'s pixels that have no calibration (no flat field: their light cannot be
    # converted, ``b``'s is in count-rate units)
    drop = ~np.isfinite(np.asarray(a['cal'], float)) & np.isin(ia, ib)
    if drop.any():
        a = {n: (np.asarray(v)[~drop] if (np.ndim(v) == 1 and np.size(v) == drop.size) else v) for n, v in a.items()}
        ay, ax, ia = ay[~drop], ax[~drop], ia[~drop]
    new = ~np.isin(ib, ia)
    m = dict(origin=np.array(o), shape=np.array(shape), idx=np.concatenate([ia, ib[new]]).astype(np.int64))
    for n in ('img', 'err', 'cal'):
        m[n] = np.concatenate([np.asarray(a[n], float), np.asarray(b[n], float)[new]]).astype(np.float32)
    ea = np.asarray(a['ext'], bool) if 'ext' in a else np.zeros(ia.size, bool)
    m['ext'] = np.concatenate([ea, np.ones(int(new.sum()), bool)])
    for n in ('prof', 'plam'):
        if n in a:                                              # the echo design covers ``a`` only
            m[n] = np.concatenate([np.asarray(a[n]), np.zeros(int(new.sum()), np.asarray(a[n]).dtype)])
    return m


def merge_frames(frames, ext):
    """Pass-1 sparse frames (on each exposure's common frame) with the extension's pixels
    added (calibration 1: count-rate units), on one canvas covering both. Each merged
    frame gets an ``ext`` flag per pixel."""
    out = dict(frames)
    for key, x in ext['frames'].items():
        rows, cols, keep = x['rows'], x['cols'], x['keep']
        ey, ex = np.nonzero(keep)
        o = (int(rows[0]) + 1, int(cols[0]) + 1)               # 1-based detector corner, like ``origin``
        shape = (rows.size, cols.size)
        sx = dict(origin=np.array(o), shape=np.array(shape), idx=(ey * shape[1] + ex).astype(np.int64),
                  img=x['img'][ey, ex].astype(np.float32), err=x['err'][ey, ex].astype(np.float32),
                  cal=np.ones(ey.size, np.float32), ext=np.ones(ey.size, bool))
        fr = frames.get(key)
        out[key] = sx if fr is None else merge_sparse(fr, sx)
    return out
