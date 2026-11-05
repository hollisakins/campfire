"""How the profile extraction fits each source: the combined 2-D spectrum, the model, and the residual.

For every source the profile fit models its 2-D spectrum as a galaxy (a round Gaussian whose center and width
may change slowly with wavelength) seen through the optics model of the MSA, in every exposure at once, and
solves for one spectrum. The product file holds the result on the combined 2-D grid: ``PROF2D`` (the model
per unit reported flux), the profile flux ``fnu_prof`` and ``CONTAM2D`` (other sources' predicted light), so
the model is PROF2D x fnu_prof + CONTAM2D. One row per source: the data, the model and data - model, all three
on the same color scale, symmetric about zero (so the residual shows how much of the light is left), and the
profile along the slit summed over wavelength (data as filled steps, model as a line). Rows below the slitlet's middle are negative: the nod subtraction puts each source's light, negative,
where the other nods had it.

    python scripts/docs/nirspec/fig_extraction.py --root <root> --obs <obs> --grating prism_clear \
        --source 635292 "bright, compact" --source 631396 "extended" ...
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='prism_clear', help='grating tag of the product names')
p.add_argument('--source', nargs=2, action='append', metavar=('ID', 'LABEL'), required=True)
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402


def load(sid):
    path = C.spec_file(C.workspace(root, args.obs), args.obs, args.grating, sid)
    with fits.open(path) as h:
        sci = np.asarray(h['SCI'].data, float)
        err = np.asarray(h['ERR'].data, float)
        wav = np.asarray(h['WAVELENGTH'].data, float)
        prof = np.asarray(h['PROF2D'].data, float)
        cont = np.asarray(h['CONTAM2D'].data, float) if 'CONTAM2D' in h else np.zeros_like(sci)
        fnu = np.asarray(h['SPEC1D'].data['fnu_prof'], float)
        hdr = h[0].header
    model = prof * np.nan_to_num(fnu)[None, :] + np.nan_to_num(cont)
    ok = np.isfinite(sci) & np.isfinite(err) & (err > 0) & np.isfinite(prof)
    return dict(sci=np.where(ok, sci, np.nan), model=np.where(ok, model, np.nan),
                res=np.where(ok, sci - model, np.nan), wav=wav, hdr=hdr)


plt = C.style()
n = len(args.source)
fig, axes = plt.subplots(n, 4, figsize=(15, 2.25 * n + 0.6), constrained_layout=True,
                         gridspec_kw=dict(width_ratios=[4, 4, 4, 1.2]), squeeze=False)
for row, (sid, label) in enumerate(args.source):
    d = load(sid)
    sci, model, res, wav = d['sci'], d['model'], d['res'], d['wav']
    from matplotlib.colors import AsinhNorm
    v = float(np.nanpercentile(np.abs(sci), 99.7))
    norm = AsinhNorm(linear_width=0.05 * v, vmin=-v, vmax=v)          # zero in the middle of the color scale
    lam = np.nanmedian(wav, axis=0)
    good = np.isfinite(lam)
    cols = np.arange(lam.size)
    for ax, img, title, kw in ((axes[row, 0], sci, 'data', dict(cmap='RdBu_r', norm=norm)),
                               (axes[row, 1], model, 'model', dict(cmap='RdBu_r', norm=norm)),
                               (axes[row, 2], res, 'data - model (same scale)', dict(cmap='RdBu_r', norm=norm))):
        ax.imshow(img, aspect='auto', **kw)
        if row == 0:
            ax.set_title(title)
        ticks = [t for t in (1, 2, 3, 4, 5) if lam[good].min() <= t <= lam[good].max()]
        ax.set_xticks(np.interp(ticks, lam[good], cols[good]))
        ax.set_xticklabels([f'{t}' for t in ticks] if row == n - 1 else [])
        ax.set_yticks([])
    C.label_panel(axes[row, 0], f'{sid}: {label}')
    h = d['hdr']
    shape = {'own': 'its own fitted shape', 'prism': 'its PRISM shape', 'default': 'the default shape'}.get(
        str(h.get('PFSHAPE')), str(h.get('PFSHAPE')))
    axes[row, 1].text(0.99, 0.04, f'{shape}; FWHM {2.355 * float(h.get("PFWIDTH", np.nan)) * 0.46:.2f}"',
                      transform=axes[row, 1].transAxes, ha='right', va='bottom', fontsize=8.5,
                      bbox=dict(boxstyle='round,pad=0.2', fc='white', ec='none', alpha=0.85))
    rows = np.arange(sci.shape[0])
    pd_ = np.nansum(sci, axis=1)
    pm_ = np.nansum(model, axis=1)
    ax = axes[row, 3]
    C.profile_step(ax, pd_, rows, '#555555', vertical=True, label='data')
    ax.plot(pm_, rows, color=C.PALETTE[5], lw=1.5, label='model')
    ax.axvline(0, color=C.MUTED, lw=0.6)
    ax.set_ylim(rows[0] - 0.5, rows[-1] + 0.5)
    ax.set_yticks([])
    ax.set_xticks([])
    if row == 0:
        ax.set_title('along the slit')
        ax.legend(loc='lower right', fontsize=7.5)
for ax in axes[-1, :3]:
    ax.set_xlabel('wavelength (µm)')
fig.suptitle(f'{args.obs} {args.grating.split("_")[0].upper()}: combined 2-D spectra (rows = position along the '
             'slitlet), the profile-extraction model, and the residual', x=0.01, ha='left', fontsize=10,
             color=C.MUTED)
C.save(fig, args, f'extraction_{args.grating.split("_")[0]}.png')
