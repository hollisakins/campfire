"""From a point source to uniform light: the light the MSA lets through, by the size and position of the source.

jwst corrects every spectrum with one of two path-loss references: one for a point source and one for
perfectly uniform light, and nothing in between. The CAMPFIRE optics model (``nirspec/optics.py``) gives
the light a source of any size passes, so every galaxy gets its own correction, from the same model.

Each panel: a round Gaussian source of one size (columns, from a point to uniform light) in one wavelength
band (rows), moved along the slit across the open part of the middle shutter of a three-shutter slitlet. The
curve is the light that reaches the detector over the light that falls on the open shutters (the light
the shutter edges and the spectrograph's pupil stop lose; the light falling on the bars is the source's
geometry, not the optics), averaged over the band. One scale for every size in a band: the model is set to
jwst's point-source value for a point at the shutter center, the only place the pipeline takes its level
from jwst. jwst's two references are drawn in the end columns on the same footing: the point reference as
it is; the uniform reference, which is a surface brightness at the shutter center on jwst's aperture area,
times jwst's own bar shadow integrated over every row of the slitlet and divided by the open length, and
the ratio of the two aperture widths.

With ``--stars`` (from ``fit_star_positions.py``), the point-source column also shows calibration stars seen
at several positions: each visit's light relative to the same star's most central visit (the model's
transmission ratio between the two positions times the ratio of the fitted fluxes), as faint points, and
their medians in bins of position.

    python scripts/docs/nirspec/fig_pathloss.py --root <root> [--stars <json>]
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--bands', nargs='+', default=['0.8:1.2', '4.5:5.3'], help='µm ranges lo:hi, one per row')
p.add_argument('--sizes', type=float, nargs='+', default=[0.0, 0.05, 0.2],
               help='source FWHM in arcsec (0 = a point); uniform light is added as the last column')
p.add_argument('--disperser', default='prism', help='for jwst\'s bar shadow (the uniform reference\'s profile)')
p.add_argument('--stars', default=None, help='fit_star_positions.py output: stars at several positions')
p.add_argument('--max-srcx', type=float, default=0.2, help='stars: largest |SRCXPOS| (along the dispersion)')
p.add_argument('--min-snr', type=float, default=30.0, help='stars: smallest band S/N')
args = p.parse_args()
C.setup_root(args.root)

import json                                                           # noqa: E402
from collections import defaultdict                                   # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from scipy.special import erf                                         # noqa: E402

from campfire_pipeline.config import get_empirical_barshadow_path    # noqa: E402,F401
from campfire_pipeline.nirspec import bar_offset as BO                # noqa: E402
from campfire_pipeline.nirspec import barshadow_calibration as B      # noqa: E402
from campfire_pipeline.nirspec import crds_refs as CR                 # noqa: E402
from campfire_pipeline.nirspec import optics as O                     # noqa: E402

plt = C.style()
BANDS = [tuple(float(v) for v in b.split(':')) for b in args.bands]
ref = CR.depends_on('pathloss')['optics']
pc = CR.pathloss_cube(ref)
with fits.open(CR._locate(ref)) as h:                                 # jwst's uniform path loss, MOS1x3
    uni = [x for x in h if x.name == 'UNI' and str(x.header.get('APERTURE', '')).upper() == 'MOS1X3'][0]
    uni_lam = (uni.header['CRVAL1'] + uni.header['CDELT1'] * np.arange(uni.header['NAXIS1'])) * 1e6
    uni_val = np.asarray(uni.data, float)
bs_name = CR.depends_on('barshadow')[f'barshadow_{args.disperser}']
bs_table = B.load_table(CR._locate(bs_name))
JWST_APERTURE_W = 0.0217240 / 0.1                 # PIXAR_A2 of an MOS cal file / pixel height: open width [arcsec]

model = O.optics_for('NRS1')
mask = (-1, 0, 1)
open_half = O.H_OPEN / O.PITCH_AS                 # pitches
pos = np.linspace(-open_half, open_half, 121)     # source position across the open part, pitches
centres_slit = [k * O.PITCH_AS / O.SLIT for k in mask]
tx = np.asarray(model.x_transmission_table(), float)


def band_lams(band, n=7):
    return np.geomspace(band[0], band[1], n)


def inside(c, sigma, half, centres):
    """Share of a Gaussian (center ``c``, ``sigma``) falling within +-``half`` of each center."""
    s = np.sqrt(2) * np.maximum(sigma, 1e-9)
    return sum(0.5 * (erf((k + half - c) / s) - erf((k - half - c) / s)) for k in centres)


def mean_barshadow(wl, table, reach=1.5):
    """A bar shadow of a three-shutter slitlet (1 at a shutter center) integrated over every row out to
    +-``reach`` pitches and divided by the open length: the slitlet's total uniform light per unit open
    length, relative to a shutter center. jwst's reference ends at the outer shutters' edges (1.5)."""
    ys = np.linspace(-reach, reach, 4001)
    k = np.rint(ys)
    seg, yseg = B.segment_map((1 + k).astype(int), ys - k, '1x1')
    t = np.full(ys.size, np.nan)
    ok = seg >= 0
    t[ok] = B.evaluate_table(table, seg[ok], yseg[ok], np.full(ok.sum(), wl))
    return float(np.nansum(t) * (ys[1] - ys[0]) / (3 * 2 * open_half))


def model_curve(wl, fwhm, where=pos):
    """Light reaching the detector / light on the open shutters at source positions ``where`` (unscaled)."""
    lam = np.full(where.size, wl)
    c = CR.slit_position(where)
    if fwhm is None:                                                   # uniform light: no position dependence
        prof = BO.uniform_profile('NRS1', mask)
        i = int(np.argmin(np.abs(O.LAM - wl)))
        t_y = prof[i].sum() / O.DY_ROW * (O.Y[1] - O.Y[0]) / 3.0
        t_x = np.interp(wl, O.LAM, tx.sum(axis=1)) * (O.XC[1] - O.XC[0]) / (2 * O.X_HALF)
        return np.full(where.size, t_y * t_x)
    w = fwhm / 2.355 / O.SLIT
    t_y = model.transmission(mask, lam, c, np.full(where.size, w))
    t_x0 = float(np.interp(wl, O.LAM, tx[:, int(np.argmin(np.abs(O.XC)))]))
    r_x = model.x_extended_ratio(np.array([wl]), 0.0, np.array([max(w, 1e-6)]))[0] if w > 0 else 1.0
    g_y = inside(c, w, 0.5, centres_slit)
    g_x = float(inside(0.0, w * O.SLIT, O.X_HALF, [0.0])) if w > 0 else 1.0
    return t_y * t_x0 * r_x / (g_y * g_x)


def band_curves(band, fwhm):
    """The model scaled to jwst's point value at the center, and jwst's references, each averaged over the band."""
    iy, ix = int(np.argmin(np.abs(pc.y))), int(np.argmin(np.abs(pc.x)))
    m, jp, ju = [], [], []
    for wl in band_lams(band):
        j = int(np.argmin(np.abs(pc.lam - wl)))
        scale = float(pc.cube[j, iy, ix]) / float(model_curve(wl, 0.0, np.zeros(1))[0])
        m.append(scale * model_curve(wl, fwhm))
        jp.append(np.interp(pos, pc.y, pc.cube[j, :, ix]))
        ju.append(float(np.interp(wl, uni_lam, uni_val)) * mean_barshadow(wl, bs_table) * JWST_APERTURE_W
                  / (2 * O.X_HALF))
    return np.mean(m, axis=0), np.mean(jp, axis=0), float(np.mean(ju))


def star_points(path, band):
    """(position [pitches], light relative to the same star centered, NGC 2506) per visit, and the stars used."""
    d = json.load(open(path))
    match = [i for i, b in enumerate(d['bands']) if abs(b[0] - band[0]) < 1e-6 and abs(b[1] - band[1]) < 1e-6]
    if not match:
        raise SystemExit(f'{path} has no band {band}; it has {d["bands"]}')
    band_index = match[0]
    rows = [r for r in d['rows'] if 'error' not in r and abs(r['srcx']) < args.max_srcx
            and r['sat'][band_index] == 0 and r['bands'][band_index][0]]
    by = defaultdict(list)
    for r in rows:
        f, e = r['bands'][band_index]
        if e and f / e >= args.min_snr:
            by[(r['obs'], r['sid'], r['det'])].append(r)
    lams = band_lams(band)
    out, stars = [], 0

    def t_point(det, c):
        o = O.optics_for(det)
        return float(np.mean(o.transmission(mask, lams, np.full(lams.size, c), np.zeros(lams.size))))
    for (obs, sid, det), vs in by.items():
        if len(vs) < 2:
            continue
        refv = min(vs, key=lambda v: abs(v['centre']))
        if abs(refv['centre']) > 0.15 * O.PITCH_AS / O.SLIT:
            continue
        t0, tref = t_point(det, 0.0), t_point(det, refv['centre'])
        stars += 1
        for v in vs:
            if v is refv:
                continue
            light = t_point(det, v['centre']) / t0 * v['bands'][band_index][0] / refv['bands'][band_index][0]
            out.append((v['centre'] * O.SLIT / O.PITCH_AS, light, 'ngc2506' in obs))
    return np.array(out, dtype=object).reshape(-1, 3), stars


cols = list(args.sizes) + [None]
fig, axes = plt.subplots(len(BANDS), len(cols), figsize=(2.85 * len(cols), 2.9 * len(BANDS)), sharex=True,
                         sharey='row', constrained_layout=True, squeeze=False)
if len(BANDS) == 2:
    row_colors = [C.WAVE_COLORS[0], C.WAVE_COLORS[-1]]
else:                                   # blue to red by wavelength, through the same three colors
    from matplotlib.colors import LinearSegmentedColormap
    cmap = LinearSegmentedColormap.from_list('wave', C.WAVE_COLORS)
    lo, hi = np.log(BANDS[0][0]), np.log(BANDS[-1][1])
    row_colors = [cmap((np.log(np.sqrt(b[0] * b[1])) - lo) / (hi - lo)) for b in BANDS]
for row, (band, color) in enumerate(zip(BANDS, row_colors)):
    for col, fwhm in enumerate(cols):
        ax = axes[row, col]
        m, jp, ju = band_curves(band, fwhm)
        if col == 0 and args.stars:
            pts, nstar = star_points(args.stars, band)
            if len(pts):
                x, y, ngc = pts[:, 0].astype(float), pts[:, 1].astype(float), pts[:, 2].astype(bool)
                ax.scatter(x[~ngc], y[~ngc], s=7, color='#9a9a9a', alpha=0.45, lw=0, zorder=1)
                ax.scatter(x[ngc], y[ngc], s=9, marker='s', color='#9a9a9a', alpha=0.45, lw=0, zorder=1)
                edges = np.linspace(-open_half, open_half, 15)
                idx = np.digitize(x, edges) - 1
                mid, med, err = [], [], []
                for k in range(edges.size - 1):
                    sel = idx == k
                    if sel.sum() >= 5:
                        v = y[sel]
                        mid.append(0.5 * (edges[k] + edges[k + 1]))
                        med.append(np.median(v))
                        err.append(1.2533 * 1.4826 * np.median(np.abs(v - np.median(v))) / np.sqrt(sel.sum()))
                ax.errorbar(mid, med, yerr=err, fmt='o', color=C.INK, ms=5, capsize=2, zorder=4,
                            label=f'calibration stars ({nstar}), median')
        ax.plot(pos, m, color=color, lw=2.0, label='CAMPFIRE model', zorder=3)
        if col == 0:
            ax.plot(pos, jp, color=C.INK, ls='--', lw=1.5, label='jwst point-source reference', zorder=2)
        if fwhm is None:
            ax.axhline(ju, color=C.INK, ls='--', lw=1.5, label='jwst uniform reference')
        if row == 0:
            ax.set_title('point source' if fwhm == 0 else ('uniform light' if fwhm is None else f'FWHM {fwhm:g}"'),
                         fontsize=11)
        if col in (0, len(cols) - 1):
            ax.legend(loc='lower center', fontsize=7.2)
        ax.set_xlim(-open_half, open_half)
    axes[row, 0].set_ylabel(f'{band[0]:g}-{band[1]:g} µm\n\nlight reaching the detector /\nlight on the open shutters')
    axes[row, 0].yaxis.label.set_color(color)
for ax in axes[-1]:
    ax.set_xlabel('position along the slit\n(shutter pitches)')
for row in range(len(BANDS)):
    axes[row, 0].set_ylim(0.3, 1.15)
fig.suptitle(f'The light the MSA lets through, from a point source to uniform light (jwst references: {ref}, '
             f'{bs_name})', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'pathloss.png')
