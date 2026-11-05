"""How much of a source's light gets through its shutter, by where the source sits: jwst's path-loss
reference against the CAMPFIRE model.

jwst corrects every spectrum with a path-loss reference measured for a point source (and one for
perfectly uniform light). CAMPFIRE's profile fit uses its own model of the MSA's optics instead, which
also covers galaxies of any size. Each panel shows, at one wavelength, the light that gets through as
the source moves along the slit from its shutter's center toward the bar, relative to a point source at
the center: jwst's reference for a point source, the model for a point source, and the model for a
small galaxy. The model's level at the center is jwst's (it only replaces how the light changes with
position).

    python scripts/docs/nirspec/fig_pathloss.py --root <root>
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
p.add_argument('--wavelengths', type=float, nargs='+', default=[1.0, 3.0, 5.0], help='µm')
p.add_argument('--galaxy-width', type=float, default=0.15,
               help='Gaussian sigma of the galaxy, in units of the shutter\'s open height (0.46")')
args = p.parse_args()
root = C.setup_root(args.root)

import numpy as np                                                    # noqa: E402

from campfire_pipeline.nirspec import crds_refs as CR                 # noqa: E402
from campfire_pipeline.nirspec import optics as O                     # noqa: E402

plt = C.style()
ref = CR.depends_on('pathloss')['optics']
pc = CR.pathloss_cube(ref)
model = O.optics_for('NRS1')
pos = np.linspace(-0.5, 0.5, 201)                 # source position, shutter pitches from the shutter center
open_half = O.H_OPEN / O.PITCH_AS                 # the open part of a shutter, in pitches
mask = (-1, 0, 1)                                 # the source's shutter and its two open neighbors

fig, axes = plt.subplots(1, len(args.wavelengths), figsize=(4.0 * len(args.wavelengths), 3.9), sharey=True,
                         constrained_layout=True)
for ax, wl in zip(np.atleast_1d(axes), args.wavelengths):
    i = int(np.argmin(np.abs(pc.lam - wl)))
    prof = pc.at_x_centre()[i]
    jw = np.interp(pos, pc.y, prof) / np.interp(0.0, pc.y, prof)
    lam = np.full(pos.size, wl)
    c = CR.slit_position(pos)
    point = model.transmission(mask, lam, c, np.zeros(pos.size))
    galaxy = model.transmission(mask, lam, c, np.full(pos.size, args.galaxy_width))
    norm = model.transmission(mask, np.array([wl]), np.array([0.0]), np.zeros(1))[0]
    for side in (-1, 1):
        ax.axvspan(side * open_half, side * 0.5 + side * 0.2, color='#e6e6e6', lw=0, zorder=0)
    ax.text(0.47, 0.06, 'bar', ha='center', fontsize=9, color=C.MUTED)
    ax.text(-0.47, 0.06, 'bar', ha='center', fontsize=9, color=C.MUTED)
    ax.plot(pos, jw, color=C.INK, ls='--', lw=1.6, label='jwst reference, point source')
    ax.plot(pos, point / norm, color=C.PALETTE[0], lw=2.0, label='CAMPFIRE model, point source')
    ax.plot(pos, galaxy / norm, color=C.PALETTE[1], lw=2.0,
            label=f'CAMPFIRE model, galaxy (FWHM {2.355 * args.galaxy_width * O.SLIT:.2f}")')
    ax.set_xlim(-0.5, 0.5)
    ax.set_ylim(0, 1.12)
    ax.set_title(f'{wl:g} µm')
    ax.set_xlabel('source position along the slit\n(shutter widths from the shutter center)')
np.atleast_1d(axes)[0].set_ylabel('light that gets through\n(1 = point source at the shutter center)')
np.atleast_1d(axes)[0].legend(loc='lower center', fontsize=8.5)
fig.suptitle(f'Light lost at the shutter edges: jwst\'s {ref} against the CAMPFIRE model', x=0.01, ha='left',
             fontsize=10, color=C.MUTED)
C.save(fig, args, 'pathloss.png')
