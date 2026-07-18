#!/usr/bin/env python
"""Per-component decomposition of the bkg step on one real exposure.

Renders: before | source mask | after (matched stretch), each accumulated
correction component (pedestal, column 1/f, amp-row 1/f, applied 2-D,
fit-only conditioning detrend) on a common symmetric stretch, and — the
diagnostic panel — PER-AMP ROW PROFILES of the h term and of the masked
output residual. A top-vs-bottom antisymmetric error within an amp is
invisible to column-collapsed metrics; these profiles show it directly.

    python inspect_components.py --pristine /tmp/rj0911_prebkg \\
        --exposure jw06882025001_04101_00004_nrcblong --out out_comp \\
        [--estimator gp] [--ped-scope frame]
"""
import argparse
import os
import shutil
import tempfile

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.stats import mad_std

from run_real_sweep import load_bkg_defaults

AMPS = {'A': (4, 512), 'B': (512, 1024), 'C': (1024, 1536),
        'D': (1536, 2044)}


def run_with_components(pristine, estimator, ped_scope, subtract_2d,
                        detrend_box=None, reject=True):
    from campfire_pipeline.nircam.steps.bkg import bkg_step
    defaults = load_bkg_defaults()
    det = dict(defaults.get('detrend', {}))
    if detrend_box:
        det['box_size'] = detrend_box * 2      # native -> SW-scale config
    sc = {
        'n_iterations': defaults.get('n_iterations', 3),
        'plot': False,
        'mask': dict(defaults['mask']),
        'pedestal': {**defaults['pedestal'], 'scope': ped_scope},
        'striping': {**defaults['striping'], 'estimator': estimator},
        'variance': dict(defaults['variance']),
        'detrend': det,
        'subtract_2d': subtract_2d,
        'bkg2d': {**defaults['bkg2d'], 'reject': reject},
    }
    comps = {}
    tmpd = tempfile.mkdtemp()
    work = os.path.join(tmpd, os.path.basename(pristine))
    try:
        shutil.copyfile(pristine, work)
        bkg_step(work, None, sc, overwrite=True, components_out=comps)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    return comps


