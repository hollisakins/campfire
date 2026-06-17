"""
Metrics for the GP-vs-median 1/f ("striping") A/B on NIRCam exposures.

All metrics operate on a post-striping canonical exposure: the SCI array
plus its ``SRCMASK`` extension and ``DQ``. "Blank" pixels are finite,
non-source, non-DO_NOT_USE, and outside the 4-px reference border.

The 1/f offset is per amp-row (the 512-col amp strip × one row), so the
residual-stripe metrics collapse each amplifier's blank pixels to a
per-row median and measure how much structure is left along the slow
(row) axis. Background-uniformity is the robust width of the blank-pixel
histogram. Both are "lower is better"; we report them per amp and pooled.
"""

import numpy as np
from astropy.io import fits
from astropy.stats import mad_std

from campfire_pipeline.nircam.constants import NIR_AMPS

_REF = 4
_DO_NOT_USE = 1  # jwst dqflags.pixel['DO_NOT_USE']


def load_exposure(path):
    """Return ``(sci, blank_mask)`` for a post-striping canonical file.

    ``blank_mask`` is True on pixels usable as background: finite, not
    flagged DO_NOT_USE, not in SRCMASK, and outside the reference border.
    """
    with fits.open(path, memmap=False) as hdul:
        sci = hdul['SCI'].data.astype(np.float64)
        dq = hdul['DQ'].data if 'DQ' in hdul else np.zeros(sci.shape, int)
        seg = (hdul['SRCMASK'].data.astype(bool) if 'SRCMASK' in hdul
               else np.zeros(sci.shape, bool))
    blank = np.isfinite(sci) & (sci != 0)
    blank &= (np.bitwise_and(dq, _DO_NOT_USE) == 0)
    blank &= ~seg
    border = np.zeros(sci.shape, bool)
    border[:_REF] = border[-_REF:] = True
    border[:, :_REF] = border[:, -_REF:] = True
    blank &= ~border
    return sci, blank, seg


def amprow_residual_medians(sci, blank):
    """Per-amp per-row median of blank pixels. Returns dict amp -> (R,) array.

    Rows with no blank pixels in that amp are NaN.
    """
    out = {}
    for amp in ('A', 'B', 'C', 'D'):
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        sub = sci[:, c0:c1].copy()
        m = blank[:, c0:c1]
        sub[~m] = np.nan
        with np.errstate(invalid='ignore'):
            rowmed = np.nanmedian(sub, axis=1)
        out[amp] = rowmed
    return out


def _highpass(seq, size=51):
    """High-pass a per-row sequence: subtract a running median to remove
    large-scale structure (e.g. cluster ICL retained through striping),
    leaving the 1/f residual. NaN gaps are interpolated for the filter then
    restored, so masked rows stay NaN.
    """
    from scipy.ndimage import median_filter
    out = np.full(seq.shape, np.nan, dtype=float)
    good = np.isfinite(seq)
    if good.sum() < max(size, 16):
        if good.any():
            out[good] = seq[good] - np.nanmedian(seq)
        return out
    idx = np.arange(seq.size)
    filled = np.interp(idx, idx[good], seq[good])
    trend = median_filter(filled, size=size, mode='nearest')
    out[good] = seq[good] - trend[good]
    return out


def stripe_metrics(sci, blank):
    """Residual-stripe + background-uniformity metrics for one exposure.

    Returns a dict with, pooled over amps (mean of per-amp values):
      stripe_std  : mad_std of the *high-passed* per-amp-row residual medians
                    (leftover 1/f striping amplitude; high-pass removes any
                    retained large-scale background / ICL that would otherwise
                    dominate this metric and mask the estimator difference).
      stripe_hf   : mad_std of the row-to-row first difference of those
                    medians (high-frequency banding / steps).
      bkg_width   : mad_std of all blank pixels (histogram width).
      psd_lowfreq : fraction of row-median power below 1/64 cycles/row.
    Plus per-amp ``stripe_std_<amp>``.
    """
    rowmeds = amprow_residual_medians(sci, blank)
    stds, hfs = [], []
    per_amp = {}
    for amp, rm in rowmeds.items():
        good = np.isfinite(rm)
        if good.sum() < 32:
            continue
        hp = _highpass(rm)
        s = float(mad_std(hp[np.isfinite(hp)]))
        diff = np.diff(rm[good])
        hf = float(mad_std(diff[np.isfinite(diff)]))
        stds.append(s)
        hfs.append(hf)
        per_amp[f'stripe_std_{amp}'] = s
    bkg = float(mad_std(sci[blank])) if blank.any() else np.nan

    # Low-frequency power fraction of the pooled, gap-filled row-median
    # sequence (interp over NaN rows so the FFT is well-defined).
    pooled = []
    for amp, rm in rowmeds.items():
        good = np.isfinite(rm)
        if good.sum() < 64:
            continue
        idx = np.arange(rm.size)
        filled = np.interp(idx, idx[good], rm[good] - np.median(rm[good]))
        pooled.append(filled)
    if pooled:
        ps = np.abs(np.fft.rfft(np.mean(pooled, axis=0))) ** 2
        freq = np.fft.rfftfreq(pooled[0].size)
        lowfrac = float(ps[freq < 1 / 64.0].sum() / ps[1:].sum())
    else:
        lowfrac = np.nan

    return {
        'stripe_std': float(np.mean(stds)) if stds else np.nan,
        'stripe_hf': float(np.mean(hfs)) if hfs else np.nan,
        'bkg_width': bkg,
        'psd_lowfreq': lowfrac,
        **per_amp,
    }


