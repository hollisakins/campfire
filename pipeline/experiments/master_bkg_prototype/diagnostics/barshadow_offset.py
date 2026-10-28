"""Is the jwst bar-shadow reference merely offset from the data by a fraction of a row?

For each empty-shutter pixel: measured surface brightness WITHOUT the bar-shadow division,
divided by the pooled sky at its wavelength, versus the CRDS reference evaluated the way
jwst's bar_shadow._calc_correction does but at yslit + dy. For a grid of dy (shutter-pitch
units; 1 pitch ~ 5.3 px) and a depth stretch A (shadow' = 1 - A (1 - shadow)), stack the
corrected ratio against the within-shutter coordinate and measure how flat it is.
"""
import sys, glob, numpy as np
sys.path.insert(0, '/Users/hollis/codes/campfire/pipeline/experiments/master_bkg_prototype')
import prototype_master_bkg as P
from astropy.io import fits
from astropy.table import Table
from multiprocessing import Pool
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt

import argparse
_ap = argparse.ArgumentParser(description='Empty-shutter bar-profile diagnostic (design doc §9).')
_ap.add_argument('--obs', default='ember_egs_p1', help='observation name (products/nirspec/<obs>)')
_ap.add_argument('--root', default='jw07076020001_04101', help='exposure root <obs>_<config> whose canonicals to stack')
_ap.add_argument('--sky', default='full_ember_egs_p1_v3/ember_egs_p1_sky.ecsv', help='<obs>_sky.ecsv from prototype_master_bkg.py')
_ap.add_argument('--max-sources', type=int, default=90)
_ap.add_argument('--out', default=None, help='output figure path')
_ap.add_argument('--coverage-dir', default=None, help='directory of coverage_<rate>_*.npy counters written by prototype_master_bkg.py (--coverage wcs); pixels under more than one slitlet are dropped')
ARGS = _ap.parse_args()
import os as _os
WS = _os.path.join(_os.environ.get('CAMPFIRE_ROOT', _os.path.expanduser('~/campfire-data')), 'products', 'nirspec', ARGS.obs)
SKY = Table.read(ARGS.sky)
SKY = SKY[SKY['root'] == ARGS.root] if 'root' in SKY.colnames and ARGS.root in set(SKY['root']) else SKY
LAM, POOL = np.array(SKY['wavelength']), np.array(SKY['surf_bright'])
CS = np.round(np.arange(-0.05, 0.011, 0.01), 3)
SS = (0.96, 1.0, 1.04, 1.08, 1.12)

