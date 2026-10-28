"""P0 prototype: a NIRSpec master background from the empty MSA shutters of existing canonicals.

Design: docs/design-nirspec-master-background.md (§1.7 conversions, §4.1 grain, §4.2 steps 2-4,
§7 P0). This script changes no pipeline product. It reads one observation's canonical
spectrum-exposure files (``<root>_<nod>_<nrsN>_<source>.fits``), converts every pixel of every
*empty* shutter to sky surface brightness, builds a per-exposure-group sky, decides per shutter
whether it is clean, and writes the diagnostics the design leaves open:

* how many shutters are clean, and why the rest are rejected (``*_shutters.ecsv``);
* the pooled sky per exposure group with its error and shutter count (``*_sky.ecsv``);
* each exposure x detector's own sky against the pooled one: scale and offset (``*_exposures.ecsv``),
  which decides whether per-exposure terms are needed at all (design §4.1);
* NRS1 vs NRS2 agreement on the prism (same wavelengths, different MSA quadrants);
* the level against ``jwst_backgrounds`` for the pointing and date (``--model``);
* whether solar absorption (Paschen beta, 1.28216 um vacuum) shows in a stacked G140M sky.

Run inside the ``campfire`` conda env with ``$CAMPFIRE_ROOT`` and CRDS set, from anywhere::

    conda run -n campfire python pipeline/experiments/master_bkg_prototype/prototype_master_bkg.py \
        --obs ember_uds_p4 --gratings prism_clear --max-sources 60 --processes 8 --model

Everything lands in ``./master_bkg_prototype_<obs>/`` (``--out`` to change): the three ECSV tables
and ``<obs>_master_bkg_prototype.pdf``.

How a canonical pixel becomes a sky sample (design §1.7, verified against jwst 1.20.2)
---------------------------------------------------------------------------------------
The canonical holds the slit calibrated as a POINT source, in MJy per pixel. For a sky pixel:

    SB [MJy/sr] = SCI * (pathloss_point / pathloss_uniform) / PIXAR_SR / barshadow(EXTENDED)

``pathloss_point`` and ``pathloss_uniform`` are both stored on every MOS slit; ``PIXAR_SR`` is the
slit's pixel solid angle; the bar-shadow array is evaluated by running jwst's ``BarShadowStep``
with ``source_type='EXTENDED'`` on a copy (the sky is extended everywhere, the source is not -- design
§1.7 explains why the two get different corrections). The uniform-source wavelength of each pixel
comes from the WCS ``detector -> slit_frame`` transform, which sits *before* the point-source
wavecorr shift in the WCS pipeline. If the canonical has been nod-subtracted (stage 2b), the
calibrated arrays are taken from its ``PRE_BKGSUB_*`` revert HDUs.

Shutter geometry comes from the same transform: ``slit_frame`` y is 0 at the centre of the
source ('x') shutter, a shutter's open area spans |y| <= 0.5 and the shutter pitch is
SLIT_YSCALE ~ 1.15 (bars at |y| = 0.575), so ``k = rint(y / pitch)`` is the shutter index relative to the source and
``f = y - k * pitch`` the position inside it (|f| > 0.5 is the bar).
"""
from __future__ import annotations

import argparse
import os
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

# Default SLIT_YSCALE if the slit does not carry one (jwst barshadow uses the same fallback).
SLITRATIO = 0.87
PABETA_VAC_UM = 1.28216

# ---------------------------------------------------------------------------
# Per-canonical reading
# ---------------------------------------------------------------------------


@dataclass
class SlitInfo:
    """Cheap, header-only facts about one canonical (pass 1)."""
    path: str
    name: str
    root: str          # jw<ppppp><ooo><vvv>_<config>
    nod: str
    detector: str
    source_id: int
    grating: str
    filter_grating: str
    xstart: int
    ystart: int
    xsize: int
    ysize: int
    srcflux: bool
    has_box: bool      # SLTSTRT*/SLTSIZE* present, so the overlap counter can use this slit
    central_lam: np.ndarray = field(default_factory=lambda: np.zeros(0))

    @property
    def rate_key(self):
        return f"{self.root}_{self.nod}_{self.detector}"


@dataclass
class ShutterSpectrum:
    """One empty shutter's binned spectrum (pass 2)."""
    root: str
    nod: str
    detector: str
    source_id: int
    slitlet_id: int
    quadrant: int
    k: int                      # shutter index relative to the source shutter
    role: str                   # 'nod_empty' (the only kind the canonicals offer)
    n_pix: int
    sb: np.ndarray              # per bin median surface brightness, NaN where empty
    err: np.ndarray             # per bin error of the median
    n: np.ndarray               # per bin pixel count
    # filled by the selection
    scale: float = np.nan
    scale_err: float = np.nan
    chi2_red: float = np.nan
    line_run: int = 0
    clean: bool = False
    reason: str = ''


