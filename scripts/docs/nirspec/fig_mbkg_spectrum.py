"""The two spectra of an extended galaxy: nod subtraction and master-background subtraction.

Nod subtraction takes each exposure's background from the other nods, where the source sat in a different shutter.
A galaxy bigger than a shutter still has light in those shutters, so the nods subtract part of the galaxy itself.
The profile fit models that (the negative images are part of its model), so the nodded spectrum still reports the
whole galaxy, but it then depends on the model of the galaxy's outskirts. Every product also carries a second
spectrum from the same exposures with the master background subtracted instead (``SPEC1D_MBKG``, ``SCI_MBKG``),
which does not; the header says when it is likely the better one (``MBKGPREF``, ``MBKGWHY``: here ``extended``, the
nods subtract more than a tenth of the galaxy's own light). Left: the 2-D spectrum after each subtraction, on one
color scale. Middle: both profiles along the slit (summed over wavelength). Right: both spectra.

    python scripts/docs/nirspec/fig_mbkg_spectrum.py --root <root> --obs <obs> --grating prism_clear --source 919692
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--grating', default='prism_clear')
p.add_argument('--source', required=True)
p.add_argument('--smooth', type=int, default=3)
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.colors import AsinhNorm                               # noqa: E402
from scipy.ndimage import median_filter                               # noqa: E402

with fits.open(C.spec_file(C.workspace(root, args.obs), args.obs, args.grating, args.source)) as h:
    nod = np.asarray(h['SCI'].data, float)
    mb = np.asarray(h['SCI_MBKG'].data, float)
    wav = np.asarray(h['WAVELENGTH'].data, float)
    t, tm = h['SPEC1D'].data, h['SPEC1D_MBKG'].data
    H = h[0].header
plt = C.style()
fig = plt.figure(figsize=(15, 4.6), constrained_layout=True)
gs = fig.add_gridspec(2, 3, width_ratios=[2.2, 0.7, 2.4])
v = float(np.nanpercentile(np.abs(mb), 99.5))
norm = AsinhNorm(linear_width=0.05 * v, vmin=-v, vmax=v)
lam = np.nanmedian(wav, axis=0)
good = np.isfinite(lam)
cols = np.arange(lam.size)
for k, (img, title) in enumerate(((nod, 'nod-subtracted'), (mb, 'master-background-subtracted'))):
    ax = fig.add_subplot(gs[k, 0])
    ax.imshow(img, aspect='auto', cmap='RdBu_r', norm=norm)
    C.label_panel(ax, title)
    ticks = [x for x in (1, 2, 3, 4, 5) if lam[good].min() <= x <= lam[good].max()]
    ax.set_xticks(np.interp(ticks, lam[good], cols[good]))
    ax.set_xticklabels([f'{x:g}' for x in ticks] if k == 1 else [])
    ax.set_yticks([])
    if k == 1:
        ax.set_xlabel('wavelength (µm)')
ax = fig.add_subplot(gs[:, 1])
rows = np.arange(nod.shape[0])
C.profile_step(ax, np.nansum(mb, axis=1), rows, C.PALETTE[0], vertical=True, label='master-background-subtracted')
C.profile_step(ax, np.nansum(nod, axis=1), rows, C.PALETTE[5], vertical=True, fill_alpha=0.15, label='nod-subtracted')
ax.axvline(0, color=C.MUTED, lw=0.6)
ax.set_ylim(rows[0] - 0.5, rows[-1] + 0.5)                      # row 0 at the bottom, as in the images
ax.set_yticks([])
ax.set_xticks([])
ax.set_title('spatial profile')
ax.legend(loc='lower right', fontsize=7.5)
ax = fig.add_subplot(gs[:, 2])
sm = lambda y: median_filter(np.nan_to_num(np.asarray(y, float)), args.smooth, mode='nearest')   # noqa: E731
ax.plot(t['wave'], sm(t['fnu']), color=C.PALETTE[5], lw=1.4, label='nod-subtracted (fnu)')
ax.plot(tm['wave'], sm(tm['fnu']), color=C.PALETTE[0], lw=1.6, label='master-background-subtracted (SPEC1D_MBKG)')
ax.axhline(0, color=C.MUTED, lw=0.7)
ax.set_xlabel('wavelength (µm)')
ax.set_ylabel('flux density (µJy)')
ax.set_title('1-D spectra')
ax.legend(loc='upper left', fontsize=8.5)
fig.suptitle(f'{args.obs} {args.grating.split("_")[0].upper()}, source {args.source} (FWHM '
             f'{2.355 * 0.46 * float(H.get("PFWIDTH", np.nan)):.2f}"): nod subtraction removes part of the '
             'galaxy (self-subtraction), which the extraction models; master-background subtraction avoids it', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'mbkg_spectrum.png')
