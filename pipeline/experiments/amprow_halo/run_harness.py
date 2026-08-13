#!/usr/bin/env python
"""Eye-first synthetic harness for the amp-row halo-oversubtraction artifact.

Builds a 'brightfield' scene (a few very bright extended galaxies — some
deliberately spanning amp boundaries — many faint ones, a smooth-but-complex
2-D background, injected per-amp 1/f + DC + column stripes), runs the REAL
``bkg_step`` with ``subtract_2d = true`` per mitigation arm, and renders PNGs.

THE METRIC IS THE EYE. This harness deliberately produces no summary tables:
success is judged by looking at the PNGs — primarily ``{arm}_error.png``
(correction error, diverging colormap, sources whited out: the amp-blocky
row-wise artifacts show as red/blue blocks with hard edges at cols
512/1024/1536, like the real-frame screenshots) and ``{arm}_after.png``
(the corrected frame at a real-image stretch). ``compare_error.png`` /
``compare_after.png`` put all arms side by side. Do not replace this with
derived statistics when evaluating mitigations.

Arms are config-override dicts (deep-merged over the shipped
``[nircam.bkg]`` defaults + ``subtract_2d = true``) — edit ``ARMS`` to add
levers. Shipped arms:

    baseline    shipped defaults (the artifact reproduction)
    strp_d40 /
    strp_d80 /
    strp_d150   [nircam.bkg.striping].extra_dilate = 40/80/150 — grow the
                source tiers for the 1/f fit mask only, pushing the amp-row
                anchors off the halos (the GP bridges the gap)
    ideal_1f    truth arm: the injected stripes+DC are subtracted exactly and
                only the 2-D/pedestal machinery runs (estimator='none') — the
                best any 1/f estimator could do; the eye's reference point

Usage (campfire conda env, from this directory):

    python run_harness.py --out out            # full 2048^2, ~2 min/arm
    python run_harness.py --out out --quick    # 1024-row smoke
    python run_harness.py --out out --arms baseline,strp_d80
"""

import argparse
import copy
import os
import shutil
import sys
import time
import tomllib

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.io import fits

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'bkg2d_synthetic'))
from scene import make_scene  # noqa: E402

# ---------------------------------------------------------------------------
# Arms: name -> deep-merged overrides on the [nircam.bkg] defaults.
# 'ideal_1f' is special-cased (truth subtraction, estimator='none').
# ---------------------------------------------------------------------------
ARMS = {
    'baseline': {},
    # global growth — kept as the cautionary reference: growing every tier
    # starves the anchors frame-wide and injects row/column noise
    'strp_d40': {'striping': {'extra_dilate': 40}},
    'strp_d80': {'striping': {'extra_dilate': 80}},
    'strp_d150': {'striping': {'extra_dilate': 150}},
    # selective growth: only footprints >= min_area px^2 (the few bright
    # galaxies) are grown; faint-source anchors untouched
    'sel_d80': {'striping': {'extra_dilate': 80,
                             'extra_dilate_min_area': 10000}},
    'sel_d150': {'striping': {'extra_dilate': 150,
                              'extra_dilate_min_area': 10000}},
    # selective growth + anchor floor: starved rows (< 50 px) become true
    # GP gaps instead of overconfident anchors
    'sel_d80_floor': {'striping': {'extra_dilate': 80,
                                   'extra_dilate_min_area': 10000,
                                   'gp': {'min_row_pixels': 50}}},
    'ideal_1f': {'striping': {'estimator': 'none'}},
}
DEFAULT_ARMS = ['baseline', 'sel_d80', 'sel_d150', 'sel_d80_floor',
                'ideal_1f']


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_bkg_defaults():
    import campfire_pipeline
    path = os.path.join(os.path.dirname(campfire_pipeline.__file__),
                        'data', 'config_default.toml')
    with open(path, 'rb') as f:
        cfg = tomllib.load(f)
    return cfg['nircam']['bkg']


def save_model(scene, path, remove_truth=None):
    from jwst.datamodels import ImageModel
    m = ImageModel()
    img = scene.image.astype(np.float64)
    if remove_truth is not None:
        img = img - remove_truth
        img[~scene.valid] = 0.0
    m.data = img.astype(np.float32)
    m.err = scene.err
    m.dq = np.where(scene.valid, 0, 1).astype(np.uint32)
    m.var_rnoise = np.full(scene.shape, (0.6 * scene.sigma) ** 2, np.float32)
    m.var_poisson = np.full(scene.shape, (0.2 * scene.sigma) ** 2, np.float32)
    m.var_flat = np.full(scene.shape, (0.1 * scene.sigma) ** 2, np.float32)
    m.meta.instrument.name = 'NIRCAM'
    m.meta.instrument.channel = 'SHORT'
    m.meta.instrument.detector = 'NRCA1'
    m.save(path)


