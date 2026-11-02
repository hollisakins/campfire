"""Failed-open shutters: MSA shutters stuck open, outside any slitlet, that let background
(and sometimes an object) onto the detector.

One row per failed-open shutter: the count rate around its trace (outlined), the master
background there, and the data minus the background. Failed-open shutters pass anything
from all of an empty shutter's background to almost none, so the master background includes
each at the fraction measured from its own pixels; light above that (an object behind the
shutter) is left for the contamination model.

    python scripts/docs/nirspec/fig_failed_open.py --root <root> --obs ember_cosmos_p1 \
        --exposure jw07076001001_03101_00001_nrs1 --shutters 11 19 12 \
        [--labels "passes all of it" "about half" "almost none"]

Shutter numbers are those of the trace map's 'failed_open_<n>' records (the msaoper list).
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--exposure', required=True)
p.add_argument('--shutters', type=int, nargs='+', required=True, help="numbers n of 'failed_open_<n>'")
p.add_argument('--labels', nargs='+', default=None, help='one short description per shutter')
p.add_argument('--margin', type=int, default=14, help='detector rows shown above and below the trace')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

plt = C.style()
_, obs = C.observation(args.obs)
ws = C.workspace(root, args.obs)
with fits.open(C.rate_file(ws, args.exposure)) as h:
    rate = np.asarray(h['SCI'].data, float)
    grating = f"{h[0].header.get('GRATING', '')}/{h[0].header.get('FILTER', '')}"
mb = np.asarray(fits.getdata(C.mbkg_file(ws, args.exposure), 'MBKG2D'), float)
tm = C.trace_map(ws, args.exposure, C.stuck_keys(obs))
NY, NX = rate.shape
iy, ix, idx, _ = tm.traces_in(0, 0, NX, NY)
names = [str(n).strip() for n in tm.slits['name']]

n = len(args.shutters)
fig, axes = plt.subplots(n, 3, figsize=(13, 2.3 * n + 0.8), constrained_layout=True, squeeze=False)
for r, num in enumerate(args.shutters):
    row = names.index(f'failed_open_{num}')
    sel = idx == row
    y0, y1 = max(iy[sel].min() - args.margin, 0), min(iy[sel].max() + args.margin + 1, NY)
    x0, x1 = ix[sel].min(), ix[sel].max() + 1
    sl = (slice(y0, y1), slice(x0, x1))
    ext = [x0 - 0.5, x1 - 0.5, y0 - 0.5, y1 - 0.5]
    d, b = rate[sl], np.nan_to_num(mb[sl])
    norm = C.asinh_norm(np.concatenate([d.ravel(), b.ravel()]))
    resid = d - b
    lim = np.nanpercentile(np.abs(resid), 98)
    for k, (img, title) in enumerate([(d, 'Data (count rate)'), (b, 'Master background'),
                                      (resid, 'Data minus master background')]):
        ax = axes[r, k]
        if k < 2:
            ax.imshow(img, extent=ext, cmap='Greys', norm=norm, aspect='auto')
        else:
            ax.imshow(img, extent=ext, cmap='RdBu_r', vmin=-lim, vmax=lim, aspect='auto')
        C.draw_trace(ax, iy[sel], ix[sel], C.PALETTE[1], lw=1.3)
        ax.set_xlim(ext[0], ext[1])
        ax.set_ylim(ext[2], ext[3])
        if r == 0:
            ax.set_title(title)
        if k == 0:
            lab = f'failed-open shutter {num}'
            if args.labels and r < len(args.labels):
                lab += f': {args.labels[r]}'
            C.label_panel(ax, lab)
            ax.set_ylabel('row')
        else:
            ax.set_yticklabels([])
        if r == n - 1:
            ax.set_xlabel('detector column')
fig.suptitle(f'{args.obs}  {args.exposure}  ({grating}); orange outline: the failed-open shutter\'s trace',
             x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'failed_open.png')