def _read_info(path):
    """Pass 1: header-only facts and the central-row wavelengths (for the grid)."""
    from astropy.io import fits

    name = os.path.basename(path)
    tokens = name[:-len('.fits')].split('_')
    det_idx = next(i for i, t in enumerate(tokens) if t in ('nrs1', 'nrs2'))
    with fits.open(path, memmap=False) as hdul:
        ph = hdul[0].header
        sh = hdul['SCI'].header
        info = SlitInfo(
            path=path, name=name,
            root='_'.join(tokens[:det_idx - 1]), nod=tokens[det_idx - 1], detector=tokens[det_idx],
            source_id=int(tokens[det_idx + 1]),
            grating=str(ph['GRATING']).upper(),
            filter_grating=f"{ph['GRATING'].lower()}_{ph['FILTER'].lower()}",
            xstart=int(sh.get('SLTSTRT1', 1)), ystart=int(sh.get('SLTSTRT2', 1)),
            xsize=int(sh.get('SLTSIZE1', sh['NAXIS1'])), ysize=int(sh.get('SLTSIZE2', sh['NAXIS2'])),
            srcflux=str(ph.get('SRCFLUX', 'T')).upper() != 'F',
            has_box=all(kw in sh for kw in ('SLTSTRT1', 'SLTSTRT2', 'SLTSIZE1', 'SLTSIZE2')),
        )
    return info


def _slit_coordinates(slit):
    """(x_slit, y_slit, lam_um) on the cutout grid from the detector -> slit_frame transform.

    ``slit_frame`` precedes the wavecorr frame, so ``lam`` is the uniform-source wavelength.
    """
    from gwcs import wcstools
    x, y = wcstools.grid_from_bounding_box(slit.meta.wcs.bounding_box, step=(1, 1))
    det2slit = slit.meta.wcs.get_transform('detector', 'slit_frame')
    xs, ys, lam = det2slit(x, y)
    lam = np.asarray(lam, dtype=float)
    if np.nanmedian(lam) < 1e-3:       # metres -> microns
        lam = lam * 1e6
    return np.asarray(xs), np.asarray(ys), lam


def _barshadow_array(model, slit):
    """Bar-shadow correction for the slit as if uniformly illuminated, or None if unavailable."""
    try:
        from jwst.barshadow import BarShadowStep
        copy = model.copy()
        copy.meta.cal_step.barshadow = None
        result = BarShadowStep.call(copy, source_type='EXTENDED', skip=False)
        arr = np.asarray(result.slits[0].barshadow, dtype=float)
        result.close()
        copy.close()
        if arr.size == 0 or arr.shape != slit.data.shape:
            return None
        return arr
    except Exception as e:  # CRDS unreachable, missing reference, ...
        warnings.warn(f"barshadow unavailable ({type(e).__name__}: {e}); continuing without it")
        return None


def _other_source_columns(workspace, info, slitlet_id, msametid, dither_point):
    """Shutter columns of every catalog source in this slitlet, from the original metafile.

    The SHUTTER_INFO table repeats the slitlet once per nod (``dither_point_index``), with the
    source in a different shutter each time, so the rows must be restricted to this exposure's
    dither point or the other nods' positions masquerade as other sources.

    Returns ``(own_column, [other columns])`` or ``(None, [])`` if the metafile is not at hand.
    """
    from astropy.io import fits
    rate = os.path.join(workspace, f"{info.rate_key}_rate.fits")
    if not os.path.exists(rate):
        return None, []
    try:
        hdr = fits.getheader(rate)
        metafile = os.path.join(workspace, os.path.basename(hdr.get('OGMETFL') or hdr['MSAMETFL']))
        with fits.open(metafile, memmap=False) as mf:
            from astropy.table import Table
            shut = Table(mf[2].data)
        sel = (shut['msa_metadata_id'] == msametid) & (shut['slitlet_id'] == slitlet_id)
        if 'dither_point_index' in shut.colnames and dither_point is not None:
            sel &= shut['dither_point_index'] == dither_point
        rows = shut[sel]
        # A source sits in the shutter whose row says primary_source == 'Y' (what jwst's assign_wcs
        # keys on). The slitlet's background rows can carry the same source_id, so a source_id test
        # would mark every shutter as a source.
        src = rows[(rows['primary_source'] == 'Y') & (rows['source_id'] > 0)]
        own = src['shutter_column'][src['source_id'] == info.source_id]
        own_col = int(own[0]) if len(own) else None
        others = sorted({int(c) for c in src['shutter_column'] if own_col is None or int(c) != own_col})
        return own_col, others
    except Exception as e:
        warnings.warn(f"{info.name}: could not read the metafile for other sources ({e})")
        return None, []


