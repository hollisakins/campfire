"""Reproduce the NIRCam bkg-step before/after docs figure.

Stages one public COSMOS-Web exposure from MAST (a stock-pipeline
``_cal.fits``, i.e. exactly the state the CAMPFIRE ``bkg`` step consumes:
flat-fielded and flux-calibrated, with sky, amp pedestals, and 1/f noise
all still present), runs the unified ``bkg`` step on it with the shipped
default config, and renders a before / removed-model / after panel figure.

Needs the campfire pipeline importable (installed, or on PYTHONPATH) plus
its science deps (jwst, photutils, celerite2). No CRDS cache required —
the bkg step is pure numerics on the cal file.

    python reproduce.py [workdir]
"""
import os
import shutil
import sys

import numpy as np
import requests

ROOT = sys.argv[1] if len(sys.argv) > 1 else '.'
EXPOSURE = 'jw01727167001_02101_00001_nrca1'  # COSMOS-Web F150W, no wisps
MAST = 'https://mast.stsci.edu/api/v0.1/Download/file?uri=mast:JWST/product/'


def stage():
    cal = os.path.join(ROOT, f'{EXPOSURE}_cal.fits')
    if not os.path.exists(cal):
        r = requests.get(f'{MAST}{EXPOSURE}_cal.fits', timeout=600)
        r.raise_for_status()
        with open(cal, 'wb') as f:
            f.write(r.content)
    work = os.path.join(ROOT, f'{EXPOSURE}_bkgrun.fits')
    shutil.copy(cal, work)
    return work


def run_bkg(work):
    from campfire_pipeline.config import load_config
    from campfire_pipeline.nircam.steps.bkg import bkg_step

    cfg = dict(load_config()['nircam']['bkg'])
    cfg['plot'] = False
    comps = {}
    bkg_step(work, None, cfg, overwrite=False, status=None,
             components_out=comps)
    return comps


def plot(comps, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from astropy.nddata import block_reduce
    from astropy.stats import mad_std

    before = comps['before'].astype(np.float64)
    after = comps['after'].astype(np.float64)
    fitmask = comps['fitmask'].astype(bool)
    good = ~fitmask & np.isfinite(before)

    sky_med = np.median(before[good])
    sig = mad_std(after[good])
    model = before - after
    model_struct = model - np.median(model[good])
    model_struct[~np.isfinite(model_struct)] = 0.0
    msig = mad_std(model_struct[good])

    def ds(a):
        return block_reduce(a, 2, func=np.nanmean)

    fig, axes = plt.subplots(1, 3, figsize=(16.5, 6.2))
    panels = [
        (ds(before) - sky_med, 2 * sig,
         'before — flat-fielded exposure, stock pipeline\n'
         f'(sky median of {sky_med:.2f} MJy/sr removed for display)'),
        (ds(model_struct), 4 * msig,
         'removed background model (structure only, tighter stretch)\n'
         'amp pedestals + smooth sky + column & row 1/f'),
        (ds(after), 2 * sig,
         'after — CAMPFIRE bkg step\n(same stretch as left panel)'),
    ]
    for ax, (img, v, title) in zip(axes, panels):
        ax.imshow(img, origin='lower', cmap='Greys', vmin=-v, vmax=v,
                  interpolation='nearest')
        ax.set_title(title, fontsize=11)
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle('CAMPFIRE NIRCam background & 1/f subtraction — '
                 'jw01727167001_02101_00001 NRCA1 (COSMOS-Web, F150W)',
                 fontsize=13, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out, dpi=180)


if __name__ == '__main__':
    work = stage()
    comps = run_bkg(work)
    plot(comps, os.path.join(ROOT, 'bkg_before_after.png'))
