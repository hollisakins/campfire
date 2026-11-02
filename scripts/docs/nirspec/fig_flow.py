"""How information flows through the NIRSpec pipeline: inputs, processing steps and the files
they write, stage by stage. A step or file that a configuration option turns on or off has a
dashed vermilion border and names the option with its default value, read from the package's
config_default.toml so the diagram follows the defaults.

Needs Graphviz (``dot``) and the ``graphviz`` Python package, e.g. in a small separate env:

    conda create -n campfire-docs -c conda-forge python=3.12 graphviz python-graphviz
    conda run -n campfire-docs python scripts/docs/nirspec/fig_flow.py

Writes flow.svg and flow.png to web/public/docs/nirspec/ (or docs/figures/pipeline-v2/ with
--kind pr).
"""
import argparse
import html
import os
import tomllib

import graphviz

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
OUT_DIRS = {'docs': os.path.join(REPO, 'web', 'public', 'docs', 'nirspec'),
            'pr': os.path.join(REPO, 'docs', 'figures', 'pipeline-v2')}

p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
p.add_argument('--kind', choices=sorted(OUT_DIRS), default='docs')
p.add_argument('--out', default='flow', help='output base name (.svg and .png are written)')
args = p.parse_args()

with open(os.path.join(REPO, 'pipeline', 'campfire_pipeline', 'data', 'config_default.toml'), 'rb') as f:
    CFG = tomllib.load(f)['nirspec']


def knob(section, key, value=None):
    """'key = default' for a [nirspec.<section>] option."""
    v = CFG[section][key] if value is None else value
    v = f'"{v}"' if isinstance(v, str) else str(v).lower()
    return f'{key} = {v}'


BLUE, ORANGE, GREEN, PINK, SKY, VERM = '#0072B2', '#E69F00', '#009E73', '#CC79A7', '#56B4E9', '#D55E00'
FILL = {'s1': '#e8f0f8', 's2a': '#e6f4ef', 'mb': '#fdf1e1', 's2b': '#f3eaf2', 's3': '#eaf4fb', 'z': '#f2f2f2',
        'in': '#ffffff'}
FONT = 'Helvetica'


def label(text, knobs=()):
    """An HTML label: the text, then each option on its own line in vermilion monospace."""
    lines = '<BR/>'.join(html.escape(t) for t in text.split('\n'))
    for k in knobs:
        lines += f'<BR/><FONT FACE="Courier" POINT-SIZE="9" COLOR="{VERM}">{html.escape(k)}</FONT>'
    return f'<{lines}>'


g = graphviz.Digraph('flow', format='svg')
g.attr(rankdir='TB', splines='spline', nodesep='0.3', ranksep='0.3', fontname=FONT, fontsize='12',
       newrank='true', pad='0.25')
g.attr('node', fontname=FONT, fontsize='10', margin='0.12,0.06')
g.attr('edge', color='#555555', arrowsize='0.7', penwidth='1.0')


def step(c, name, text, fill, knobs=()):
    """A processing step; one an option controls is dashed and names the option."""
    gated = bool(knobs)
    c.node(name, label(text, knobs), shape='box', style='rounded,filled' + (',dashed' if gated else ''),
           fillcolor=fill, color=VERM if gated else '#444444', penwidth='1.2' if gated else '1.0')


def data(c, name, text, knobs=()):
    """A file or table."""
    gated = bool(knobs)
    c.node(name, label(text, knobs), shape='note', style='filled' + (',dashed' if gated else ''), fillcolor='white',
           color=VERM if gated else '#777777', fontsize='9.5')


def inp(c, name, text):
    """An input the step beside it reads (calibration or catalog data)."""
    c.node(name, label(text), shape='note', style='filled', fillcolor='#f7f7f7', color='#aaaaaa', fontsize='9',
           fontcolor='#444444')


def ref(a, b):
    g.edge(a, b, style='dotted', color='#888888')


def opt(a, b):
    g.edge(a, b, style='dashed', color=VERM)


