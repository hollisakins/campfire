"""Other sources' light on a spectrum: what the joint model predicts, and the spectrum with and without it.

In a crowded MSA configuration other slitlets' spectra fall on a source's pixels. Stage 3 solves every source of
the configuration together with its neighbors (the joint first pass), so each source's light is known where it
lands on someone else, and the product records it: ``CONTAM2D`` (other sources' predicted light on the combined
2-D grid) and ``fnu_contam`` (that light, extracted with the same weights as the source's own spectrum). The
spectrum ``fnu_prof`` is extracted with nothing subtracted; ``fnu_prof - fnu_contam`` is the source alone.

Top: the data, this source's model, the other sources' predicted light, and what is left after both, on one
color scale. Bottom: ``fnu_prof``, ``fnu_contam`` and their difference; where the source has a PRISM spectrum
too (another disperser, so the neighbors' light lands elsewhere or not at all), it is drawn as a check.

    python scripts/docs/nirspec/fig_contamination.py --root <root> --obs <obs> --grating g395m_f290lp \
        --source 635314 [--prism prism_clear]
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='g395m_f290lp')
p.add_argument('--source', required=True)
p.add_argument('--prism', default='prism_clear', help='grating tag of the comparison spectrum ("" for none)')
p.add_argument('--smooth', type=int, default=5, help='running median over this many pixels for the 1-D plot')
args = p.parse_args()
root = C.setup_root(args.root)

import os                                                             # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.colors import AsinhNorm                               # noqa: E402
from scipy.ndimage import median_filter                               # noqa: E402

ws = C.workspace(root, args.obs)
with fits.open(C.spec_file(ws, args.obs, args.grating, args.source)) as h:
    sci = np.asarray(h['SCI'].data, float)
    err = np.asarray(h['ERR'].data, float)
    wav = np.asarray(h['WAVELENGTH'].data, float)
    prof = np.asarray(h['PROF2D'].data, float)
    cont = np.nan_to_num(np.asarray(h['CONTAM2D'].data, float))
    t = h['SPEC1D'].data
    lam1 = np.asarray(t['wave'], float)
    fprof = np.asarray(t['fnu_prof'], float)
    fcont = np.asarray(t['fnu_contam'], float)
    hdr = h[0].header
own = fprof - np.nan_to_num(fcont)
ok = np.isfinite(sci) & np.isfinite(err) & (err > 0) & np.isfinite(prof)
mine = np.where(ok, prof * np.nan_to_num(own)[None, :], np.nan)
others = np.where(ok, cont, np.nan)
data = np.where(ok, sci, np.nan)
left = data - mine - others

plt = C.style()
fig = plt.figure(figsize=(14, 7.6), constrained_layout=True)
gs = fig.add_gridspec(2, 4, height_ratios=[1.0, 1.15])
v = float(np.nanpercentile(np.abs(data), 99.5))
norm = AsinhNorm(linear_width=0.05 * v, vmin=-v, vmax=v)
lam = np.nanmedian(wav, axis=0)
good = np.isfinite(lam)
cols = np.arange(lam.size)
for k, (img, title) in enumerate(((data, 'data'), (mine, 'target model'),
                                  (others, 'predicted contamination'), (left, 'residual'))):
    ax = fig.add_subplot(gs[0, k])
    ax.imshow(img, aspect='auto', cmap='RdBu_r', norm=norm)
    ax.set_title(title, fontsize=11)
    ticks = [x for x in (3, 3.5, 4, 4.5, 5) if lam[good].min() <= x <= lam[good].max()]
    ax.set_xticks(np.interp(ticks, lam[good], cols[good]))
    ax.set_xticklabels([f'{x:g}' for x in ticks])
    ax.set_yticks([])
    ax.set_xlabel('wavelength (µm)')

ax = fig.add_subplot(gs[1, :])
sm = lambda y: median_filter(np.nan_to_num(y), args.smooth, mode='nearest')   # noqa: E731
okl = np.isfinite(fprof)
ax.plot(lam1[okl], sm(fprof)[okl], color='#555555', lw=1.3, label='uncorrected (fnu_prof)')
ax.plot(lam1[okl], sm(np.nan_to_num(fcont))[okl], color=C.PALETTE[1], lw=1.3,
        label='predicted contamination (fnu_contam)')
ax.plot(lam1[okl], sm(own)[okl], color=C.PALETTE[0], lw=1.8, label='decontaminated (fnu_prof - fnu_contam)')
if args.prism:
    pp = C.spec_file(ws, args.obs, args.prism, args.source)
    if os.path.exists(pp):
        with fits.open(pp) as h:
            tp = h['SPEC1D'].data
            lp, fp = np.asarray(tp['wave'], float), np.asarray(tp['fnu_prof'], float) - np.nan_to_num(
                np.asarray(tp['fnu_contam'], float))
        sel = (lp >= np.nanmin(lam1)) & (lp <= np.nanmax(lam1)) & np.isfinite(fp)
        ax.plot(lp[sel], fp[sel], color=C.INK, ls='--', lw=1.4, label='PRISM spectrum')
ax.axhline(0, color=C.MUTED, lw=0.7)
ax.set_xlim(np.nanmin(lam1[okl]), np.nanmax(lam1[okl]))
ax.set_xlabel('wavelength (µm)')
ax.set_ylabel('flux density (µJy)')
ax.legend(loc='upper right', fontsize=9)
fig.suptitle(f'{args.obs} {args.grating.split("_")[0].upper()}, source {args.source}: contamination from '
             f'{int(hdr.get("NCONTAM", 0))} overlapping sources', x=0.01, ha='left', fontsize=10,
             color=C.MUTED)
C.save(fig, args, f'contamination_{args.grating.split("_")[0]}.png')
