"""Reproduce the GP-vs-conventional striping docs figure.

Stages a public UNCOVER exposure from MAST with a bright elliptical whose
envelope spans amplifier rows, runs the unified ``bkg`` step twice — once
approximating the conventional treatment (``estimator="median"``,
conditioning detrend disabled), once with shipped defaults (GP + detrend) —
and renders a native-pixel-scale crop of the input frame beside the two
corrected frames, where the conventional arm's blocky over-subtraction
around the galaxy is visible by eye.

Frame selection notes (2026-08): on LW frames (F444W) the leak never rises
above the pixel noise unbinned — the sky is too bright — so the figure uses
the SW counterpart (F200W), where striping and the leak are prominent by
eye. All three nrcb1 dithers of this group show the artifact; dither 00001
shows it best.

Needs the campfire pipeline importable plus its science deps (jwst,
photutils, celerite2). No CRDS cache required.

    python reproduce.py [workdir]
"""
import os
import shutil
import sys

import numpy as np
import requests

ROOT = sys.argv[1] if len(sys.argv) > 1 else '.'
EXPOSURE = 'jw02561006002_07201_00001_nrcb1'  # A2744/UNCOVER F200W
MAST = 'https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:JWST/product/'
CROP = np.s_[0:1040, 210:1250]  # native px around the bright elliptical


def stage():
    cal = os.path.join(ROOT, f'{EXPOSURE}_cal.fits')
    if not os.path.exists(cal):
        r = requests.get(f'{MAST}{EXPOSURE}_cal.fits', timeout=600)
        r.raise_for_status()
        with open(cal, 'wb') as f:
            f.write(r.content)
    return cal


def run_arm(cal, name, estimator, detrend_enabled):
    from campfire_pipeline.config import load_config
    from campfire_pipeline.nircam.steps.bkg import bkg_step

    work = os.path.join(ROOT, f'arm_{name}.fits')
    shutil.copy(cal, work)
    cfg = dict(load_config()['nircam']['bkg'])
    cfg['plot'] = False
    cfg['striping'] = dict(cfg.get('striping', {}), estimator=estimator)
    cfg['detrend'] = dict(cfg.get('detrend', {}), enabled=detrend_enabled)
    comps = {}
    bkg_step(work, None, cfg, overwrite=False, status=None,
             components_out=comps)
    return comps


def plot(conv, default, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from astropy.stats import mad_std

    good = (~default['fitmask'].astype(bool)) & np.isfinite(default['before'])
    sig = mad_std(default['after'][good])
    med = np.median(default['before'][good])

    fig, axes = plt.subplots(1, 3, figsize=(16.8, 6.15))
    panels = [
        (default['before'].astype(float) - med,
         'before — flat-fielded exposure, stock pipeline\n'
         '(sky median removed for display)'),
        (conv['after'].astype(float),
         'after — plain per-amp-row & column medians\n'
         '(no conditioning; the conventional approach)'),
        (default['after'].astype(float),
         'after — CAMPFIRE bkg step\n'
         '(Gaussian process + conditioning detrend)'),
    ]
    x0 = CROP[1].start
    for ax, (img, title) in zip(axes, panels):
        ax.imshow(np.nan_to_num(img)[CROP], origin='lower', cmap='Greys',
                  vmin=-1.3 * sig, vmax=1.3 * sig, interpolation='nearest')
        ax.set_title(title, fontsize=10.5)
        for x in (512, 1024):
            ax.axvline(x - x0, color='k', lw=0.6, ls=':', alpha=0.6)
        ax.set_xticks([])
        ax.set_yticks([])
    axes[1].annotate('bright blocks & streaks: galaxy flux\n'
                     'absorbed into the row/column estimates,\n'
                     'subtracted from the surrounding sky',
                     xy=(800, 210), xycoords='data', annotation_clip=False,
                     xytext=(0.97, 0.64), textcoords='axes fraction',
                     ha='right', va='bottom', fontsize=9.5,
                     bbox=dict(boxstyle='round,pad=0.35', fc='white',
                               ec='0.4', alpha=0.9),
                     arrowprops=dict(arrowstyle='->', lw=1.4, color='0.1',
                                     shrinkB=4,
                                     connectionstyle='arc3,rad=0.12'))
    fig.suptitle('Striping residuals around bright extended sources — '
                 'jw02561006002_07201_00001 NRCB1 '
                 '(Abell 2744 / UNCOVER, F200W)', fontsize=12.5, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out, dpi=180)


if __name__ == '__main__':
    cal = stage()
    conv = run_arm(cal, 'conventional', 'median', False)
    default = run_arm(cal, 'default', 'gp', True)
    plot(conv, default, os.path.join(ROOT, 'gp_vs_median.png'))
