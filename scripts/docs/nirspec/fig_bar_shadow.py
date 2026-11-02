"""How the MSA dims light across a shutter: the measured bar-shadow profile against the
calibration reference.

The MSA's bars and shutter edges block part of the light of a uniformly lit shutter, more
toward its edges. CAMPFIRE measures that profile from the empty shutters of many
observations (the background is the uniform light source) instead of using the CRDS
reference. Left: an open shutter whose neighbors are closed. Right: the bar between two
open shutters. Each at three wavelengths; solid lines are the measured profile, dashed lines
the CRDS reference.

    python scripts/docs/nirspec/fig_bar_shadow.py --root <root> --disperser prism
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--disperser', default='prism', choices=['prism', 'g140m', 'g235m', 'g395m'])
p.add_argument('--wavelengths', type=float, nargs='+', default=None, help='µm; default: three across the band')
args = p.parse_args()
root = C.setup_root(args.root)

import glob                                                           # noqa: E402
import os                                                             # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

plt = C.style()
measured = os.path.join(C.REPO, 'pipeline', 'campfire_pipeline', 'data',
                        f'empirical_jwst_nirspec_barshadow_{args.disperser}.fits')
crds = None
for f in sorted(glob.glob(os.path.join(root, 'cache', 'crds', 'references', 'jwst', 'nirspec',
                                       'jwst_nirspec_barshadow_*.fits'))):
    if str(fits.getheader(f).get('GRATING', '')).lower() == args.disperser:
        crds = f                                                       # the latest one in the cache
if crds is None:
    raise SystemExit(f'no CRDS bar-shadow reference for {args.disperser} in {root}/cache/crds')


def table(path):
    with fits.open(path) as h:
        out = {}
        for name in ('DATA1X1', 'DATA1X3'):
            hd = h[name].header
            d = np.asarray(h[name].data, float)
            lam = (hd['CRVAL1'] + hd['CDELT1'] * (np.arange(d.shape[1]) + 1 - hd['CRPIX1'])) * 1e6
            y = hd['CRVAL2'] + hd['CDELT2'] * (np.arange(d.shape[0]) + 1 - hd['CRPIX2'])
            out[name] = (y, lam, d)
        return out


tm, tc = table(measured), table(crds)
lam_all = tm['DATA1X1'][1]
waves = args.wavelengths or list(np.round(np.percentile(lam_all, [15, 50, 85]), 1))

fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.6), sharey=True, constrained_layout=True)
for ax, name, title, xlab in [
        (axes[0], 'DATA1X1', 'An open shutter between closed ones',
         'position across the shutter (shutter widths from its center)'),
        (axes[1], 'DATA1X3', 'The bar between two open shutters',
         'position (shutter widths; the bar is at -0.5)')]:
    for k, wl in enumerate(waves):
        color = C.PALETTE[k]
        for t, ls, lab in ((tm, '-', 'measured'), (tc, '--', 'CRDS reference')):
            y, lam, d = t[name]
            col = int(np.argmin(np.abs(lam - wl)))
            prof = d[:, col].copy()
            if t is tc and name == 'DATA1X1':
                prof[np.abs(y) > 0.5] = np.nan        # jwst reads the reference only inside the open shutter
            ax.step(y, prof, where='mid', color=color, ls=ls, lw=1.8 if ls == '-' else 1.4,
                    label=f'{wl:g} µm, {lab}')
    ax.axhline(1.0, color='#cccccc', lw=0.8, zorder=0)
    ax.set_title(title)
    ax.set_xlabel(xlab)
axes[0].set_ylabel('transmission (1 = open shutter center)')
axes[0].set_xlim(-1, 1)
axes[1].set_xlim(-1, 0)
axes[0].legend(loc='lower center', ncol=1, fontsize=9)
fig.suptitle(f'{args.disperser.upper()}: measured profile ({os.path.basename(measured)}) and '
             f'{os.path.basename(crds)}', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, f'bar_shadow_{args.disperser}.png')
