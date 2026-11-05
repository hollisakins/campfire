"""From a point source to uniform light: the light the MSA lets through, by the size and position of the source.

jwst corrects every spectrum with one of two path-loss references: one for a point source and one for
perfectly uniform light, and nothing in between. The CAMPFIRE optics model (``nirspec/optics.py``) gives
the light a source of any size passes, so every galaxy gets its own correction, from the same model.

Each panel: a round Gaussian source of one size (columns, from a point to uniform light) at one wavelength
(rows), moved along the slit across the open part of the middle shutter of a three-shutter slitlet. The
curve is the light that reaches the detector over the light that falls on the open shutters (the light
the shutter edges and the spectrograph's pupil stop lose; the light falling on the bars is the source's
geometry, not the optics). One scale for every size at a wavelength: the model is set to jwst's
point-source value for a point at the shutter center, the only place the pipeline takes its level from
jwst. jwst's two references are drawn in the end columns on the same footing: the point reference as it
is; the uniform reference, which is a surface brightness at the shutter center on jwst's aperture area,
times jwst's own bar shadow averaged over the open shutters and the ratio of the two aperture widths.

    python scripts/docs/nirspec/fig_pathloss.py --root <root>
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--wavelengths', type=float, nargs=2, default=[1.0, 5.0], help='µm')
p.add_argument('--sizes', type=float, nargs='+', default=[0.0, 0.1, 0.3, 1.0],
               help='source FWHM in arcsec (0 = a point); uniform light is added as the last column')
p.add_argument('--disperser', default='prism', help='for jwst\'s bar shadow (the uniform reference\'s profile)')
args = p.parse_args()
C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402
from scipy.special import erf                                         # noqa: E402

from campfire_pipeline.nirspec import bar_offset as BO                # noqa: E402
from campfire_pipeline.nirspec import barshadow_calibration as B      # noqa: E402
from campfire_pipeline.nirspec import crds_refs as CR                 # noqa: E402
from campfire_pipeline.nirspec import optics as O                     # noqa: E402

plt = C.style()
ref = CR.depends_on('pathloss')['optics']
pc = CR.pathloss_cube(ref)
with fits.open(CR._locate(ref)) as h:                                 # jwst's uniform path loss, MOS1x3
    uni = [x for x in h if x.name == 'UNI' and str(x.header.get('APERTURE', '')).upper() == 'MOS1X3'][0]
    uni_lam = (uni.header['CRVAL1'] + uni.header['CDELT1'] * np.arange(uni.header['NAXIS1'])) * 1e6
    uni_val = np.asarray(uni.data, float)
bs_name = CR.depends_on('barshadow')[f'barshadow_{args.disperser}']
bs_table = B.load_table(CR._locate(bs_name))
from campfire_pipeline.config import get_empirical_barshadow_path    # noqa: E402
bs_measured = B.load_table(get_empirical_barshadow_path(args.disperser))
JWST_APERTURE_W = 0.0217240 / 0.1                 # PIXAR_A2 of an MOS cal file / pixel height: open width [arcsec]

model = O.optics_for('NRS1')
mask = (-1, 0, 1)
open_half = O.H_OPEN / O.PITCH_AS                 # pitches
pos = np.linspace(-open_half, open_half, 121)     # source position across the open part, pitches
centres_slit = [k * O.PITCH_AS / O.SLIT for k in mask]
tx = np.asarray(model.x_transmission_table(), float)


def inside(c, sigma, half, centres):
    """Share of a Gaussian (center ``c``, ``sigma``) falling within +-``half`` of each center."""
    s = np.sqrt(2) * np.maximum(sigma, 1e-9)
    return sum(0.5 * (erf((k + half - c) / s) - erf((k - half - c) / s)) for k in centres)


def jwst_mean_barshadow(wl, table=None, reach=1.5):
    """A bar shadow of a three-shutter slitlet (jwst's, or ``table``; 1 at a shutter center) integrated over
    every row out to +-``reach`` pitches (the light under the bars and past the outer edges counts, as it
    does in the model's total) and divided by the open length: the slitlet's total uniform light per unit
    open length, relative to a shutter center. jwst's reference ends at the outer shutters' edges (1.5)."""
    table = bs_table if table is None else table
    ys = np.linspace(-reach, reach, 4001)                              # pitches
    k = np.rint(ys)
    seg, yseg = B.segment_map((1 + k).astype(int), ys - k, '1x1')
    t = np.full(ys.size, np.nan)
    ok = seg >= 0
    t[ok] = B.evaluate_table(table, seg[ok], yseg[ok], np.full(ok.sum(), wl))
    return float(np.nansum(t) * (ys[1] - ys[0]) / (3 * 2 * open_half))


def model_curve(wl, fwhm):
    """Light reaching the detector / light on the open shutters, against position (unscaled)."""
    lam = np.full(pos.size, wl)
    c = CR.slit_position(pos)
    if fwhm is None:                                                   # uniform light: no position dependence
        prof = BO.uniform_profile('NRS1', mask)
        i = int(np.argmin(np.abs(O.LAM - wl)))
        t_y = prof[i].sum() / O.DY_ROW * (O.Y[1] - O.Y[0]) / 3.0       # per slit unit of open length
        t_x = np.interp(wl, O.LAM, tx.sum(axis=1)) * (O.XC[1] - O.XC[0]) / (2 * O.X_HALF)
        return np.full(pos.size, t_y * t_x)
    w = fwhm / 2.355 / O.SLIT                                          # sigma, slit units
    t_y = model.transmission(mask, lam, c, np.full(pos.size, w))
    t_x0 = float(np.interp(wl, O.LAM, tx[:, int(np.argmin(np.abs(O.XC)))]))
    r_x = model.x_extended_ratio(np.array([wl]), 0.0, np.array([max(w, 1e-6)]))[0] if w > 0 else 1.0
    g_y = inside(c, w, 0.5, centres_slit)
    g_x = float(inside(0.0, w * O.SLIT, O.X_HALF, [0.0])) if w > 0 else 1.0
    return t_y * t_x0 * r_x / (g_y * g_x)


cols = list(args.sizes) + [None]
fig, axes = plt.subplots(2, len(cols), figsize=(2.75 * len(cols), 5.6), sharex=True, sharey='row',
                         constrained_layout=True)
for row, (wl, color) in enumerate(zip(args.wavelengths, (C.WAVE_COLORS[0], C.WAVE_COLORS[-1]))):
    j = int(np.argmin(np.abs(pc.lam - wl)))
    iy, ix = int(np.argmin(np.abs(pc.y))), int(np.argmin(np.abs(pc.x)))
    ps_profile = np.interp(pos, pc.y, pc.cube[j, :, ix])
    scale = float(pc.cube[j, iy, ix]) / float(model_curve(wl, 0.0)[pos.size // 2])
    jwst_uniform = (float(np.interp(wl, uni_lam, uni_val)) * jwst_mean_barshadow(wl) * JWST_APERTURE_W
                    / (2 * O.X_HALF))
    print(f'{wl:g} um: model uniform {scale * model_curve(wl, None)[0]:.3f}; jwst uniform x its bar shadow '
          f'{jwst_uniform:.3f} (mean {jwst_mean_barshadow(wl):.3f}); x the measured bar shadow '
          f'{jwst_uniform / jwst_mean_barshadow(wl) * jwst_mean_barshadow(wl, bs_measured, 2.0):.3f} '
          f'(mean {jwst_mean_barshadow(wl, bs_measured, 2.0):.3f}; to 1.5 only '
          f'{jwst_mean_barshadow(wl, bs_measured):.3f})')
    for col, fwhm in enumerate(cols):
        ax = axes[row, col]
        ax.plot(pos, scale * model_curve(wl, fwhm), color=color, lw=2.0, label='CAMPFIRE model')
        if col == 0:
            ax.plot(pos, ps_profile, color=C.INK, ls='--', lw=1.5, label='jwst point-source reference')
        if fwhm is None:
            ax.axhline(jwst_uniform, color=C.INK, ls='--', lw=1.5, label='jwst uniform reference')
        if row == 0:
            ax.set_title('point source' if fwhm == 0 else ('uniform light' if fwhm is None else f'FWHM {fwhm:g}"'),
                         fontsize=11)
        if col in (0, len(cols) - 1):
            ax.legend(loc='lower center', fontsize=7.5)
        ax.set_xlim(-open_half, open_half)
    axes[row, 0].set_ylabel(f'{wl:g} µm\n\nlight reaching the detector /\nlight on the open shutters')
    axes[row, 0].yaxis.label.set_color(color)
for ax in axes[-1]:
    ax.set_xlabel('source position\n(shutter widths from center)')
for row in range(2):
    lo = min(ax.get_ylim()[0] for ax in axes[row])
    axes[row, 0].set_ylim(max(lo, 0.0), None)
fig.suptitle(f'The light the MSA lets through, from a point source to uniform light (jwst references: {ref}, '
             f'{bs_name})', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'pathloss.png')
