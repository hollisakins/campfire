"""The trace past the nominal red cutoff, and what it does to the empty shutters downstream.

The assign_wcs bounding box of a slit stops at the filter's nominal wavelength range, but
the detector keeps receiving the dispersed light beyond it. Two measurements on one
exposure root and detector:

1. **The tail**: for every canonical whose nominal red end lies on the detector, the rate
   in its trace rows past that end, stacked against the distance beyond the end, using only
   pixels under no nominal footprint (the WCS counter from ``prototype_master_bkg.py``).
2. **The contamination**: every nominal footprint extended redward by ``--tail-px``; for each
   empty shutter (``k = +-1``, nominal counter == 1) the fraction of its pixels under another
   slitlet's tail, against the shutter's scale ``a_s`` from ``<obs>_shutters.ecsv``.

Design doc §9 (ember_cosmos_p1 G395M: the NRS2 excess).
"""
import argparse
import glob
import logging
import os
import re
import sys

import numpy as np
from astropy.io import fits
from astropy.table import Table
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import prototype_master_bkg as P  # noqa: E402

logging.disable(logging.CRITICAL)

ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
ap.add_argument('--obs', required=True)
ap.add_argument('--grating', required=True, help='filter_grating value in the shutter table')
ap.add_argument('--root', required=True, help='exposure root, e.g. jw07076001001_05101')
ap.add_argument('--nod', default='00001')
ap.add_argument('--shutters', required=True, help='<obs>_shutters.ecsv from prototype_master_bkg.py')
ap.add_argument('--coverage-dir', required=True, help='coverage_<rate>_*.npy counters (the plain WCS ones, no tail)')
ap.add_argument('--tail-px', type=int, default=170)
ap.add_argument('--out', default=None, help='output prefix (default <obs>_<root>)')
ARGS = ap.parse_args()
WS = os.path.join(os.environ.get('CAMPFIRE_ROOT', os.path.expanduser('~/campfire-data')), 'products', 'nirspec', ARGS.obs)
OUT = ARGS.out or f'{ARGS.obs}_{ARGS.root}'


def footprints(det):
    out = {}
    for f in glob.glob(os.path.join(WS, f'{ARGS.root}_{ARGS.nod}_{det}_[0-9]*.fits')):
        if not re.search(r'_nrs[12]_\d+\.fits$', f):
            continue
        sid = int(f.split('_')[-1][:-5]); hs = fits.getheader(f, 'SCI')
        with fits.open(f) as h:
            fin = np.isfinite(h['SCI'].data)
        out[sid] = (hs['SLTSTRT1'] - 1, hs['SLTSTRT2'] - 1, fin, f)
    return out


def load_counter(det):
    hits = [p for p in glob.glob(os.path.join(ARGS.coverage_dir, f'coverage_{ARGS.root}_{ARGS.nod}_{det}_rate_*.npy')) if 'tail' not in os.path.basename(p)]
    if not hits:
        raise SystemExit(f'no plain coverage counter for {det} in {ARGS.coverage_dir}')
    return np.load(hits[0])


