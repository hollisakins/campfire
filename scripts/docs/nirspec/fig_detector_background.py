"""The detector's own background and 1/f noise, removed in stage 1.

Before any spectrum is calibrated, stage 1 fits and subtracts the detector's background from each count-rate image:
the "picture frame" (a pedestal pattern toward the detector edges, from jwst's reference, scaled in each quarter of
the detector), 1/f noise along columns (and along rows for the PRISM), and optionally a smooth 2-D background. Every
spectrum's footprint is kept out of the fit, so a source's light or the sky is never fitted as detector background.
The rate file keeps both the background it subtracted (``CFBKG``) and the mask (``CFBKGMASK``).

Top: a region of the detector before, the background model, and after, on one color scale set by the empty pixels
(spectra saturate). Bottom: the median of the pixels the fit used in each detector column of the region, before and
after: the detector's offset goes to zero. jwst's flicker-noise cleaning runs first and removes most of the 1/f
noise, so the column-to-column stripes left for this step are small.

    python scripts/docs/nirspec/fig_detector_background.py --root <root> --obs <obs> \
        --exposure jw07076001001_03101_00001_nrs1 --region 0 1300 700 400
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--exposure', required=True)
p.add_argument('--region', type=int, nargs=4, default=[0, 1300, 700, 400], metavar=('X0', 'Y0', 'W', 'H'),
               help='detector region: first column, first row, width, height')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

with fits.open(C.rate_file(C.workspace(root, args.obs), args.exposure)) as h:
    after = np.asarray(h['SCI'].data, float)
    bkg = np.asarray(h['CFBKG'].data, float)
    mask = np.asarray(h['CFBKGMASK'].data) > 0
    dq = np.asarray(h['DQ'].data)
    grating = f"{h[0].header.get('GRATING')}/{h[0].header.get('FILTER')}"
x0, y0, w, hgt = args.region
sl = (slice(y0, y0 + hgt), slice(x0, x0 + w))
bad = (dq & 1) > 0
full_model = bkg.copy()
before = np.where(bad, np.nan, after + bkg)[sl]
after = np.where(bad, np.nan, after)[sl]
model = bkg[sl]
used = mask[sl]
med = np.nanmedian(before[used])
mad = 1.4826 * np.nanmedian(np.abs(before[used] - med))
vmin, vmax = med - 4 * mad, med + 4 * mad
extent = [x0 - 0.5, x0 + w - 0.5, y0 - 0.5, y0 + hgt - 0.5]

plt = C.style()
from matplotlib.patches import Rectangle                              # noqa: E402
fig = plt.figure(figsize=(15, 7.4), constrained_layout=True)
gs = fig.add_gridspec(2, 4, height_ratios=[1.0, 0.55], width_ratios=[1.15, 1, 1, 1])
ax = fig.add_subplot(gs[0, 0])
lo, hi = np.nanpercentile(full_model, [1, 99])
ax.imshow(full_model, cmap='Greys_r', vmin=lo, vmax=hi, origin='lower')
ax.add_patch(Rectangle((x0, y0), w, hgt, fill=False, ec=C.PALETTE[1], lw=1.6))
ax.axhspan(924, 1124, color=C.PALETTE[0], alpha=0.18, lw=0)
ax.text(1024, 1024, 'fixed slits (always excluded)', ha='center', va='center', fontsize=8, color=C.PALETTE[0])
ax.set_title('background model, whole detector', fontsize=11)
ax.set_xlabel('detector column')
ax.set_ylabel('detector row')
for k, (img, title) in enumerate(((before, 'count rate before'), (model, 'background model'),
                                  (after, 'count rate after'))):
    ax = fig.add_subplot(gs[0, k + 1])
    ax.imshow(img, cmap='Greys_r', vmin=vmin, vmax=vmax, extent=extent, aspect='auto', origin='lower')
    ax.set_title(title + ' (orange box)', fontsize=11)
    for s in ax.spines.values():
        s.set_edgecolor(C.PALETTE[1])
    ax.set_xlabel('detector column')
    ax.set_yticklabels([])
ax = fig.add_subplot(gs[1, :])
cols = np.arange(x0, x0 + w)
for img, color, label in ((before, '#9a9a9a', 'before'), (after, C.PALETTE[0], 'after')):
    m = np.array([np.nanmedian(img[used[:, j], j]) if used[:, j].sum() > 20 else np.nan for j in range(w)])
    ax.plot(cols, m, color=color, lw=1.0, label=label)
ax.axhline(0, color=C.MUTED, lw=0.7)
ax.set_xlim(x0, x0 + w)
ax.set_xlabel('detector column')
ax.set_ylabel('median of the fitted\npixels (count rate)')
ax.legend(loc='upper right', fontsize=9)
fig.suptitle(f'{args.obs} {grating}, {args.exposure}: the detector background removed in stage 1 (one color '
             'scale; spectra are kept out of the fit)', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'detector_background.png')
