#!/usr/bin/env python
"""Prototype: region-aware dilation of the OUTLIER DQ mask.

Detector-fixed artifacts (scattered-light arcs) move on-sky between dithers,
so outlier detection flags their bright cores — but the wings survive below
threshold and drizzle into the mosaic. Idea: find connected components of
the OUTLIER mask, and dilate only components larger than ``min_area``
(cosmic-ray hits stay untouched), flagging the grown region DO_NOT_USE.

This script measures the component-size distribution on real work-tree
exposures and visualizes original vs grown mask over the science image.

    python proto_outlier_dilate.py --exposure jw..._00004_nrcblong \
        [--min-area 100] [--radius 12] [--filter f444w]
"""
import argparse
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.io import fits
from astropy.stats import mad_std
from scipy.ndimage import distance_transform_edt, label

WORK = '/Users/hollis/campfire-data/products/nircam_work/rj0911'


def _large_components(outlier_mask, min_area):
    lab, nlab = label(outlier_mask)
    if nlab == 0:
        return np.zeros_like(outlier_mask), 0, 0
    areas = np.bincount(lab.ravel())[1:]          # per-component area
    large_ids = np.flatnonzero(areas >= min_area) + 1
    large = (np.isin(lab, large_ids) if large_ids.size
             else np.zeros_like(outlier_mask))
    return large, int(large_ids.size), int(nlab - large_ids.size)


def grow_large_regions(outlier_mask, min_area, radius):
    """Mechanism A: fixed-radius EDT dilation of large components."""
    large, n_large, n_small = _large_components(outlier_mask, min_area)
    if not n_large:
        return np.zeros_like(outlier_mask), n_large, n_small
    grown = distance_transform_edt(~large) <= radius
    return grown, n_large, n_small


def waterfall_large_regions(sci, outlier_mask, min_area, expand_nsigma=1.0,
                            smooth=2.0, skirt=2.0):
    """Mechanism B: hysteresis ("waterfall") expansion of large components.

    Flood outward from the large flagged components through connected
    pixels of the (lightly smoothed) science residual above
    ``expand_nsigma`` * sigma — the mask follows the artifact's actual
    morphology and stops at the background. A small ``skirt`` dilation
    covers the final sub-threshold edge. Requires a flat background (the
    bkg chain provides it), since the threshold is global.
    """
    from scipy.ndimage import binary_propagation, gaussian_filter

    large, n_large, n_small = _large_components(outlier_mask, min_area)
    if not n_large:
        return np.zeros_like(outlier_mask), n_large, n_small
    work = gaussian_filter(np.nan_to_num(sci - np.nanmedian(sci)), smooth)
    sig = mad_std(work, ignore_nan=True)
    above = work > expand_nsigma * sig
    expanded = binary_propagation(large, mask=above | large)
    if skirt > 0:
        expanded = distance_transform_edt(~expanded) <= skirt
    return expanded, n_large, n_small


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exposure', required=True)
    ap.add_argument('--filter', default='f444w')
    ap.add_argument('--min-area', type=int, default=100)
    ap.add_argument('--radius', type=float, default=12)
    ap.add_argument('--expand-nsigma', type=float, default=1.0)
    ap.add_argument('--out', default='out_outlier_proto')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    path = os.path.join(WORK, args.filter, f'{args.exposure}.fits')
    with fits.open(path) as h:
        sci = h['SCI'].data.astype(np.float64)
        dq = h['DQ'].data
    from jwst.datamodels.dqflags import pixel as pf
    outlier = (dq & pf['OUTLIER']) != 0

    lab, nlab = label(outlier)
    areas = np.bincount(lab.ravel())[1:]
    print(f'{args.exposure}: {outlier.sum()} OUTLIER px in {nlab} components')
    for thresh in (10, 50, 100, 300, 1000):
        n = (areas >= thresh).sum()
        px = areas[areas >= thresh].sum()
        print(f'  components >= {thresh:5d} px: {n:5d}  '
              f'({px} px, {100 * px / max(1, outlier.sum()):.1f}% of flagged)')

    grown, n_large, n_small = grow_large_regions(outlier, args.min_area,
                                                 args.radius)
    added = grown & ~outlier
    print(f'DILATE  min_area={args.min_area}, radius={args.radius}: '
          f'{n_large} large + {n_small} small components; '
          f'mask {outlier.sum()} -> {(outlier | grown).sum()} px '
          f'(+{added.sum()}, {100 * (outlier | grown).mean():.2f}% of frame)')

    wgrown, _, _ = waterfall_large_regions(sci, outlier, args.min_area,
                                           expand_nsigma=args.expand_nsigma)
    wadded = wgrown & ~outlier
    print(f'WATERFALL expand_nsigma={args.expand_nsigma}: '
          f'mask {outlier.sum()} -> {(outlier | wgrown).sum()} px '
          f'(+{wadded.sum()}, '
          f'{100 * (outlier | wgrown).mean():.2f}% of frame)')

    sig = mad_std(sci, ignore_nan=True)
    med = np.nanmedian(sci)
    fig, axes = plt.subplots(1, 4, figsize=(24, 6.4))
    ds = 2
    for ax, title in zip(axes, ('science', 'current OUTLIER mask',
                                f'A: dilate (min_area={args.min_area}, '
                                f'r={args.radius})',
                                f'B: waterfall (expand at '
                                f'{args.expand_nsigma}σ)')):
        ax.imshow(sci[::ds, ::ds] - med, vmin=-1.5 * sig, vmax=6 * sig,
                  cmap='gray_r', origin='lower')
        ax.set_title(title, fontsize=11)
        ax.set_xticks([]), ax.set_yticks([])
    axes[1].contourf(outlier[::ds, ::ds], levels=[0.5, 2], colors='red',
                     alpha=0.45)
    axes[2].contourf(added[::ds, ::ds], levels=[0.5, 2], colors='orange',
                     alpha=0.45)
    axes[2].contourf(outlier[::ds, ::ds], levels=[0.5, 2], colors='red',
                     alpha=0.45)
    axes[3].contourf(wadded[::ds, ::ds], levels=[0.5, 2], colors='deepskyblue',
                     alpha=0.5)
    axes[3].contourf(outlier[::ds, ::ds], levels=[0.5, 2], colors='red',
                     alpha=0.45)
    fig.suptitle(f'{args.exposure} ({args.filter}): outlier region-dilation '
                 f'prototype', fontsize=13)
    fig.tight_layout()
    out = os.path.join(args.out,
                       f'{args.exposure}_a{args.min_area}_r{args.radius:g}.png')
    fig.savefig(out, dpi=110, bbox_inches='tight')
    print(f'wrote {out}')


if __name__ == '__main__':
    main()
