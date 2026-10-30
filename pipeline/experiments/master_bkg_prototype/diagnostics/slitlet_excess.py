"""Slitlets whose empty shutters are brighter than the pooled sky: counts or calibration,
what spectrum, and does it track the neighbouring traces?

For the slitlets of one detector whose median ``a_s`` (from ``<obs>_shutters.ecsv``) falls
in the ``--normal`` and ``--high`` windows, in the ``k = +-1`` shutter centres (``|f| < 0.3``,
bar shadow > 0.8, DQ clean):

1. rate-file DN/s vs calibrated surface brightness (uniform calibration) in a wavelength
   window — equal calibration factors mean the excess is in the counts;
2. the median empty-shutter spectrum of each group and the difference (the excess
   spectrum) against the pooled sky;
3. the correlation of the own-shutter counts and of ``a_s`` with the brightness of the
   rate-file pixels 2-10 px outside the slitlet footprint (a neighbouring trace's wings
   would correlate).

Design doc §9 (ember_cosmos_p1 G395M NRS2).
"""
import argparse
import glob
import logging
import os
import sys

import numpy as np
from astropy.io import fits
from astropy.table import Table
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prototype_master_bkg as P  # noqa: E402

logging.disable(logging.CRITICAL)   # BarShadowStep chatter

ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
ap.add_argument('--obs', required=True)
ap.add_argument('--grating', required=True, help='filter_grating value in the shutter table')
ap.add_argument('--root', required=True, help='exposure root, e.g. jw07076001001_05101')
ap.add_argument('--detector', default='nrs2', choices=['nrs1', 'nrs2'])
ap.add_argument('--sky', required=True, help='<obs>_sky.ecsv from prototype_master_bkg.py')
ap.add_argument('--shutters', required=True, help='<obs>_shutters.ecsv from prototype_master_bkg.py')
ap.add_argument('--normal', type=float, nargs=2, default=(0.95, 1.05), help='a_s window of the normal group')
ap.add_argument('--high', type=float, nargs=2, default=(1.45, 1.8), help='a_s window of the high group')
ap.add_argument('--n-per-group', type=int, default=12)
ap.add_argument('--window', type=float, nargs=2, default=(4.6, 5.0), help='wavelength window [um] of the rate-vs-cal comparison')
ap.add_argument('--out', default=None, help='output prefix (default <obs>_<root>_<detector>)')
ARGS = ap.parse_args()
WS = os.path.join(os.environ.get('CAMPFIRE_ROOT', os.path.expanduser('~/campfire-data')), 'products', 'nirspec', ARGS.obs)
OUT = ARGS.out or f'{ARGS.obs}_{ARGS.root}_{ARGS.detector}'
NODS = ('00001', '00002', '00003')
_RATE = {}


def rate_image(nod):
    f = os.path.join(WS, f'{ARGS.root}_{nod}_{ARGS.detector}_rate.fits')
    if f not in _RATE:
        _RATE[f] = fits.getdata(f, 'SCI')
    return _RATE[f]


def empty_pixels(path):
    """Per-pixel arrays of one canonical: uniform-calibrated SB, wavelength, the k = +-1
    centre mask, and the cutout origin."""
    from jwst.datamodels import MultiSlitModel
    with fits.open(path, memmap=False) as h:
        sci = np.array(h['SCI'].data, float); dq = np.array(h['DQ'].data); hs = h['SCI'].header
        x0, y0 = hs['SLTSTRT1'] - 1, hs['SLTSTRT2'] - 1; pix = hs['PIXAR_SR']
        pu = np.array(h['PATHLOSS_UN'].data); pp = np.array(h['PATHLOSS_PS'].data)
    m = MultiSlitModel(path); s = m.slits[0]
    xs, ys, lam = P._slit_coordinates(s)
    pitch = s.slit_yscale if s.slit_yscale > 1 else 1 / s.slit_yscale
    bar = P._barshadow_array(m, s); m.close()
    k = np.rint(ys / pitch); fr = np.abs(ys / pitch - k)
    good = (np.abs(k) == 1) & (fr < 0.3) & (dq == 0) & np.isfinite(sci)
    if bar is not None:
        good &= bar > 0.8
    sb = sci / pix * pp / pu      # back to the uniform calibration
    return sb, lam, good, (x0, y0), np.isfinite(sci)


