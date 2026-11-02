"""Why overlapping traces matter: how slitlets share the detector in a grating exposure.

Top: the whole detector, colored by how many slitlets' light lands on each pixel (from the
exposure's trace map). Middle: a strip of rows across the full width, as count rate.
Bottom: the same strip with each slitlet's footprint shaded in its own color and labeled at
its blue end. On the gratings a spectrum is long, so slitlets at different positions in the
MSA land on the same detector rows, shifted along the dispersion: one source's red end lies
on another's blue end.

    python scripts/docs/nirspec/fig_overlap.py --root <root> --obs ember_cosmos_p1 \
        --exposure jw07076001001_05101_00001_nrs1 [--rows Y0 NY]
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--exposure', required=True, help="e.g. 'jw07076001001_05101_00001_nrs1'")
p.add_argument('--rows', type=int, nargs=2, metavar=('Y0', 'NY'), default=None,
               help='strip of detector rows (0-based first row, number of rows); default: chosen automatically')
p.add_argument('--strip', type=int, default=36, help='rows in the automatic strip')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.colors import BoundaryNorm, ListedColormap            # noqa: E402

plt = C.style()
_, obs = C.observation(args.obs)
ws = C.workspace(root, args.obs)
rate = np.asarray(fits.getdata(C.rate_file(ws, args.exposure), 'SCI'), float)
tm = C.trace_map(ws, args.exposure, C.stuck_keys(obs))
count = tm.count.astype(float)
NY, NX = rate.shape

# one footprint per trace (slitlet or failed-open shutter)
iy, ix, idx, flg = tm.traces_in(0, 0, NX, NY)

if args.rows:
    y0, ny = args.rows
else:
    # a strip where most lit pixels carry two slitlets: overlap that is common, not extreme
    two = np.convolve((count == 2).mean(axis=1), np.ones(args.strip), 'valid')
    y0, ny = int(np.argmax(two)), args.strip
sl = (slice(y0, y0 + ny), slice(0, NX))
ext = [-0.5, NX - 0.5, y0 - 0.5, y0 + ny - 0.5]

fig = plt.figure(figsize=(11, 10.5), constrained_layout=True)
gs = fig.add_gridspec(3, 1, height_ratios=[2.2, 1, 1])

ax = fig.add_subplot(gs[0])
cmap = ListedColormap(['#ffffff', '#cfe3f1', '#6baed6', '#2171b5', '#08306b'])
im = ax.imshow(np.minimum(count, 4), cmap=cmap, norm=BoundaryNorm(np.arange(-0.5, 5), 5), aspect='auto')
ax.axhspan(y0 - 0.5, y0 + ny - 0.5, fc='none', ec=C.PALETTE[5], lw=1.8)
cb = fig.colorbar(im, ax=ax, ticks=range(5), shrink=0.85, pad=0.01)
cb.ax.set_yticklabels(['0', '1', '2', '3', '4+'])
cb.set_label('slitlets lighting the pixel')
ax.set_title('The whole detector: most pixels receive light from more than one slitlet')
ax.set_ylabel('detector row')

ax = fig.add_subplot(gs[1])
ax.imshow(rate[sl], extent=ext, cmap='Greys', norm=C.asinh_norm(rate[sl]), aspect='auto')
ax.set_title('A strip of the detector (orange box above): count rate')
ax.set_ylabel('row')

ax = fig.add_subplot(gs[2], sharex=ax, sharey=ax)
ax.imshow(rate[sl], extent=ext, cmap='Greys', norm=C.asinh_norm(rate[sl]), aspect='auto', alpha=0.35)
in_strip = (iy >= y0) & (iy < y0 + ny)
ids, n = np.unique(idx[in_strip], return_counts=True)
ids = ids[n > 200]                                                     # traces with a real footprint in the strip
order = ids[np.argsort([np.median(ix[in_strip & (idx == t)]) for t in ids])]
for k, t in enumerate(order):
    sel = in_strip & (idx == t)
    m = np.zeros((ny, NX))
    m[iy[sel] - y0, ix[sel]] = 1.0
    color = C.PALETTE[k % 6]                                           # 6 strong colors; neighbors differ
    ax.contourf(np.arange(NX), np.arange(y0, y0 + ny), m, levels=[0.5, 1.5], colors=[color], alpha=0.35)
    ax.contour(np.arange(NX), np.arange(y0, y0 + ny), m, levels=[0.5], colors=[color], linewidths=1.2)
    rec = tm.slits[int(t)]
    sid = int(rec['source_id']) if 'source_id' in tm.slits.colnames else -1
    name = str(sid) if sid > 0 else ('failed-open shutter' if 'failed' in str(rec['kind']) else str(rec['kind']))
    xa = int(np.percentile(ix[sel], 1))
    ya = float(np.median(iy[sel]))
    ax.text(max(xa, 5), ya, name, fontsize=8.5, color=C.INK, va='center', ha='left',
            bbox=dict(boxstyle='round,pad=0.15', fc='white', ec=color, lw=0.8, alpha=0.9))
ax.set_title("The same strip: each slitlet's footprint, labeled with its source")
ax.set_ylabel('row')
ax.set_xlabel('detector column (wavelength increases to the right)')
fig.suptitle(f'{args.obs}  {args.exposure}  ({tm.meta.get("GRATING", "")}/{tm.meta.get("FILTER", "")})',
             x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'overlap.png')
