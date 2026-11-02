"""Why overlapping traces matter: how slitlets share the detectors in a grating exposure.

Top: both detectors of one exposure, colored by how many slitlets' light lands on each
pixel (from the exposure's trace maps), with a band of rows boxed. Below, that band across
the full width of the detector: the count rate, then each slitlet's footprint shaded in its
own color and labeled at its blue end. On the gratings a spectrum is long, so slitlets at
different positions in the MSA land on the same detector rows, shifted along the
dispersion: one source's red end lies on another's blue end. Zeroth-order images (compact
undispersed images of bright sources) are marked.

    python scripts/docs/nirspec/fig_overlap.py --root <root> --obs ember_cosmos_p1 \
        --exposure jw07076001001_05101_00001_nrs1 [--rows Y0 NY]

``--exposure`` names the NRS1 file; its NRS2 partner is shown beside it, and the band is
taken from NRS1.
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--exposure', required=True, help="the NRS1 exposure, e.g. 'jw07076001001_05101_00001_nrs1'")
p.add_argument('--rows', type=int, nargs=2, metavar=('Y0', 'NY'), default=None,
               help='band of NRS1 detector rows (0-based first row, number of rows); default: chosen automatically')
p.add_argument('--strip', type=int, default=36, help='rows in the automatic band')
p.add_argument('--zeroth', type=int, default=8,
               help='zeroth-order images looked for in the band; one is labeled (0: none)')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from matplotlib.colors import BoundaryNorm, ListedColormap            # noqa: E402
from matplotlib.patches import ConnectionPatch, Rectangle             # noqa: E402
from scipy.ndimage import median_filter                               # noqa: E402

plt = C.style()
_, obs = C.observation(args.obs)
ws = C.workspace(root, args.obs)
stuck = C.stuck_keys(obs)
exp1, exp2 = args.exposure, args.exposure.replace('_nrs1', '_nrs2')
rate = np.asarray(fits.getdata(C.rate_file(ws, exp1), 'SCI'), float)
tm1 = C.trace_map(ws, exp1, stuck)
tm2 = C.trace_map(ws, exp2, stuck)
count1, count2 = tm1.count.astype(float), tm2.count.astype(float)
NY, NX = rate.shape
iy, ix, idx, flg = tm1.traces_in(0, 0, NX, NY)

if args.rows:
    y0, ny = args.rows
else:
    # a band where most lit pixels carry two slitlets: overlap that is common, not extreme
    two = np.convolve((count1 == 2).mean(axis=1), np.ones(args.strip), 'valid')
    y0, ny = int(np.argmax(two)), args.strip
sl = (slice(y0, y0 + ny), slice(0, NX))
ext = [-0.5, NX - 0.5, y0 - 0.5, y0 + ny - 0.5]

# axes placed by hand so the square detector panels fill their space (figure fractions)
FW, FH = 12.0, 10.6
fig = plt.figure(figsize=(FW, FH))
L, R, GAP = 0.075, 0.885, 0.025
w = (R - L - GAP) / 2                                  # width of one detector panel
h_top = w * FW / FH                                    # square
y_top = 0.955 - h_top
h_band = 1.55 / FH
y_r = y_top - 0.075 - h_band                           # room for the zoom lines
y_f = y_r - 0.018 - h_band
cmap = ListedColormap(['#ffffff', '#cfe3f1', '#6baed6', '#2171b5', '#08306b'])
cnorm = BoundaryNorm(np.arange(-0.5, 5), 5)
top = []
for k, (cnt, det) in enumerate([(count1, 'NRS1'), (count2, 'NRS2')]):
    ax = fig.add_axes([L + k * (w + GAP), y_top, w, h_top])
    im = ax.imshow(np.minimum(cnt, 4), cmap=cmap, norm=cnorm)
    C.label_panel(ax, det)
    ax.set_xticklabels([])
    if k == 0:
        ax.set_ylabel('detector row')
        ax.add_patch(Rectangle((-0.5, y0 - 0.5), NX, ny, fc='none', ec=C.PALETTE[5], lw=1.8))
        for xa in (-0.5, NX - 0.5):                 # guides from the box down to the panel's bottom edge
            ax.plot([xa, xa], [-0.5, y0 - 0.5], color=C.PALETTE[5], lw=1.0, ls=':', clip_on=False)
    else:
        ax.set_yticklabels([])
    top.append(ax)
cax = fig.add_axes([R + 0.012, y_top, 0.013, h_top])
cb = fig.colorbar(im, cax=cax, ticks=range(5))
cb.ax.set_yticklabels(['0', '1', '2', '3', '4+'])
cb.set_label('# of contributing slitlets')
cb.ax.minorticks_off()

ax_r = fig.add_axes([L, y_r, R - L, h_band])
ax_r.imshow(rate[sl], extent=ext, cmap='Greys', norm=C.asinh_norm(rate[sl]), aspect='auto')
ax_r.set_ylabel('row')
ax_f = fig.add_axes([L, y_f, R - L, h_band], sharex=ax_r, sharey=ax_r)
plt.setp(ax_r.get_xticklabels(), visible=False)
ax_f.imshow(rate[sl], extent=ext, cmap='Greys', norm=C.asinh_norm(rate[sl]), aspect='auto', alpha=0.35)
for xa in (-0.5, NX - 0.5):                       # NRS1's bottom edge under the box to the band below
    fig.add_artist(ConnectionPatch(xyA=(xa, -0.5), coordsA=top[0].transData,
                                   xyB=(xa, y0 + ny - 0.5), coordsB=ax_r.transData,
                                   color=C.PALETTE[5], lw=1.2, alpha=0.8))
for sp in ax_r.spines.values():
    sp.set_edgecolor(C.PALETTE[5])
    sp.set_linewidth(1.6)

# zeroth-order images: compact blobs a few pixels wide over several rows, which stand out once
# each row's smooth light (the dispersed traces) is taken away
if args.zeroth > 0:
    band = rate[sl]
    hp = band - median_filter(np.nan_to_num(band), size=(1, 31))
    noise = 1.4826 * np.nanmedian(np.abs(hp - np.nanmedian(hp)))
    tall = (hp > 5 * noise).sum(axis=0)                    # rows above the threshold, per column
    peaks = []
    for c in np.argsort(-tall):
        if tall[c] < 6:
            break
        if all(abs(c - q) > 6 for q in peaks):
            peaks.append(int(c))
        if len(peaks) == args.zeroth:
            break
    if peaks:                                      # label one, inside the panel
        c = sorted(peaks)[len(peaks) // 2]
        rows_hi = np.flatnonzero(hp[:, c] > 5 * noise)
        yc = y0 + float(np.median(rows_hi))
        tx = c + 0.06 * NX
        ty = y0 + 0.25 * ny if yc > y0 + 0.5 * ny else y0 + 0.75 * ny
        ax_r.annotate('zeroth-order image', xy=(c + 4, yc), xytext=(tx, ty), color=C.PALETTE[5], fontsize=10,
                      fontweight='bold', va='center', ha='left',
                      bbox=dict(boxstyle='round,pad=0.2', fc='white', ec='none', alpha=0.85),
                      arrowprops=dict(arrowstyle='-|>', color=C.PALETTE[5], lw=1.4))

in_band = (iy >= y0) & (iy < y0 + ny)
ids, n = np.unique(idx[in_band], return_counts=True)
ids = ids[n > 200]                                                     # traces with a real footprint in the band
order = ids[np.argsort([np.median(ix[in_band & (idx == t)]) for t in ids])]
for k, t in enumerate(order):
    sel_all = idx == t                                                 # the whole trace, so its edges are smooth
    sel = in_band & sel_all
    color = C.PALETTE[k % 6]                                           # 6 strong colors; neighbors differ
    C.draw_trace(ax_f, iy[sel_all], ix[sel_all], color, fill_alpha=0.30, lw=1.3)
    rec = tm1.slits[int(t)]
    sid = int(rec['source_id']) if 'source_id' in tm1.slits.colnames else -1
    name = str(sid) if sid > 0 else ('failed-open shutter' if 'failed' in str(rec['kind']) else str(rec['kind']))
    ax_f.text(max(int(np.percentile(ix[sel], 1)), 5), float(np.median(iy[sel])), name, fontsize=8.5, color=C.INK,
              va='center', ha='left', bbox=dict(boxstyle='round,pad=0.15', fc='white', ec=color, lw=0.8, alpha=0.9))
ax_f.set_ylim(y0 - 0.5, y0 + ny - 0.5)
ax_f.set_ylabel('row')
ax_f.set_xlabel('detector column (wavelength increases to the right)')
C.label_panel(ax_r, 'count rate')
C.label_panel(ax_f, "each slitlet's footprint, labeled with its source")
fig.suptitle(f'{args.obs}  {exp1[:-5]}  ({tm1.meta.get("GRATING", "")}/{tm1.meta.get("FILTER", "")}): '
             'most pixels receive light from more than one slitlet', x=0.07, ha='left', fontsize=11, color=C.INK)
C.save(fig, args, 'overlap.png')
