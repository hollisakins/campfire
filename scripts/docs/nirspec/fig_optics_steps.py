"""How the CAMPFIRE optics model turns light into what the detector sees, along the slit: a point source step by
step (optics_steps.png) and uniform light (optics_uniform.png), each against real data.

The profile fit's model of the MSA (``nirspec/optics.py``) follows the light through four steps, drawn top to
bottom for a three-shutter slitlet:

1. the telescope's diffraction pattern at the MSA: the image of a point source through the telescope's pupil
   (its amplitude across the slit direction tapered as (1 - u^2)^0.25, fitted), wider in the red;
2. the image convolved with the kernel: the telescope's wavefront error and pointing jitter move the light by
   small amounts before it reaches the MSA. The kernel (inset, log scale) was measured from calibration stars,
   one per detector. Uniform light is unchanged by it (shifted uniform light is still uniform), which is why
   the stars and not the empty shutters fix it;
3. the MSA's open shutters pass the light, the bars and closed shutters block it: a sharp cut of the light wave
   at the shutter edges;
4. what reaches the detector: the cut diffracts light sideways, the spectrograph's pupil stop removes the part
   diffracted furthest, the detector blurs the rest and sums it in 0.1" pixels.

optics_steps.png: a point source 0.3 of a shutter pitch from the center of the middle shutter. Steps 1-3 at 1 and 5 µm;
step 4 averaged over the bands the calibration stars are measured in (``--bands``), with the stars
(``star_profiles.py``, ``--stars``) scaled to the model's total, and their ratio to the model below.

optics_uniform.png: uniform light filling the slitlet, the sum of point-source images at every position across
it, on the detector (step 4; steps 1 and 2 leave uniform light unchanged and step 3 is the open shutters), scaled to one at the middle shutter's center and compared with the bar shadow measured from empty shutters
(``data/empirical_jwst_nirspec_barshadow_prism.fits``, averaged over the same bands), with their ratio below.

    python scripts/docs/nirspec/fig_optics_steps.py --root <root> [--stars <star_profiles.json>]
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--wavelengths', type=float, nargs=2, default=[1.0, 5.0], help='µm, steps 1-3')
p.add_argument('--bands', nargs=2, default=['0.8:1.2', '4.5:5.3'], help='µm ranges of step 4, blue and red')
p.add_argument('--position', type=float, default=0.3, help='source position, shutter pitches from the center')
p.add_argument('--detector', default='NRS1', choices=['NRS1', 'NRS2'], help='whose kernel')
p.add_argument('--stars', default=None, help="star_profiles.py's JSON (its bands must be --bands)")
p.add_argument('--n-lam', type=int, default=7, help='wavelengths averaged over each band')
args = p.parse_args()
C.setup_root(args.root)

import json                                                           # noqa: E402
import os                                                             # noqa: E402

import numpy as np                                                    # noqa: E402
from astropy.io import fits                                           # noqa: E402

from campfire_pipeline.nirspec import optics as O                     # noqa: E402

plt = C.style()
par = json.load(open(O.OPTICS_FILE))['diffraction']
model = O.optics_for(args.detector)
shutters = (-1, 0, 1)
y, f = O._grid()
dy = y[1] - y[0]
c = args.position * O.PITCH_AS                                        # arcsec
mask = np.zeros_like(y)
for k in shutters:
    mask[np.abs(y - k * O.PITCH_AS) < O.H_OPEN] = 1.0
offs = np.round(np.arange(-2.0, 2.0001, 0.0125), 5)                   # kernel shifts, slit units
BANDS = [tuple(float(v) for v in b.split(':')) for b in args.bands]
COLORS = (C.WAVE_COLORS[0], C.WAVE_COLORS[-1])
WIN = 1.6 * O.PITCH_AS                                                # the slitlet's extent used for totals


def sigma_det(lam):
    return max(par['s0'] + par['s1'] * (lam - 3.0), 1e-4)


def arriving(lam, pos):
    """The telescope's image at the MSA of a point source at ``pos`` arcsec, per arcsec, unit total."""
    fmax = O.D_TEL / (2 * lam * 1e-6)
    pupil = np.clip(1.0 - (f / fmax) ** 2, 0.0, None) ** par['alpha']
    field = np.fft.fftshift(np.fft.ifft(pupil * np.exp(-2j * np.pi * f * (pos * O.RAD))))
    inten = np.abs(field) ** 2
    return inten / inten.sum() / dy


