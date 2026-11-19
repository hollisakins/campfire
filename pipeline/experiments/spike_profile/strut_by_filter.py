"""Strut arms against segment arms, filter by filter.

For every star-filter pair (pipeline-processed frames) the on-axis brightness
of the two strut arms divided by that of the six segment arms at the same
distance, and both against the adopted profile, on wavelength-scaled axes.
Left: segment arms. Middle: strut arms. Right: strut / segment. Colour = filter.
"""
import os, glob, numpy as np
from astropy.table import Table
from astropy.io import fits
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from campfire_pipeline.nircam import spike_mask as sm
import gaia_sed as gs
D = os.path.dirname(os.path.abspath(__file__))
order = ['F070W', 'F090W', 'F115W', 'F150W', 'F182M', 'F200W', 'F210M', 'F277W', 'F356W', 'F444W', 'F480M']
cmap = plt.get_cmap('turbo')
prof = fits.open(D + '/spike_profile_measured_pipe.fits')
nu = prof['HEX'].data.shape[0]
h = prof['HEX'].header
uc = np.exp(np.log(h['U0']) + (np.arange(nu) + 0.5) * (np.log(h['U1']) - np.log(h['U0'])) / nu)
fig, ax = plt.subplots(1, 3, figsize=(24, 7.5))
rows = []
for f in sorted(glob.glob(D + '/pipe/arms_*_F*.npz')):
    star, filt = os.path.basename(f)[5:-4].rsplit('_', 1)
    if filt not in order or star == 'p5398_c': continue
    z = np.load(f)
    if 'strut_data' not in z or 'hex_data' not in z: continue
    lam = sm.PIVOT_UM[filt]
    flux = 3631e-6 * 10 ** (-0.4 * gs.predict_ab(Table.read(f'{D}/gaia_{star}.ecsv')[:1], lam, 'nir')[0])
    pc, rc = z['pc'], z['rc']
    core = np.abs(pc) < 0.25
    noise = z['sigma'] / np.sqrt(30) * lam ** 2 / flux
    hx = np.nanmean(z['hex_data'][:, core], axis=1) * lam ** 2 / flux
    st = np.nanmean(z['strut_data'][:, core], axis=1) * lam ** 2 / flux
    c = cmap(order.index(filt) / (len(order) - 1))
    ok_h, ok_s = hx > 3 * noise, st > 3 * noise
    lab = filt if star in ('macs0647', 'cos_101', 'p5398_a') else None
    ax[0].plot((rc / lam)[ok_h], hx[ok_h], '.-', color=c, lw=0.8, ms=3, label=lab)
    ax[1].plot((rc / lam)[ok_s], st[ok_s], '.-', color=c, lw=0.8, ms=3)
    both = ok_h & ok_s
    ax[2].plot((rc / lam)[both], (st / hx)[both], '.-', color=c, lw=0.8, ms=3)
    for u0, u1 in ((4, 10), (10, 25)):
        w = both & (rc / lam > u0) & (rc / lam < u1)
        if w.sum() >= 2:
            rows.append((filt, star, u0, u1, float(np.median((st / hx)[w]))))
ax[0].plot(uc, prof['HEX'].data[:, 0], 'k', lw=2, label='adopted profile')
ax[1].plot(uc, np.maximum(prof['STRUT'].data[:, 0], 1), 'k', lw=2)
ax[2].plot(uc, prof['STRUT'].data[:, 0] / prof['HEX'].data[:, 0], 'k', lw=2)
for a, t in zip(ax, ('six segment arms, on the axis', 'two strut arms, on the axis', 'strut / segment')):
    a.set_xscale('log'); a.set_yscale('log'); a.set_title(t); a.set_xlabel('distance [arcsec/um]')
ax[0].set_ylim(1e3, 1e9); ax[1].set_ylim(1e3, 1e9); ax[2].set_ylim(1e-3, 3)
hh, ll = ax[0].get_legend_handles_labels(); u = dict(zip(ll, hh)); ax[0].legend(u.values(), u.keys(), fontsize=8)
plt.tight_layout(); out = D + '/strut_by_filter.png'; plt.savefig(out, dpi=60); print(out)
t = Table(rows=rows, names=('filter', 'star', 'u0', 'u1', 'ratio'))
for (u0, u1) in ((4, 10), (10, 25)):
    print(f'strut / segment at {u0}-{u1} arcsec/um, median over stars:')
    for f in order:
        w = (t['filter'] == f) & (t['u0'] == u0)
        if w.sum(): print(f'   {f}: {np.median(t["ratio"][w]):.2f}  ({w.sum()} stars)')
