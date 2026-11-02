"""The path a spectrum takes through the NIRSpec pipeline: one box per step, what it does,
and what it writes.

    python scripts/docs/nirspec/fig_flow.py [--kind docs] [--out flow.png]
"""
import argparse

import common as C

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument('--kind', choices=sorted(C.OUT_DIRS), default='docs')
p.add_argument('--out', default=None)
args = p.parse_args()

from matplotlib.patches import FancyArrowPatch, FancyBboxPatch     # noqa: E402

plt = C.style()

STEPS = [
    ('Stage 1: detector processing',
     'jwst Detector1, then the detector background and\n1/f noise, fitted outside the traces, removed',
     'rate file per exposure', 'main'),
    ('Master background, step 1',
     'background spectrum from the empty shutters (count rate);\n'
     'stuck-closed shutters found by fitting each shutter\'s background;\ntrace maps of every slitlet',
     'tables and trace maps,\nstored in the rate file', 'mbkg'),
    ('Stage 2a: calibration',
     'one jwst Spec2 run per rate file, stuck shutters closed:\nflat field, path loss, flux and wavelength calibration',
     'calibrated cutout per\nsource and exposure', 'main'),
    ('Master background, step 2',
     'background spectrum in calibrated units, modeled on the\n'
     'detector through every shutter (failed-open shutters too)',
     '_mbkg.fits per exposure', 'mbkg'),
    ('Stage 2b: nod subtraction',
     'each exposure minus its other nods; a nod is left out\n'
     'when another source sits in the shutter it would use',
     'background-subtracted\ncutouts', 'main'),
    ('Stage 3: combination and extraction',
     'jwst Spec3 rectifies and combines; overlapping sources\n'
     'solved together; profile extraction; other sources\' light\n'
     'predicted; master-background spectrum; exposure groups combined',
     '_spec.fits per source\nand disperser', 'main'),
    ('Redshift fitting',
     'templates fitted over a grid of redshifts',
     '_zfit.fits', 'main'),
]

H, STEP = 1.2, 1.5                                    # box height and spacing (data units)
fig, ax = plt.subplots(figsize=(10.5, 10.6))
ax.set_xlim(0, 10.5)
ax.set_ylim(-0.55, len(STEPS) * STEP + 0.15)
ax.axis('off')
colors = {'main': ('#e8f0f8', C.PALETTE[0]), 'mbkg': ('#fdf1e1', C.PALETTE[1])}
y = len(STEPS) * STEP - H - 0.25
prev = None
for title, what, writes, kind in STEPS:
    fc, ec = colors[kind]
    x0 = 0.25 if kind == 'main' else 0.65
    box = FancyBboxPatch((x0, y), 6.4 - (x0 - 0.25), H, boxstyle='round,pad=0.04,rounding_size=0.08',
                         fc=fc, ec=ec, lw=1.4)
    ax.add_patch(box)
    ax.text(x0 + 0.15, y + H - 0.16, title, fontsize=11, fontweight='bold', va='top', color=C.INK)
    ax.text(x0 + 0.15, y + H - 0.44, what, fontsize=9, va='top', color=C.INK, linespacing=1.25)
    out = FancyBboxPatch((7.25, y + H / 2 - 0.3), 3.0, 0.6, boxstyle='round,pad=0.04,rounding_size=0.08',
                         fc='white', ec='#999999', lw=1.0, ls='--')
    ax.add_patch(out)
    ax.text(8.75, y + H / 2, writes, fontsize=9, ha='center', va='center', color=C.INK, linespacing=1.2)
    ax.add_patch(FancyArrowPatch((6.68, y + H / 2), (7.2, y + H / 2), arrowstyle='-|>', mutation_scale=12,
                                 color='#777777', lw=1.0))
    if prev is not None:
        ax.add_patch(FancyArrowPatch((3.4, prev), (3.4, y + H + 0.06), arrowstyle='-|>', mutation_scale=14,
                                     color='#555555', lw=1.3))
    prev = y - 0.04
    y -= STEP
ax.text(8.75, len(STEPS) * STEP - 0.12, 'writes', fontsize=10, ha='center', va='bottom', color=C.MUTED)
ax.text(0.25, -0.45, 'Blue: the steps every spectrum goes through.  Orange: the master background, '
        'measured within stage 2a.', fontsize=9.5, color=C.MUTED, va='bottom')
C.save(fig, args, 'flow.png')