def point_steps(lam):
    """Steps 1-4 of the point source at ``lam``, in units of step 1's peak, and the kernel."""
    k = model.kernel_at(lam, offs)
    use = k > 1e-6 * k.max()
    raw = arriving(lam, c)
    spread, det, frac = np.zeros_like(y), np.zeros_like(y), 0.0
    for w, d in zip(k[use], offs[use]):
        spread += w * arriving(lam, c + d * O.SLIT)
        img, fr = O.point_image(c + d * O.SLIT, lam, shutters, par['rho'], sigma_det(lam), par['alpha'], y, f)
        det += w * img
        frac += w * fr
    peak = raw.max()
    return dict(raw=raw / peak, spread=spread / peak, passed=spread * mask / peak,
                det=O.row_integrate(y, det) / peak, frac=frac, passed_frac=(spread * mask).sum() * dy, kernel=k)


def uniform_detector(lam):
    """Uniform light through the slitlet at ``lam``: the sum of point images over every position."""
    pos = np.arange(-1.3, 1.3001, 0.005)
    det = np.zeros_like(y)
    for q in pos:
        det += O.point_image(q, lam, shutters, par['rho'], sigma_det(lam), par['alpha'], y, f)[0]
    det = O.row_integrate(y, det)
    return det / np.interp(0.0, y, det)                               # one at the middle shutter's center


steps = {lam: point_steps(lam) for lam in args.wavelengths}
band_point, band_uniform = [], []
for lo, hi in BANDS:
    lams = np.linspace(lo, hi, args.n_lam)
    per = [point_steps(L) for L in lams]
    band_point.append(dict(det=np.mean([q['det'] for q in per], axis=0), frac=np.mean([q['frac'] for q in per])))
    band_uniform.append(np.mean([uniform_detector(L) for L in lams], axis=0))


def measured_bar_shadow(lo, hi):
    """The bar shadow measured from empty shutters (PRISM table), a three-shutter slitlet, averaged over the
    band: (y arcsec, transmission, 1 at the open shutter center)."""
    path = os.path.join(C.REPO, 'pipeline', 'campfire_pipeline', 'data', 'empirical_jwst_nirspec_barshadow_prism.fits')
    tab = {}
    with fits.open(path) as h:
        for name in ('DATA1X1', 'DATA1X3'):
            hd, d = h[name].header, np.asarray(h[name].data, float)
            lam = (hd['CRVAL1'] + hd['CDELT1'] * (np.arange(d.shape[1]) + 1 - hd['CRPIX1'])) * 1e6
            ty = hd['CRVAL2'] + hd['CDELT2'] * (np.arange(d.shape[0]) + 1 - hd['CRPIX2'])
            cols = (lam >= lo) & (lam <= hi)
            tab[name] = (ty, np.nanmean(d[:, cols], axis=1))
    yp = np.linspace(-1.9, 1.9, 761)                                  # shutter pitches
    out = np.full(yp.shape, np.nan)
    for name, a, b, centre in (('DATA1X1', -2.0, -1.0, -1.0), ('DATA1X3', -1.0, 0.0, 0.0),
                               ('DATA1X3', 0.0, 1.0, 1.0), ('DATA1X1', 1.0, 2.0, 1.0)):
        ty, prof = tab[name]
        sel = (yp >= a) & (yp < b)
        out[sel] = np.interp(yp[sel] - centre, ty, prof)
    return yp * O.PITCH_AS, out / np.interp(0.0, yp, out)


def shade(ax):
    for kk in (-1.5, -0.5, 0.5, 1.5):                                # the bars, and closed shutters beyond
        lo, hi = kk * O.PITCH_AS - (O.PITCH_AS / 2 - O.H_OPEN), kk * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)
        ax.axvspan(lo, hi, color='#d9d9d9', lw=0, zorder=0)
    for side in (-1, 1):
        ax.axvspan(side * (1.5 * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)), side * 2.0, color='#d9d9d9', lw=0,
                   zorder=0)


