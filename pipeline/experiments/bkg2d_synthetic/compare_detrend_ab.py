#!/usr/bin/env python
"""Detrend A/B figure from run_real_sweep --detrend-ab outputs.

Per exposure: masked-residual maps for each arm (matched stretch) over a
shared column-median profile panel — the amp-seam signature read directly
off the data. Usage:

    python compare_detrend_ab.py --out out_real2 \\
        --exposure jw06882025001_04101_00004_nrcblong \\
        --arms gp_only_nodet,gp_only_det,b64_d20_rej_nodet,b64_d20_rej_det
"""
import argparse
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.stats import mad_std

AMP_BOUNDS = (512, 1024, 1536)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', required=True)
    ap.add_argument('--exposure', required=True)
    ap.add_argument('--arms', required=True)
    args = ap.parse_args()
    arms = args.arms.split(',')

    resids = {}
    for arm in arms:
        path = os.path.join(args.out, f'{args.exposure}-{arm}_resid.npz')
        resids[arm] = np.load(path)['resid'].astype(np.float64)

    sig = np.nanmedian([mad_std(r, ignore_nan=True)
                        for r in resids.values()])
    n = len(arms)
    fig = plt.figure(figsize=(4.6 * n, 9))
    gs = fig.add_gridspec(2, n, height_ratios=[2.2, 1], hspace=0.18)

    ds = 4
    for i, arm in enumerate(arms):
        ax = fig.add_subplot(gs[0, i])
        im = ax.imshow(resids[arm][::ds, ::ds], vmin=-2 * sig, vmax=2 * sig,
                       cmap='RdBu_r', origin='lower')
        for b in AMP_BOUNDS:
            ax.axvline(b / ds, color='k', lw=0.3, alpha=0.5)
        ax.set_title(arm, fontsize=11)
        ax.set_xticks([]), ax.set_yticks([])
    fig.colorbar(im, ax=fig.axes, fraction=0.012, pad=0.01,
                 label=f'masked residual [MJy/sr], +-2sig, sig={sig:.4f}')

    axp = fig.add_subplot(gs[1, :])
    x = np.arange(next(iter(resids.values())).shape[1])
    for arm in arms:
        with np.errstate(all='ignore'):
            prof = np.nanmedian(resids[arm], axis=0) / sig
        # light smoothing for display only (25-px running median)
        k = 25
        pad = np.pad(prof, k // 2, mode='edge')
        smooth = np.array([np.nanmedian(pad[j:j + k])
                           for j in range(len(prof))])
        axp.plot(x, smooth, lw=1.2, label=arm)
    for b in AMP_BOUNDS:
        axp.axvline(b, color='k', lw=0.5, ls=':')
    axp.axhline(0, color='gray', lw=0.5)
    axp.set_xlabel('detector column [px]')
    axp.set_ylabel('column median [sigma]')
    axp.set_xlim(0, len(x))
    axp.legend(fontsize=9, ncol=len(arms))
    axp.set_title('column-median profile of the masked residual '
                  '(dotted = amp boundaries)', fontsize=10)

    fig.suptitle(f'{args.exposure}: conditioning-detrend A/B', fontsize=13)
    path = os.path.join(args.out, f'{args.exposure}_detrend_ab.png')
    fig.savefig(path, dpi=110, bbox_inches='tight')
    print(f'wrote {path}')


if __name__ == '__main__':
    main()
