"""The detector's own background and 1/f noise, removed in stage 1.

Before any spectrum is calibrated, stage 1 fits and subtracts the detector's background from each count-rate image:
the "picture frame" (a pattern brighter toward the detector edges, from jwst's reference, scaled in each of four
bands of 512 rows), 1/f stripes along columns (and along rows for the PRISM), and optionally a smooth 2-D
background. Every spectrum's footprint is kept out of the fit, so a source's light or the astronomical background is
never fitted as detector background. The rate file keeps both the background it subtracted (``CFBKG``) and the
pixels the fit used (``CFBKGMASK``).

Top left: the background model over the whole detector, smoothed so that the large-scale pattern shows (the 1/f
stripes are averaged away). Top right: a region of the detector before (pixels kept out of the fit tinted orange),
the background model, and after, on one color scale centered on zero. Bottom: the median of the pixels the fit used
in each detector column of the region, before and after: the detector's offset goes to zero.

With ``--full``: three square panels of the whole detector, before, the background model and after, on one color
scale centered on zero, and nothing else.

    python scripts/docs/nirspec/fig_detector_background.py --root <root> --obs <obs> \
        --exposure jw07076001001_03101_00001_nrs1 --region 0 1300 700 400
    python scripts/docs/nirspec/fig_detector_background.py --root <root> --obs <obs> \
        --exposure jw07076001001_03101_00001_nrs1 --full
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--exposure', required=True)
p.add_argument('--region', type=int, nargs=4, default=[0, 1300, 700, 400], metavar=('X0', 'Y0', 'W', 'H'),
               help='detector region: first column, first row, width, height')
p.add_argument('--smooth', type=int, default=33, help='median-filter box for the whole-detector panel (pixels)')
p.add_argument('--full', action='store_true', help='three square whole-detector panels: before, model, after')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.patches import Rectangle                              # noqa: E402
from scipy.ndimage import median_filter                               # noqa: E402

with fits.open(C.rate_file(C.workspace(root, args.obs), args.exposure)) as h:
    after = np.asarray(h['SCI'].data, float)
    bkg = np.asarray(h['CFBKG'].data, float)
    mask = np.asarray(h['CFBKGMASK'].data) > 0
    dq = np.asarray(h['DQ'].data)
    grating = f"{h[0].header.get('GRATING')}/{h[0].header.get('FILTER')}"
if args.full:
    bad = (dq & 1) > 0
    imgs = ((np.where(bad, np.nan, after + bkg), 'Before'), (bkg, 'Background model'),
            (np.where(bad, np.nan, after), 'After'))
    used = mask & ~bad
    res = imgs[2][0][used]
    v = 4 * 1.4826 * np.nanmedian(np.abs(res - np.nanmedian(res)))
    plt = C.style()
    fig = plt.figure(figsize=(15.5, 5.4), constrained_layout=True)
    gs = fig.add_gridspec(1, 4, width_ratios=[1, 1, 1, 0.05])
    for k, (img, title) in enumerate(imgs):
        ax = fig.add_subplot(gs[0, k])
        im = ax.imshow(img, cmap='Greys_r', vmin=-v, vmax=v, origin='lower', interpolation='antialiased')
        ax.set_title(title, fontsize=11)
        ax.set_xlabel('detector column')
        if k == 0:
            ax.set_ylabel('detector row')
        else:
            ax.set_yticklabels([])
    cb = fig.colorbar(im, cax=fig.add_subplot(gs[0, 3]))
    cb.set_label('count rate (DN/s)')
    fig.suptitle(f'{args.obs} {grating}, {args.exposure}: Stage 1 detector background subtraction',
                 x=0.01, ha='left', fontsize=10, color=C.MUTED)
    C.save(fig, args, 'detector_background_full.png')
    raise SystemExit

x0, y0, w, hgt = args.region
sl = (slice(y0, y0 + hgt), slice(x0, x0 + w))
bad = (dq & 1) > 0
smooth_model = median_filter(bkg, args.smooth, mode='nearest')
before = np.where(bad, np.nan, after + bkg)[sl]
after = np.where(bad, np.nan, after)[sl]
model = bkg[sl]
used = mask[sl]
mad = 1.4826 * np.nanmedian(np.abs(after[used] - np.nanmedian(after[used])))
v = 4 * mad
extent = [x0 - 0.5, x0 + w - 0.5, y0 - 0.5, y0 + hgt - 0.5]

plt = C.style()
fig = plt.figure(figsize=(15.5, 7.6), constrained_layout=True)
gs = fig.add_gridspec(2, 5, height_ratios=[1.0, 0.5], width_ratios=[1.15, 1, 1, 1, 0.06])
ax = fig.add_subplot(gs[0, 0])
lo, hi = np.nanpercentile(smooth_model, [0.5, 99.5])
ax.imshow(smooth_model, cmap='Greys_r', vmin=lo, vmax=hi, origin='lower')
ax.add_patch(Rectangle((x0, y0), w, hgt, fill=False, ec=C.PALETTE[1], lw=1.8))
ax.axhspan(924, 1124, fill=False, hatch='///', ec=C.PALETTE[0], lw=0)
ax.set_title('Background model, full detector\n(smoothed; hatched: fixed-slit region)', fontsize=10.5)
ax.set_xlabel('detector column')
ax.set_ylabel('detector row')

tint = np.zeros(used.shape + (4,))
tint[~used] = (0.90, 0.62, 0.0, 0.55)
for k, (img, title) in enumerate(((before, 'Before (orange: excluded from fit)'), (model, 'Background model'),
                                  (after, 'After'))):
    ax = fig.add_subplot(gs[0, k + 1])
    im = ax.imshow(img, cmap='Greys_r', vmin=-v, vmax=v, extent=extent, aspect='auto', origin='lower')
    if k == 0:
        ax.imshow(tint, extent=extent, aspect='auto', origin='lower', interpolation='nearest')
        ax.set_ylabel('detector row')
    else:
        ax.set_yticklabels([])
    ax.set_title(title, fontsize=10.5)
    for s in ax.spines.values():
        s.set_edgecolor(C.PALETTE[1])
        s.set_linewidth(1.8)
    ax.set_xlabel('detector column')
cb = fig.colorbar(im, cax=fig.add_subplot(gs[0, 4]))
cb.set_label('count rate (DN/s)')

ax = fig.add_subplot(gs[1, :4])
cols = np.arange(x0, x0 + w)
for img, color, label in ((before, '#9a9a9a', 'before'), (after, C.PALETTE[0], 'after')):
    m = np.array([np.nanmedian(img[used[:, j], j]) if used[:, j].sum() > 20 else np.nan for j in range(w)])
    ax.plot(cols, m, color=color, lw=1.0, label=label)
ax.axhline(0, color=C.MUTED, lw=0.7)
ax.set_xlim(x0, x0 + w)
ax.set_xlabel('detector column')
ax.set_ylabel('median of fitted\npixels (DN/s)')
ax.legend(loc='center right', fontsize=9, framealpha=0.9)
fig.suptitle(f'{args.obs} {grating}, {args.exposure}: Stage 1 detector background subtraction',
             x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'detector_background.png')
