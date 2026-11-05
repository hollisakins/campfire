"""Where the MSA bars sit in each observation, measured from the background.

The MSA's bars can sit up to about 10 mas from where the calibration puts them, by a different amount in
each observation. For a source close to a bar that changes how much of its light gets through, so stage 3
measures the offset per observation from the background in its empty shutters
(``nirspec/bar_offset.py``) and moves the profile fit's shutters by it.

Left: the edges of the empty shutters of one observation, the data over the model, drawn against the
position across the edge (the half of each shutter facing away from the source, both edges of the
slitlet). With the bars where the calibration puts them, the lower edge reads one way and the upper edge
the other: the slitlet sits off center. Moved by the measured offset, the two edges agree; what is left
is the same on both sides (the edges are a little softer or sharper than the model) and does not move
the source. Right: the offset of every observation measured so far, against date.

    python scripts/docs/nirspec/fig_bar_offset.py --root <root> --obs <observation> [--processes 4]
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--processes', type=int, default=4)
p.add_argument('--label', default=None, help='readable name of the observation for the title')
p.add_argument('--workspace', default=None,
               help='read the observation\'s cutouts from this workspace instead of <root>/products/nirspec/<obs> '
                    '(read only; caches stay in --root)')
p.add_argument('--coverage-cache', default=None, help='overlap-counter cache (default: the root\'s)')
p.add_argument('--offsets', default=None, help='table of offsets per observation (default: data/bar_offsets.csv)')
args = p.parse_args()
root = C.setup_root(args.root)

import csv                                                            # noqa: E402
import os                                                             # noqa: E402
import warnings                                                       # noqa: E402
from datetime import datetime                                         # noqa: E402

import numpy as np                                                    # noqa: E402

from campfire_pipeline.nirspec import bar_offset as BO                # noqa: E402
from campfire_pipeline.nirspec import barshadow_calibration as B      # noqa: E402
from campfire_pipeline.nirspec import optics as O                     # noqa: E402

MAS = O.SLIT * 1000.0                                                 # mas per slit unit


def main():
    plt = C.style()
    warnings.filterwarnings('ignore', category=RuntimeWarning)
    ws = args.workspace or C.workspace(root, args.obs)
    cans = B.discover_workspace(ws, 'PRISM')
    s = BO.samples(cans, ws, processes=args.processes, cache_dir=args.coverage_cache)
    offset, lo, hi = BO.fit(s)
    ok = np.isfinite(s['r']) & np.all(np.isfinite(s['T']), axis=0)
    i0 = int(np.argmin(np.abs(BO.SHIFTS)))
    T_best = np.array([np.interp(offset, BO.SHIFTS, s['T'][:, j]) for j in np.flatnonzero(ok)])
    u = s['u'][ok]                                                    # pitches from the shutter center, signed
    edges = np.arange(-0.75, 0.7501, 0.025)
    mid = 0.5 * (edges[1:] + edges[:-1])

    def binned(v):
        idx = np.digitize(u, edges) - 1
        out = np.full(mid.size, np.nan)
        for k in range(mid.size):
            m = idx == k
            if m.sum() > 200:
                out[k] = np.median(v[m])
        return out

    before = binned(s['r'][ok] - s['T'][i0, ok])                     # both relative to the shutter's center level
    after = binned(s['r'][ok] - T_best)

    fig, (a, b) = plt.subplots(1, 2, figsize=(12, 4.3), constrained_layout=True,
                               gridspec_kw=dict(width_ratios=[1.0, 1.25]))
    for side in (-1, 1):
        m = np.sign(mid) == side
        a.plot(mid[m], 100 * before[m], color=C.PALETTE[5], lw=2.0, marker='o', ms=3.5,
               label='bars where the calibration puts them' if side < 0 else None)
        a.plot(mid[m], 100 * after[m], color=C.PALETTE[0], lw=2.0, marker='o', ms=3.5,
               label=f'bars moved by the measured offset ({offset * MAS:+.1f} mas)' if side < 0 else None)
    a.axhline(0, color=C.MUTED, lw=0.8)
    a.axvspan(-0.2, 0.2, color='#f0f0f0', lw=0, zorder=0)
    a.text(0, 0.5, 'shutter\ncenters', transform=a.get_xaxis_transform(), ha='center', va='center', fontsize=8.5,
           color=C.MUTED)
    a.text(-0.47, 0.93, 'lower edge', transform=a.get_xaxis_transform(), ha='center', fontsize=9.5)
    a.text(0.47, 0.93, 'upper edge', transform=a.get_xaxis_transform(), ha='center', fontsize=9.5)
    a.set_xlim(-0.77, 0.77)
    lim = 1.15 * np.nanmax(np.abs(100 * np.r_[before, after]))
    a.set_ylim(-lim, lim)
    a.set_xlabel('position across the edge (shutter widths from the shutter center)')
    a.set_ylabel('background, data - model\n(% of the shutter center)')
    a.set_title('Edges of the empty shutters')
    a.text(0.5, 0.06, (args.label or args.obs).replace(' (', '\n('), transform=a.transAxes, ha='center',
           fontsize=9, color=C.MUTED)
    a.legend(loc='upper center', bbox_to_anchor=(0.5, 0.88), fontsize=8.5)

    path = args.offsets or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'bar_offsets.csv')
    rows = list(csv.DictReader(line for line in open(path) if not line.startswith('#')))
    for kind, color, marker in (('calibration', C.PALETTE[1], 'o'), ('survey', C.PALETTE[0], 's')):
        sel = [r for r in rows if r['kind'] == kind]
        b.scatter([datetime.fromisoformat(r['date']) for r in sel], [float(r['offset']) * MAS for r in sel],
                  s=46, color=color, marker=marker, edgecolor='white', lw=0.8, zorder=3,
                  label=f'{kind} observations')
    b.axhline(0, color=C.MUTED, lw=0.8)
    same_day = [r for r in rows if r['date'].startswith('2026-01-06')]
    if same_day:
        d = datetime.fromisoformat(same_day[0]['date'])
        b.annotate('four pointings\non one day', xy=(d, 3.5), xytext=(datetime(2024, 11, 1), 4.6), fontsize=8.5,
                   arrowprops=dict(arrowstyle='->', color=C.MUTED, lw=0.8), color=C.INK)
    b.set_ylabel('bar offset (mas)')
    b.set_title('Every observation measured so far (PRISM)')
    sec = b.secondary_yaxis('right', functions=(lambda v: v / 100.0, lambda v: v * 100.0))
    sec.set_ylabel('detector pixels')
    b.legend(loc='lower left', fontsize=8.5)
    fig.suptitle('The bars move between observations; each observation\'s own background measures where they are',
                 x=0.01, ha='left', fontsize=10, color=C.MUTED)
    C.save(fig, args, 'bar_offset.png')
    print(f'{args.obs}: offset {offset:+.4f} slit units ({offset * MAS:+.1f} mas), halves {lo:+.4f} / {hi:+.4f}')


if __name__ == '__main__':
    main()