# ---------------------------------------------------------------------------
# Plotting — stretches chosen to mimic how the artifact is diagnosed on real
# frames (grayscale asinh for images, diverging map with whited-out sources
# for the error), all panels across arms on IDENTICAL stretches.
# ---------------------------------------------------------------------------
DS = 2          # display downsample
GRAY_A = 2.0    # asinh softening, in sigma
GRAY_LO, GRAY_HI = -1.5, 3.5    # asinh display range
ERR_SPAN = 1.5  # diverging error map span, in sigma


def _gray(img, med, sigma):
    return np.arcsinh((img - med) / (GRAY_A * sigma))


def plot_single_gray(path, img, sigma, title):
    med = float(np.median(img))
    disp = _gray(img, med, sigma)[::DS, ::DS]
    n = disp.shape[0] / 100
    fig, ax = plt.subplots(figsize=(disp.shape[1] / 100, n), dpi=100)
    ax.imshow(disp, vmin=GRAY_LO, vmax=GRAY_HI, cmap='gray', origin='lower',
              interpolation='nearest')
    ax.set_axis_off()
    ax.set_position([0, 0, 1, 1])
    ax.text(0.01, 0.99, title, transform=ax.transAxes, va='top', ha='left',
            color='yellow', fontsize=11)
    fig.savefig(path)
    plt.close(fig)


def plot_single_error(path, err_map, srcmask, sigma, title):
    disp = np.where(srcmask, np.nan, err_map)
    disp = disp - np.nanmedian(disp)
    disp = disp[::DS, ::DS]
    fig, ax = plt.subplots(figsize=(disp.shape[1] / 100,
                                    disp.shape[0] / 100), dpi=100)
    cmap = plt.get_cmap('RdBu_r').copy()
    cmap.set_bad('white')
    ax.imshow(disp, vmin=-ERR_SPAN * sigma, vmax=ERR_SPAN * sigma,
              cmap=cmap, origin='lower', interpolation='nearest')
    ax.set_axis_off()
    ax.set_position([0, 0, 1, 1])
    ax.text(0.01, 0.99, title, transform=ax.transAxes, va='top', ha='left',
            color='black', fontsize=11)
    fig.savefig(path)
    plt.close(fig)