def _read_samples(args):
    """Pass 2 worker: one canonical -> list of ShutterSpectrum (its empty shutters, binned).

    ``args`` = (info, workspace, edges, coverage_box_mask, options). Returns (info.name, spectra,
    stats) where stats counts the pixels dropped by each cut.
    """
    info, workspace, edges, cover_ok, opt = args
    from astropy.io import fits
    from jwst.datamodels import MultiSlitModel, dqflags

    stats = defaultdict(int)
    with fits.open(info.path, memmap=False) as hdul:
        pre = 'PRE_BKGSUB_SCI' in hdul
        sci = np.array(hdul['PRE_BKGSUB_SCI' if pre else 'SCI'].data, dtype=float)
        dq = np.array(hdul['PRE_BKGSUB_DQ' if pre else 'DQ'].data)
        var = np.zeros_like(sci)
        for ext in ('VAR_RNOISE', 'VAR_POISSON'):
            name = f'PRE_BKGSUB_{ext}' if pre else ext
            if name in hdul:
                var += np.array(hdul[name].data, dtype=float)
        msametid = int(hdul[0].header.get('MSAMETID', -1))
        dither_point = hdul[0].header.get('PATT_NUM')
        sci_hdr = hdul['SCI'].header

    model = MultiSlitModel(info.path)
    slit = model.slits[0]
    pix_sr = slit.meta.photometry.pixelarea_steradians
    if not pix_sr:
        model.close()
        return info.name, [], {'no_pixar_sr': 1}

    # --- conversions (design §1.7) -------------------------------------------------------------
    # Both calibrations end in MJy per pixel: POINT natively, UNIFORM after fix_units divided the
    # MJy/sr data by PHOTMJSR (= 1 / PIXAR_SR for NIRSpec, whose photom PHOTMJ is 1). A UNIFORM
    # canonical already carries the uniform pathloss, so only the POINT one needs the ratio.
    factor = np.full(sci.shape, 1.0 / pix_sr)
    pthloss = str(sci_hdr.get('PTHLOSS', 'POINT')).upper()
    if pthloss == 'POINT':
        pl_point = np.asarray(slit.pathloss_point, dtype=float) if slit.pathloss_point is not None else np.zeros(0)
        pl_unif = np.asarray(slit.pathloss_uniform, dtype=float) if slit.pathloss_uniform is not None else np.zeros(0)
        if pl_point.shape == sci.shape and pl_unif.shape == sci.shape:
            with np.errstate(divide='ignore', invalid='ignore'):
                factor *= pl_point / pl_unif
        else:
            stats['no_pathloss_arrays'] += 1
    else:
        stats['uniform_calibrated'] += 1
    bar = None
    if opt['barshadow']:
        bar = _barshadow_array(model, slit)
        if bar is not None:
            with np.errstate(divide='ignore', invalid='ignore'):
                factor /= bar
        else:
            stats['no_barshadow'] += 1

    xs, ys, lam = _slit_coordinates(slit)
    if lam.shape != sci.shape:
        model.close()
        return info.name, [], {'wcs_shape_mismatch': 1}
    # SLTYSCL is the shutter pitch in slit_frame units (~1.15: measured 1.143 on real
    # canonicals, where the bar-shadow dips sit at +-0.575 and +-1.725). jwst's own fallback
    # constant is the inverse (0.87), so accept either convention.
    yscale = slit.slit_yscale or SLITRATIO
    pitch = yscale if yscale > 1 else 1.0 / yscale
    k = np.rint(ys / pitch)
    f = ys - k * pitch
    shutter_state = str(slit.shutter_state or '')
    slitlet_id = int(slit.slitlet_id or -1)
    quadrant = int(slit.quadrant or 0)
    model.close()

    sb = sci * factor
    sb_var = var * factor ** 2

    # pixel rows per slit_frame unit, to express the source pad in pixels
    yvalid = ys[np.isfinite(ys)]
    px_per_unit = (sci.shape[0] - 1) / max(np.nanmax(yvalid) - np.nanmin(yvalid), 1e-6)
    pad_units = opt['source_pad_px'] / px_per_unit

    # --- per-pixel cuts (design §4.2 step 2 + the generalised geometric source cut) ------------
    good = np.isfinite(sb) & np.isfinite(lam) & np.isfinite(ys) & (sb_var > 0)
    stats['pixels'] += int(good.sum())
    bad_dq = (dq & (dqflags.pixel['DO_NOT_USE'] | dqflags.pixel['MSA_FAILED_OPEN'])) > 0
    stats['dq'] += int((good & bad_dq).sum()); good &= ~bad_dq
    if bar is not None:
        under_bar = ~(bar > opt['bar_min'])
    else:
        under_bar = np.abs(f) > 0.4            # geometric fallback without the reference
    stats['bar'] += int((good & under_bar).sum()); good &= ~under_bar
    # the slitlet's outer half-shutter padding (jwst margin) is not sky from a real open shutter
    if shutter_state:
        n_sh = len(shutter_state)
        kx = shutter_state.find('x')
        kmin, kmax = -kx, n_sh - 1 - kx
        outside = (k < kmin) | (k > kmax)
        stats['outside_slitlet'] += int((good & outside).sum()); good &= ~outside
        closed = np.zeros_like(good)
        for kk in range(kmin, kmax + 1):
            if shutter_state[kk + kx] == '0':
                closed |= (k == kk)
        stats['closed_shutter'] += int((good & closed).sum()); good &= ~closed
    # source cut: every catalog source in the slitlet, +- pad from its trace
    own_col, other_cols = _other_source_columns(workspace, info, slitlet_id, msametid, dither_point)
    source_ks = [0]
    if own_col is not None:
        source_ks += [c - own_col for c in other_cols]
    src_mask = np.zeros_like(good)
    for ksrc in source_ks:
        src_mask |= np.abs(ys - ksrc * pitch) < (0.5 + pad_units)
    stats['source'] += int((good & src_mask).sum()); good &= ~src_mask
    # overlap with another slitlet on the detector (coverage counter from pass 1)
    if cover_ok is not None:
        stats['overlap'] += int((good & ~cover_ok).sum()); good &= cover_ok
    stats['kept'] += int(good.sum())

    # --- bin each remaining shutter onto the grid ---------------------------------------------
    spectra = []
    nb = len(edges) - 1
    for kk in np.unique(k[good]):
        sel = good & (k == kk)
        if sel.sum() < opt['min_pixels']:
            continue
        idx = np.digitize(lam[sel], edges) - 1
        ok = (idx >= 0) & (idx < nb)
        idx, vals, v = idx[ok], sb[sel][ok], sb_var[sel][ok]
        med = np.full(nb, np.nan); err = np.full(nb, np.nan); cnt = np.zeros(nb, int)
        order = np.argsort(idx)
        idx, vals, v = idx[order], vals[order], v[order]
        bounds = np.flatnonzero(np.diff(idx)) + 1
        for chunk_i, chunk_v, chunk_var in zip(np.split(idx, bounds), np.split(vals, bounds), np.split(v, bounds)):
            b = chunk_i[0]; n = len(chunk_v)
            cnt[b] = n
            med[b] = np.median(chunk_v)
            # error of the median: the larger of the propagated and the empirical estimate
            emp = 1.2533 * np.median(np.abs(chunk_v - med[b])) * 1.4826 / np.sqrt(n) if n > 2 else np.inf
            prop = 1.2533 * np.sqrt(np.mean(chunk_var) / n)
            err[b] = prop if not np.isfinite(emp) else max(emp, prop)
        spectra.append(ShutterSpectrum(
            root=info.root, nod=info.nod, detector=info.detector, source_id=info.source_id,
            slitlet_id=slitlet_id, quadrant=quadrant, k=int(kk), role='nod_empty',
            n_pix=int(sel.sum()), sb=med, err=err, n=cnt))
    return info.name, spectra, dict(stats)