def main():
    from jwst.datamodels import MultiSlitModel
    from scipy import stats
    sh = Table.read(ARGS.shutters)
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    for j, det in enumerate(('nrs1', 'nrs2')):
        rate_path = os.path.join(WS, f'{ARGS.root}_{ARGS.nod}_{det}_rate.fits')
        rate = fits.getdata(rate_path, 'SCI'); dq = fits.getdata(rate_path, 'DQ')
        cov = load_counter(det)
        foot = footprints(det)

        # 1. the tail
        dxs = np.arange(-30, 400); prof = []
        for sid, (x0, y0, fin, _) in foot.items():
            cols = np.where(fin.any(axis=0))[0]
            if len(cols) == 0:
                continue
            xmax = x0 + cols[-1]
            if xmax > 2048 - 60:
                continue                      # nominal end runs off the detector
            rows = np.where(fin[:, cols[-1]])[0]; ya, yb = y0 + rows[0], y0 + rows[-1]
            row = []
            for dx in dxs:
                x = xmax + dx
                if x < 0 or x >= 2048:
                    row.append(np.nan); continue
                col = rate[ya:yb + 1, x]; c = cov[ya:yb + 1, x]; d = dq[ya:yb + 1, x]
                ok = (d == 0) & ((c == 1) if dx <= 0 else (c == 0))
                row.append(np.nanmedian(col[ok]) if ok.sum() >= 3 else np.nan)
            prof.append(row)
        prof = np.array(prof)
        ax = axes[0, j]
        if len(prof):
            med = np.nanmedian(prof, axis=0)
            ax.plot(dxs, med, 'k-', lw=1.5, label=f'median over {len(prof)} traces whose nominal end is on the detector')
            ax.plot(dxs, np.nanpercentile(prof, 25, axis=0), 'k:', lw=0.8); ax.plot(dxs, np.nanpercentile(prof, 75, axis=0), 'k:', lw=0.8, label='25 / 75 pct')
            for dx in (0, 50, 100, 150, 200, 300):
                i = np.searchsorted(dxs, dx)
                print(f'{det}: {dx:4d} px past the nominal end: median {med[i]:+.4f} DN/s (n={np.sum(np.isfinite(prof[:, i]))})')
        ax.axvline(0, color='r', lw=0.8); ax.axhline(0, color='gray', lw=0.5); ax.legend(fontsize=8)
        ax.set_xlabel('pixels beyond the nominal red end of the trace (WCS bounding box)'); ax.set_ylabel('rate in the trace rows [DN/s], pixels under no nominal footprint')
        ax.set_title(f'{det} {ARGS.grating} nod {ARGS.nod}: light past the nominal cutoff', fontsize=10)

        # 2. tail-aware overlap vs a_s
        tail_sum = np.zeros((2048, 2048), np.int16); own = {}
        T = ARGS.tail_px
        for sid, (x0, y0, fin, _) in foot.items():
            ny, nx = fin.shape
            ext = np.zeros((ny, nx + T), bool)
            for dx in range(1, T + 1):
                ext[:, dx:dx + nx] |= fin
            ext[:, :nx] &= ~fin
            xe = min(x0 + nx + T, 2048); ye = min(y0 + ny, 2048)
            sub = ext[:ye - y0, :xe - x0]
            tail_sum[y0:ye, x0:xe] += sub; own[sid] = sub
        g = sh[(sh['filter_grating'] == ARGS.grating) & np.isfinite(sh['scale']) & (sh['detector'] == det) & (sh['nod'] == ARGS.nod)]
        rows = []
        for sid, (x0, y0, fin, f) in foot.items():
            sel = g[g['source_id'] == sid]
            if not len(sel):
                continue
            m = MultiSlitModel(f); s = m.slits[0]; xs, ys, lam = P._slit_coordinates(s)
            pitch = s.slit_yscale if s.slit_yscale > 1 else 1 / s.slit_yscale; m.close()
            k = np.rint(ys / pitch); fr = np.abs(ys / pitch - k)
            ny, nx = fin.shape; ye = min(y0 + ny, 2048); xe = min(x0 + nx, 2048)
            c = cov[y0:ye, x0:xe]; t = tail_sum[y0:ye, x0:xe].astype(int) - own[sid][:, :xe - x0].astype(int)
            for kk in (-1, 1):
                empty = (k[:ye - y0, :xe - x0] == kk) & (fr[:ye - y0, :xe - x0] < 0.3) & fin[:ye - y0, :xe - x0] & (c == 1)
                if empty.sum() < 50 or not np.any(sel['k'] == kk):
                    continue
                rows.append((np.mean(t[empty] > 0), float(sel['scale'][sel['k'] == kk][0])))
        v = np.array(rows)
        ax = axes[1, j]
        if len(v):
            print(f'{det}: {len(v)} empty shutters; fraction of pixels under another trace\'s tail (tail {T} px): median {np.median(v[:, 0]):.2f}; '
                  f'Spearman rho(a_s, fraction) = {stats.spearmanr(v[:, 1], v[:, 0])[0]:.2f}')
            for lo, hi in ((0, 0.05), (0.05, 0.5), (0.5, 1.01)):
                mm = (v[:, 0] >= lo) & (v[:, 0] < hi)
                if mm.sum() > 3:
                    print(f'   fraction {lo:.2f}-{hi:.2f}: n={mm.sum():3d}  a_s median {np.median(v[mm, 1]):.3f}  frac a_s > 1.4 {np.mean(v[mm, 1] > 1.4):.2f}')
            ax.scatter(v[:, 0], v[:, 1], s=10, alpha=0.6)
        ax.axhline(1, color='k', lw=0.5); ax.set_ylim(0.6, 2.4)
        ax.set_xlabel(f"fraction of the shutter's pixels under another trace's tail (nominal end + {T} px)"); ax.set_ylabel('shutter a_s')
        ax.set_title(f'{det}: empty-shutter scale vs tail-aware overlap', fontsize=10)
    fig.suptitle(f'{ARGS.obs} {ARGS.grating}: the trace past the nominal cutoff and the shutters it lands in')
    fig.tight_layout(); fig.savefig(f'{OUT}_trace_tails.png', dpi=100); print('saved', f'{OUT}_trace_tails.png')


if __name__ == '__main__':
    main()