def plot_compare(path, rows, sigma, kind):
    """rows: list of (title, image, srcmask-or-None)."""
    n = len(rows)
    ncol = 2
    nrow = (n + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(7.2 * ncol, 7.2 * nrow),
                             dpi=110)
    axes = np.atleast_1d(axes).ravel()
    for ax in axes[n:]:
        ax.set_axis_off()
    for ax, (title, img, srcmask) in zip(axes, rows):
        if kind == 'gray':
            med = float(np.median(img))
            ax.imshow(_gray(img, med, sigma)[::DS, ::DS], vmin=GRAY_LO,
                      vmax=GRAY_HI, cmap='gray', origin='lower',
                      interpolation='nearest')
        else:
            disp = np.where(srcmask, np.nan, img) if srcmask is not None \
                else img
            disp = disp - np.nanmedian(disp)
            cmap = plt.get_cmap('RdBu_r').copy()
            cmap.set_bad('white')
            ax.imshow(disp[::DS, ::DS], vmin=-ERR_SPAN * sigma,
                      vmax=ERR_SPAN * sigma, cmap=cmap, origin='lower',
                      interpolation='nearest')
        ax.set_title(title, fontsize=11)
        ax.set_xticks([]), ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def amprow_ledger_error(h, stripes):
    """Fitted amp-row profile minus the injected truth banding, per amp,
    both zero-DC per amp — the isolator for row-wise MISattribution (halo
    theft, noise-following). Zero everywhere = perfect amp-row estimate;
    the real banding is removed from view by construction."""
    err = np.zeros_like(h)
    W = h.shape[1]
    for c0 in range(0, W, 512):
        c1 = min(c0 + 512, W)
        hprof = np.median(h[:, c0:c1], axis=1)
        tprof = np.median(stripes[:, c0:c1], axis=1)
        hprof = hprof - np.median(hprof)
        tprof = tprof - np.median(tprof)
        err[:, c0:c1] = (hprof - tprof)[:, None]
    return err


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default='out')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--arms', default=','.join(DEFAULT_ARMS),
                    help='comma list from: ' + ', '.join(ARMS))
    ap.add_argument('--n-gal', type=int, default=450)
    ap.add_argument('--n-bright', type=int, default=5)
    ap.add_argument('--halo-peak', type=float, default=2.0,
                    help='bright-galaxy halo envelope peak, in sigma_pix')
    ap.add_argument('--sky-patch-amp', type=float, default=0.10,
                    help='complex smooth background amplitude, in sky units')
    ap.add_argument('--quick', action='store_true',
                    help='1024-row frame, fewer galaxies')
    ap.add_argument('--keep-fits', action='store_true')
    ap.add_argument('--save-arrays', action='store_true',
                    help='save per-arm float32 npz (after/error/h/b2d/mask)')
    args = ap.parse_args()

    shape = (1024, 2048) if args.quick else (2048, 2048)
    n_gal = min(args.n_gal, 200) if args.quick else args.n_gal

    os.makedirs(args.out, exist_ok=True)
    workdir = os.path.join(args.out, 'work')
    os.makedirs(workdir, exist_ok=True)

    print(f'== building brightfield scene seed={args.seed} '
          f'{shape[0]}x{shape[1]} ({n_gal} faint + {args.n_bright} bright)',
          flush=True)
    scene = make_scene(preset='brightfield', shape=shape, channel='sw',
                       seed=args.seed, n_gal=n_gal, n_bright=args.n_bright,
                       bright_halo_peak_sigma=args.halo_peak,
                       sky_patch_amp=args.sky_patch_amp,
                       inject_1f=True)
    sig = scene.sigma
    image = scene.image.astype(np.float64)
    # everything the step is ALLOWED to remove: sky (incl. complex patches),
    # injected detector systematics, and the halo/ICL plane (accepted loss
    # under subtract_2d). The error map charges only what is removed BEYOND
    # this (galaxy flux / noise structure) or missed from it.
    removable = scene.sky + scene.stripes + scene.icl
    target = image - removable          # galaxies + noise (the eye's goal)

    pristine = os.path.join(workdir, f'scene_s{args.seed}.fits')
    save_model(scene, pristine)

    plot_single_gray(os.path.join(args.out, 'input.png'), image, sig,
                     'input frame (sky + stripes + halos + galaxies)')
    plot_single_gray(os.path.join(args.out, 'target.png'), target, sig,
                     'target: galaxies + noise (all removable removed)')

    defaults = load_bkg_defaults()
    from campfire_pipeline.nircam.steps.bkg import bkg_step

    arm_names = [a for a in args.arms.split(',') if a]
    unknown = [a for a in arm_names if a not in ARMS]
    if unknown:
        ap.error(f'unknown arms: {unknown}')

    gray_rows = [('input', image, None)]
    err_rows = []
    hrow_rows = []
    for name in arm_names:
        print(f'-- arm {name}', flush=True)
        sc = deep_merge(defaults, ARMS[name])
        sc['subtract_2d'] = True
        sc['plot'] = False

        workfile = os.path.join(workdir, 'run.fits')
        if name == 'ideal_1f':
            # truth arm: hand the step a frame whose detector systematics
            # are already exactly removed; run mask/pedestal/b2d only
            save_model(scene, workfile, remove_truth=scene.stripes)
        else:
            shutil.copyfile(pristine, workfile)

        comp = {}
        t0 = time.time()
        bkg_step(workfile, None, sc, overwrite=True, components_out=comp)
        print(f'   bkg_step {time.time() - t0:.0f}s', flush=True)

        with fits.open(workfile) as hdul:
            after = hdul['SCI'].data.astype(np.float64)
            srcmask = hdul['SRCMASK'].data.astype(bool)
        # ideal_1f's input had the stripes pre-removed, so its error map is
        # free of 1/f residuals by construction — the reference floor.
        err_map = after - target

        plot_single_gray(os.path.join(args.out, f'{name}_after.png'),
                         after, sig, f'{name}: after bkg')
        plot_single_error(os.path.join(args.out, f'{name}_error.png'),
                          err_map, srcmask, sig,
                          f'{name}: after − target  '
                          f'[±{ERR_SPAN:.1f}σ, sources whited]')
        h = comp.get('h')
        if h is not None and np.ndim(h) and h.any():
            plot_single_error(os.path.join(args.out, f'{name}_hledger.png'),
                              h, np.zeros_like(srcmask), sig,
                              f'{name}: accumulated amp-row term h [±'
                              f'{ERR_SPAN:.1f}σ]')
            hrow = amprow_ledger_error(np.asarray(h, dtype=np.float64),
                                       scene.stripes)
            plot_single_error(os.path.join(args.out, f'{name}_hrow_err.png'),
                              hrow, np.zeros_like(srcmask), sig,
                              f'{name}: amp-row ledger error (fitted − '
                              f'injected banding) [±{ERR_SPAN:.1f}σ]')
            hrow_rows.append((f'{name}: amp-row ledger error', hrow, None))

        gray_rows.append((f'{name}: after', after, None))
        err_rows.append((f'{name}: after − target', err_map, srcmask))

        if args.save_arrays:
            np.savez_compressed(
                os.path.join(args.out, f'{name}_arrays.npz'),
                after=after.astype(np.float32),
                error=err_map.astype(np.float32),
                srcmask=srcmask,
                **{k: np.asarray(v).astype(np.float32)
                   for k, v in comp.items()
                   if k in ('h', 'b2d', 'vcol', 'ped', 'det_struct')})

    plot_compare(os.path.join(args.out, 'compare_after.png'),
                 gray_rows, sig, 'gray')
    plot_compare(os.path.join(args.out, 'compare_error.png'),
                 err_rows, sig, 'error')
    if hrow_rows:
        plot_compare(os.path.join(args.out, 'compare_hrow_err.png'),
                     hrow_rows, sig, 'error')

    if not args.keep_fits:
        shutil.rmtree(workdir, ignore_errors=True)
    print(f'wrote PNGs to {args.out}/ — judge by eye '
          f'(compare_error.png first)', flush=True)


if __name__ == '__main__':
    main()
