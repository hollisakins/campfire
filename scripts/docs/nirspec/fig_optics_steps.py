"""How the CAMPFIRE optics model turns a point source into what the detector sees, along the slit.

The profile fit's model of the MSA (``nirspec/optics.py``) follows the light of a point source through
four steps, drawn here top to bottom for a source sitting off center in its shutter, at a blue and a
red wavelength in the same panels:

1. the telescope's diffraction pattern at the MSA: the image of a point source through the telescope's
   pupil (its amplitude across the slit direction tapered as (1 - u^2)^0.25, fitted), wider in the red;
2. the same image spread by the kernel: the telescope's wavefront error and pointing jitter move the
   source's light by small amounts before it reaches the MSA. The kernel says how much light lands at
   each offset; it was measured from calibration stars, one per detector, and is drawn at the right
   (log scale, so its faint wide wing shows). Uniform light does not feel it, which is why the stars
   and not the empty shutters fix it;
3. the part of it the MSA's open shutters transmit: the image cut sharply at the shutter edges (the
   bars and the closed shutters beyond the slitlet block the rest);
4. what reaches the detector: the sharp cut diffracts light sideways, the spectrograph's pupil stop
   (0.996 of the telescope pupil's image) removes the part diffracted furthest, the detector blurs
   the rest (a Gaussian of 0.034" - 0.0026" per micron from 3 µm) and sums it in 0.1" pixels.

Steps 3 and 4 include the kernel, as the pipeline does: every offset point of step 2 goes through
the shutters, the stop and the blur on its own, and the results add. The fitted numbers come from
calibration stars and empty shutters (``data/nirspec_optics.json``).

    python scripts/docs/nirspec/fig_optics_steps.py --root <root> [--detector NRS1]
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--wavelengths', type=float, nargs=2, default=[1.0, 5.0], help='µm, blue and red')
p.add_argument('--position', type=float, default=0.3, help='source position, shutter pitches from the center')
p.add_argument('--detector', default='NRS1', choices=['NRS1', 'NRS2'], help='whose kernel')
p.add_argument('--stars', default=None,
               help="star_profiles.py's JSON: calibration stars' light along the slit, drawn on step 4 (its bands "
                    'must be the figure\'s wavelengths; the first band goes with the first wavelength)')
args = p.parse_args()
C.setup_root(args.root)

import json                                                           # noqa: E402

import numpy as np                                                    # noqa: E402

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
offs = np.round(np.arange(-2.0, 2.0001, 0.0125), 5)                   # kernel offsets, slit units


def arriving(lam, pos):
    """The telescope's image at the MSA of a point source at ``pos`` arcsec, per arcsec, unit total."""
    fmax = O.D_TEL / (2 * lam * 1e-6)
    pupil = np.clip(1.0 - (f / fmax) ** 2, 0.0, None) ** par['alpha']
    field = np.fft.fftshift(np.fft.ifft(pupil * np.exp(-2j * np.pi * f * (pos * O.RAD))))
    inten = np.abs(field) ** 2
    return inten / inten.sum() / dy


curves = {}
for lam in args.wavelengths:
    k = model.kernel_at(lam, offs)
    use = k > 1e-6 * k.max()
    sigma = max(par['s0'] + par['s1'] * (lam - 3.0), 1e-4)
    raw = arriving(lam, c)
    spread = np.zeros_like(y)
    det = np.zeros_like(y)
    frac = 0.0
    for w, d in zip(k[use], offs[use]):
        spread += w * arriving(lam, c + d * O.SLIT)
        img, fr = O.point_image(c + d * O.SLIT, lam, shutters, par['rho'], sigma, par['alpha'], y, f)
        det += w * img
        frac += w * fr
    curves[lam] = dict(raw=raw, spread=spread, passed=spread * mask, det=O.row_integrate(y, det), frac=frac,
                       kernel=k)

titles = ("1. Arriving at the MSA: the telescope's diffraction pattern",
          "2. Spread by the telescope's wavefront error and pointing jitter (the kernel, measured from stars)",
          '3. Transmitted by the MSA: a sharp cut at the shutter edges',
          '4. Reaching the detector: edge diffraction partly lost at the pupil stop, detector blur, 0.1" pixels')
