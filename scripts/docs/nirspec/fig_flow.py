"""How information flows through the NIRSpec pipeline, one column per stage, read top to
bottom; each stage's output feeds the top of the next column. A step a configuration option
turns on or off is dashed and shows the option with its default, read from the package's
config_default.toml.

    python scripts/docs/nirspec/fig_flow.py [--kind docs] [--out flow.png]
"""
import argparse
import os
import tomllib

import common as C

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument('--kind', choices=sorted(C.OUT_DIRS), default='docs')
p.add_argument('--out', default=None)
args = p.parse_args()

from matplotlib.patches import FancyArrowPatch, FancyBboxPatch     # noqa: E402

with open(os.path.join(C.REPO, 'pipeline', 'campfire_pipeline', 'data', 'config_default.toml'), 'rb') as f:
    CFG = tomllib.load(f)['nirspec']


def knob(section, key):
    v = CFG[section][key]
    v = f'"{v}"' if isinstance(v, str) else str(v).lower()
    return f'{key} = {v}'


VERM, MB_FILL, MB_EDGE = '#D55E00', '#fdf1e1', '#b07800'
W, ROW = 2.5, 1.0                       # box width, row spacing (data units)
STAGES = [  # title, column x (left edge), width, color, fill
    ('Stage 1: detector', 0.0, 2.9, '#0072B2', '#f5f9fd'),
    ('Stage 2a: calibration and master background', 3.3, 5.6, '#009E73', '#f4faf7'),
    ('Stage 2b: nod subtraction', 9.3, 2.9, '#CC79A7', '#fbf6fa'),
    ('Stage 3: extraction', 12.6, 5.6, '#3a8fc4', '#f4f9fd'),
    ('Redshifts and lines', 18.6, 2.9, '#777777', '#f8f8f8'),
]
c1, c2a, c2b, c2b2, c3a, c3b, c4 = 0.2, 3.5, 6.2, 9.5, 12.8, 15.5, 18.8

# name: (x of left edge, row, text, kind, options); kind: step | mb | file
N = {
    'uncal':  (c1, 0, 'raw exposures', 'file', ()),
    'det1':   (c1, 1, 'ramp fitting (jwst Detector1)', 'step', ()),
    'cfn':    (c1, 2, 'flicker-noise cleaning (jwst)', 'step', (knob('stage1', 'do_clean_flicker_noise'),)),
    'bkg1':   (c1, 3, 'detector background and\n1/f noise subtraction', 'step', ()),
    'bkg2d':  (c1, 4, 'smooth 2-D background\nsubtraction', 'step', (knob('stage1', 'subtract_2d'),)),
    'mask':   (c1, 5, 'manual masking', 'step', ('auto_apply = true',)),
    'rate':   (c1, 6, 'count-rate frames', 'file', ()),

    'geo':    (c2a, 0, 'slitlet footprint mapping', 'mb', ()),
    'tmap':   (c2b, 0, 'trace maps', 'file', ()),
    'mb1':    (c2a, 1, 'background spectrum\nmeasurement (empty shutters)', 'mb', ()),
    'sfit':   (c2a, 2, 'stuck-closed shutter detection', 'mb', (knob('stage2', 'detect_stuck_shutters'),)),
    'stuck':  (c2b, 2, 'stuck-shutter list', 'file', ()),
    'spec2':  (c2a, 3.2, 'calibration (jwst Spec2)', 'step',
               (knob('stage2', 'empirical_wavecorr'), knob('stage2', 'extend_g140m_g235m'))),
    'canon':  (c2a, 4.4, 'calibrated cutouts', 'file', ()),
    'mb2':    (c2a, 5.4, 'master background modeling', 'mb', ()),
    'mbkg':   (c2a, 6.4, 'master background', 'file', ()),

    'nbr':    (c2b2, 0, 'background nod selection\n(a nod with a bright neighbor\nat the source is left out)', 'step',
               (knob('stage2', 'slitlet_neighbor_min_background'),)),
    'sub':    (c2b2, 1.4, 'nod subtraction', 'step', ()),
    'bsub':   (c2b2, 2.4, 'background-subtracted\ncutouts', 'file', ()),

    'spec3':  (c3a, 0, 'exposure combination (jwst Spec3)', 'step', ()),
    'dmap':   (c3a, 1, 'pixel map\n(each 2-D drizzled once)', 'step', ()),
    'joint':  (c3a, 2, 'joint solve of\noverlapping sources', 'step', (knob('stage3', 'contam_predict'),)),
    'recheck': (c3b, 2, 'neighbor recheck', 'step', (knob('stage3', 'slitlet_neighbor_brightness'),)),
    'shape':  (c3a, 3.1, 'source shape selection', 'step', (knob('stage3', 'profile_fit_shape'),)),
    'pfit':   (c3a, 4.1, 'profile extraction\n(galaxy x star-measured PSF)', 'step', (knob('stage3', 'profile_fit'),)),
    'opt':    (c3b, 1, 'optimal and boxcar\nextractions', 'step', ()),
    'mbs':    (c3b, 4.1, 'master-background spectrum\n(cutouts minus master background)', 'mb',
               (knob('stage3', 'mbkg_spectrum'),)),
    'contam': (c3a, 5.2, 'contamination prediction', 'step', ()),
    'comb':   (c3a, 6.2, '1-D combination of\nexposure groups', 'step',
               ('combine_method = "1d"', '(default "' + CFG['stage3']['combine_method'] + '")')),
    'spec':   (c3b, 6.4, 'spectrum file (_spec.fits)', 'file', ()),

    'zfit':   (c4, 0, 'redshift fitting', 'step', ()),
    'zf':     (c4, 1, 'redshift fit (_zfit.fits)', 'file', ()),
    'lines':  (c4, 2.2, 'line fitting at the\ninspected redshift', 'step', ()),
    'lf':     (c4, 3.2, 'line fits (_lines.fits)', 'file', ()),
}

