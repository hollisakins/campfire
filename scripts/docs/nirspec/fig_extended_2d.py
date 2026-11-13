"""Reading an extended source's 2-D spectra: the nod differences with stage 3's model (``--part nod``), or the
master-background 2-D divided by its transmission (``--part mbkg``).

nod: the nod-subtracted 2-D spectrum (``SCI``), stage 3's model PROF2D x (fnu_prof - fnu_contam) + CONTAM2D and
the residual, all on one color scale symmetric about zero, and the profile along the slit summed over wavelength.
mbkg: the master-background 2-D spectrum (``SCI_MBKG``), the share of the source's light the MSA passes along the
slit (``TRANS2D_MBKG``) and their ratio, with the profiles along the slit of ``SCI_MBKG`` and of the ratio, each
scaled to its peak.

    python scripts/docs/nirspec/fig_extended_2d.py --root <root> --obs zenith_p15 --source 547856 --part nod
    python scripts/docs/nirspec/fig_extended_2d.py --root <root> --obs zenith_p15 --source 547856 --part mbkg
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='prism_clear', help='grating tag of the product names')
p.add_argument('--source', required=True)
p.add_argument('--part', choices=('nod', 'mbkg'), required=True)
p.add_argument('--tmin', type=float, default=0.1, help='leave out pixels where TRANS2D_MBKG is below this')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.colors import AsinhNorm                               # noqa: E402

with fits.open(C.spec_file(C.workspace(root, args.obs), args.obs, args.grating, args.source)) as h:
    g = {k: np.asarray(h[k].data, float) for k in ('SCI', 'ERR', 'WAVELENGTH', 'PROF2D', 'SCI_MBKG', 'TRANS2D_MBKG')}
    cont = np.nan_to_num(np.asarray(h['CONTAM2D'].data, float)) if 'CONTAM2D' in h else 0.0
    t = h['SPEC1D'].data
    own = np.asarray(t['fnu_prof'], float) - np.nan_to_num(np.asarray(t['fnu_contam'], float))
ok = np.isfinite(g['SCI']) & np.isfinite(g['ERR']) & (g['ERR'] > 0) & np.isfinite(g['PROF2D'])
sci = np.where(ok, g['SCI'], np.nan)
model = np.where(ok, g['PROF2D'] * np.nan_to_num(own)[None, :] + cont, np.nan)
mb = g['SCI_MBKG']
tr = g['TRANS2D_MBKG']
div = np.where(np.isfinite(tr) & (tr >= args.tmin), mb / tr, np.nan)


def norm_of(img):
    v = float(np.nanpercentile(np.abs(img), 99.7))
    return AsinhNorm(linear_width=0.05 * v, vmin=-v, vmax=v)


if args.part == 'nod':
    n = norm_of(sci)
    panels = [(sci, 'nod differences (SCI)', 'RdBu_r', n), (model, 'model', 'RdBu_r', n),
              (sci - model, 'residual (same scale)', 'RdBu_r', n)]
else:
    panels = [(mb, 'master-background 2-D (SCI_MBKG)', 'RdBu_r', norm_of(mb)),
              (tr, 'TRANS2D_MBKG (0 dark, 1 yellow)', 'viridis', None),
              (div, 'SCI_MBKG / TRANS2D_MBKG', 'RdBu_r', norm_of(div))]

plt = C.style()
fig, axes = plt.subplots(1, 4, figsize=(15, 2.9), constrained_layout=True,
                         gridspec_kw=dict(width_ratios=[4, 4, 4, 1.2]))
lam = np.nanmedian(g['WAVELENGTH'], axis=0)
good = np.isfinite(lam)
cols = np.arange(lam.size)
ticks = [x for x in (1, 2, 3, 4, 5) if lam[good].min() <= x <= lam[good].max()]
rows = np.arange(sci.shape[0])
for ax, (img, title, cmap, norm) in zip(axes, panels):
    ax.imshow(img, aspect='auto', cmap=cmap, **(dict(norm=norm) if norm is not None else dict(vmin=0, vmax=1)))
    ax.set_title(title, fontsize=10.5)
    ax.set_xticks(np.interp(ticks, lam[good], cols[good]))
    ax.set_xticklabels([f'{x}' for x in ticks])
    ax.set_xlabel('wavelength (µm)')
    ax.set_yticks([])
ax = axes[3]
if args.part == 'nod':
    C.profile_step(ax, np.nansum(sci, axis=1), rows, '#555555', vertical=True, label='data')
    ax.plot(np.nansum(model, axis=1), rows, color=C.PALETTE[5], lw=1.5, label='model')
    ax.axvline(0, color=C.MUTED, lw=0.6)
else:
    pm, pdv = np.nansum(mb, axis=1), np.nansum(div, axis=1)
    C.profile_step(ax, pm / np.nanmax(pm), rows, '#555555', vertical=True, label='SCI_MBKG')
    ax.plot(pdv / np.nanmax(pdv), rows, color=C.PALETTE[0], lw=1.5, label='divided')
ax.set_title('spatial profile', fontsize=10.5)
ax.legend(loc='lower right', fontsize=7.5)
ax.set_ylim(rows[0] - 0.5, rows[-1] + 0.5)
ax.set_yticks([])
ax.set_xticks([])
fig.suptitle(f'{args.obs} {args.source} {args.grating.split("_")[0].upper()}: combined 2-D spectrum (rows = '
             'position along the slitlet)', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, f'extended_2d_{args.part}.png')
