"""Nod subtraction: each exposure minus the other nods, and when a nod is left out.

One row per exposure of a source (one exposure group, one detector), rectified and averaged
in blocks along the wavelength axis for display: the calibrated frame, the frame after the
other nods are subtracted (the source positive, its copies from the other nods negative),
and the brightness along the slitlet averaged over wavelength. When the pipeline left a nod
out of the background because another source sits in the shutter that nod would use, the
profile also shows the same exposure minus the mean of all the other nods, dashed: an
illustration of what leaving it out avoids, computed here from the rectified frames.

    python scripts/docs/nirspec/fig_nod_subtraction.py --root <root> --obs ember_cosmos_p1 \
        --source 634072 --exposure-group jw07076001001_03101 --detector nrs2
"""
import common as C

p = C.parser(__doc__.splitlines()[0])
p.add_argument('--source', type=int, required=True)
p.add_argument('--exposure-group', required=True, help="e.g. 'jw07076001001_03101'")
p.add_argument('--detector', default='nrs1')
p.add_argument('--bin', type=int, default=20, help='wavelength pixels averaged per displayed column (1: none)')
args = p.parse_args()
root = C.setup_root(args.root)

import glob                                                           # noqa: E402
import os                                                             # noqa: E402
import warnings                                                       # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

warnings.filterwarnings('ignore', category=RuntimeWarning)
plt = C.style()
ws = C.workspace(root, args.obs)
files = sorted(glob.glob(os.path.join(ws, f'{args.exposure_group}_?????_{args.detector}_{args.source}.fits')))
if not files:
    raise SystemExit('no cutouts found')


def binned(img, n):
    """Mean over blocks of n wavelength pixels, so faint light stands above the noise."""
    if n <= 1:
        return img
    m = img.shape[1] // n * n
    return np.nanmean(img[:, :m].reshape(img.shape[0], -1, n), axis=2)


frames = []
for f in files:
    with fits.open(f) as h:
        hd = h[0].header
        before = np.asarray(h['S2D_SCI'].data, float)
        frames.append(dict(nod=int(hd.get('PATT_NUM', 0)), before_full=before, before=binned(before, args.bin),
                           after_full=np.asarray(h['S2D_BKGSUB_SCI'].data, float),
                           why=str(hd.get('BKGWHY', '') or ''), used=str(hd.get('BKGNODS', '') or ''),
                           override=bool(hd.get('BKGOVR', False))))
    frames[-1]['after'] = binned(frames[-1]['after_full'], args.bin)
frames.sort(key=lambda d: d['nod'])
same_grid = len({fr['before_full'].shape for fr in frames}) == 1
va = np.nanpercentile(np.abs(np.concatenate([fr['after'].ravel() for fr in frames])), 99)
# profiles on one relative scale: 1 = the brightest point of any subtracted profile
pscale = max(np.nanmax(np.abs(np.nanmean(fr['after_full'], axis=1))) for fr in frames)

fig, axes = plt.subplots(len(frames), 3, figsize=(13.5, 2.0 * len(frames) + 0.9), constrained_layout=True,
                         squeeze=False, gridspec_kw=dict(width_ratios=[1, 1, 0.55]))
handles = None
for r, fr in enumerate(frames):
    others = [o for o in frames if o is not fr]
    ax = axes[r, 0]
    ax.imshow(fr['before'], cmap='Greys', norm=C.asinh_norm(fr['before']), aspect='auto')
    C.label_panel(ax, f'nod {fr["nod"]}')
    ax = axes[r, 1]
    ax.imshow(fr['after'], cmap='RdBu_r', vmin=-va, vmax=va, aspect='auto')
    used = fr['used'] if fr['override'] else ', '.join(str(o['nod']) for o in others)
    if fr['override']:
        left = ', '.join(str(o['nod']) for o in others if str(o['nod']) not in fr['used'].split(','))
        who = fr['why'].split(' at ')[0]
        C.label_panel(ax, f'minus nod {used}\n(nod {left} left out: {who} in its shutter)')
    else:
        C.label_panel(ax, f'minus the mean of nods {used}')
    ax = axes[r, 2]
    rows = np.arange(fr['after_full'].shape[0])
    ax.axvline(0, color='#999999', lw=0.8)
    C.profile_step(ax, np.nanmean(fr['after_full'], axis=1) / pscale, rows, C.PALETTE[0], vertical=True,
                   label='as subtracted')
    if fr['override'] and same_grid:
        alt = fr['before_full'] - np.nanmean([o['before_full'] for o in others], axis=0)
        C.profile_step(ax, np.nanmean(alt, axis=1) / pscale, rows, C.PALETTE[5], vertical=True, fill_alpha=0,
                       ls='--', label='if no nod were left out')
        handles = ax.get_legend_handles_labels()
    ax.set_ylim(rows[0] - 0.5, rows[-1] + 0.5)
    for a in axes[r, :2]:
        a.set_yticks([])
    axes[r, 2].set_yticklabels([])
    if r < len(frames) - 1:
        for a in axes[r]:
            a.set_xticklabels([])
axes[0, 0].set_title('Calibrated exposure')
axes[0, 1].set_title('After nod subtraction')
axes[0, 2].set_title('Along the slitlet')
xl = f'wavelength (blocks of {args.bin} pixels)' if args.bin > 1 else 'wavelength pixel'
axes[-1, 0].set_xlabel(xl)
axes[-1, 1].set_xlabel(xl)
axes[-1, 2].set_xlabel('mean over wavelength (relative)')
for a in axes[:, 2]:
    a.set_xlim(-1.15, 1.15)
if handles:
    fig.legend(*handles, loc='outside lower right', ncol=2, fontsize=9.5)
fig.suptitle(f'{args.obs}  source {args.source}  {args.exposure_group} {args.detector}  (rectified; rows = position '
             'along the slitlet; wavelength increases to the right)', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'nod_subtraction.png')
