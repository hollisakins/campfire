"""Stack empty-shutter surface brightness / pooled sky against the within-shutter coordinate f,
per nod and shutter class, to see how far the bar-shadow over-correction reaches."""
import sys, glob, numpy as np
sys.path.insert(0, '/Users/hollis/codes/campfire/pipeline/experiments/master_bkg_prototype')
import prototype_master_bkg as P
from astropy.io import fits
from astropy.table import Table
from jwst.datamodels import MultiSlitModel, dqflags
from multiprocessing import Pool
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt

import argparse
_ap = argparse.ArgumentParser(description='Empty-shutter bar-profile diagnostic (design doc §9).')
_ap.add_argument('--obs', default='ember_egs_p1', help='observation name (products/nirspec/<obs>)')
_ap.add_argument('--root', default='jw07076020001_04101', help='exposure root <obs>_<config> whose canonicals to stack')
_ap.add_argument('--sky', default='full_ember_egs_p1_v3/ember_egs_p1_sky.ecsv', help='<obs>_sky.ecsv from prototype_master_bkg.py')
_ap.add_argument('--max-sources', type=int, default=90)
_ap.add_argument('--out', default=None, help='output figure path')
_ap.add_argument('--detector', choices=['nrs1', 'nrs2'], default=None, help='restrict to one detector')
_ap.add_argument('--coverage-dir', default=None, help='directory of coverage_<rate>_*.npy counters written by prototype_master_bkg.py (--coverage wcs); pixels under more than one slitlet are dropped')
_ap.add_argument('--band', type=float, nargs=2, default=(1.0, 3.0), help='wavelength window [um] to stack')
ARGS = _ap.parse_args()
import os as _os
WS = _os.path.join(_os.environ.get('CAMPFIRE_ROOT', _os.path.expanduser('~/campfire-data')), 'products', 'nirspec', ARGS.obs)
SKY = Table.read(ARGS.sky)
SKY = SKY[SKY['root'] == ARGS.root] if 'root' in SKY.colnames and ARGS.root in set(SKY['root']) else SKY
LAM, POOL = np.array(SKY['wavelength']), np.array(SKY['surf_bright'])

def one(info):
    with fits.open(info.path, memmap=False) as h:
        pre = 'PRE_BKGSUB_SCI' in h
        sci = np.array(h['PRE_BKGSUB_SCI' if pre else 'SCI'].data, float)
        dq = np.array(h['PRE_BKGSUB_DQ' if pre else 'DQ'].data)
        pix = h['SCI'].header['PIXAR_SR']; pth = h['SCI'].header.get('PTHLOSS', 'POINT')
        patt = h[0].header.get('PATT_NUM'); msametid = int(h[0].header.get('MSAMETID', -1))
    m = MultiSlitModel(info.path); s = m.slits[0]
    xs, ys, lam = P._slit_coordinates(s)
    f = np.ones_like(sci) / pix
    if pth == 'POINT': f *= np.asarray(s.pathloss_point) / np.asarray(s.pathloss_uniform)
    bar = P._barshadow_array(m, s)
    pitch = s.slit_yscale if s.slit_yscale > 1 else 1 / s.slit_yscale
    state = str(s.shutter_state); slitlet_id = int(s.slitlet_id); m.close()
    sb_raw = sci * f                              # without the bar-shadow division
    sb_cor = sb_raw / bar if bar is not None else sb_raw
    k = np.rint(ys / pitch); fr = ys - k * pitch
    kx = state.find('x'); kmin, kmax = -kx, len(state) - 1 - kx
    good = np.isfinite(sb_raw) & np.isfinite(lam) & ((dq & (dqflags.pixel['DO_NOT_USE'] | dqflags.pixel['MSA_FAILED_OPEN'])) == 0)
    good &= (k >= kmin) & (k <= kmax) & (k != 0)
    if ARGS.coverage_dir:
        hits = glob.glob(_os.path.join(ARGS.coverage_dir, f'coverage_{info.rate_key}_rate_*.npy'))
        if hits:
            cov = np.load(hits[0])
            box = cov[info.ystart - 1:info.ystart - 1 + info.ysize, info.xstart - 1:info.xstart - 1 + info.xsize]
            if box.shape == good.shape:
                good &= box == 1
    own_col, others = P._other_source_columns(WS, info, slitlet_id, msametid, patt)
    for ko in ([c - own_col for c in others] if own_col is not None else []):
        good &= k != ko
    ratio_cor = sb_cor / np.interp(lam, LAM, POOL); ratio_raw = sb_raw / np.interp(lam, LAM, POOL)
    band = good & (lam > ARGS.band[0]) & (lam < ARGS.band[1])
    out = []
    for kk in np.unique(k[band]):
        sel = band & (k == kk)
        out.append((info.nod, int(abs(kk)), int(np.sign(kk)), fr[sel], ratio_cor[sel], ratio_raw[sel], (bar[sel] if bar is not None else np.ones(sel.sum()))))
    return out