# --- stage 1 -------------------------------------------------------------------------
with g.subgraph(name='cluster_s1') as c:
    c.attr(label='Stage 1: detector', style='rounded,filled', fillcolor='#fafcfe', color=BLUE, fontcolor=BLUE)
    data(c, 'uncal', 'raw exposures (_uncal.fits)')
    step(c, 'det1', 'jwst Detector1: ramps to count rate', FILL['s1'])
    step(c, 'cfn', 'jwst clean_flicker_noise', FILL['s1'], [knob('stage1', 'do_clean_flicker_noise')])
    inp(c, 'msa1', 'MSA metafile')
    step(c, 'bkg1', 'detector background, fitted outside the traces, removed:\n'
                    'picture frame, column 1/f, row 1/f (PRISM); 5 passes;\nvariance rescaled', FILL['s1'])
    step(c, 'bkg2d', 'smooth 2-D background', FILL['s1'], [knob('stage1', 'subtract_2d')])
    inp(c, 'masks', 'manual masks\n(observations.toml)')
    step(c, 'mask', 'manual masks applied', FILL['s1'], ['[nirspec.masks] auto_apply = true'])
    data(c, 'rate', 'count-rate frame (_rate.fits)')
g.edge('uncal', 'det1')
g.edge('det1', 'cfn')
g.edge('cfn', 'bkg1')
ref('msa1', 'bkg1')
g.edge('bkg1', 'bkg2d')
g.edge('bkg2d', 'mask')
ref('masks', 'mask')
g.edge('mask', 'rate')

# --- stage 2a ------------------------------------------------------------------------
with g.subgraph(name='cluster_s2a') as c:
    c.attr(label='Stage 2a: calibration', style='rounded,filled', fillcolor='#f9fcfb', color=GREEN, fontcolor=GREEN)
    with c.subgraph(name='cluster_mb1') as m:
        m.attr(label='master background, step 1 (count rate)', style='rounded,filled', fillcolor='#fffaf2',
               color=ORANGE, fontcolor='#a86e00')
        inp(m, 'msa2', 'MSA metafile;\nfailed-open shutters (msaoper)')
        step(m, 'geo', 'footprint of every commanded slitlet and\nfailed-open shutter (each with its margin)', FILL['mb'])
        inp(m, 'bs', 'measured bar-shadow tables')
        step(m, 'mb1', 'background spectrum from the empty shutters;\nquadrant scales', FILL['mb'])
        step(m, 'sfit', 'each commanded-open shutter\'s background amplitude\nfitted; stuck when < '
                        f'{CFG["stage2"]["master_bkg_stuck_a_cut"]} over nods and detectors', FILL['mb'],
             [knob('stage2', 'detect_stuck_shutters') + '  (writes the list)'])
        data(m, 'tmap', 'trace maps: which slitlets light\neach pixel (kept in the rate file)')
    data(c, 'stuck', 'stuck-shutter list\n(manual > web > auto)')
    inp(c, 'crds', 'CRDS reference files')
    step(c, 'spec2', 'jwst Spec2, one run per rate file, stuck shutters closed:\n'
                     'WCS, flat field, path loss, flux and wavelength calibration', FILL['s2a'],
         [knob('stage2', 'empirical_wavecorr') + '  (JADES wavelength correction)',
          knob('stage2', 'extend_g140m_g235m') + '  (G140M / G235M past the red end)'])
    data(c, 'canon', 'calibrated cutout\nper source and exposure')
    data(c, 'bkgslits', 'background-only slitlets', [knob('stage2', 'extract_bkgslits')])
    step(c, 'units', 'units fixed where jwst found no source', FILL['s2a'])
    with c.subgraph(name='cluster_mb2') as m:
        m.attr(label='master background, step 2 (calibrated)', style='rounded,filled', fillcolor='#fffaf2',
               color=ORANGE, fontcolor='#a86e00')
        step(m, 'mb2', 'clean empty shutters reselected; background spectrum\n'
                       '(the exposure group\'s pool unless an exposure differs)', FILL['mb'])
        step(m, 'paint', 'master background on the detector, through every\nshutter\'s footprint and bar shadow '
                         '(failed-open\nshutters at their measured transmission)', FILL['mb'],
             [knob('stage2', 'master_bkg_write_2d')])
    data(c, 'mbkg', 'master background (_mbkg.fits)')
