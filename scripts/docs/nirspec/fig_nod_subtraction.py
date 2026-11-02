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
p.add_argument('--bin', type=int, default=1,
               help='wavelength pixels averaged per displayed column, for faint sources (1: none)')
p.add_argument('--grating-tag', default='prism_clear',
               help="the source's spec.fits grating tag, for the wavelength axis (e.g. 'g395m_f290lp')")
p.add_argument('--stretch', type=float, nargs=2, default=(2.0, 92.0), metavar=('LO', 'HI'),
               help='percentiles clipped in the calibrated frames (lower HI = harder stretch)')
p.add_argument('--per-column', action='store_true',
               help="show the calibrated frames divided by each column's median, so the background's rise "
                    "with wavelength does not take the display range")
p.add_argument('--mark', action='store_true',
               help='mark the source and the neighbors that caused a nod to be left out on the calibrated frames')
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


def source_rows(canonical, radec):
    """Row of each (ra, dec) along the slitlet in the canonical's rectified frame. The
    rectified frames do not store their coordinates, so the exposure is rectified again in
    memory with the same jwst step stage 2a uses (nothing is written); each row's sky
    position at the central column defines a line, and each source is projected onto it."""
    import logging
    for name in ('stpipe', 'jwst', 'stcal', 'CRDS'):
        logging.getLogger(name).setLevel(logging.WARNING)
    from jwst.datamodels import MultiSlitModel
    from jwst.resample import ResampleSpecStep
    cwd = os.getcwd()
    os.chdir(os.path.dirname(canonical))
    try:
        with MultiSlitModel(os.path.basename(canonical)) as m:
            s = ResampleSpecStep().call(m).slits[0]
            ny, nx = s.data.shape
            ra, dec, _ = s.meta.wcs(np.full(ny, nx // 2), np.arange(ny))
    finally:
        os.chdir(cwd)
    cosd = np.cos(np.radians(np.nanmedian(dec)))
    e, n = (ra - ra[0]) * cosd * 3600, (dec - dec[0]) * 3600
    step = np.hypot(np.diff(e), np.diff(n)).mean()
    ux, uy = (e[-1] - e[0]) / np.hypot(e[-1] - e[0], n[-1] - n[0]), (n[-1] - n[0]) / np.hypot(e[-1] - e[0], n[-1] - n[0])
    return [((r0 - ra[0]) * cosd * 3600 * ux + (d0 - dec[0]) * 3600 * uy) / step for r0, d0 in radec]


frames = []
for f in files:
    with fits.open(f) as h:
        hd = h[0].header
        before = np.asarray(h['S2D_SCI'].data, float)
        frames.append(dict(nod=int(hd.get('PATT_NUM', 0)), before_full=before, before=binned(before, args.bin),
                           path=f, metafile=str(hd.get('MSAMETFL', '')),
                           after_full=np.asarray(h['S2D_BKGSUB_SCI'].data, float),
                           why=str(hd.get('BKGWHY', '') or ''), used=str(hd.get('BKGNODS', '') or ''),
                           override=bool(hd.get('BKGOVR', False))))
    frames[-1]['after'] = binned(frames[-1]['after_full'], args.bin)
frames.sort(key=lambda d: d['nod'])

# the sources to mark: this one, and every neighbor a nod was left out for
marks = {}
if args.mark:
    import re
    from astropy.table import Table
    ids = [args.source] + sorted({int(m) for fr in frames for m in re.findall(r'neighbor (\d+)', fr['why'])})
    src = Table.read(os.path.join(ws, frames[0]['metafile']), hdu='SOURCE_INFO')
    radec = []
    for s in ids:
        row = src[np.asarray(src['source_id']) == s]
        radec.append((float(row['ra'][0]), float(row['dec'][0])) if len(row) else (np.nan, np.nan))
    for fr in frames:
        fr['marks'] = dict(zip(ids, source_rows(fr['path'], radec)))
    marks = {s: C.PALETTE[k % len(C.PALETTE)] for k, s in enumerate(ids)}

# wavelength of each rectified column: the rectified frames do not store it, but the
# source's combined spectrum is rectified onto the same column grid by the same jwst step
wave = None
spec = C.spec_file(ws, args.obs, args.grating_tag, args.source)
if os.path.exists(spec):
    w = np.asarray(fits.getdata(spec, 'SPEC1D')['wave'], float)
    if w.size == frames[0]['before_full'].shape[1]:
        wave = w


def wavelength_axis(ax):
    """Ticks at whole microns when the column wavelengths are known, else pixel columns."""
    if wave is None:
        ax.set_xlabel(f'wavelength (blocks of {args.bin} pixels)' if args.bin > 1 else 'wavelength pixel')
        return
    cols = (np.arange(wave.size) + 0.5) / args.bin - 0.5          # column centres in displayed units
    ok = np.isfinite(wave)
    lo, hi = np.nanmin(wave), np.nanmax(wave)
    step = 1.0 if hi - lo > 2 else 0.2
    ticks = np.arange(np.ceil(lo / step) * step, hi, step)
    ax.set_xticks(np.interp(ticks, wave[ok], cols[ok]))
    ax.set_xticklabels([f'{t:g}' for t in ticks])
    ax.minorticks_off()
    ax.set_xlabel('wavelength (µm)')
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
    shown = fr['before']
    if args.per_column:
        lit = np.where(shown > 0, shown, np.nan)
        shown = shown / np.nanmedian(lit, axis=0)[None, :]
        inside = shown[np.isfinite(shown) & (shown > 0.5)]           # the open shutters, not the margins
        from matplotlib.colors import Normalize
        norm = Normalize(vmin=np.percentile(inside, args.stretch[0]), vmax=np.percentile(inside, args.stretch[1]))
    else:
        norm = C.asinh_norm(shown, *args.stretch, soft=0.02)
    ax.imshow(shown, cmap='Greys', norm=norm, aspect='auto')
    C.label_panel(ax, f'nod {fr["nod"]}')
    nxd = fr['before'].shape[1]
    for s, color in marks.items():
        y = fr['marks'].get(s, np.nan)
        if not np.isfinite(y) or not (-0.5 <= y <= fr['before'].shape[0] - 0.5):
            continue
        for xa, xb in ((-0.5, 0.06 * nxd), (0.94 * nxd, nxd - 0.5)):     # ticks at both edges, data left clear
            ax.plot([xa, xb], [y, y], color=color, lw=2.2, solid_capstyle='butt')
        ax.text(0.065 * nxd, y, f' {s}' + (' (this source)' if s == args.source else ''), color=color,
                fontsize=9, va='center', ha='left', fontweight='bold',
                bbox=dict(boxstyle='round,pad=0.1', fc='white', ec='none', alpha=0.8))
    ax.set_xlim(-0.5, nxd - 0.5)
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
axes[0, 0].set_title('Calibrated exposure' + (' (each column / its median)' if args.per_column else ''))
axes[0, 1].set_title('After nod subtraction')
axes[0, 2].set_title('Along the slitlet')
for r in range(len(frames)):
    for a in axes[r, :2]:
        wavelength_axis(a)
        if r < len(frames) - 1:
            a.set_xticklabels([])
            a.set_xlabel('')
axes[-1, 2].set_xlabel('mean over wavelength (relative)')
for a in axes[:, 2]:
    a.set_xlim(-1.15, 1.15)
if handles:
    fig.legend(*handles, loc='outside lower right', ncol=2, fontsize=9.5)
fig.suptitle(f'{args.obs}  source {args.source}  {args.exposure_group} {args.detector}  (rectified; rows = position '
             'along the slitlet; wavelength increases to the right)', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'nod_subtraction.png')
