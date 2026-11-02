"""Finding stuck-closed shutters: each commanded-open shutter's background is fitted with
the model of the background seen through it.

Top: one slitlet containing a stuck shutter, as count rate, each shutter outlined and
labeled with its fitted amplitude a (1 = passes the background like an open shutter, 0 =
passes nothing). Bottom: a for every commanded-open shutter of the MSA configuration (the
median over its nods and detectors). Shutters below 0.3 are called stuck; between 0.3 and
0.8 they are listed for a look by eye; at 0.8 and above they are open.

    python scripts/docs/nirspec/fig_stuck_shutters.py --root <root> --obs ember_cosmos_p1 \
        --exposure jw07076001001_05101_00001_nrs1 [--shutter q3r100c50]
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--exposure', required=True, help='exposure whose configuration the histogram shows')
p.add_argument('--example-exposure', default=None,
               help='exposure for the slitlet example (default: --exposure); a PRISM exposure shows it best')
p.add_argument('--shutter', default=None, help="physical key like 'q3r100c50'; default: a clear stuck example")
args = p.parse_args()
root = C.setup_root(args.root)

import re                                                             # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from astropy.table import Table                                       # noqa: E402

from campfire_pipeline.nirspec import trace_overlap as TO             # noqa: E402

plt = C.style()
ws = C.workspace(root, args.obs)
with fits.open(C.rate_file(ws, args.exposure)) as h:
    calls_all = Table(h['CALLS'].data)
    grating = f"{h[0].header.get('GRATING', '')}/{h[0].header.get('FILTER', '')}"
example = args.example_exposure or args.exposure
rpath = C.rate_file(ws, example)
with fits.open(rpath) as h:
    rate = np.asarray(h['SCI'].data, float)
    calls = Table(h['CALLS'].data)
    grating_ex = f"{h[0].header.get('GRATING', '')}/{h[0].header.get('FILTER', '')}"
sm = TO.ShutterMap.read(rpath)
sh = sm.shutters
keys = list(zip(np.asarray(sh['q'], int), np.asarray(sh['row'], int), np.asarray(sh['col'], int)))
a_of = {(int(r['q']), int(r['row']), int(r['col'])): float(r['a']) for r in calls}
core = (sm.flag & TO.CORE) > 0


def pixels_of(k):
    """Pixels inside shutter row k's open area (any layer)."""
    m = np.zeros(rate.shape, bool)
    for layer in range(sm.sh1.shape[0]):
        m |= (sm.sh1[layer] == k) & core[layer]
    return m


if args.shutter:
    q, r, c = map(int, re.match(r'q(\d)r(\d+)c(\d+)', args.shutter).groups())
    target = keys.index((q, r, c))
else:                       # the darkest stuck shutter here whose slitlet has an open shutter too
    best = None
    for k, key in enumerate(keys):
        a = a_of.get(key, np.nan)
        if not (a < 0.1):
            continue
        mates = [i for i in range(len(sh)) if sh['slit'][i] == sh['slit'][k] and i != k]
        if not any(a_of.get(keys[i], 0) > 0.85 for i in mates):
            continue
        n = pixels_of(k).sum()
        if n > 2000 and (best is None or n > best[1]):
            best = (k, n)
    target = best[0]
slit = int(sh['slit'][target])
members = [i for i in range(len(sh)) if int(sh['slit'][i]) == slit]
foot = np.zeros(rate.shape, bool)
masks = {}
for i in members:
    masks[i] = pixels_of(i)
    foot |= masks[i]
ys, xs = np.nonzero(foot)
y0, y1 = ys.min() - 6, ys.max() + 7
x0, x1 = xs.min(), xs.max() + 1
sl = (slice(y0, y1), slice(x0, x1))
ext = [x0 - 0.5, x1 - 0.5, y0 - 0.5, y1 - 0.5]

fig = plt.figure(figsize=(11.5, 7.6), constrained_layout=True)
gs = fig.add_gridspec(2, 1, height_ratios=[1, 1.25])
ax = fig.add_subplot(gs[0])
ax.imshow(rate[sl], extent=ext, cmap='Greys', norm=C.asinh_norm(rate[sl]), aspect='auto')
for i in members:
    key = keys[i]
    a = a_of.get(key, np.nan)
    stuck = a < 0.3
    color = C.PALETTE[5] if stuck else C.PALETTE[0]
    yy, xx = np.nonzero(masks[i])
    C.draw_trace(ax, yy, xx, color, lw=1.8)
    ax.text(x0 + 0.01 * (x1 - x0), float(np.median(yy)),
            f'q{key[0]} r{key[1]} c{key[2]}:  a = {a:.2f}  ({"stuck" if stuck else "open"})',
            fontsize=9.5, va='center', ha='left', color=C.INK,
            bbox=dict(boxstyle='round,pad=0.2', fc='white', ec=color, lw=1, alpha=0.9))
ax.set_xlim(ext[0], ext[1])
ax.set_ylim(ext[2], ext[3])
ax.set_title(f'One slitlet with a stuck shutter ({grating_ex}): no background light comes through it')
ax.set_xlabel('detector column')
ax.set_ylabel('detector row')

ax = fig.add_subplot(gs[1])
a_all = np.asarray(calls_all['a'], float)
a_all = a_all[np.isfinite(a_all)]
bins = np.linspace(-0.2, 2.0, 111)
ax.axvspan(bins[0], 0.3, color=C.PALETTE[5], alpha=0.10, lw=0)
ax.axvspan(0.3, 0.8, color=C.PALETTE[1], alpha=0.12, lw=0)
ax.hist(np.clip(a_all, bins[0], bins[-1]), bins=bins, color=C.PALETTE[0])
ax.set_yscale('log')
ax.set_ylim(0.7, None)
top = ax.get_ylim()[1]
for x, txt in ((0.05, 'called stuck'), (0.55, 'checked by eye'), (1.15, 'open')):
    ax.text(x, top * 0.6, txt, ha='center', va='top', fontsize=10.5, color=C.INK)
ax.set_xlabel('fitted amplitude a (median over nods and detectors)')
ax.set_ylabel('shutters')
ax.set_title(f'Every commanded-open shutter of one MSA configuration ({grating}, {len(a_all)} shutters; '
             'above 1: a source adds light)')
fig.suptitle(f'{args.obs}  example {example}, histogram {args.exposure[:19]}', x=0.01, ha='left', fontsize=10,
             color=C.MUTED)
C.save(fig, args, 'stuck_shutters.png')