def stripe_metrics_split(sci, blank, seg, clean_frac=0.20, source_frac=0.40):
    """Residual amp-row-median scatter split by amp-row source coverage.

    For each amp, the per-row residual median is computed on blank pixels.
    Rows are classified by the SRCMASK coverage of that amp-row:
      clean rows  : source fraction < clean_frac
      source rows : source fraction > source_frac   (where the median
                    estimator tends to hit its full-row fallback)
    Returns mad_std of the residual medians in each class, pooled over amps.
    This isolates the GP's target regime (source rows) from the clean-row
    regime where the per-row median is already near-optimal.
    """
    clean_vals, source_vals = [], []
    for amp in ('A', 'B', 'C', 'D'):
        _, _, c0, c1 = NIR_AMPS[amp]['data']
        sub = sci[:, c0:c1].copy()
        m = blank[:, c0:c1]
        sub[~m] = np.nan
        with np.errstate(invalid='ignore'):
            rowmed = np.nanmedian(sub, axis=1)
        srcfrac = seg[:, c0:c1].mean(axis=1)
        # High-pass to strip any retained large-scale background (ICL) so the
        # metric isolates the 1/f residual, not the cluster light.
        rowmed = _highpass(rowmed)
        clean = np.isfinite(rowmed) & (srcfrac < clean_frac)
        source = np.isfinite(rowmed) & (srcfrac > source_frac)
        clean_vals.append(rowmed[clean])
        source_vals.append(rowmed[source])
    cv = np.concatenate(clean_vals) if clean_vals else np.array([])
    sv = np.concatenate(source_vals) if source_vals else np.array([])
    return {
        'stripe_std_clean': float(mad_std(cv)) if cv.size > 16 else np.nan,
        'stripe_std_source': float(mad_std(sv)) if sv.size > 16 else np.nan,
        'n_clean_rows': int(cv.size),
        'n_source_rows': int(sv.size),
    }


def detect_bright_sources(sci, blank, nmax=8, min_sep=80, edge=60):
    """Bright source centroids via sep, ordered by flux.

    Returns a list of ``(y, x, flux)``; sources within ``min_sep`` of a
    brighter one or within ``edge`` of the frame edge are dropped.
    """
    import sep
    data = sci.byteswap().view(sci.dtype.newbyteorder('=')) \
        if sci.dtype.byteorder not in ('=', '|') else sci.copy()
    bkg = sep.Background(data, mask=~blank)
    sub = data - bkg.back()
    try:
        obj = sep.extract(sub, thresh=8.0, err=bkg.globalrms, minarea=9)
    except Exception:
        return []
    h, w = sci.shape
    order = np.argsort(obj['flux'])[::-1]
    picked = []
    for i in order:
        y, x = float(obj['y'][i]), float(obj['x'][i])
        if x < edge or x > w - edge or y < edge or y > h - edge:
            continue
        if any((y - py) ** 2 + (x - px) ** 2 < min_sep ** 2
               for py, px, _ in picked):
            continue
        picked.append((y, x, float(obj['flux'][i])))
        if len(picked) >= nmax:
            break
    return picked


def radial_profile(sci, blank, yc, xc, rmax=150, nbin=30):
    """Median radial profile of *blank* pixels around (yc, xc).

    Returns ``(r_centers, profile)``. Source pixels are excluded (blank),
    so the profile measures the background floor around the source — a
    negative trough flags oversubtraction (leaked flux biased the median).
    """
    y0, y1 = int(max(yc - rmax, 0)), int(min(yc + rmax, sci.shape[0]))
    x0, x1 = int(max(xc - rmax, 0)), int(min(xc + rmax, sci.shape[1]))
    yy, xx = np.mgrid[y0:y1, x0:x1]
    r = np.sqrt((yy - yc) ** 2 + (xx - xc) ** 2)
    stamp = sci[y0:y1, x0:x1]
    m = blank[y0:y1, x0:x1] & (r <= rmax)
    edges = np.linspace(0, rmax, nbin + 1)
    centers = 0.5 * (edges[1:] + edges[:-1])
    prof = np.full(nbin, np.nan)
    rr, vv = r[m], stamp[m]
    for k in range(nbin):
        sel = (rr >= edges[k]) & (rr < edges[k + 1])
        if sel.sum() >= 5:
            prof[k] = np.median(vv[sel])
    return centers, prof


def aperture_photometry(sci, sources, r=10.0, r_in=15.0, r_out=25.0):
    """Local-background-subtracted aperture sums at ``sources`` positions.

    Returns an array of fluxes aligned with ``sources``. Used to check
    photometric conservation across arms (same positions, same apertures).
    """
    import sep
    data = sci.byteswap().view(sci.dtype.newbyteorder('=')) \
        if sci.dtype.byteorder not in ('=', '|') else sci.copy()
    # Non-finite pixels (bad/edge) must be masked, not summed, or sep
    # propagates NaN through the aperture/annulus.
    badmask = ~np.isfinite(data)
    data = np.where(badmask, 0.0, data)
    ys = np.array([s[0] for s in sources])
    xs = np.array([s[1] for s in sources])
    flux, _, _ = sep.sum_circle(data, xs, ys, r, mask=badmask,
                                bkgann=(r_in, r_out), subpix=5)
    return flux
