"""Where does the nod-2 middle-shutter excess live? Ratio spectra per shutter class + 2-D cutouts."""
import sys, glob, os, numpy as np
sys.path.insert(0, '/Users/hollis/codes/campfire/pipeline/experiments/master_bkg_prototype')
import prototype_master_bkg as P
from astropy.io import fits
from astropy.table import Table
from multiprocessing import Pool
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
def main():
    ws = '/Users/hollis/campfire-data/products/nirspec/ember_egs_p1'
    sky = Table.read('full_ember_egs_p1_pad3/ember_egs_p1_sky.ecsv')
    lam_c = np.array(sky['wavelength']); pooled = np.array(sky['surf_bright'])
    edges = np.concatenate([[lam_c[0] - (lam_c[1]-lam_c[0])/2], 0.5*(lam_c[1:]+lam_c[:-1]), [lam_c[-1] + (lam_c[-1]-lam_c[-2])/2]])
    paths = sorted(p for p in glob.glob(ws + '/jw07076020001_04101_0000?_nrs2_*.fits') if not p.endswith('_rate.fits'))
    infos = [P._read_info(p) for p in paths]
    sids = sorted(set(i.source_id for i in infos))[:60]
    infos = [i for i in infos if i.source_id in sids]
    opt = dict(source_pad_px=3, bar_min=0.35, min_pixels=20, barshadow=True)
    with Pool(8) as pool:
        res = pool.map(P._read_samples, [(i, ws, edges, None, opt) for i in infos])
    classes = {}
    for name, spectra, _ in res:
        for s in spectra:
            key = (s.nod, abs(s.k))
            classes.setdefault(key, []).append(s.sb / pooled)
    fig, axes = plt.subplots(2, 1, figsize=(11, 9))
    ax = axes[0]
    for key in sorted(classes):
        R = np.array(classes[key]); med = np.nanmedian(R, axis=0)
        ax.plot(lam_c, med, lw=1.2, label=f'nod {key[0]} |k|={key[1]} (n={len(R)})')
    ax.axhline(1, color='k', lw=0.5); ax.set_ylim(0.85, 1.25); ax.set_xlabel('wavelength [um]'); ax.set_ylabel('median shutter / pooled sky')
    ax.set_title('ember_egs_p1 prism, NRS2, first 60 sources: empty-shutter spectra relative to the pooled sky, by nod and distance from the source'); ax.legend(fontsize=8)
    # second panel: same but per *physical* shutter: majority pattern nod1 = source middle
    ax = axes[1]
    labels = {('00001', 1): 'nod1: end shutters A,B', ('00002', 1): 'nod2: middle M (source in A)', ('00002', 2): 'nod2: end B',
              ('00003', 1): 'nod3: middle M (source in B)', ('00003', 2): 'nod3: end A'}
    for key, lab in labels.items():
        if key in classes:
            R = np.array(classes[key]); ax.plot(lam_c, np.nanmedian(R, axis=0), lw=1.2, label=lab)
    ax.axhline(1, color='k', lw=0.5); ax.set_ylim(0.85, 1.25); ax.set_xlabel('wavelength [um]'); ax.set_ylabel('median shutter / pooled sky'); ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig('nod2_excess_spectra.png', dpi=110)

    # 2-D cutouts for three example slitlets: SB image per nod + cross-dispersion profile
    ex = [i for i in infos if i.source_id in sids[:12]]
    by_src = {}
    for i in ex: by_src.setdefault(i.source_id, {})[i.nod] = i
    chosen = [s for s, d in by_src.items() if len(d) == 3][:3]
    from jwst.datamodels import MultiSlitModel
    fig, axes = plt.subplots(len(chosen), 6, figsize=(18, 2.6 * len(chosen)))
    for r, sid in enumerate(chosen):
        for c, nod in enumerate(['00001', '00002', '00003']):
            i = by_src[sid][nod]
            with fits.open(i.path) as h:
                sci = np.array(h['PRE_BKGSUB_SCI'].data if 'PRE_BKGSUB_SCI' in h else h['SCI'].data, dtype=float)
                pix = h['SCI'].header['PIXAR_SR']; pth = h['SCI'].header.get('PTHLOSS', 'POINT')
            m = MultiSlitModel(i.path); s = m.slits[0]
            xs, ys, lam = P._slit_coordinates(s)
            f = np.ones_like(sci) / pix
            if pth == 'POINT': f *= np.asarray(s.pathloss_point) / np.asarray(s.pathloss_uniform)
            bar = P._barshadow_array(m, s); m.close()
            if bar is not None: f /= bar
            sb = sci * f
            ax = axes[r, c]
            ax.imshow(sb, origin='lower', aspect='auto', vmin=0, vmax=0.4, cmap='viridis'); ax.set_title(f'{sid} nod {int(nod)} SB (0-0.4 MJy/sr)', fontsize=8); ax.set_xticks([])
            ax = axes[r, 3 + c]
            band = (lam > 1.0) & (lam < 3.0)
            prof = np.nanmedian(np.where(band, sb, np.nan), axis=1); ycol = np.nanmedian(np.where(band, ys, np.nan), axis=1)
            ax.plot(ycol, prof, 'o-', ms=3); ax.set_ylim(0, 0.4); ax.axhline(np.nanmedian(pooled[(lam_c > 1) & (lam_c < 3)]), color='k', lw=0.5)
            for b in (-1.725, -0.575, 0.575, 1.725): ax.axvline(b, color='gray', lw=0.5, ls='--')
            ax.set_xlabel('slit-frame y (0 = source shutter)'); ax.set_title(f'{sid} nod {int(nod)} profile 1-3 um', fontsize=8)
    fig.tight_layout(); fig.savefig('nod2_excess_cutouts.png', dpi=100); print('saved')

if __name__ == '__main__':
    main()
