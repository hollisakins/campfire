import numpy as np, glob, os
from astropy.table import Table
from astropy.io import fits
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
sh = Table.read('full_ember_egs_p1_pad3/ember_egs_p1_shutters.ecsv')
ws = '/Users/hollis/campfire-data/products/nirspec/ember_egs_p1'
flux = {}
for sid in np.unique(sh['source_id']):
    f = os.path.join(ws, f'ember_egs_p1_prism_clear_{sid}_spec.fits')
    if os.path.exists(f):
        d = fits.getdata(f, 'SPEC1D'); w = d['wave']; m = (w > 1.0) & (w < 3.0) & np.isfinite(d['fnu'])
        flux[sid] = float(np.nanmedian(d['fnu'][m])) if m.sum() else np.nan   # uJy, 1-3 um
sh['fnu'] = [flux.get(s, np.nan) for s in sh['source_id']]
ok = np.isfinite(sh['fnu']) & np.isfinite(sh['scale'])
print(f'{ok.sum()} shutters with a source flux; {len(flux)} sources')
# bin by source flux: median a_s for adjacent (|k|=1) and far (|k|=2) shutters
edges = [-1, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 5, 1e9]
for ak in (1, 2):
    print(f'|k|={ak}: median a_s and rejected fraction by source median fnu (uJy, 1-3 um)')
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = ok & (np.abs(sh['k']) == ak) & (sh['fnu'] > lo) & (sh['fnu'] <= hi)
        if m.sum() > 3:
            print(f'   {lo:6.2f} < fnu <= {hi:6.2f}: n={m.sum():3d}  a_s={np.nanmedian(sh["scale"][m]):.3f}  rejected={1-np.mean(sh["clean"][m]):.2f}')
fig, ax = plt.subplots(1, 2, figsize=(11, 4))
for i, ak in enumerate((1, 2)):
    m = ok & (np.abs(sh['k']) == ak)
    c = np.where(sh['clean'][m], 'tab:green', 'tab:red')
    ax[i].scatter(sh['fnu'][m], sh['scale'][m], s=8, c=c, alpha=0.6)
    ax[i].set_xscale('log'); ax[i].axhline(1, color='k', lw=0.5); ax[i].set_ylim(0.8, 1.4)
    ax[i].set_xlabel('source median fnu 1-3 um [uJy]'); ax[i].set_ylabel('shutter scale a_s vs pooled sky')
    ax[i].set_title(f'ember_egs_p1 prism: empty shutter |k|={ak} ({"adjacent to" if ak == 1 else "two from"} the source)')
fig.tight_layout(); fig.savefig('excess_vs_flux.png', dpi=110); print('saved')