g.edge('rate', 'geo')
ref('msa2', 'geo')
g.edge('geo', 'mb1')
ref('bs', 'mb1')
g.edge('mb1', 'sfit')
g.edge('geo', 'tmap')
opt('sfit', 'stuck')
g.edge('stuck', 'spec2')
g.edge('rate', 'spec2')
ref('crds', 'spec2')
g.edge('spec2', 'canon')
opt('spec2', 'bkgslits')
g.edge('canon', 'units')
g.edge('units', 'mb2')
opt('bkgslits', 'mb2')
g.edge('mb1', 'mb2')
opt('mb2', 'paint')
g.edge('paint', 'mbkg')

# --- stage 2b ------------------------------------------------------------------------
with g.subgraph(name='cluster_s2b') as c:
    c.attr(label='Stage 2b: nod subtraction', style='rounded,filled', fillcolor='#fcf9fc', color=PINK, fontcolor=PINK)
    inp(c, 'msa3', 'MSA metafile\n(catalog positions)')
    step(c, 'nbr', 'other catalog sources placed along the slitlet in every nod;\n'
                   'their brightness against the master background', FILL['s2b'])
    inp(c, 'ovr', 'hand-set background nods\n(these win)')
    step(c, 'pick', 'background nods of each exposure: a nod is left out\n'
                    'when a bright neighbor sits where the source is', FILL['s2b'],
         [knob('stage2', 'slitlet_neighbor_min_background'), knob('stage2', 'slitlet_neighbor_max_distance')])
    step(c, 'sub', 'path loss undone, background nods subtracted\n(jwst Background), path loss redone', FILL['s2b'])
    data(c, 'bsub', 'background-subtracted cutouts\n(calibrated frame kept)')
g.edge('canon', 'nbr', constraint='false')
g.edge('mbkg', 'nbr')
ref('msa3', 'nbr')
g.edge('nbr', 'pick')
ref('ovr', 'pick')
g.edge('pick', 'sub')
g.edge('sub', 'bsub')

# --- stage 3 -------------------------------------------------------------------------
with g.subgraph(name='cluster_s3') as c:
    c.attr(label='Stage 3: combination and extraction', style='rounded,filled', fillcolor='#f8fbfd', color=SKY,
           fontcolor='#2b7fb0')
    step(c, 'spec3', 'jwst Spec3: exposures rectified and combined;\noutlier pixels flagged', FILL['s3'])
    data(c, 's2d', '2-D spectrum and each exposure\non the product grid (crf)')
    step(c, 'dmap', 'pixel map: the product\'s grid and each exposure\'s\nmapping onto it, built once; '
                    'PROF2D, CONTAM2D\nand the master-background 2-D are drizzled through it', FILL['s3'])
    step(c, 'opt', 'optimal extraction;\n3-, 4-, 5-pixel boxcars', FILL['s3'])
    with c.subgraph(name='cluster_pf') as m:
        m.attr(label=label('profile extraction', [knob('stage3', 'profile_fit')]), style='rounded,dashed',
               color=VERM, fontcolor='#333333')
        step(m, 'joint', 'first pass: every source solved together with the\nsources whose light shares its pixels '
                         '(trace maps)', FILL['s3'], [knob('stage3', 'contam_predict')])
        data(m, 'pass1', 'first-pass spectrum of every source')
        step(m, 'redo', 'neighbor check of stage 2b redone with each neighbor\'s\nmodeled brightness; '
                        'sources whose background nods\nchange go through stage 2b and Spec3 again', FILL['s3'],
             [knob('stage3', 'slitlet_neighbor_brightness')])
        inp(m, 'psf', 'PSF table and trace-centre law\n(calibration stars)')
        step(m, 'shape', 'source shape: own fit above the detection gate,\nelse the PRISM shape (gratings), '
                         'else the default', FILL['s3'],
             [knob('stage3', 'profile_fit_shape'), knob('stage3', 'profile_fit_prism_transfer')])
        step(m, 'pfit', 'one spectrum fitted to every nod difference:\nGaussian galaxy x PSF, clipped by the shutters',
             FILL['s3'])
        step(m, 'contam', 'other sources\' light on the source\npredicted (fnu_contam, CONTAM2D)', FILL['s3'],
             [knob('stage3', 'contam_predict')])
        step(m, 'scan', 'search for uncataloged neighbors in the residuals;\nproposed background-nod changes, '
                        'applied by stage 2b\non its next run with scan_action = "auto_override"', FILL['s3'],
             [knob('stage3', 'scan_neighbours'), knob('stage3', 'scan_action')])
    step(c, 'mbs', 'master-background spectrum: calibrated cutouts minus their\nmaster background (_mbkg.fits), '
                   'in memory; bad pixels = Spec3\'s\noutlier flags + hot pixels; drizzled through the pixel map;\n'
                   'the same fit at the nodded shape, light through\nfailed-open shutters solved with the source',
         FILL['mb'], [knob('stage3', 'mbkg_spectrum')])
    step(c, 'best', 'best spectrum chosen (fnu)', FILL['s3'], [knob('stage3', 'contam_1d')])
    step(c, 'comb', '1-D combination of exposure groups', FILL['s3'], ['combine_method = "1d"  (default "'
                                                                       + CFG['stage3']['combine_method'] + '")'])
    data(c, 'spec', '_spec.fits: SPEC1D (fnu, fnu_opt, boxcars, fnu_prof, fnu_contam),\n'
                    '2-D spectrum, PROF2D, CONTAM2D; SPEC1D_MBKG and its 2-D')