plt = C.style()
fig = plt.figure(figsize=(22.6, 8.9))                 # one data unit = one inch, so text sizes are known
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(-0.2, 22.4)
ax.set_ylim(-7.85, 1.05)
ax.axis('off')

boxes = {}
for name, (x, row, text, kind, knobs) in N.items():
    nk = len(knobs)
    nl = text.count('\n') + 1
    h = 0.2 + 0.17 * nl + 0.13 * nk
    y = -row * ROW - h / 2
    gated = bool(knobs)
    fill = MB_FILL if kind == 'mb' else 'white'
    edge = VERM if gated else (MB_EDGE if kind == 'mb' else ('#888888' if kind == 'file' else '#444444'))
    style = 'square,pad=0.02' if kind == 'file' else 'round,pad=0.02,rounding_size=0.12'
    ax.add_patch(FancyBboxPatch((x, y), W, h, boxstyle=style, fc=fill, ec=edge, lw=1.3,
                                ls='--' if gated else '-', zorder=3))
    if kind == 'file':                                  # a folded corner marks a file
        ax.plot([x + W - 0.16, x + W - 0.16, x + W], [y + h, y + h - 0.13, y + h - 0.13], color=edge, lw=0.9,
                zorder=4)
    if nk:
        ax.text(x + W / 2, y + h - 0.1, text, ha='center', va='top', fontsize=9.5, color=C.INK, zorder=5,
                linespacing=1.15)
    else:
        ax.text(x + W / 2, y + h / 2, text, ha='center', va='center', fontsize=9.5, color=C.INK, zorder=5,
                linespacing=1.15)
    for i, k in enumerate(knobs):
        ax.text(x + W / 2, y + 0.07 + 0.13 * (nk - 1 - i), k, ha='center', va='bottom', fontsize=7,
                family='monospace', color=VERM, zorder=5)
    boxes[name] = (x, y, W, h)


def anchor(name, side):
    x, y, w, h = boxes[name]
    return {'top': (x + w / 2, y + h), 'bottom': (x + w / 2, y), 'left': (x, y + h / 2),
            'right': (x + w, y + h / 2)}[side]


