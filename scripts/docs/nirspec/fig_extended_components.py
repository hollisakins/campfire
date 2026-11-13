"""Stage 3's one-component fit and a three-component source model, on the master-background 2-D spectrum.

Fits the Extended sources page's example to the master-background frames: a compact core and one component on
each side of it along the slit, round Gaussians with their own spectra, sharing one offset across the shutter.
The data and the three-component model are drawn on the product's 2-D grid with ``fw.product_grid``,
``fw.data_2d`` and ``fw.model_2d``.
Top rows: the master-background 2-D spectrum (``SCI_MBKG``), the model and the residual, all on one color
scale, and the profile along the slit summed over wavelength; first stage 3's one-component fit (``PROF2D_MBKG``
x (fnu_prof - fnu_contam) + ``CONTAM2D_MBKG``), then the three components. Bottom: stage 3's master-background
spectrum and the three component spectra (each a component's total light), in flam.

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
p.add_argument('--panels', default=None, help="also write the fit's per-exposure data / model / residual panels")
args = p.parse_args()
root = C.setup_root(args.root)

import os                                                             # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.colors import AsinhNorm                               # noqa: E402

from campfire_pipeline.nirspec import forward as fw                   # noqa: E402
from campfire_pipeline.nirspec.forward import plots                  # noqa: E402

ws = C.workspace(root, args.obs)
tag = args.grating.replace('/', '_').lower()
base = os.path.join(ws, f'{args.obs}_{tag}_{args.source}')

# --- the fit, as printed on the page
exps = fw.load(fw.files_for(ws, args.source, args.grating), frames=True)
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

# --- on the product's 2-D grid: the data, stage 3's one-component model, and the three components
grid = fw.product_grid(fw.files_for(ws, args.source, args.grating))
d2 = fw.data_2d(grid, frames=True)
sci, err = np.asarray(d2['data'], float), np.asarray(d2['err'], float)
model3 = np.asarray(fw.model_2d(fit, result, grid), float)
with fits.open(base + '_spec.fits') as h:
    wav = np.asarray(h['WAVELENGTH'].data, float)
    prof = np.asarray(h['PROF2D_MBKG'].data, float)
    cont = np.nan_to_num(np.asarray(h['CONTAM2D_MBKG'].data, float))
    t = h['SPEC1D_MBKG'].data
    own = np.asarray(t['fnu_prof'], float) - np.nan_to_num(np.asarray(t['fnu_contam'], float))
    w1, fl1, fe1 = (np.asarray(t[c], float) for c in ('wave', 'flam_prof', 'flam_prof_err'))
model1 = prof * np.nan_to_num(own)[None, :] + cont

ok = np.isfinite(sci) & np.isfinite(err) & (err > 0) & np.isfinite(prof)
sci = np.where(ok, sci, np.nan)
rows_2d = [('stage 3: one component', np.where(ok, model1, np.nan)),
           ('three components', np.where(ok, model3, np.nan))]
for lab, mod in rows_2d:
    r = ((sci - mod) / err)[ok]
    print(f'{lab}: sum (residual/err)^2 / n over the 2-D: {np.nansum(r ** 2) / np.isfinite(r).sum():.3f}')

# --- the figure
plt = C.style()
fig = plt.figure(figsize=(15, 9.2), constrained_layout=True)
gs = fig.add_gridspec(3, 4, width_ratios=[4, 4, 4, 1.2], height_ratios=[1, 1, 2.1])
v = float(np.nanpercentile(np.abs(sci), 99.7))
norm = AsinhNorm(linear_width=0.05 * v, vmin=-v, vmax=v)
lam = np.nanmedian(wav, axis=0)
good = np.isfinite(lam)
cols = np.arange(lam.size)
ticks = [x for x in (1, 2, 3, 4, 5) if lam[good].min() <= x <= lam[good].max()]
yrows = np.arange(sci.shape[0])
for i, (lab, mod) in enumerate(rows_2d):
    for j, (img, title) in enumerate(((sci, 'master-background 2-D'), (mod, 'model'),
                                      (sci - mod, 'residual (same scale)'))):
        ax = fig.add_subplot(gs[i, j])
        ax.imshow(img, aspect='auto', cmap='RdBu_r', norm=norm)
        if i == 0:
            ax.set_title(title)
        ax.set_xticks(np.interp(ticks, lam[good], cols[good]))
        ax.set_xticklabels([f'{x}' for x in ticks] if i == 1 else [])
        ax.set_yticks([])
        if j == 0:
            C.label_panel(ax, lab)
        if i == 1:
            ax.set_xlabel('wavelength (µm)')
    ax = fig.add_subplot(gs[i, 3])
    C.profile_step(ax, np.nansum(sci, axis=1), yrows, '#555555', vertical=True, label='data')
    ax.plot(np.nansum(mod, axis=1), yrows, color=C.PALETTE[5], lw=1.5, label='model')
    ax.set_ylim(yrows[0] - 0.5, yrows[-1] + 0.5)
    ax.set_yticks([])
    ax.set_xticks([])
    if i == 0:
        ax.set_title('spatial profile')
        ax.legend(loc='lower right', fontsize=7.5)

UNIT = 1e-19                                                          # erg/s/cm2/A
lam3 = np.asarray(result.lam, float)
conv = 1e6 * 1e-23 * 2.99792458e18 / (lam3 * 1e4) ** 2 / UNIT        # MJy -> flam / UNIT
f = np.asarray(result.spectra, float) * conv
e = np.asarray(result.spectra_err, float) * conv
ax = fig.add_subplot(gs[2, :])
ax.step(w1, fl1 / UNIT, where='mid', lw=1.0, color=C.INK, label='stage 3, one component (fnu_prof)')
for i, name in enumerate(names):
    ax.fill_between(lam3, f[i] - e[i], f[i] + e[i], step='mid', color=C.PALETTE[i], alpha=0.25, lw=0)
    ax.step(lam3, f[i], where='mid', lw=1.0, color=C.PALETTE[i], label=f'{name} (total light)')
ax.axhline(0, color='#bbbbbb', lw=0.6, zorder=0)
if args.z is not None:
    for nm, w in ((s.split('=')[0], float(s.split('=')[1])) for s in args.line):
        ax.axvline(w * (1 + args.z), color='#aaaaaa', ls=':', lw=0.8, zorder=0)
        ax.text(w * (1 + args.z) + 0.02, 0.97, nm, transform=ax.get_xaxis_transform(), ha='left', va='top',
                fontsize=9.5)
fin = np.isfinite(fl1)
ax.set_xlim(max(0.7, w1[fin].min()), w1[fin].max())
ax.set_ylim(-0.5, 1.1 * np.nanmax(np.r_[fl1[fin] / UNIT, f.ravel()]))
ax.set_xlabel('Observed wavelength [µm]')
ax.set_ylabel(r'$f_\lambda$ [10$^{-19}$ erg s$^{-1}$ cm$^{-2}$ Å$^{-1}$]')
ax.legend(loc='upper left')
fig.suptitle(f'{args.obs} {args.source}: one component (stage 3) and three components along the slit, '
             'on the master-background frames', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'extended_components.png')
if args.panels:
    plots.panels(fit, result, path=args.panels,
                 title=f'{args.obs} {args.source}, master-background frames: three components along the slit')
    print(args.panels)
