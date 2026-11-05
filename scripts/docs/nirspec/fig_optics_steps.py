"""How the CAMPFIRE optics model turns a point source into what the detector sees, along the slit.

The profile fit's model of the MSA (``nirspec/optics.py``) follows the light of a point source through
three steps, drawn here top to bottom for a source sitting off center in its shutter, at a blue and a
red wavelength in the same panels:

1. the telescope's diffraction pattern at the MSA: the image of a point source through the telescope's
   pupil (its amplitude across the slit direction tapered as (1 - u^2)^0.25, fitted), wider in the red;
2. the part of it the MSA's open shutters transmit: the image cut sharply at the shutter edges (the
   bars and the closed shutters beyond the slitlet block the rest);
3. what reaches the detector: the sharp cut diffracts light sideways, the spectrograph's pupil stop
   (0.996 of the telescope pupil's image) removes the part diffracted furthest, the detector blurs
   the rest (a Gaussian of 0.034" - 0.0026" per micron from 3 µm) and sums it in 0.1" pixels.

The fitted numbers come from calibration stars and empty shutters (``data/nirspec_optics.json``). An
empirical blur before the shutter (telescope wavefront error and pointing jitter) is applied on top in
the pipeline and is left out here.

    python scripts/docs/nirspec/fig_optics_steps.py --root <root>
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--wavelengths', type=float, nargs=2, default=[1.0, 5.0], help='µm, blue and red')
p.add_argument('--position', type=float, default=0.3, help='source position, shutter widths from the center')
args = p.parse_args()
C.setup_root(args.root)

import json                                                           # noqa: E402

import numpy as np                                                    # noqa: E402

from campfire_pipeline.nirspec import optics as O                     # noqa: E402

plt = C.style()
par = json.load(open(O.OPTICS_FILE))['diffraction']
shutters = (-1, 0, 1)
y, f = O._grid()
c = args.position * O.PITCH_AS                                        # arcsec
mask = np.zeros_like(y)
for k in shutters:
    mask[np.abs(y - k * O.PITCH_AS) < O.H_OPEN] = 1.0


def arriving(lam):
    """Step 1: the telescope's image of the point source at the MSA, per arcsec, unit total."""
    fmax = O.D_TEL / (2 * lam * 1e-6)
    pupil = np.clip(1.0 - (f / fmax) ** 2, 0.0, None) ** par['alpha']
    field = np.fft.fftshift(np.fft.ifft(pupil * np.exp(-2j * np.pi * f * (c * O.RAD))))
    inten = np.abs(field) ** 2
    return inten / inten.sum() / (y[1] - y[0])


curves = {}
for lam in args.wavelengths:
    a = arriving(lam)
    det, frac = O.point_image(c, lam, shutters, par['rho'], max(par['s0'] + par['s1'] * (lam - 3.0), 1e-4),
                              par['alpha'], y, f)
    curves[lam] = (a, a * mask, O.row_integrate(y, det), frac)

titles = ('1. Arriving at the MSA: the telescope\'s diffraction pattern',
          '2. Transmitted by the MSA shutters: a sharp cut at the shutter edges',
          '3. Reaching the detector: edge diffraction partly lost at the pupil stop, detector blur, 0.1" pixels')
fig, axes = plt.subplots(3, 1, figsize=(9.5, 8.2), sharex=True, constrained_layout=True)
top = 1.12
for row, (ax, title) in enumerate(zip(axes, titles)):
    for k in (-1.5, -0.5, 0.5, 1.5):                                 # the bars, and closed shutters beyond
        lo, hi = k * O.PITCH_AS - (O.PITCH_AS / 2 - O.H_OPEN), k * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)
        ax.axvspan(lo, hi, color='#d9d9d9', lw=0, zorder=0)
    for side in (-1, 1):
        ax.axvspan(side * (1.5 * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)), side * 2.0, color='#d9d9d9', lw=0,
                   zorder=0)
    for lam, color in zip(args.wavelengths, (C.WAVE_COLORS[0], C.WAVE_COLORS[-1])):
        a, passed, det, frac = curves[lam]
        curve = (a, passed, det)[row] / a.max()                      # each wavelength in units of its step-1 peak
        share = {0: None, 1: passed.sum() * (y[1] - y[0]), 2: frac}[row]
        label = f'{lam:g} µm' + (f': {100 * share:.0f}% of the light' if share is not None else '')
        ax.fill_between(y, 0, curve, color=color, alpha=0.18, lw=0)
        ax.plot(y, curve, color=color, lw=1.8, label=label)
    ax.set_ylim(0, top)
    ax.set_xlim(-0.95, 0.95)
    ax.set_ylabel('light per arcsec\n(1 = peak in step 1)')
    ax.set_title(title, fontsize=10.5)
    ax.legend(loc='upper left', fontsize=9)
axes[0].text(0.5 * O.PITCH_AS, 0.82 * top, 'bar', ha='center', fontsize=8.5, color=C.MUTED)
axes[0].text(0.94, 0.5 * top, 'closed\nshutter', ha='right', fontsize=8.5, color=C.MUTED)
axes[-1].set_xlabel('position along the slit (arcsec)')
fig.suptitle(f'A point source {args.position:g} shutter widths from the center of the middle shutter of a '
             'three-shutter slitlet', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'optics_steps.png')