g.edge('bsub', 'spec3')
g.edge('spec3', 's2d')
g.edge('s2d', 'opt')
g.edge('s2d', 'dmap')
g.edge('dmap', 'pfit')
g.edge('dmap', 'mbs', style='dashed', color=VERM, constraint='false')
g.edge('tmap', 'joint', style='dashed', color=VERM, constraint='false')
g.edge('bsub', 'joint', style='dashed', color=VERM)
g.edge('joint', 'pass1')
g.edge('pass1', 'redo')
g.edge('pass1', 'shape')
g.edge('shape', 'pfit')
ref('psf', 'joint')
ref('psf', 'pfit')
g.edge('bsub', 'pfit')
g.edge('pass1', 'contam')
g.edge('pfit', 'scan')
opt('shape', 'mbs')
g.edge('opt', 'best')
g.edge('pfit', 'best')
g.edge('contam', 'best')
opt('best', 'comb')
g.edge('best', 'spec')
opt('comb', 'spec')
opt('mbs', 'spec')

# --- redshift and line fitting ------------------------------------------------------------
with g.subgraph(name='cluster_z') as c:
    c.attr(label='Redshift and line fitting', style='rounded,filled', fillcolor='#fbfbfb', color='#888888',
           fontcolor='#555555')
    step(c, 'zfit', 'templates fitted over a grid of redshifts', FILL['z'])
    data(c, 'zf', '_zfit.fits')
    step(c, 'lines', 'line fluxes at the inspected redshift\n(cfpipe nirspec linefit, after inspection)', FILL['z'])
    data(c, 'lf', '_lines.fits')
g.edge('spec', 'zfit')
g.edge('zfit', 'zf')
g.edge('spec', 'lines')
g.edge('lines', 'lf')

# --- layout: keep the main path straight down the page ---------------------------------------
SPINE = ['uncal', 'det1', 'cfn', 'bkg1', 'bkg2d', 'mask', 'rate', 'spec2', 'canon', 'units', 'mb2', 'paint', 'mbkg',
         'nbr', 'pick', 'sub', 'bsub', 'spec3', 's2d', 'dmap', 'opt', 'best', 'spec', 'zfit']
for a, b in zip(SPINE[:-1], SPINE[1:]):
    g.edge(a, b, style='invis', weight='30')

# --- key ------------------------------------------------------------------------------
with g.subgraph(name='cluster_key') as c:
    c.attr(label='Key', style='rounded', color='#bbbbbb', fontcolor='#555555', rank='sink')
    step(c, 'k_step', 'processing step', '#ffffff')
    step(c, 'k_opt', 'step an option turns on or off', '#ffffff', ['option = default'])
    data(c, 'k_data', 'file or table written')
    inp(c, 'k_in', 'input read')
    c.edge('k_step', 'k_opt', style='invis')
    c.edge('k_opt', 'k_data', style='invis')
    c.edge('k_data', 'k_in', style='invis')
g.edge('lf', 'k_step', style='invis')

os.makedirs(OUT_DIRS[args.kind], exist_ok=True)
base = os.path.join(OUT_DIRS[args.kind], args.out)
g.render(base, format='svg', cleanup=True)
g.render(base, format='png', cleanup=True)
print(base + '.svg')
print(base + '.png')
