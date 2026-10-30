"""Where do the empty shutters that disagree with the pooled sky sit?

Reads the ``<obs>_shutters.ecsv`` written by ``prototype_master_bkg.py`` and places
each shutter's scale ``a_s`` (shutter / pooled sky) on the detector (at the shutter's
trace centre, from the canonical's ``SLTSTRT*``/``SLTSIZE*`` cards) and each slitlet's
median ``a_s`` on the MSA plane (quadrant, shutter column, shutter row, from the
metafile). Prints the breakdown by detector, quadrant, nod and distance from the
source. Design doc §9 (the G395M NRS2 excess was found with this).
"""
import argparse
import os

import numpy as np
from astropy.io import fits
from astropy.table import Table
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
ap.add_argument('--obs', required=True)
ap.add_argument('--grating', required=True, help='filter_grating value in the shutter table, e.g. g395m_f290lp')
ap.add_argument('--root', required=True, help='exposure root, e.g. jw07076001001_05101')
ap.add_argument('--shutters', required=True, help='<obs>_shutters.ecsv from prototype_master_bkg.py')
ap.add_argument('--vmax', type=float, default=1.8)
ap.add_argument('--out', default=None, help='output prefix (default <obs>_<root>)')
ARGS = ap.parse_args()
WS = os.path.join(os.environ.get('CAMPFIRE_ROOT', os.path.expanduser('~/campfire-data')), 'products', 'nirspec', ARGS.obs)
OUT = ARGS.out or f'{ARGS.obs}_{ARGS.root}'


def pct(x):
    return ' / '.join(f'{v:.3f}' for v in np.nanpercentile(x, [16, 50, 84]))