def main():
    from scipy import ndimage, stats
    sky = Table.read(ARGS.sky)
    sky = sky[sky['root'] == ARGS.root] if 'root' in sky.colnames else sky
    LAM, POOL = np.array(sky['wavelength']), np.array(sky['surf_bright'])
    sh = Table.read(ARGS.shutters)
    g = sh[(sh['filter_grating'] == ARGS.grating) & np.isfinite(sh['scale']) & (sh['detector'] == ARGS.detector)]
    med = {}
    for sl in np.unique(g['slitlet_id']):
        sel = g[g['slitlet_id'] == sl]
        if len(sel) >= 4:
            med[int(sl)] = (float(np.median(sel['scale'])), int(sel['source_id'][0]))
    groups = {name: [(sl, a, sid) for sl, (a, sid) in med.items() if lo < a < hi][:ARGS.n_per_group]
              for name, (lo, hi) in (('normal', ARGS.normal), ('high', ARGS.high))}
    print(f'{ARGS.detector}: {len(groups["normal"])} normal and {len(groups["high"])} high slitlets')

    # 1. rate vs calibrated, and 2. spectra
    edges = np.arange(2.5, 5.4, 0.1); lc = 0.5 * (edges[1:] + edges[:-1])
    lo, hi = ARGS.window
    spec, summary = {}, {}
    for name, lst in groups.items():
        per_spec, per_rate = [], []
        for sl, a, sid in lst:
            lam_all, sb_all, r_all = [], [], []
            for nod in NODS:
                f = os.path.join(WS, f'{ARGS.root}_{nod}_{ARGS.detector}_{sid}.fits')
                if not os.path.exists(f):
                    continue
                sb, lam, good, (x0, y0), _ = empty_pixels(f)
                r = rate_image(nod)[y0:y0 + sb.shape[0], x0:x0 + sb.shape[1]]
                if r.shape != sb.shape:
                    continue
                lam_all.append(lam[good]); sb_all.append(sb[good]); r_all.append(r[good])
            if not lam_all:
                continue
            lam_all, sb_all, r_all = (np.concatenate(v) for v in (lam_all, sb_all, r_all))
            w = (lam_all > lo) & (lam_all < hi)
            if w.sum() > 50:
                per_rate.append((np.nanmedian(r_all[w]), np.nanmedian(sb_all[w]), np.nanmedian(sb_all[w] / r_all[w])))
                print(f'  {name:6s} slitlet {sl:4d} src {sid} a_s={a:.2f}: {lo}-{hi} um  rate {per_rate[-1][0]:.4f} DN/s'
                      f'  SB {per_rate[-1][1]:.3f} MJy/sr  SB/rate {per_rate[-1][2]:.1f}')
            idx = np.digitize(lam_all, edges) - 1
            per_spec.append([np.nanmedian(sb_all[idx == b]) if np.sum(idx == b) > 30 else np.nan for b in range(len(lc))])
        spec[name] = np.array(per_spec); summary[name] = np.array(per_rate)
    for name in groups:
        v = summary[name]
        if len(v):
            print(f'{name}: median rate {np.median(v[:, 0]):.4f} DN/s, SB {np.median(v[:, 1]):.3f} MJy/sr, SB/rate {np.median(v[:, 2]):.1f}')
    pool_b = np.interp(lc, LAM, POOL)
    hi_med = np.nanmedian(spec['high'], axis=0); no_med = np.nanmedian(spec['normal'], axis=0)
    print('lambda  pooled  normal  high  high-normal  (high-normal)/pooled')
    for i in range(len(lc)):
        if np.isfinite(hi_med[i]) and np.isfinite(no_med[i]):
            print(f'{lc[i]:.2f}  {pool_b[i]:.3f}  {no_med[i]:.3f}  {hi_med[i]:.3f}  {hi_med[i] - no_med[i]:+.3f}  {(hi_med[i] - no_med[i]) / pool_b[i]:+.2f}')
    fig, ax = plt.subplots(1, 2, figsize=(13, 4.5))
    for name, col in (('normal', 'tab:blue'), ('high', 'tab:red')):
        for row in spec[name]:
            ax[0].plot(lc, row, color=col, lw=0.6, alpha=0.4)
        ax[0].plot(lc, np.nanmedian(spec[name], axis=0), color=col, lw=2, label=f'{name} slitlets (n={len(spec[name])}), median')
    ax[0].plot(lc, pool_b, 'k--', lw=1.5, label='pooled sky'); ax[0].set_ylabel('SB in the k=+-1 empty shutters [MJy/sr]')
    ax[0].set_xlabel('wavelength [um]'); ax[0].legend(fontsize=8)
    ax[1].plot(lc, hi_med - no_med, 'r-', lw=2, label='high - normal'); ax[1].plot(lc, pool_b, 'k--', lw=1, label='pooled sky')
    ax[1].set_ylabel('MJy/sr'); ax[1].set_xlabel('wavelength [um]'); ax[1].legend(fontsize=8); ax[1].set_title('excess spectrum of the high slitlets')
    fig.suptitle(f'{ARGS.obs} {ARGS.grating} {ARGS.detector.upper()}: empty shutters of high vs normal slitlets (uniform calibration)')
    fig.tight_layout(); fig.savefig(f'{OUT}_excess_spectrum.png', dpi=100)

    # 3. neighbouring traces (nod 1, all slitlets of the detector)
    g1 = g[g['nod'] == NODS[0]]
    rows = []
    for sl in np.unique(g1['slitlet_id']):
        sel = g1[g1['slitlet_id'] == sl]; sid = int(sel['source_id'][0]); a = float(np.median(sel['scale']))
        f = os.path.join(WS, f'{ARGS.root}_{NODS[0]}_{ARGS.detector}_{sid}.fits')
        if not os.path.exists(f):
            continue
        sb, lam, good, (x0, y0), finite = empty_pixels(f)
        ny, nx = sb.shape
        rate = rate_image(NODS[0])
        ya, yb = max(y0 - 12, 0), min(y0 + ny + 12, 2048)
        box = rate[ya:yb, x0:x0 + nx]
        foot = np.zeros_like(box, bool); foot[y0 - ya:y0 - ya + ny, :] = finite
        near = ndimage.binary_dilation(foot, iterations=10) & ~ndimage.binary_dilation(foot, iterations=2)
        lam_col = np.nanmedian(lam, axis=0); cols = (lam_col > lo) & (lam_col < hi)
        own = good & cols[None, :]; nb = near & cols[None, :]
        if own.sum() < 50 or nb.sum() < 50:
            continue
        r_own = np.nanmedian(rate[y0:y0 + ny, x0:x0 + nx][own])
        rows.append((int(sl), sid, a, r_own, np.nanpercentile(box[nb], 90)))
    t = Table(rows=rows, names=('slitlet', 'source_id', 'a_s', 'own_rate', 'neighbour_p90'))
    t.write(f'{OUT}_neighbours.ecsv', overwrite=True)
    x, y, a = np.array(t['neighbour_p90']), np.array(t['own_rate']), np.array(t['a_s'])
    print(f'neighbours ({lo}-{hi} um, nod 1, n={len(t)}): Spearman rho(own, neighbour p90) = {stats.spearmanr(x, y)[0]:.2f}, '
          f'rho(a_s, neighbour p90) = {stats.spearmanr(a, x)[0]:.2f}, rho(a_s, own) = {stats.spearmanr(a, y)[0]:.2f}')
    fig, ax = plt.subplots(figsize=(7, 5))
    sc = ax.scatter(x, y, c=a, s=14, cmap='coolwarm', vmin=0.8, vmax=1.8)
    ax.set_xlabel('rate 2-10 px outside the footprint, 90th percentile [DN/s]'); ax.set_ylabel('own k=+-1 shutter rate [DN/s]')
    ax.set_title(f'{ARGS.obs} {ARGS.grating} {ARGS.detector.upper()} {lo}-{hi} um: empty-shutter counts vs the neighbouring traces (colour: a_s)', fontsize=9)
    fig.colorbar(sc, ax=ax, label='a_s'); fig.tight_layout(); fig.savefig(f'{OUT}_neighbours.png', dpi=100)
    print('saved', f'{OUT}_excess_spectrum.png', f'{OUT}_neighbours.png')


if __name__ == '__main__':
    main()