def one(info):
    from jwst.datamodels import MultiSlitModel, dqflags
    from jwst.barshadow.barshadow_step import BarShadowStep
    from jwst.barshadow.bar_shadow import create_shutter_elements, create_shadow
    from stdatamodels.jwst import datamodels
    from scipy import ndimage
    from gwcs import wcstools
    with fits.open(info.path, memmap=False) as h:
        pre = 'PRE_BKGSUB_SCI' in h
        sci = np.array(h['PRE_BKGSUB_SCI' if pre else 'SCI'].data, float)
        dq = np.array(h['PRE_BKGSUB_DQ' if pre else 'DQ'].data)
        pix = h['SCI'].header['PIXAR_SR']; pth = h['SCI'].header.get('PTHLOSS', 'POINT')
        patt = h[0].header.get('PATT_NUM'); msametid = int(h[0].header.get('MSAMETID', -1))
    m = MultiSlitModel(info.path); s = m.slits[0]
    ref = BarShadowStep().get_reference_file(m, 'barshadow')
    bm = datamodels.BarshadowModel(ref)
    elements = create_shutter_elements(bm)
    w0, dw, dyinc = bm.crval1, bm.cdelt1, bm.cdelt2
    shutter_height = 1.0 / dyinc
    state = str(s.shutter_state); shadow = create_shadow(elements, state)
    x, y = wcstools.grid_from_bounding_box(s.meta.wcs.bounding_box, step=(1, 1))
    xs, ys_raw, lam_m = s.meta.wcs.get_transform('detector', 'slit_frame')(x, y)
    yscale = s.slit_yscale
    ys = ys_raw / yscale                       # jwst: shutter-centre separation = 1 in these units
    src_loc = state.find('x'); idx_fid = len(state) - src_loc
    fid_row = shadow.shape[0] - shutter_height * idx_fid
    wcol = (lam_m - w0) / dw
    f = np.ones_like(sci) / pix
    if pth == 'POINT': f *= np.asarray(s.pathloss_point) / np.asarray(s.pathloss_uniform)
    lam = lam_m * 1e6 if np.nanmedian(lam_m) < 1e-3 else lam_m
    slitlet_id = int(s.slitlet_id); quadrant = int(s.quadrant); bm.close(); m.close()
    sb = sci * f / np.interp(lam, LAM, POOL)   # ratio to the pooled sky, no bar-shadow division
    k = np.rint(ys); fr = ys - k               # within-shutter coordinate in pitch units
    kx = state.find('x'); kmin, kmax = -kx, len(state) - 1 - kx
    good = np.isfinite(sb) & np.isfinite(ys) & ((dq & (dqflags.pixel['DO_NOT_USE'] | dqflags.pixel['MSA_FAILED_OPEN'])) == 0)
    good &= (k >= kmin) & (k <= kmax) & (k != 0) & (lam > 0.8) & (lam < 5.0)
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
    end = (k == kmin) | (k == kmax)            # end shutter of the slitlet vs middle
    # row height in pitch units, from the slit-frame y step between adjacent rows
    rowh = float(np.nanmedian(np.abs(np.diff(ys, axis=0))))
    out = {}
    for c in CS:
        for st in SS:
            # evaluate the reference at k + (f + c) / st: c shifts the shutter, st > 1 widens its open area
            yref = k + (fr + c) / st
            corr = ndimage.map_coordinates(shadow, [fid_row + yref * shutter_height, wcol], cval=np.nan, order=1, mode='nearest')
            out[(c, st)] = corr[good].astype(np.float32)
    return dict(f=fr[good], sb=sb[good], lam=lam[good], end=end[good], side=np.sign(k[good]), rowh=rowh, sid=np.full(good.sum(), info.source_id), kk=k[good],
                quadrant=np.full(good.sum(), quadrant), det=np.full(good.sum(), info.detector == 'nrs2'), corr=out)

def flatness(ratio, f, lo=-0.5, hi=0.5, nb=20):
    edges = np.linspace(lo, hi, nb + 1); idx = np.digitize(f, edges) - 1
    med = np.array([np.nanmedian(ratio[idx == b]) if np.sum(idx == b) > 50 else np.nan for b in range(nb)])
    return np.sqrt(np.nanmean((med - 1) ** 2)), med, 0.5 * (edges[1:] + edges[:-1])

