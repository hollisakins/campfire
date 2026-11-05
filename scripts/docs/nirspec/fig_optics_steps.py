"""How the CAMPFIRE optics model turns a point source into what the detector sees, along the slit.

The profile fit's model of the MSA (``nirspec/optics.py``) follows the light of a point source through
three steps, drawn here top to bottom for a source sitting off center in its shutter, at a blue and a
red wavelength:

1. the telescope's image of the source as it arrives at the MSA (wider in the red: diffraction);
2. the part of it the open shutters let through (the bars and the closed shutters beyond the slitlet
   block the rest);
3. what reaches the detector: cutting the image at the shutter edges spreads light sideways, the
   spectrograph's pupil stop removes the part spread furthest, and the detector blurs the rest.

The fitted numbers of steps 1 and 3 (pupil shape, stop size, detector blur) come from calibration
stars and empty shutters (``data/nirspec_optics.json``). An empirical blur before the shutter
(telescope wavefront error and pointing jitter) is applied on top and is left out here.

    python scripts/docs/nirspec/fig_optics_steps.py --root <root>
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--wavelengths', type=float, nargs=2, default=[1.2, 4.5], help='µm, blue and red')
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


xlim = (-0.95, 0.95)
fig, axes = plt.subplots(3, 2, figsize=(10.5, 7.6), sharex=True, constrained_layout=True)
for col, lam in enumerate(args.wavelengths):
    a = arriving(lam)
    passed = a * mask
    det, frac = O.point_image(c, lam, shutters, par['rho'], max(par['s0'] + par['s1'] * (lam - 3.0), 1e-4),
                              par['alpha'], y, f)
    det = O.row_integrate(y, det)
    top = a.max() * 1.12
    color = C.PALETTE[0] if col == 0 else C.PALETTE[5]
    for row, (ax, curve, title) in enumerate(zip(
            axes[:, col],
            (a, passed, det),
            ('1. arriving at the shutters', '2. let through by the open shutters',
             '3. reaching the detector (blurred and summed in 0.1" detector pixels)'))):
        # the MSA: bars between the open shutters, closed shutters beyond them
        for k in (-1.5, -0.5, 0.5, 1.5):
            lo, hi = k * O.PITCH_AS - (O.PITCH_AS / 2 - O.H_OPEN), k * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)
            ax.axvspan(lo, hi, color='#d9d9d9', lw=0, zorder=0)
        for side in (-1, 1):
            ax.axvspan(side * (1.5 * O.PITCH_AS + (O.PITCH_AS / 2 - O.H_OPEN)), side * 2.0, color='#d9d9d9',
                       lw=0, zorder=0)
        if row == 2:
            ax.plot(y, passed, color=C.MUTED, lw=1.0, ls='--', label='step 2, for comparison')
        ax.fill_between(y, 0, curve, color=color, alpha=0.25, lw=0)
        ax.plot(y, curve, color=color, lw=1.8)
        ax.set_ylim(0, top)
        ax.set_xlim(*xlim)
        ax.set_yticklabels([])
        ax.set_title(title if col == 0 else '', fontsize=11)
        if row == 0:
            ax.text(0.99, 0.95, f'{lam:g} µm', transform=ax.transAxes, ha='right', va='top', fontsize=12,
                    fontweight='bold', color=color)
        if row == 1:
            ax.text(0.99, 0.95, f'{100 * passed.sum() * (y[1] - y[0]):.0f}% of the light gets through',
                    transform=ax.transAxes, ha='right', va='top', fontsize=9.5)
        if row == 2:
            ax.text(0.99, 0.95, f'{100 * frac:.0f}% reaches the detector', transform=ax.transAxes, ha='right',
                    va='top', fontsize=9.5)
            ax.legend(loc='upper left', fontsize=8.5)
    axes[-1, col].set_xlabel('position along the slit (arcsec)')
for ax in axes[:, 0]:
    ax.set_ylabel('light per arcsec')
axes[0, 0].text(0.94, 0.5 * axes[0, 0].get_ylim()[1], 'closed\nshutter', ha='right', fontsize=8.5,
                color=C.MUTED)
axes[0, 0].text(0.5 * O.PITCH_AS, 0.75 * axes[0, 0].get_ylim()[1], 'bar', ha='center', fontsize=8.5,
                color=C.MUTED)
fig.suptitle(f'A point source {args.position:g} shutter widths from the center of the middle shutter of a '
             'three-shutter slitlet', x=0.01, ha='left', fontsize=10, color=C.MUTED)
C.save(fig, args, 'optics_steps.png')