def curve(ax, values, color, label, floor, fill=True):
    v = np.maximum(values, floor * 0.5)
    if fill:
        ax.fill_between(y, floor * 0.5, v, color=color, alpha=0.18, lw=0)
    ax.plot(y, v, color=color, lw=1.8, label=label)


def binned(ys, vs, edges, scale):
    mid = 0.5 * (edges[1:] + edges[:-1])
    med, lo, hi = (np.full(mid.size, np.nan) for _ in range(3))
    for i in range(mid.size):
        s = (ys >= edges[i]) & (ys < edges[i + 1])
        if s.sum() >= 20:
            med[i], lo[i], hi[i] = np.percentile(vs[s], [50, 25, 75]) * scale
    return mid, med, lo, hi


fig = plt.figure(figsize=(11, 13.2), constrained_layout=True)
gs = fig.add_gridspec(5, 1, height_ratios=[1, 1, 1, 1, 0.5])
L = [fig.add_subplot(gs[0])]
L += [fig.add_subplot(gs[r], sharex=L[0]) for r in range(1, 5)]
ufig = plt.figure(figsize=(10, 5.6), constrained_layout=True)          # uniform light: its own figure
ugs = ufig.add_gridspec(2, 1, height_ratios=[1, 0.42])
R = [ufig.add_subplot(ugs[0])]
R.append(ufig.add_subplot(ugs[1], sharex=R[0]))
top, floor = 3.0, 1e-4
titles = ("1. Arriving at the MSA: the telescope's diffraction pattern",
          "2. Convolved with the kernel: the telescope's wavefront error and pointing jitter",
          '3. Transmitted by the MSA: a sharp cut at the shutter edges',
          '4. Reaching the detector: edge diffraction partly lost at the pupil stop, detector blur, 0.1" pixels')

# --- left: a point source -------------------------------------------------------------------------------
for row in range(4):
    ax = L[row]
    shade(ax)
    if row < 3:
        for lam, color in zip(args.wavelengths, COLORS):
            st = steps[lam]
            vals = (st['raw'], st['spread'], st['passed'])[row]
            share = {2: st['passed_frac']}.get(row)
            if row == 1:
                ax.plot(y, np.maximum(st['raw'], floor * 0.5), color=color, lw=0.9, ls='--', alpha=0.8)
            curve(ax, vals, color, f'{lam:g} µm' + (f': {100 * share:.0f}% of the light' if share else ''), floor)
    else:
        for (lo, hi), bp, color in zip(BANDS, band_point, COLORS):
            curve(ax, bp['det'], color, f'model, {lo:g}-{hi:g} µm: {100 * bp["frac"]:.0f}% of the light', floor)
    ax.set_yscale('log')
    ax.set_ylim(floor, top)
    ax.set_ylabel('light per arcsec\n(1 = peak in step 1)')
    ax.set_title(titles[row], fontsize=10.5, loc='left')
    ax.tick_params(labelbottom=False)
L[1].plot([], [], color=C.MUTED, lw=0.9, ls='--', label='step 1, for comparison')
L[0].text(0.5 * O.PITCH_AS, 0.93, 'bar', ha='center', va='top', fontsize=8.5, color=C.MUTED,
          transform=L[0].get_xaxis_transform())
L[0].text(0.94, 0.93, 'closed\nshutter', ha='right', va='top', fontsize=8.5, color=C.MUTED,
          transform=L[0].get_xaxis_transform())

kx = L[1].inset_axes([0.83, 0.5, 0.15, 0.44])                          # the kernel, inside step 2
step_as = (offs[1] - offs[0]) * O.SLIT
for lam, color in zip(args.wavelengths, COLORS):
    k = steps[lam]['kernel'] / step_as
    kx.plot(offs * O.SLIT, np.maximum(k / k.max(), 1e-5), color=color, lw=1.2)
kx.set_yscale('log')
kx.set_ylim(3e-4, 1.5)
kx.set_xlim(-0.5, 0.5)
kx.tick_params(labelsize=7.5)
kx.set_xticks([-0.4, 0.0, 0.4])
kx.set_xlabel('shift (arcsec)', fontsize=8, labelpad=1)
kx.text(0.04, 0.95, f'kernel\n({args.detector})', transform=kx.transAxes, ha='left', va='top', fontsize=8,
        fontweight='bold', color=C.INK)