# ---------------------------------------------------------------------------
# Wavelength grid
# ---------------------------------------------------------------------------


def build_grid(central_lams, bin_pixels):
    """Bin edges with ``bin_pixels`` detector pixels per bin, following the local dispersion.

    ``central_lams`` are the central-row wavelength vectors of a handful of slits.
    """
    lam, dl = [], []
    for row in central_lams:
        row = row[np.isfinite(row)]
        if len(row) < 10:
            continue
        row = np.sort(row)
        lam.append(row[1:]); dl.append(np.diff(row))
    lam = np.concatenate(lam); dl = np.concatenate(dl)
    good = dl > 0
    lam, dl = lam[good], dl[good]
    order = np.argsort(lam); lam, dl = lam[order], dl[order]
    # smooth dispersion curve: median dλ in 60 quantile bins of λ
    qs = np.quantile(lam, np.linspace(0, 1, 61))
    centres, disp = [], []
    for lo, hi in zip(qs[:-1], qs[1:]):
        m = (lam >= lo) & (lam < hi)
        if m.sum() > 5:
            centres.append(0.5 * (lo + hi)); disp.append(np.median(dl[m]))
    centres, disp = np.array(centres), np.array(disp)
    edges = [lam.min()]
    while edges[-1] < lam.max():
        step = bin_pixels * np.interp(edges[-1], centres, disp)
        edges.append(edges[-1] + max(step, 1e-6))
    return np.array(edges)


# ---------------------------------------------------------------------------
# Selection (design §4.2 step 4)
# ---------------------------------------------------------------------------