def main():
    sh = Table.read(ARGS.shutters)
    g = sh[(sh['filter_grating'] == ARGS.grating) & np.isfinite(sh['scale'])]
    print(f'{ARGS.grating}: {np.sum(g["clean"])}/{len(g)} clean')
    for col in ('detector', 'quadrant', 'nod', 'k'):
        print(f'-- by {col}')
        for v in sorted(set(g[col])):
            m = g[col] == v
            if m.sum() > 5:
                print(f'   {col}={v}: n={m.sum():4d} a_s 16/50/84 {pct(g["scale"][m])}  rejected {1 - np.mean(g["clean"][m]):.2f}'
                      f'  frac a_s>1.4 {np.mean(g["scale"][m] > 1.4):.2f}')
    print('-- by detector x quadrant')
    for d in sorted(set(g['detector'])):
        for q in sorted(set(g['quadrant'])):
            m = (g['detector'] == d) & (g['quadrant'] == q)
            if m.sum() > 5:
                print(f'   {d} Q{q}: n={m.sum():4d} a_s {pct(g["scale"][m])}  rejected {1 - np.mean(g["clean"][m]):.2f}')

    # per-slitlet coherence: is a_s a property of the slitlet (per detector)?
    for d in sorted(set(g['detector'])):
        gd = g[g['detector'] == d]
        med, spread = [], []
        for sl in np.unique(gd['slitlet_id']):
            a = np.array(gd['scale'][gd['slitlet_id'] == sl])
            if len(a) >= 4:
                med.append(np.median(a)); spread.append(np.percentile(a, 84) - np.percentile(a, 16))
        med, spread = np.array(med), np.array(spread)
        if len(med):
            print(f'-- {d}: {len(med)} slitlets with >= 4 shutters: within-slitlet 16-84 spread median {np.median(spread):.3f}, '
                  f'between-slitlet spread of medians {np.percentile(med, 84) - np.percentile(med, 16):.3f}, '
                  f'{np.sum(med > 1.4)} slitlets with median a_s > 1.4')

    # detector map
    xs, ys, det, a = [], [], [], []
    cache = {}
    for r in g:
        f = os.path.join(WS, f"{ARGS.root}_{r['nod']}_{r['detector']}_{r['source_id']}.fits")
        if f not in cache:
            if not os.path.exists(f):
                cache[f] = None
            else:
                h = fits.getheader(f, 'SCI')
                cache[f] = (h['SLTSTRT1'], h['SLTSTRT2'], h['SLTSIZE1'], h['SLTSIZE2'], str(h.get('SHUTSTA', '')))
        c = cache[f]
        if c is None:
            continue
        x0, y0, nx, ny, state = c
        kx = state.find('x'); nsh = max(len(state), 1)
        xs.append(x0 + nx / 2); ys.append(y0 + (kx + r['k'] + 0.5) / nsh * ny); det.append(r['detector']); a.append(r['scale'])
    xs, ys, det, a = map(np.array, (xs, ys, det, a))
    fig, axes = plt.subplots(1, 2, figsize=(15, 7), sharey=True)
    for ax, d in zip(axes, ('nrs1', 'nrs2')):
        m = det == d
        sc = ax.scatter(xs[m], ys[m], c=a[m], s=14, cmap='coolwarm', vmin=0.8, vmax=ARGS.vmax)
        ax.set_title(f'{ARGS.grating} {d}: {m.sum()} empty shutters, median a_s {np.nanmedian(a[m]):.3f}, '
                     f'{np.mean(a[m] > 1.4):.0%} above 1.4')
        ax.set_xlim(0, 2048); ax.set_ylim(0, 2048); ax.set_xlabel('detector x (dispersion)'); ax.set_aspect('equal')
    axes[0].set_ylabel('detector y'); fig.colorbar(sc, ax=axes, label='a_s = shutter / pooled sky', shrink=0.8)
    fig.suptitle(f'{ARGS.obs} {ARGS.grating}: per-shutter scale against the pooled sky at the shutter trace centre (all nods)')
    fig.savefig(f'{OUT}_as_detector_map.png', dpi=100)

    # MSA map (slitlet medians)
    from campfire_pipeline.nirspec.metafile import MetaFile
    rate = os.path.join(WS, f'{ARGS.root}_00001_nrs1_rate.fits')
    prev = os.getcwd(); os.chdir(WS)
    try:
        mf = MetaFile.load_for_rate_file(rate)
    finally:
        os.chdir(prev)
    st = mf.shutter_table
    st = st[st['msa_metadata_id'] == int(fits.getheader(rate)['MSAMETID'])]
    pos = {}
    for sl in np.unique(st['slitlet_id']):
        r = st[st['slitlet_id'] == sl]
        pos[int(sl)] = (int(r['shutter_quadrant'][0]), float(np.median(r['shutter_column'])), float(np.median(r['shutter_row'])))
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    for ax, d in zip(axes, ('nrs1', 'nrs2')):
        gd = g[g['detector'] == d]
        px, py, pa = [], [], []
        for sl in np.unique(gd['slitlet_id']):
            if int(sl) not in pos:
                continue
            q, c, r = pos[int(sl)]
            px.append(c + 400 * (q in (2, 4))); py.append(r + 200 * (q in (1, 2)))
            pa.append(np.median(gd['scale'][gd['slitlet_id'] == sl]))
        px, py, pa = map(np.array, (px, py, pa))
        sc = ax.scatter(px, py, c=pa, s=22, cmap='coolwarm', vmin=0.8, vmax=ARGS.vmax)
        for xq, yq, lab in ((0, 200, 'Q1'), (400, 200, 'Q2'), (0, 0, 'Q3'), (400, 0, 'Q4')):
            ax.text(xq + 5, yq + 5, lab, fontsize=11, weight='bold')
        ax.axvline(400, color='gray', lw=0.5); ax.axhline(186, color='gray', lw=0.5)
        ax.set_title(f'{ARGS.grating} {d}: slitlet-median a_s, {len(pa)} slitlets, {np.mean(pa > 1.4):.0%} above 1.4')
        ax.set_xlabel('MSA shutter column (+400 for Q2/Q4)'); ax.set_ylabel('MSA shutter row (+200 for Q1/Q2)')
        fig.colorbar(sc, ax=ax, shrink=0.8)
    fig.suptitle(f'{ARGS.obs} {ARGS.grating}: empty-shutter sky scale per slitlet on the MSA plane (all nods)')
    fig.tight_layout(); fig.savefig(f'{OUT}_as_msa_map.png', dpi=100)
    print('saved', f'{OUT}_as_detector_map.png', f'{OUT}_as_msa_map.png')


if __name__ == '__main__':
    main()
