"""Do the faint wings beside an arm scale with wavelength?

Measured cross-sections of the six segment arms (pipeline-processed frames),
each divided by its value on the axis, at five distances from the star.
Top row: against the offset in arcsec. Bottom row: against offset / wavelength.
Whichever row brings the filters together is the right coordinate. Only
star-filter pairs whose axis value is above 300x the cell noise are drawn, so
the wings can be followed down to ~1% of the axis.
"""
import os, glob, numpy as np
import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
from campfire_pipeline.nircam import spike_mask as sm
D = os.path.dirname(os.path.abspath(__file__))
order = ['F070W', 'F090W', 'F115W', 'F150W', 'F200W', 'F277W', 'F356W', 'F444W', 'F480M']
cmap = plt.get_cmap('turbo')
RS = (8, 15, 30, 60, 120)
fig, ax = plt.subplots(2, len(RS), figsize=(5 * len(RS), 9.5))
for f in sorted(glob.glob(D + '/pipe/arms_*_F*.npz')):
    star, filt = os.path.basename(f)[5:-4].rsplit('_', 1)
    if filt not in order or star == 'p5398_c': continue
    z = np.load(f)
    if 'hex_data' not in z: continue
    lam = sm.PIVOT_UM[filt]; pc, rc = z['pc'], z['rc']
    noise = z['sigma'] / np.sqrt(30)
    c = cmap(order.index(filt) / (len(order) - 1))
    for k, r0 in enumerate(RS):
        j = int(np.argmin(np.abs(rc - r0)))
        d = z['hex_data'][j]
        fold = np.nanmean([d[pc > 0], d[pc < 0][::-1]], axis=0)
        p = pc[pc > 0]
        a0 = np.nanmean(fold[p < 0.25])
        if not (a0 > 300 * noise): continue
        y = np.where(fold > 2 * noise, fold / a0, np.nan)
        ls = '-' if star == 'macs0647' else '--'
        ax[0, k].plot(p, y, ls, color=c, lw=1, label=filt if (star == 'macs0647' or k == 0) else None)
        ax[1, k].plot(p / lam, y, ls, color=c, lw=1)
for k, r0 in enumerate(RS):
    ax[0, k].set_title(f'{r0}" from the star'); ax[0, k].set_xlabel('offset across the arm [arcsec]')
    ax[1, k].set_xlabel('offset / wavelength [arcsec/um]')
    for a in ax[:, k]: a.set_yscale('log'); a.set_ylim(3e-4, 1.5)
    ax[0, k].set_xlim(0, 6); ax[1, k].set_xlim(0, 4)
    h, l = ax[0, k].get_legend_handles_labels(); u = dict(zip(l, h))
    if u: ax[0, k].legend(u.values(), u.keys(), fontsize=7)
ax[0, 0].set_ylabel('brightness / axis value'); ax[1, 0].set_ylabel('brightness / axis value')
plt.tight_layout(); out = D + '/wing_scaling.png'; plt.savefig(out, dpi=60); print(out)
