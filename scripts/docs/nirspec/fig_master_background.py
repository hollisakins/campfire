"""The master background of one exposure: measured from empty shutters, then modeled on
the detector through every shutter's geometry.

Top row: a detector region as count rate, the master background there, and the two
subtracted (what is left is the light of the sources). Bottom left: the background
spectrum measured from the empty shutters, with the zodiacal-light prediction for the
pointing and date. Bottom right: each empty shutter's brightness relative to that spectrum;
shutters that disagree (a faint source, a stuck or partly open shutter) are left out.

    python scripts/docs/nirspec/fig_master_background.py --root <root> --obs ember_cosmos_p1 \
        --exposure jw07076001001_03101_00001_nrs1 [--region X0 Y0 NX NY]
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--exposure', required=True)
p.add_argument('--region', type=int, nargs=4, metavar=('X0', 'Y0', 'NX', 'NY'), default=None)
p.add_argument('--no-annotate', action='store_true',
               help='leave out the outlines of the failed-open shutters and of the brightest source')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

from campfire_pipeline.nirspec import trace_overlap as TO             # noqa: E402

plt = C.style()
_, obs = C.observation(args.obs)
ws = C.workspace(root, args.obs)
with fits.open(C.mbkg_file(ws, args.exposure)) as h:
    mb2d = np.asarray(h['MBKG2D'].data, float)
    t1 = h['MBKG1D'].data
    sh = h['SHUTTERS'].data
rate, rhdr = fits.getdata(C.rate_file(ws, args.exposure), 'SCI', header=False), fits.getheader(C.rate_file(ws, args.exposure))
rate = np.asarray(rate, float)
grating = f"{rhdr.get('GRATING', '')}/{rhdr.get('FILTER', '')}"

if args.region:
    x0, y0, nx, ny = args.region
else:                                                    # the region of this size the master background covers best
    nx, ny = 500, 220
    from scipy.ndimage import uniform_filter
    cover = uniform_filter((np.isfinite(mb2d) & (mb2d > 0)).astype(float), size=(ny, nx), mode='constant')
    yc, xc = np.unravel_index(np.argmax(cover), cover.shape)
    x0, y0 = int(np.clip(xc - nx // 2, 0, rate.shape[1] - nx)), int(np.clip(yc - ny // 2, 0, rate.shape[0] - ny))
sl = (slice(y0, y0 + ny), slice(x0, x0 + nx))
ext = [x0 - 0.5, x0 + nx - 0.5, y0 - 0.5, y0 + ny - 0.5]
d, m = rate[sl], np.where(np.isfinite(mb2d[sl]), mb2d[sl], np.nan)

# what to outline: the failed-open shutters' traces in the region, and the brightest
# source's own shutter (where the data minus the master background holds most light)
fo_traces, src_trace = [], None
if not args.no_annotate:
    tm = C.trace_map(ws, args.exposure, C.stuck_keys(obs))
    iy, ix, idx, flg = tm.traces_in(0, 0, rate.shape[1], rate.shape[0])
    inreg = (iy >= y0) & (iy < y0 + ny) & (ix >= x0) & (ix < x0 + nx)
    kind = np.asarray(tm.slits['kind']).astype(str)
    resid_full = rate - np.nan_to_num(mb2d)
    best = -np.inf
    def well_inside(sel):                          # the trace's rows here sit away from the region's edges
        r = np.median(iy[sel & inreg]) if (sel & inreg).any() else -1
        return y0 + 0.12 * ny < r < y0 + 0.88 * ny
    for t in np.unique(idx[inreg]):
        sel = idx == t
        if 'failed' in kind[t]:
            if (sel & inreg).sum() > 500 and well_inside(sel):
                fo_traces.append((iy[sel], ix[sel], str(tm.slits['name'][t]).strip()))
            continue
        src = sel & TO.TraceMap.source_shutter(flg) & inreg
        if kind[t] == 'slitlet' and src.sum() > 300 and well_inside(src):
            flux = np.nansum(resid_full[iy[src], ix[src]])
            if flux > best:
                best, src_trace = flux, (iy[sel & TO.TraceMap.source_shutter(flg)],
                                         ix[sel & TO.TraceMap.source_shutter(flg)], int(tm.slits['source_id'][t]))

fig = plt.figure(figsize=(12, 8.2), constrained_layout=True)
gs = fig.add_gridspec(2, 3, height_ratios=[1, 0.95])
norm = C.asinh_norm(d)
for k, (img, title) in enumerate([(d, 'Data (count rate)'), (m, 'Master background'),
                                  (d - np.nan_to_num(m), 'Data minus master background')]):
    ax = fig.add_subplot(gs[0, k])
    if k < 2:
        ax.imshow(img, extent=ext, cmap='Greys', norm=norm, aspect='auto')
    else:
        lim = np.nanpercentile(np.abs(img), 99)
        ax.imshow(img, extent=ext, cmap='RdBu_r', vmin=-lim, vmax=lim, aspect='auto')
    for fy, fx, name in fo_traces:                 # failed-open shutters, on every panel
        e = C.draw_trace(ax, fy, fx, C.PALETTE[1], lw=1.4)
        if e is not None and k == 0:
            xs = np.clip(e[0], x0, x0 + nx - 1)
            j = int(np.argmin(np.abs(xs - (x0 + 0.03 * nx))))
            ax.text(x0 + 0.03 * nx, e[2][j] + 2, 'failed-open shutter', color=C.PALETTE[1], fontsize=9.5,
                    fontweight='bold', va='bottom', ha='left',
                    bbox=dict(boxstyle='round,pad=0.15', fc='white', ec='none', alpha=0.8))
    if src_trace is not None and k == 2:           # one real source, on the residual
        e = C.draw_trace(ax, src_trace[0], src_trace[1], C.PALETTE[2], lw=1.3)
        if e is not None:
            ax.text(x0 + 0.03 * nx, e[2][len(e[2]) // 10] + 2, f'source {src_trace[2]}', color=C.PALETTE[2],
                    fontsize=9.5, fontweight='bold', va='bottom', ha='left',
                    bbox=dict(boxstyle='round,pad=0.15', fc='white', ec='none', alpha=0.8))
    ax.set_xlim(ext[0], ext[1])
    ax.set_ylim(ext[2], ext[3])
    ax.set_title(title)
    ax.set_xlabel('detector column')
    if k == 0:
        ax.set_ylabel('detector row')
    else:
        ax.set_yticklabels([])

ax = fig.add_subplot(gs[1, :2])
w = np.asarray(t1['wavelength'], float)
sb, se = np.asarray(t1['surf_bright'], float), np.asarray(t1['surf_bright_err'], float)
ok = np.isfinite(sb)
ax.fill_between(w[ok], (sb - se)[ok], (sb + se)[ok], color=C.PALETTE[0], alpha=0.25, lw=0)
ax.plot(w[ok], sb[ok], color=C.PALETTE[0], label='measured from the empty shutters')
if 'model_surf_bright' in t1.columns.names:
    mod = np.asarray(t1['model_surf_bright'], float)
    if np.any(np.isfinite(mod)):
        ax.plot(w, mod, color=C.PALETTE[1], ls='--', label='zodiacal light predicted for this pointing and date')
ax.set_xlabel('wavelength (µm)')
ax.set_ylabel('background (MJy/sr)')
ax.set_title('The background spectrum of this exposure')
ax.legend(loc='upper left')

ax = fig.add_subplot(gs[1, 2])
scale = np.asarray(sh['scale'], float)
clean = np.asarray(sh['clean'], bool)
bins = np.linspace(0.8, 1.2, 41)
ax.hist(np.clip(scale[clean], bins[0], bins[-1]), bins=bins, color=C.PALETTE[0], label=f'used ({clean.sum()})')
ax.hist(np.clip(scale[~clean], bins[0], bins[-1]), bins=bins, color=C.PALETTE[5], alpha=0.8,
        label=f'left out ({(~clean).sum()})')
ax.axvline(1.0, color=C.MUTED, lw=0.8)
ax.set_xlabel('empty shutter / background spectrum')
ax.set_ylabel('shutters')
ax.set_title('Each empty shutter against the spectrum')
ax.legend(loc='upper right')
fig.suptitle(f'{args.obs}  {args.exposure}  ({grating})', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'master_background.png')