def combine(spectra, clip_sigma):
    """Sigma-clipped inverse-variance mean over shutters, per bin -> (sb, err, n_shutters)."""
    if not spectra:
        return None
    S = np.array([s.sb for s in spectra]); E = np.array([s.err for s in spectra])
    with np.errstate(invalid='ignore', divide='ignore'):
        W = 1.0 / E ** 2
    W[~np.isfinite(S) | ~np.isfinite(W)] = 0
    S = np.where(W > 0, S, np.nan)
    for _ in range(5):
        with np.errstate(invalid='ignore', divide='ignore'):
            mean = np.nansum(W * S, axis=0) / np.nansum(W, axis=0)
            med = np.nanmedian(S, axis=0)
            mad = 1.4826 * np.nanmedian(np.abs(S - med), axis=0)
            dev = np.abs(S - med) > clip_sigma * np.maximum(mad, E)
        if not np.any(dev & (W > 0)):
            break
        W[dev] = 0; S[dev] = np.nan
    with np.errstate(invalid='ignore', divide='ignore'):
        wsum = np.nansum(W, axis=0)
        mean = np.nansum(W * S, axis=0) / wsum
        err = 1.0 / np.sqrt(wsum)
    n = np.sum(W > 0, axis=0)
    mean[n == 0] = np.nan; err[n == 0] = np.nan
    return mean, err, n


def fit_scale(s, ref):
    """Weighted least-squares scale a (s ~ a * ref) -> (a, sigma_a, chi2_red, residual z, n_bins)."""
    m = np.isfinite(s.sb) & np.isfinite(ref[0]) & np.isfinite(s.err) & np.isfinite(ref[1]) & (ref[0] != 0)
    if m.sum() < 3:
        return np.nan, np.nan, np.nan, None, int(m.sum())
    r, e2 = ref[0][m], s.err[m] ** 2 + ref[1][m] ** 2
    w = 1.0 / e2
    a = np.sum(w * s.sb[m] * r) / np.sum(w * r * r)
    sa = 1.0 / np.sqrt(np.sum(w * r * r))
    z = np.full_like(s.sb, np.nan)
    z[m] = (s.sb[m] - a * r) / np.sqrt(e2)
    chi2 = np.nansum(z[m] ** 2) / max(m.sum() - 1, 1)
    return a, sa, chi2, z, int(m.sum())


def longest_run(z, thresh):
    """Longest run of consecutive bins with z > thresh (emission-line-shaped excess)."""
    best = cur = 0
    for v in z:
        if np.isfinite(v) and v > thresh:
            cur += 1; best = max(best, cur)
        else:
            cur = 0
    return best


def select_clean(spectra, seed, opt):
    """Iterate scale fits against the reference; returns the final pooled sky (sb, err, n)."""
    ref = seed
    for it in range(opt['n_iter']):
        for s in spectra:
            a, sa, chi2, z, nb = fit_scale(s, ref)
            s.scale, s.scale_err, s.chi2_red = a, sa, chi2
            s.line_run = longest_run(z, opt['line_sigma']) if z is not None else 0
            if nb < opt['min_bins']:
                s.clean, s.reason = False, 'too_few_bins'
            elif a < 1 - opt['low_min'] and (1 - a) > 3 * sa:
                s.clean, s.reason = False, 'low_throughput'
            elif a > 1 + opt['excess_min'] and (a - 1) > 3 * sa:
                s.clean, s.reason = False, 'continuum_excess'
            elif s.line_run >= opt['line_min_bins']:
                s.clean, s.reason = False, 'line_excess'
            else:
                s.clean, s.reason = True, 'clean'
        clean = [s for s in spectra if s.clean]
        new = combine(clean, opt['clip_sigma'])
        if new is None:
            break
        ref = new
    return ref


