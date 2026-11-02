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

def slitlet_profile(t, wl, y, crds=False):
    """Transmission across a 3-shutter slitlet (shutter centres at -1, 0, +1) at wavelength
    ``wl``: the outer edges from DATA1X1 (an open shutter with closed neighbors, its rows -1..1
    around the shutter), the two interior bars from DATA1X3 (rows -1..0 from one shutter's
    centre to the next). The CRDS reference is used by jwst only inside open shutters, so
    beyond the outer shutters' edges it is left blank."""
    out = np.full(y.shape, np.nan)
    for name, lo, hi, centre in (('DATA1X1', -2.0, -1.0, -1.0), ('DATA1X3', -1.0, 0.0, 0.0),
                                 ('DATA1X3', 0.0, 1.0, 1.0), ('DATA1X1', 1.0, 2.0, 1.0)):
        ty, lam, d = t[name]
        col = int(np.argmin(np.abs(lam - wl)))
        sel = (y >= lo) & (y < hi) if hi < 2 else (y >= lo) & (y <= hi)
        out[sel] = np.interp(y[sel] - centre, ty, d[:, col])
    if crds:
        out[np.abs(y) > 1.5] = np.nan
    return out


y = np.linspace(-2, 2, 2001)
fig, ax = plt.subplots(figsize=(7.5, 4.2), constrained_layout=True)
for k, wl in enumerate(waves):
    color = C.PALETTE[k]
    ax.step(y, slitlet_profile(tm, wl, y), where='mid', color=color, lw=1.8, label=f'{wl:g} µm, measured')
    ax.step(y, slitlet_profile(tc, wl, y, crds=True), where='mid', color=color, ls='--', lw=1.3,
            label=f'{wl:g} µm, CRDS reference')
ax.axhline(1.0, color='#bbbbbb', lw=0.8, zorder=0)
ax.set_xlim(-2, 2)
ax.set_ylim(-0.03, 1.32)
ax.set_xlabel('position across the slitlet (shutter widths from its center)')
ax.set_ylabel('transmission (1 = open shutter center)')
ax.set_title('Bar shadow for a three-shutter slitlet')
ax.legend(loc='upper center', ncol=3, fontsize=8.5, columnspacing=1.2, handlelength=2.2)
fig.suptitle(f'{args.disperser.upper()}: measured profile ({os.path.basename(measured)}) and '
             f'{os.path.basename(crds)}', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, f'bar_shadow_{args.disperser}.png')
