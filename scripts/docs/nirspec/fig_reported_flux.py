"""What the reported flux means: the fitted source's light within the shutter's open width across the dispersion,
and all of it along the slit.

Along the slit the fit measures the source's profile; across the shutter the data do not constrain the shape, so
the reported flux stops at the shutter's edges (profile_fit.reported_factor). Left: a galaxy, the open shutter and
its neighbors in the slitlet, and the band whose light is reported. Right: the reported share of a round Gaussian's
total light against its FWHM, for three positions across the shutter, with the position scatter the optics model
carries (optics.X_BLUR). Computed from the definition; no reduction is read.

    python scripts/docs/nirspec/fig_reported_flux.py --root <root>
"""
import common as C

p = C.parser(__doc__.splitlines()[0], obs=False)
args = p.parse_args()
C.setup_root(args.root)

import numpy as np                                                    # noqa: E402
from matplotlib.patches import Rectangle                              # noqa: E402
from scipy.special import erf                                         # noqa: E402

from campfire_pipeline.nirspec.optics import X_BLUR, X_HALF           # noqa: E402

OPEN_H, PITCH_Y = 0.46, 0.53                  # open shutter height and pitch along the slit [arcsec]
GALAXY = 0.4                                  # FWHM of the drawn galaxy [arcsec]


def share(fwhm, x):
    """reported_factor for one exposure: a round Gaussian of this FWHM at x across the shutter."""
    s = np.sqrt((np.asarray(fwhm) / 2.355) ** 2 + X_BLUR ** 2) * np.sqrt(2.0)
    return 0.5 * (erf((X_HALF - x) / s) - erf((-X_HALF - x) / s))


plt = C.style()
fig, (a, b) = plt.subplots(1, 2, figsize=(11.5, 4.8), constrained_layout=True,
                           gridspec_kw=dict(width_ratios=[1, 1.4]))
g = np.linspace(-0.75, 0.75, 301)
X, Y = np.meshgrid(g, g)
s = GALAXY / 2.355
a.imshow(np.exp(-0.5 * (X ** 2 + Y ** 2) / s ** 2), extent=[g[0], g[-1], g[0], g[-1]], cmap='Greys', origin='lower')
a.axvspan(-X_HALF, X_HALF, color=C.PALETTE[1], alpha=0.25, lw=0)
for k in (-1, 0, 1):
    a.add_patch(Rectangle((-X_HALF, k * PITCH_Y - OPEN_H / 2), 2 * X_HALF, OPEN_H, fill=False,
                          ec=C.PALETTE[0] if k == 0 else '#8fb8d8', lw=2.0 if k == 0 else 1.0,
                          ls='-' if k == 0 else '--'))
a.text(X_HALF + 0.03, 0.05, 'open\nshutter', ha='left', fontsize=9, color=C.PALETTE[0])
a.text(0.0, -0.70, 'reported', ha='center', fontsize=9, color='#a86f00')
a.set_xlim(g[0], g[-1])
a.set_ylim(g[0], g[-1])
a.set_xlabel('along the dispersion (arcsec)')
a.set_ylabel('along the slit (arcsec)')
a.set_title(f'A {GALAXY:g}" FWHM galaxy: {share(GALAXY, 0.0):.0%} of its light is reported')

fwhm = np.linspace(0.0, 1.0, 201)
for i, x in enumerate((0.0, 0.05, 0.10)):
    color = C.PALETTE[[0, 2, 5][i]]
    b.plot(fwhm, share(fwhm, x), color=color, lw=2.0, label=f'{x:.2f}" from the shutter center')
b.plot([GALAXY], [share(GALAXY, 0.0)], 'o', color=C.PALETTE[0], ms=7, mec='white', mew=1.5)
b.set_xlim(0, 1.0)
b.set_ylim(0, 1.05)
b.set_xlabel('source FWHM, intrinsic (arcsec)')
b.set_ylabel('reported flux / total flux')
b.set_title(f'Shutter open width {2 * X_HALF:.2f}" (FLUXWID), position scatter {X_BLUR:.2f}"')
b.legend(loc='upper right', fontsize=9)
C.save(fig, args, 'reported_flux.png')