def main():
    import os
    paths = sorted(p for p in glob.glob(WS + f'/{ARGS.root}_*_nrs?_*.fits') if not p.endswith('_rate.fits'))
    infos = [P._read_info(p) for p in paths]
    sids = sorted(set(i.source_id for i in infos))[:ARGS.max_sources]
    infos = [i for i in infos if i.source_id in sids]
    with Pool(8) as pool:
        res = pool.map(one, infos)
    cat = lambda key: np.concatenate([r[key] for r in res])
    f, sb, lam, side, sid, kk = (cat(k) for k in ('f', 'sb', 'lam', 'side', 'sid', 'kk'))
    keys = list(res[0]['corr']); corr = {key: np.concatenate([r['corr'][key] for r in res]) for key in keys}
    print(f'{len(f)} pixels')
    rms = {key: flatness(sb / corr[key], f)[0] for key in keys}
    print('rms (%) vs shift c (rows) and width stretch s (cols):')
    print('      s: ' + ' '.join(f'{st:5.2f}' for st in SS))
    for c in CS: print(f'  c={c:+.2f}: ' + ' '.join(f'{rms[(c, st)]*100:5.2f}' for st in SS))
    best = min(keys, key=rms.get); print(f'best: c={best[0]:+.2f} ({best[0]*5.3:+.2f} px), s={best[1]:.2f}, rms {rms[best]*100:.2f}%  (jwst as is: {rms[(0.0, 1.0)]*100:.2f}%)')
    for name, msk in (('k<0', side < 0), ('k>0', side > 0)):
        r = {key: flatness(sb[msk] / corr[key][msk], f[msk])[0] for key in keys}; b = min(r, key=r.get)
        print(f'  {name}: best c={b[0]:+.2f} s={b[1]:.2f} rms {r[b]*100:.2f}%  (at global best {r[best]*100:.2f}%)')
    # does the source-adjacent edge excess depend on source brightness? (ratio with jwst as is, edge bin toward the source)
    flux = {}
    for s_ in np.unique(sid):
        cands = glob.glob(os.path.join(WS, f'{ARGS.obs}_*_{s_}_spec.fits'))
        fn = cands[0] if cands else ''
        if fn:
            d = fits.getdata(fn, 'SPEC1D'); w = d['wave']; m = (w > 1) & (w < 3) & np.isfinite(d['fnu']); flux[s_] = float(np.nanmedian(d['fnu'][m]))
    fl = np.array([flux.get(s_, np.nan) for s_ in sid])
    adj = (np.abs(kk) == 1)
    toward = (side * f) < 0                 # f pointing toward the source shutter
    r0 = sb / corr[(0.0, 1.0)]
    print('\nedge rows (0.3 < |f| < 0.5) of the shutter ADJACENT to the source, data / reference as jwst evaluates it, by source flux:')
    for lo, hi in ((-1, 0.02), (0.02, 0.1), (0.1, 10)):
        for lab, sel in (('toward source, k<0', toward & (side < 0)), ('toward source, k>0', toward & (side > 0)), ('away from source, k<0', ~toward & (side < 0)), ('away from source, k>0', ~toward & (side > 0))):
            m = adj & sel & (np.abs(f) > 0.3) & (np.abs(f) < 0.5) & (fl > lo) & (fl <= hi)
            if m.sum() > 300: print(f'  fnu {lo:5.2f}-{hi:5.2f} uJy  {lab:24s} n={m.sum():6d}  median ratio {np.nanmedian(r0[m]):.3f}')
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for key, col, lab in (((0.0, 1.0), 'tab:red', 'jwst as is'), (best, 'tab:green', f'shift {best[0]:+.2f}, width x{best[1]:.2f}')):
        for name, msk, ls in (('k<0', side < 0, '-'), ('k>0', side > 0, '--')):
            r, med, fc = flatness(sb[msk] / corr[key][msk], f[msk], -0.6, 0.6, 24); axes[0].plot(fc, med, ls, color=col, label=f'{lab}, {name}')
    axes[0].axhline(1, color='k', lw=0.5); axes[0].set_ylim(0.85, 1.4); axes[0].legend(fontsize=8); axes[0].set_xlabel('f (pitch units; |f| = 0.5 is the bar centre)')
    axes[0].set_title('data / reference, stacked vs within-shutter coordinate')
    _, med_d, fc = flatness(sb, f, -0.6, 0.6, 24); axes[1].plot(fc, med_d, 'k-', lw=2, label='data / pooled sky')
    for key, col, lab in (((0.0, 1.0), 'tab:red', 'reference as jwst evaluates it'), (best, 'tab:green', 'reference, shifted and widened')):
        _, med_r, fc = flatness(corr[key], f, -0.6, 0.6, 24); axes[1].plot(fc, med_r, color=col, ls='--', label=lab)
    axes[1].set_ylim(0.3, 1.1); axes[1].legend(fontsize=8); axes[1].set_title('measured profile vs reference'); axes[1].set_xlabel('f')
    fig.suptitle(f'{ARGS.obs} {ARGS.root}, empty shutters: CRDS bar-shadow reference, shift and width')
    fig.tight_layout(); fig.savefig(ARGS.out or f'{ARGS.obs}_{ARGS.root}_barshadow_offset3.png', dpi=110); print('saved')

if __name__ == '__main__':
    main()