def _smooth(y, k=15):
    pad = np.pad(y, k // 2, mode='edge')
    return np.array([np.nanmedian(pad[j:j + k]) for j in range(len(y))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pristine', required=True)
    ap.add_argument('--exposure', required=True)
    ap.add_argument('--out', default='out_comp')
    ap.add_argument('--estimator', default='gp',
                    choices=['gp', 'median', 'none'])
    ap.add_argument('--ped-scope', default='frame',
                    choices=['auto', 'per_amp', 'frame'])
    ap.add_argument('--no-subtract-2d', action='store_true')
    ap.add_argument('--no-reject', action='store_true')
    ap.add_argument('--detrend-box', type=int, default=None,
                    help='detrend box size in NATIVE px (default: config)')
    ap.add_argument('--tag', default='')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    pristine = os.path.join(args.pristine, f'{args.exposure}.fits')
    comps = run_with_components(pristine, args.estimator, args.ped_scope,
                                not args.no_subtract_2d,
                                detrend_box=args.detrend_box,
                                reject=not args.no_reject)

    before = comps['before'].astype(np.float64)
    after = comps['after'].astype(np.float64)
    bad = comps['fitmask'] | ~np.isfinite(after) | (before == 0)
    resid_m = np.where(bad, np.nan, after)
    sig = mad_std(resid_m, ignore_nan=True)
    med_b = np.nanmedian(np.where(bad, np.nan, before))

    fig = plt.figure(figsize=(19, 13))
    gs = fig.add_gridspec(3, 5, height_ratios=[1.15, 1.15, 0.9],
                          hspace=0.25, wspace=0.12)
    ds = 4

    def imshow(ax, img, title, vmin, vmax, cmap='RdBu_r'):
        im = ax.imshow(img[::ds, ::ds], vmin=vmin, vmax=vmax, cmap=cmap,
                       origin='lower')
        for b in (512, 1024, 1536):
            ax.axvline(b / ds, color='k', lw=0.3, alpha=0.5)
        ax.set_title(title, fontsize=10)
        ax.set_xticks([]), ax.set_yticks([])
        fig.colorbar(im, ax=ax, fraction=0.046)

    # row 1: before / mask / after / (spare: b2d+det sum) / total correction
    imshow(fig.add_subplot(gs[0, 0]), before - med_b,
           'before − median', -2 * sig, 10 * sig, 'gray_r')
    ax = fig.add_subplot(gs[0, 1])
    ax.imshow(comps['srcmask'][::ds, ::ds], cmap='gray_r', origin='lower')
    ax.set_title('source mask', fontsize=10)
    ax.set_xticks([]), ax.set_yticks([])
    imshow(fig.add_subplot(gs[0, 2]), after,
           'after', -2 * sig, 10 * sig, 'gray_r')
    total = comps['ped'] + comps['vcol'] + comps['h'] + comps['b2d']
    imshow(fig.add_subplot(gs[0, 3]), total - np.nanmedian(total),
           'total correction − median', -3 * sig, 3 * sig)
    imshow(fig.add_subplot(gs[0, 4]), np.where(bad, np.nan, after),
           'after (masked)', -2 * sig, 2 * sig)

    # row 2: each component, common symmetric stretch, median removed
    for i, (key, label) in enumerate([
            ('ped', 'pedestal (accum)'), ('vcol', 'column 1/f (accum)'),
            ('h', 'amp-row 1/f h (accum)'), ('b2d', 'applied 2-D (accum)'),
            ('det_struct', 'conditioning detrend (FIT-ONLY, last)')]):
        img = comps[key].astype(np.float64)
        imshow(fig.add_subplot(gs[1, i]), img - np.nanmedian(img),
               label, -3 * sig, 3 * sig)

    # row 3: per-amp row profiles — h term and masked output residual
    axh = fig.add_subplot(gs[2, 0:2])
    axr = fig.add_subplot(gs[2, 2:4])
    axd = fig.add_subplot(gs[2, 4])
    rows = np.arange(before.shape[0])
    for amp, (c0, c1) in AMPS.items():
        hprof = comps['h'][:, (c0 + c1) // 2] / sig   # h const across amp
        axh.plot(rows, hprof, lw=0.8, label=amp)
        rprof = _smooth(np.nanmedian(resid_m[:, c0:c1], axis=1)) / sig
        axr.plot(rows, rprof, lw=1.0, label=amp)
        dprof = _smooth(
            np.nanmedian(np.where(bad, np.nan,
                                  comps['det_struct'])[:, c0:c1],
                         axis=1)) / sig
        axd.plot(rows, dprof, lw=1.0, label=amp)
    for ax, t in ((axh, 'accumulated h(row) per amp [sigma]'),
                  (axr, 'masked AFTER residual: per-amp row medians [sigma]'),
                  (axd, 'detrend row-collapse per amp [sigma]')):
        ax.axhline(0, color='gray', lw=0.5)
        ax.set_title(t, fontsize=10)
        ax.set_xlabel('detector row')
        ax.legend(fontsize=8, ncol=4)

    fig.suptitle(f'{args.exposure}{args.tag}: bkg components '
                 f'(estimator={args.estimator}, ped={args.ped_scope}, '
                 f'reject={not args.no_reject}, '
                 f'detrend_box={args.detrend_box or "cfg"}, '
                 f'sig={sig:.4f} MJy/sr)', fontsize=13)
    path = os.path.join(args.out,
                        f'{args.exposure}{args.tag}_components.png')
    fig.savefig(path, dpi=110, bbox_inches='tight')
    print(f'wrote {path}')


if __name__ == '__main__':
    main()