def main():
    import re as _re
    paths = sorted(p for p in glob.glob(WS + f'/{ARGS.root}_*_nrs?_*.fits') if _re.search(r'_nrs[12]_\d+\.fits$', p))
    infos = [P._read_info(p) for p in paths]
    sids = sorted(set(i.source_id for i in infos))[:ARGS.max_sources]
    infos = [i for i in infos if i.source_id in sids]
    if ARGS.detector:
        infos = [i for i in infos if i.detector == ARGS.detector]
    with Pool(4) as pool:
        res = pool.map(one, infos)
    fbins = np.linspace(-0.6, 0.6, 25); fc = 0.5 * (fbins[1:] + fbins[:-1])
    classes = {}
    for lst in res:
        for nod, ak, sg, fr, rc, rr, br in lst:
            d = classes.setdefault((nod, ak), [[], [], [], []])
            d[0].append(fr * sg); d[1].append(rc); d[2].append(rr); d[3].append(br)   # f signed toward the source: f*sign(k) > 0 is the side away from the source? keep raw sign for now
    fig, axes = plt.subplots(1, 3, figsize=(17, 5))
    for key in sorted(classes):
        fr, rc, rr, br = [np.concatenate(v) for v in classes[key]]
        idx = np.digitize(fr, fbins) - 1
        med_c = [np.nanmedian(rc[idx == b]) if np.sum(idx == b) > 30 else np.nan for b in range(len(fc))]
        med_r = [np.nanmedian(rr[idx == b]) if np.sum(idx == b) > 30 else np.nan for b in range(len(fc))]
        med_b = [np.nanmedian(br[idx == b]) if np.sum(idx == b) > 30 else np.nan for b in range(len(fc))]
        lab = f'nod {int(key[0])} |k|={key[1]} (n={len(fr)})'
        axes[0].plot(fc, med_c, 'o-', ms=3, label=lab); axes[1].plot(fc, med_r, 'o-', ms=3, label=lab); axes[2].plot(fc, med_b, 'o-', ms=3, label=lab)
    for ax, t in zip(axes, ['SB / pooled sky, WITH bar-shadow division', 'SB / pooled sky, WITHOUT bar-shadow division', 'bar-shadow reference value']):
        ax.set_title(t, fontsize=10); ax.set_xlabel('within-shutter coordinate f  (0 = shutter centre, |f| = 0.5 = shutter edge; sign: f*sign(k))')
        for b in (-0.5, 0.5): ax.axvline(b, color='gray', lw=0.5, ls='--')
    axes[0].set_ylim(0.9, 1.5); axes[1].set_ylim(0.3, 1.2); axes[2].set_ylim(0.3, 1.05); axes[0].axhline(1, color='k', lw=0.5); axes[1].axhline(1, color='k', lw=0.5); axes[0].legend(fontsize=8)
    fig.suptitle(f'{ARGS.obs} {ARGS.root}{(" " + ARGS.detector.upper()) if ARGS.detector else ""}, empty shutters, {ARGS.band[0]:g}-{ARGS.band[1]:g} um, first {ARGS.max_sources} sources: row-level profile across a shutter')
    fig.tight_layout(); fig.savefig(ARGS.out or f'{ARGS.obs}_{ARGS.root}_profile_vs_f{("_" + ARGS.detector) if ARGS.detector else ""}.png', dpi=110); print('saved')

if __name__ == '__main__':
    main()
