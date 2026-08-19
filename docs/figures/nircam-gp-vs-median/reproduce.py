"""Reproduce the GP-vs-median 1/f docs figure.

Stages a public UNCOVER exposure from MAST whose bright galaxy group spans
amplifier rows, runs the unified ``bkg`` step twice — once approximating
the conventional treatment (``estimator="median"``, conditioning detrend
disabled), once with shipped defaults (GP + detrend) — and renders the
input frame beside the two removed 1/f models (``h + vcol`` ledgers) at a
common stretch.

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
EXPOSURE = 'jw02561006002_07201_00001_nrcblong'  # A2744/UNCOVER F444W
MAST = 'https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:JWST/product/'
GALAXY = (1408, 1068)  # x, y of the bright group (detector px)


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

    work = os.path.join(ROOT, f'ab_{name}.fits')
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
    from astropy.nddata import block_reduce
    from astropy.stats import mad_std

    before = default['before'].astype(float)
    good = (~default['fitmask'].astype(bool)) & np.isfinite(before)
    sky_med = np.median(before[good])
    sig = mad_std(default['after'][good])

    def oneoverf(c):
        h = c['h'].astype(float) + c['vcol'].astype(float)
        return h - np.median(h[np.isfinite(h)])

    def ds(a):
        return block_reduce(np.nan_to_num(a), 2, np.mean)

    fig, axes = plt.subplots(1, 3, figsize=(16.5, 6.2))
    axes[0].imshow(ds(before) - sky_med, origin='lower', cmap='Greys',
                   vmin=-2 * sig, vmax=6 * sig, interpolation='nearest')
    axes[0].set_title('input frame — bright galaxy group with an\n'
                      'extended envelope spanning amplifier rows',
                      fontsize=11)
    v = 0.008
    panels = [
        (axes[1], oneoverf(conv),
         'removed 1/f model — plain per-amp-row median\n'
         '(no conditioning; the conventional approach)'),
        (axes[2], oneoverf(default),
         'removed 1/f model — CAMPFIRE default\n'
         '(Gaussian process + conditioning detrend)'),
    ]
    for ax, h, title in panels:
        ax.imshow(ds(h), origin='lower', cmap='RdBu_r', vmin=-v, vmax=v,
                  interpolation='nearest')
        ax.set_title(title, fontsize=11)
        for x in (512, 1024, 1536):
            ax.axvline(x / 2, color='k', lw=0.5, ls=':', alpha=0.6)
    gx, gy = GALAXY
    for ax in axes:
        ax.add_patch(plt.Circle((gx / 2, gy / 2), 140, fill=False,
                                color='k' if ax is axes[0] else '0.2',
                                lw=1.0, ls='--', alpha=0.8))
        ax.set_xticks([])
        ax.set_yticks([])
    axes[1].annotate('galaxy flux absorbed into the\namp-row estimate, '
                     'broadcast\nacross the full amplifier',
                     xy=(1290 / 2, 1150 / 2), xytext=(660 / 2, 1730 / 2),
                     fontsize=9.5,
                     arrowprops=dict(arrowstyle='->', lw=1.0, color='0.15'))
    fig.suptitle('Why a Gaussian-process 1/f model — '
                 'jw02561006002_07201_00001 NRCBLONG '
                 '(Abell 2744 / UNCOVER, F444W)', fontsize=13, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out, dpi=180)


if __name__ == '__main__':
    cal = stage()
    conv = run_arm(cal, 'conventional', 'median', False)
    default = run_arm(cal, 'default', 'gp', True)
    plot(conv, default, os.path.join(ROOT, 'gp_vs_median.png'))