def arrow(a, b, sa='bottom', sb='top', rad=0.0, dashed=False, color='#555555', lw=1.1, label=None):
    p0, p1 = anchor(a, sa), anchor(b, sb)
    ax.add_patch(FancyArrowPatch(p0, p1, connectionstyle=f'arc3,rad={rad}', arrowstyle='-|>', mutation_scale=10,
                                 color=VERM if dashed and color == '#555555' else color, lw=lw,
                                 ls='--' if dashed else '-', zorder=2, shrinkA=1, shrinkB=1))
    if label:
        ax.text((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2 + 0.12, label, fontsize=8, color='#555555',
                ha='center', va='bottom', zorder=6, bbox=dict(boxstyle='round,pad=0.1', fc='white', ec='none'))


def route(a, b, path, sa='right', sb='left', dashed=False, color='#222222', lw=1.6, label=None, label_at=None):
    """A right-angled link: from ``a``'s side ``sa`` through the corner points ``path`` (each an
    (x, y), or ('x', value) / ('y', value) to move along one axis) to ``b``'s side ``sb``."""
    pts = [anchor(a, sa)]
    for q in path:
        if q[0] == 'x':
            pts.append((q[1], pts[-1][1]))
        elif q[0] == 'y':
            pts.append((pts[-1][0], q[1]))
        else:
            pts.append(q)
    end = anchor(b, sb)
    if sb in ('left', 'right'):
        pts.append((pts[-1][0], end[1]))
    else:
        pts.append((end[0], pts[-1][1]))
    col = VERM if dashed and color == '#222222' else color
    xs, ys = zip(*pts)
    ax.plot(xs, ys, color=col, lw=lw, ls='--' if dashed else '-', zorder=2, solid_capstyle='butt')
    ax.add_patch(FancyArrowPatch(pts[-1], end, arrowstyle='-|>', mutation_scale=11, color=col, lw=lw,
                                 ls='--' if dashed else '-', zorder=2, shrinkA=0, shrinkB=1))
    if label:
        lx, ly = label_at
        ax.text(lx, ly, label, fontsize=8.5, color='#444444', ha='center', va='center', zorder=6,
                bbox=dict(boxstyle='round,pad=0.15', fc='white', ec='none'))


# stage panels
for title, x0, wd, col, fill in STAGES:
    ax.add_patch(FancyBboxPatch((x0, -7.45), wd, 8.15, boxstyle='round,pad=0.02,rounding_size=0.2', fc=fill,
                                ec=col, lw=1.3, zorder=0))
    ax.text(x0 + 0.15, 0.55, title, fontsize=11, fontweight='bold', color=col, va='center')

# within the stages
for a, b in [('uncal', 'det1'), ('det1', 'cfn'), ('cfn', 'bkg1'), ('bkg1', 'bkg2d'), ('bkg2d', 'mask'),
             ('mask', 'rate'),
             ('geo', 'mb1'), ('mb1', 'sfit'), ('spec2', 'canon'), ('canon', 'mb2'), ('mb2', 'mbkg'),
             ('nbr', 'sub'), ('sub', 'bsub'),
             ('spec3', 'dmap'), ('dmap', 'joint'), ('joint', 'shape'), ('shape', 'pfit'), ('pfit', 'contam'),
             ('contam', 'comb'),
             ('zfit', 'zf'), ('lines', 'lf')]:
    arrow(a, b)
arrow('geo', 'tmap', 'right', 'left')
arrow('sfit', 'stuck', 'right', 'left')
G = dict(color='#555555', lw=1.1)
route('stuck', 'spec2', [], sa='bottom', sb='right', **G)
arrow('dmap', 'opt', 'right', 'left')
arrow('joint', 'recheck', 'right', 'left')
arrow('pfit', 'mbs', 'right', 'left')
_o, _s = anchor('opt', 'right'), anchor('spec', 'top')     # into the spectrum file's top, right of centre
_xe, _ye = boxes['spec'][0] + 0.8 * W, _s[1]
ax.plot([_o[0], 18.08, 18.08, _xe], [_o[1], _o[1], _ye + 0.25, _ye + 0.25], color='#555555', lw=1.1, zorder=2)
ax.add_patch(FancyArrowPatch((_xe, _ye + 0.25), (_xe, _ye), arrowstyle='-|>', mutation_scale=11, color='#555555',
                             lw=1.1, zorder=2, shrinkA=0, shrinkB=1))
arrow('mbs', 'spec', 'bottom', 'top', dashed=True)
arrow('comb', 'spec', 'right', 'left', dashed=True)

# between the stages (heavier), through the gaps between the columns
route('rate', 'geo', [('x', 3.06), ('y', anchor('geo', 'left')[1])])
route('rate', 'spec2', [('x', 3.18)])
route('mbkg', 'nbr', [('x', 9.06), ('y', anchor('nbr', 'left')[1] - 0.1)])
route('canon', 'nbr', [('x', 9.18), ('y', anchor('nbr', 'left')[1] + 0.1)])
route('bsub', 'spec3', [('x', 12.42), ('y', anchor('spec3', 'left')[1])])
route('spec', 'zfit', [('x', 18.36)])
route('spec', 'lines', [('x', 18.48)])
route('tmap', 'joint', [('y', 0.85), ('x', 12.52), ('y', anchor('joint', 'left')[1])], sa='top', dashed=True)
route('recheck', 'nbr', [('y', 0.92), ('x', anchor('nbr', 'top')[0])], sa='top', sb='top', dashed=True,
      label='changed sources run again', label_at=(14.2, 0.92))

# key
kx, ky = 18.8, -4.6
ax.text(kx, ky + 0.15, 'Key', fontsize=10, fontweight='bold', color='#555555')
for i, (text, kind, gated) in enumerate([('processing step', 'step', False), ('master background', 'mb', False),
                                         ('switchable step', 'step', True), ('file written', 'file', False)]):
    y = ky - 0.25 - i * 0.6
    ax.add_patch(FancyBboxPatch((kx, y - 0.4), 1.4, 0.4,
                                boxstyle='square,pad=0.02' if kind == 'file' else 'round,pad=0.02,rounding_size=0.1',
                                fc=MB_FILL if kind == 'mb' else 'white',
                                ec=VERM if gated else (MB_EDGE if kind == 'mb' else '#666666'),
                                ls='--' if gated else '-', lw=1.2, zorder=3))
    ax.text(kx + 1.55, y - 0.2, text + ('\n(option = default)' if gated else ''), fontsize=8.5, va='center',
            color=C.INK)
C.save(fig, args, 'flow.png')