def model_seed(lam_centres, sb_median, ra, dec, date_obs):
    """jwst_backgrounds total background for the pointing/date, scaled to the data at R~20."""
    from jwst_backgrounds import jbt
    from datetime import datetime
    bg = jbt.background(ra, dec, wavelength=float(np.nanmedian(lam_centres)))
    doy = datetime.fromisoformat(date_obs[:19]).timetuple().tm_yday - 1
    cal = np.asarray(bg.bkg_data['calendar'])
    i = int(np.argmin(np.abs(cal - doy)))
    model = np.interp(lam_centres, bg.bkg_data['wave_array'], bg.bkg_data['total_bg'][i],
                      left=np.nan, right=np.nan)
    # one global scale on coarse bins (R ~ 20): the model has no line structure
    coarse = np.geomspace(np.nanmin(lam_centres), np.nanmax(lam_centres), 25)
    idx = np.digitize(lam_centres, coarse) - 1
    d, m = [], []
    for b in range(len(coarse) - 1):
        sel = (idx == b) & np.isfinite(sb_median) & np.isfinite(model)
        if sel.sum() > 3:
            d.append(np.median(sb_median[sel])); m.append(np.median(model[sel]))
    d, m = np.array(d), np.array(m)
    scale = np.sum(d * m) / np.sum(m * m) if len(m) else np.nan
    return model, scale, int(cal[i])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--obs', required=True)
    ap.add_argument('--config', default=None, help='pipeline config.toml (default: auto-discovered)')
    ap.add_argument('--gratings', nargs='*', default=None,
                    help='filter_grating names to include, e.g. prism_clear g395m_f290lp (default all)')
    ap.add_argument('--max-sources', type=int, default=None, help='cap on sources per grating (speed)')
    ap.add_argument('--processes', type=int, default=1)
    ap.add_argument('--source-pad-px', type=float, default=5.0,
                    help='pixels either side of every catalog source trace to drop (msaexp: 5)')
    ap.add_argument('--bar-min', type=float, default=0.35, help='drop pixels with barshadow below this')
    ap.add_argument('--bin-pixels', type=float, default=2.0, help='detector pixels per wavelength bin')
    ap.add_argument('--min-pixels', type=int, default=20, help='pixels a shutter needs to count')
    ap.add_argument('--min-bins', type=int, default=10)
    ap.add_argument('--clip-sigma', type=float, default=3.0)
    ap.add_argument('--line-sigma', type=float, default=4.0)
    ap.add_argument('--line-min-bins', type=int, default=3)
    ap.add_argument('--excess-min', type=float, default=0.05, help='reject a_s above 1 + this (and 3 sigma)')
    ap.add_argument('--low-min', type=float, default=0.30, help='reject a_s below 1 - this (and 3 sigma)')
    ap.add_argument('--n-iter', type=int, default=3)
    ap.add_argument('--seed', choices=['median', 'model'], default='median')
    ap.add_argument('--no-barshadow', action='store_true')
    ap.add_argument('--model', action='store_true', help='compare with jwst_backgrounds (needs its cache)')
    ap.add_argument('--out', default=None)
    args = ap.parse_args()
    warnings.filterwarnings('ignore', message='All-NaN slice')
    warnings.filterwarnings('ignore', message='Mean of empty slice')
    warnings.filterwarnings('ignore', category=RuntimeWarning, message='invalid value')

    from campfire_pipeline.config import load_config, setup_environment, resolve_paths
    from campfire_pipeline.nirspec.observation import Observation
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from astropy.table import Table
    from astropy.io import fits

    config = load_config(args.config)
    setup_environment(config)
    paths = resolve_paths(config)
    obs = Observation.load(args.obs)
    obs.setup_workspace_directory(paths['data_dir'], paths['products_dir'], overwrite=False)
    out = args.out or f'master_bkg_prototype_{args.obs}'
    os.makedirs(out, exist_ok=True)

    files = obs.discover_files(ext='canonical')
    if len(files) == 0:
        sys.exit(f'no canonical files for {args.obs}')
    files = Observation.group_files(files)
    if args.gratings:
        files = files[np.isin(files['filter_grating'], [g.lower() for g in args.gratings])]
    if args.max_sources:
        keep = []
        for fg in np.unique(files['filter_grating']):
            sids = np.unique(files['source_id'][files['filter_grating'] == fg])[:args.max_sources]
            keep += list(np.flatnonzero((files['filter_grating'] == fg) & np.isin(files['source_id'], sids)))
        files = files[sorted(keep)]
    print(f'{len(files)} canonicals, {len(np.unique(files["source_id"]))} sources, '
          f'gratings {sorted(set(files["filter_grating"]))}')

    opt = dict(source_pad_px=args.source_pad_px, bar_min=args.bar_min, min_pixels=args.min_pixels,
               barshadow=not args.no_barshadow, min_bins=args.min_bins, clip_sigma=args.clip_sigma,
               line_sigma=args.line_sigma, line_min_bins=args.line_min_bins,
               excess_min=args.excess_min, low_min=args.low_min, n_iter=args.n_iter)

    # ---- pass 1: headers, coverage counters, grid --------------------------------------------
    infos = [_read_info(p) for p in files['path']]
    n_nosrc = sum(not i.srcflux for i in infos)
    if n_nosrc:
        print(f'{n_nosrc} canonicals carry SRCFLUX=F (UNIFORM-calibrated); kept, their empty shutters are sky too')
    coverage = defaultdict(lambda: np.zeros((2048, 2048), np.int16))
    for i in infos:
        if i.has_box:
            coverage[i.rate_key][i.ystart - 1:i.ystart - 1 + i.ysize, i.xstart - 1:i.xstart - 1 + i.xsize] += 1
    if not all(i.has_box for i in infos):
        print('some canonicals lack SLTSTRT*/SLTSIZE*: the overlap cut is skipped for them')

    from jwst.datamodels import MultiSlitModel
    grids = {}
    for fg in sorted(set(i.filter_grating for i in infos)):
        sample = [i for i in infos if i.filter_grating == fg][:12]
        rows = []
        for i in sample:
            with MultiSlitModel(i.path) as m:
                _, _, lam = _slit_coordinates(m.slits[0])
            rows.append(lam[lam.shape[0] // 2])
        grids[fg] = build_grid(rows, args.bin_pixels)
        print(f'{fg}: {len(grids[fg]) - 1} bins, {grids[fg][0]:.3f}-{grids[fg][-1]:.3f} um')

    # ---- pass 2: samples -> shutter spectra ---------------------------------------------------
    def cover_ok(i):
        if not i.has_box:
            return None
        c = coverage[i.rate_key]
        box = c[i.ystart - 1:i.ystart - 1 + i.ysize, i.xstart - 1:i.xstart - 1 + i.xsize] == 1
        return box if box.shape == (i.ysize, i.xsize) else None

    tasks = [(i, obs.workspace_dir, grids[i.filter_grating], cover_ok(i), opt) for i in infos]
    if args.processes > 1:
        from multiprocessing import Pool
        with Pool(args.processes) as pool:
            results = pool.map(_read_samples, tasks, chunksize=4)
    else:
        results = [_read_samples(t) for t in tasks]

    spectra_by_fg = defaultdict(list)
    totals = defaultdict(int)
    fg_of = {i.name: i.filter_grating for i in infos}
    for name, spectra, stats in results:
        spectra_by_fg[fg_of[name]] += spectra
        for kk, v in stats.items():
            totals[kk] += v
    print('pixel budget:', dict(totals))

    # ---- selection, per filter_grating and exposure group (root) ------------------------------
    shutter_rows, sky_rows, exp_rows, summary = [], [], [], []
    pdf = PdfPages(os.path.join(out, f'{args.obs}_master_bkg_prototype.pdf'))
    for fg, spectra in spectra_by_fg.items():
        edges = grids[fg]; lam_c = 0.5 * (edges[1:] + edges[:-1])
        for root in sorted(set(s.root for s in spectra)):
            group = [s for s in spectra if s.root == root]
            if len(group) < 3:
                continue
            S = np.array([s.sb for s in group])
            seed_sb = np.nanmedian(S, axis=0)
            seed_err = 1.2533 * 1.4826 * np.nanmedian(np.abs(S - seed_sb), axis=0) / np.sqrt(
                np.maximum(np.sum(np.isfinite(S), axis=0), 1))
            model_sb = model_scale = None
            if args.model or args.seed == 'model':
                try:
                    any_info = next(i for i in infos if i.root == root)
                    ph = fits.getheader(any_info.path)
                    model_sb, model_scale, doy = model_seed(lam_c, seed_sb, ph['TARG_RA'], ph['TARG_DEC'], ph['DATE-OBS'])
                    print(f'{fg} {root}: jwst_backgrounds day {doy}: data/model = {model_scale:.3f}')
                except Exception as e:
                    print(f'{fg} {root}: jwst_backgrounds comparison skipped ({type(e).__name__}: {e})')
            if args.seed == 'model' and model_sb is not None:
                seed = (model_sb * model_scale, np.abs(model_sb * model_scale) * 0.1, np.ones_like(lam_c, int))
            else:
                seed = (seed_sb, seed_err, np.sum(np.isfinite(S), axis=0))
            sky = select_clean(group, seed, opt)
            clean = [s for s in group if s.clean]
            reasons = defaultdict(int)
            for s in group:
                reasons[s.reason] += 1
            summary.append((fg, root, len(group), len(clean), dict(reasons), model_scale))
            print(f'{fg} {root}: {len(clean)}/{len(group)} shutters clean; {dict(reasons)}')

            for s in group:
                shutter_rows.append(dict(filter_grating=fg, root=s.root, nod=s.nod, detector=s.detector,
                                         source_id=s.source_id, slitlet_id=s.slitlet_id, quadrant=s.quadrant,
                                         k=s.k, n_pix=s.n_pix, scale=s.scale, scale_err=s.scale_err,
                                         chi2_red=s.chi2_red, line_run=s.line_run, clean=s.clean, reason=s.reason))
            for b in range(len(lam_c)):
                sky_rows.append(dict(filter_grating=fg, root=root, wavelength=lam_c[b], surf_bright=sky[0][b],
                                     surf_bright_err=sky[1][b], n_shutters=int(sky[2][b]),
                                     seed=seed_sb[b],
                                     model=(model_sb[b] * model_scale) if model_sb is not None else np.nan))

            # per exposure x detector against the pooled sky (design §4.1: is a scale/offset needed?)
            per_exp = {}
            for key in sorted(set((s.nod, s.detector) for s in clean)):
                sub = combine([s for s in clean if (s.nod, s.detector) == key], opt['clip_sigma'])
                if sub is None:
                    continue
                m = np.isfinite(sub[0]) & np.isfinite(sky[0])
                if m.sum() < 10:
                    continue
                w = 1.0 / (sub[1][m] ** 2 + sky[1][m] ** 2)
                A = np.vstack([sky[0][m], np.ones(m.sum())]).T * np.sqrt(w)[:, None]
                coef, *_ = np.linalg.lstsq(A, sub[0][m] * np.sqrt(w), rcond=None)
                cov = np.linalg.inv(A.T @ A)
                per_exp[key] = sub
                exp_rows.append(dict(filter_grating=fg, root=root, nod=key[0], detector=key[1],
                                     n_shutters=sum(1 for s in clean if (s.nod, s.detector) == key),
                                     scale=coef[0], scale_err=np.sqrt(cov[0, 0]),
                                     offset=coef[1], offset_err=np.sqrt(cov[1, 1]),
                                     offset_rel=coef[1] / np.nanmedian(sky[0][m])))

            # NRS1 vs NRS2 at the same wavelengths (prism) ------------------------------------
            det_sky = {}
            for det in ('nrs1', 'nrs2'):
                sub = combine([s for s in clean if s.detector == det], opt['clip_sigma'])
                if sub is not None:
                    det_sky[det] = sub
            ratio_note = ''
            if len(det_sky) == 2:
                m = np.isfinite(det_sky['nrs1'][0]) & np.isfinite(det_sky['nrs2'][0])
                if m.sum() > 10:
                    r = det_sky['nrs1'][0][m] / det_sky['nrs2'][0][m]
                    ratio_note = f'NRS1/NRS2 median {np.nanmedian(r):.3f}, scatter {1.4826 * np.nanmedian(np.abs(r - np.nanmedian(r))):.3f} over {m.sum()} bins'
                    print(f'{fg} {root}: {ratio_note}')

            # Paschen beta check -------------------------------------------------------------
            pab_note = ''
            win = (lam_c > PABETA_VAC_UM - 0.012) & (lam_c < PABETA_VAC_UM + 0.012) & np.isfinite(sky[0])
            if win.sum() > 8 and 'prism' not in fg:
                core = np.abs(lam_c - PABETA_VAC_UM) < 0.004
                side = win & ~core
                p = np.polyfit(lam_c[side], sky[0][side], 1)
                norm = sky[0] / np.polyval(p, lam_c)
                depth = 1 - np.nanmin(norm[win & core])
                pab_note = f'Pa-beta depth {depth * 100:.1f}% (noise {np.nanstd(norm[side]) * 100:.1f}%)'
                print(f'{fg} {root}: {pab_note}')

            # ---- plots --------------------------------------------------------------------
            fig, axes = plt.subplots(3, 1, figsize=(10, 11), sharex=False)
            ax = axes[0]
            for s in group:
                ax.plot(lam_c, s.sb, lw=0.3, alpha=0.5, color='tab:green' if s.clean else 'tab:red')
            ax.plot(lam_c, sky[0], color='k', lw=1.2, label=f'pooled sky, {len(clean)} clean of {len(group)}')
            if model_sb is not None:
                ax.plot(lam_c, model_sb * model_scale, color='tab:blue', lw=1, ls='--',
                        label=f'jwst_backgrounds x {model_scale:.2f}')
            ax.set_ylabel('surface brightness [MJy/sr]'); ax.set_title(f'{args.obs} {fg} {root}')
            ax.set_ylim(0, np.nanpercentile(sky[0], 99) * 2); ax.legend(fontsize=8)
            ax = axes[1]
            for det, sub in det_sky.items():
                ax.plot(lam_c, sub[0] / sky[0], lw=0.8, label=f'{det} / pooled')
            for key, sub in per_exp.items():
                ax.plot(lam_c, sub[0] / sky[0], lw=0.3, alpha=0.4, color='gray')
            ax.axhline(1, color='k', lw=0.5); ax.set_ylim(0.8, 1.2)
            ax.set_ylabel('ratio to pooled'); ax.set_xlabel('wavelength [um]')
            ax.legend(fontsize=8, title=ratio_note or None)
            ax = axes[2]
            a = np.array([s.scale for s in group]); ok = np.isfinite(a)
            ax.hist(a[ok & np.array([s.clean for s in group])], bins=40, range=(0, 2), color='tab:green', label='clean')
            ax.hist(a[ok & ~np.array([s.clean for s in group])], bins=40, range=(0, 2), color='tab:red', alpha=0.6, label='rejected')
            ax.set_xlabel('per-shutter scale a_s against the pooled sky'); ax.legend(fontsize=8,
                                                                                  title=pab_note or None)
            fig.tight_layout(); pdf.savefig(fig); plt.close(fig)
    pdf.close()

    Table(rows=shutter_rows).write(os.path.join(out, f'{args.obs}_shutters.ecsv'), overwrite=True)
    Table(rows=sky_rows).write(os.path.join(out, f'{args.obs}_sky.ecsv'), overwrite=True)
    if exp_rows:
        t = Table(rows=exp_rows)
        t.write(os.path.join(out, f'{args.obs}_exposures.ecsv'), overwrite=True)
        print('\nper exposure x detector against the pooled sky (design §4.1):')
        print(f'  scale: median {np.nanmedian(t["scale"]):.4f}, scatter {np.nanstd(t["scale"]):.4f}, '
              f'median error {np.nanmedian(t["scale_err"]):.4f}')
        print(f'  offset/sky: median {np.nanmedian(t["offset_rel"]):.4f}, scatter {np.nanstd(t["offset_rel"]):.4f}')
        print('  -> terms are needed only if the scatter clearly exceeds the median error')
    print(f'\nwrote {out}/')


if __name__ == '__main__':
    main()
