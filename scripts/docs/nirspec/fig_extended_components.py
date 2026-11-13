"""Spectra of a multi-component source model fitted to the master-background frames.

Fits the Extended sources page's example: a compact core and one component on each side of it along the
slit, round Gaussians with their own spectra, sharing one offset across the shutter. Top: each component's
spectrum (total light, flam) and their sum. Bottom: each component's share of the sum. ``--panels`` also
writes the fit's data / model / residual panels.

    python scripts/docs/nirspec/fig_extended_components.py --root <root> --obs zenith_p15 --source 547856 \
        --z 1.6418 --line Hα=0.6563 --line Paβ=1.2818 --line Paα=1.8756
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='PRISM/CLEAR')
p.add_argument('--source', type=int, required=True)
p.add_argument('--z', type=float, default=None, help='redshift for the line markers')
p.add_argument('--line', action='append', default=[], help='NAME=rest wavelength [um] to mark (repeatable)')
p.add_argument('--mbkg-term', action='store_true',
               help="fit stage 3's master-background scale term (leave off when stage 3 dropped it, MBKGSCL 0)")
p.add_argument('--panels', default=None, help="also write the fit's data / model / residual panels to this path")
args = p.parse_args()
root = C.setup_root(args.root)

import os                                                             # noqa: E402

import numpy as np                                                    # noqa: E402

from campfire_pipeline.nirspec import forward as fw                   # noqa: E402
from campfire_pipeline.nirspec.forward import plots                   # noqa: E402

files = fw.files_for(C.workspace(root, args.obs), args.source, args.grating)
exps = fw.load(files, frames=True)
model = fw.ForwardModel(exps, mbkg_sigma=0.01 if args.mbkg_term else None)

names = ['core', 'southwest', 'northeast']
across = fw.Param(0.0, bounds=(-0.15, 0.15), name='across')
comps = []
for name, along, sigma in zip(names, (0.0, -0.5, 0.5), (0.10, 0.2, 0.2)):
    dx, dy = fw.slit_frame_offset(exps, along=fw.Param(along, bounds=(-1.2, 1.2), name=f'{name}.along'),
                                  across=across)
    comps.append(fw.Gaussian(dx=dx, dy=dy, sigma=fw.Param(sigma, bounds=(0.02, 0.8), name=f'{name}.sigma')))
fit = fw.Fit(model, comps, names=names)
result = fit.run()
print(f'chi2/dof {result.chi2_red:.3f}')
for k in result.labels:
    print(f'  {k} = {result.params[k]:.4f} +- {result.errors[k]:.4f}')

UNIT = 1e-19                                                          # erg/s/cm2/A
lam = np.asarray(result.lam, float)
conv = 1e6 * 1e-23 * 2.99792458e18 / (lam * 1e4) ** 2 / UNIT          # MJy -> flam / UNIT
f = np.asarray(result.spectra, float) * conv
e = np.asarray(result.spectra_err, float) * conv
tot = f.sum(axis=0)
tot_e = np.sqrt((e ** 2).sum(axis=0))
lines = [(s.split('=')[0], float(s.split('=')[1])) for s in args.line]

plt = C.style()
fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 6.2), sharex=True, constrained_layout=True,
                             gridspec_kw={'height_ratios': [3, 1.6]})
a1.step(lam, tot, where='mid', lw=0.9, color=C.MUTED, label='sum')
ok = tot > 5 * tot_e
for i, name in enumerate(names):
    a1.fill_between(lam, f[i] - e[i], f[i] + e[i], step='mid', color=C.PALETTE[i], alpha=0.25, lw=0)
    a1.step(lam, f[i], where='mid', lw=1.0, color=C.PALETTE[i], label=name)
    with np.errstate(invalid='ignore', divide='ignore'):
        a2.step(lam[ok], (f[i] / tot)[ok], where='mid', lw=1.0, color=C.PALETTE[i])
for ax in (a1, a2):
    ax.axhline(0, color='#bbbbbb', lw=0.6, zorder=0)
    if args.z is not None:
        for _, w in lines:
            ax.axvline(w * (1 + args.z), color='#aaaaaa', ls=':', lw=0.8, zorder=0)
if args.z is not None:
    for nm, w in lines:
        a1.text(w * (1 + args.z) + 0.03, 0.96, nm, transform=a1.get_xaxis_transform(), ha='left', va='top',
                fontsize=9.5)
a1.set_ylabel(r'$f_\lambda$ [10$^{-19}$ erg s$^{-1}$ cm$^{-2}$ Å$^{-1}$]')
a1.set_ylim(min(-0.05 * np.nanmax(tot), np.nanmin(f[:, ok])), 1.08 * np.nanmax(tot))
a1.legend(loc='upper right', bbox_to_anchor=(0.86, 1.0))
a1.set_title(f'{args.obs} {args.source}: three components along the slit')
a2.set_ylabel('share of the sum')
a2.set_ylim(0, 1)
a2.set_xlabel('Observed wavelength [µm]')
a2.set_xlim(max(0.7, lam[ok].min()), lam[ok].max())
C.save(fig, args, 'extended_components.png')
if args.panels:
    plots.panels(fit, result, path=args.panels,
                 title=f'{args.obs} {args.source}, master-background frames: three components along the slit')
    print(args.panels)