fig, axes = plt.subplots(4, 1, figsize=(10, 10.4), sharex=True, constrained_layout=True)
top, floor = 3.0, 1e-4                                                # log axis: room above the peak for the legend
for row, (ax, title) in enumerate(zip(axes, titles)):
    for kk in (-1.5, -0.5, 0.5, 1.5):                                # the bars, and closed shutters beyond
        lo, hi = kk * O.PITCH_AS - (O.PITCH_AS / 2 - O.H_OPEN), kk * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)
        ax.axvspan(lo, hi, color='#d9d9d9', lw=0, zorder=0)
    for side in (-1, 1):
        ax.axvspan(side * (1.5 * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)), side * 2.0, color='#d9d9d9', lw=0,
                   zorder=0)
    for lam, color in zip(args.wavelengths, (C.WAVE_COLORS[0], C.WAVE_COLORS[-1])):
        cv = curves[lam]
        peak = cv['raw'].max()
        curve = (cv['raw'], cv['spread'], cv['passed'], cv['det'])[row] / peak   # in units of the step-1 peak
        share = {0: None, 1: None, 2: cv['passed'].sum() * dy, 3: cv['frac']}[row]
        label = f'{lam:g} µm' + (f': {100 * share:.0f}% of the light' if share is not None else '')
        if row == 1:                                                 # step 1 for comparison
            ax.plot(y, cv['raw'] / peak, color=color, lw=0.9, ls='--', alpha=0.8)
        curve = np.maximum(curve, floor * 0.5)
        ax.fill_between(y, floor * 0.5, curve, color=color, alpha=0.18, lw=0)
        ax.plot(y, curve, color=color, lw=1.8, label=label)
    ax.set_yscale('log')
    ax.set_ylim(floor, top)
    ax.set_xlim(-0.95, 0.95)
    ax.set_ylabel('light per arcsec\n(1 = peak in step 1)')
    ax.set_title(title, fontsize=10.5, loc='left')
    ax.legend(loc='upper left', fontsize=9)
if args.stars:                                                        # calibration stars on step 4
    with open(args.stars) as fh:
        S = json.load(fh)
    win = 1.6 * 1.15 * 0.46                                           # the stars' columns were summed over |y| < 1.6 pitch
    edges = np.arange(-0.95, 0.9501, 0.02)
    mid = 0.5 * (edges[1:] + edges[:-1])
    for (key, band), lam, color in zip(S['bands'].items(), args.wavelengths, (C.WAVE_COLORS[0], C.WAVE_COLORS[-1])):
        ys, vs = np.asarray(band['y'], float), np.asarray(band['v'], float)
        if ys.size == 0:
            continue
        cv = curves[lam]
        total = cv['det'][np.abs(y) < win].sum() * dy / cv['raw'].max()   # the model's light over the same rows
        med, lo, hi = (np.full(mid.size, np.nan) for _ in range(3))
        for i in range(mid.size):
            s = (ys >= edges[i]) & (ys < edges[i + 1])
            if s.sum() >= 20:
                med[i], lo[i], hi[i] = np.percentile(vs[s], [50, 25, 75]) * total
        good = np.isfinite(med) & (med > 0)
        axes[3].vlines(mid[good], np.maximum(lo[good], 1e-4), hi[good], color=color, lw=0.8, alpha=0.6, zorder=6)
        axes[3].plot(mid[good], med[good], 'o', ms=3.6, mfc='white', mec=color, mew=1.1, zorder=7,
                     label=f'calibration stars, {key.replace(":", "-")} µm ({band["n_visits"]} visits)')
    axes[3].legend(loc='upper left', fontsize=9)
axes[1].plot([], [], color=C.MUTED, lw=0.9, ls='--', label='step 1, for comparison')
axes[1].legend(loc='upper left', fontsize=9)
axes[0].text(0.5 * O.PITCH_AS, 0.93, 'bar', ha='center', va='top', fontsize=8.5, color=C.MUTED,
             transform=axes[0].get_xaxis_transform())
axes[0].text(0.94, 0.93, 'closed\nshutter', ha='right', va='top', fontsize=8.5, color=C.MUTED,
             transform=axes[0].get_xaxis_transform())
axes[-1].set_xlabel("position along the slit (arcsec; 0 = center of the source's shutter)")

kx = axes[1].inset_axes([0.765, 0.47, 0.215, 0.36])                   # the kernel, inside step 2
dist = np.abs(offs * O.SLIT)                                          # how far each shift moves the light
s = np.linspace(0.0, 0.6, 241)
for lam, color in zip(args.wavelengths, (C.WAVE_COLORS[0], C.WAVE_COLORS[-1])):
    k = curves[lam]['kernel']                                         # on the pipeline's uniform grid, sum 1
    beyond = np.array([k[dist > v].sum() for v in s])
    kx.plot(s, np.maximum(beyond, 1e-5), color=color, lw=1.5)
kx.set_yscale('log')
kx.set_ylim(1e-3, 1.2)
kx.set_xlim(0, 0.6)
kx.tick_params(labelsize=7.5)
kx.set_xlabel('distance (arcsec)', fontsize=8, labelpad=1)
kx.set_title(f'cumulative kernel ({args.detector})', fontsize=8.5, loc='left')
kx.set_ylabel('fraction beyond', fontsize=7.5, labelpad=1)
kx.patch.set_facecolor('white')
fig.suptitle(f'A point source {args.position * O.PITCH_AS:.2f}" ({args.position:g} of a shutter pitch) from the '
             'center of the middle shutter of a three-shutter slitlet', x=0.01, ha='left', fontsize=10,
             color=C.MUTED)
C.save(fig, args, 'optics_steps.png')