kx.patch.set_facecolor('white')

edges = np.arange(-0.95, 0.9501, 0.02)
ratio_ax = L[4]
shade(ratio_ax)
if args.stars:
    with open(args.stars) as fh:
        S = json.load(fh)
    for (key, band), bp, color in zip(S['bands'].items(), band_point, COLORS):
        ys, vs = np.asarray(band['y'], float), np.asarray(band['v'], float)
        if ys.size == 0:
            continue
        total = bp['det'][np.abs(y) < WIN].sum() * dy                 # the model's light over the stars' rows
        mid, med, lo, hi = binned(ys, vs, edges, total)
        good = np.isfinite(med) & (med > 0)
        L[3].vlines(mid[good], np.maximum(lo[good], floor), hi[good], color=color, lw=0.8, alpha=0.6, zorder=6)
        L[3].plot(mid[good], med[good], 'o', ms=3.6, mfc='white', mec=color, mew=1.1, zorder=7,
                  label=f'calibration stars, {key.replace(":", "-")} µm ({band["n_visits"]} visits)')
        m = np.interp(mid, y, bp['det'])
        ok = good & (m > 1e-2 * bp['det'].max())                      # where the model holds enough light
        ratio_ax.vlines(mid[ok], lo[ok] / m[ok], hi[ok] / m[ok], color=color, lw=0.8, alpha=0.6, zorder=6)
        ratio_ax.plot(mid[ok], med[ok] / m[ok], 'o', ms=3.6, mfc='white', mec=color, mew=1.1, zorder=7)
for ax in L[:4]:
    ax.legend(loc='upper left', fontsize=9)

# --- uniform light, its own figure ------------------------------------------------------------------------
ax = R[0]
shade(ax)
for (lo, hi), u, color in zip(BANDS, band_uniform, COLORS):
    curve(ax, u, color, f'model, {lo:g}-{hi:g} µm', floor, fill=False)
    yb, tb = measured_bar_shadow(lo, hi)
    pick = slice(None, None, 8)                                       # every 0.02"
    ax.plot(yb[pick], np.maximum(tb[pick], floor), 'o', ms=3.4, mfc='white', mec=color, mew=1.1, zorder=7)
    m = np.interp(yb, y, u)
    ok = np.isfinite(tb) & (np.abs(yb) < O.PITCH_AS + O.H_OPEN)       # inside the slitlet's outer edges
    R[1].plot(yb[ok][::8], tb[ok][::8] / m[ok][::8], 'o', ms=3.4, mfc='white', mec=color, mew=1.1, zorder=7)
ax.plot([], [], 'o', ms=3.4, mfc='white', mec='#555555', mew=1.1, label='measured from empty shutters')
ax.set_yscale('log')
ax.set_ylim(1e-3, 3.0)
ax.set_ylabel('light per arcsec\n(1 = open shutter center)')
ax.set_title('Uniform light through a three-shutter slitlet, on the detector', fontsize=10.5, loc='left')
ax.legend(loc='lower center', fontsize=9)
ax.tick_params(labelbottom=False)
ax.text(0.5 * O.PITCH_AS, 0.93, 'bar', ha='center', va='top', fontsize=8.5, color=C.MUTED,
        transform=ax.get_xaxis_transform())

for ax, what in ((L[4], 'stars / model'), (R[1], 'measured / model')):
    if ax is R[1]:
        shade(ax)
    ax.axhspan(0.9, 1.1, color='#bbbbbb', alpha=0.35, lw=0, zorder=1)
    ax.axhline(1.0, color=C.INK, lw=0.8, zorder=2)
    ax.set_ylim(0.5, 1.5)
    ax.set_ylabel(what)
    ax.set_xlim(-0.95, 0.95)
    ax.set_xlabel("position along the slit (arcsec; 0 = center of the middle shutter)")
fig.suptitle(f'A point source {args.position * O.PITCH_AS:.2f}" ({args.position:g} of a shutter pitch) from the center '
             'of the middle shutter of a three-shutter slitlet', x=0.01, ha='left', fontsize=10, color=C.MUTED)
ufig.suptitle('The same model for uniform light, against the bar shadow measured from empty shutters (PRISM)',
              x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'optics_steps.png')
C.save(ufig, args, 'optics_uniform.png')
