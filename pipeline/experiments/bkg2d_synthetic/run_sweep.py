#!/usr/bin/env python
"""Sweep the bkg2d (subtract_2d) parameter space on synthetic scenes.

Runs the REAL ``bkg_step`` (via a jwst ImageModel on disk, so config
plumbing, channel scaling, provenance and SRCMASK all get exercised) on
scenes from scene.py, and evaluates each cell through the correction-error
metrics in metrics.py.

Arms: a 'off' baseline (subtract_2d=false — the E map then still contains
the un-removed sky gradient, for reference) plus the product of
box_size x extra_dilate x reject. The mask/pedestal/striping config comes
from the package config defaults; striping defaults to estimator='none' to
isolate the 2-D stage (use --estimator gp for the full chain).

Usage (in the campfire conda env, from this directory):

    python run_sweep.py --out out --quick          # ~5 min smoke
    python run_sweep.py --out out                  # default sweep
    python run_sweep.py --out out --preset both --channels sw,lw --seeds 3

Outputs under --out: results_cells.csv, results_sources.csv, summary.md,
and one diagnostic PNG per cell (E map, bowl profiles, frac-loss vs r_e).
"""

import argparse
import csv
import os
import shutil
import time
import tomllib

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.io import fits

from scene import make_scene
from metrics import evaluate


def load_bkg_defaults():
    import campfire_pipeline
    path = os.path.join(os.path.dirname(campfire_pipeline.__file__),
                        'data', 'config_default.toml')
    with open(path, 'rb') as f:
        cfg = tomllib.load(f)
    return cfg['nircam']['bkg']


def save_model(scene, path):
    from jwst.datamodels import ImageModel
    m = ImageModel()
    m.data = scene.image
    m.err = scene.err
    # reference-pixel border is off-detector, as on real cal frames
    m.dq = np.where(scene.valid, 0, 1).astype(np.uint32)
    m.var_rnoise = np.full(scene.shape, (0.6 * scene.sigma) ** 2, np.float32)
    m.var_poisson = np.full(scene.shape, (0.2 * scene.sigma) ** 2, np.float32)
    m.var_flat = np.full(scene.shape, (0.1 * scene.sigma) ** 2, np.float32)
    m.meta.instrument.name = 'NIRCAM'
    if scene.channel == 'sw':
        m.meta.instrument.channel = 'SHORT'
        m.meta.instrument.detector = 'NRCA1'
    else:
        m.meta.instrument.channel = 'LONG'
        m.meta.instrument.detector = 'NRCALONG'
    m.save(path)


def build_step_config(defaults, arm, n_iter, estimator):
    sc = {
        'n_iterations': n_iter or defaults.get('n_iterations', 3),
        'plot': False,
        'mask': dict(defaults['mask']),
        'pedestal': dict(defaults['pedestal']),
        'striping': {**defaults['striping'], 'estimator': estimator},
        'variance': dict(defaults['variance']),
        'detrend': {**defaults.get('detrend', {}),
                    'enabled': arm.get('detrend', True)},
        'subtract_2d': arm['subtract_2d'],
    }
    if arm['subtract_2d']:
        b2d = dict(defaults['bkg2d'])
        b2d.update(box_size=arm['box'], extra_dilate=arm['dilate'],
                   reject=arm['reject'])
        sc['bkg2d'] = b2d
    return sc


def run_cell(pristine, workfile, scene, step_config):
    from campfire_pipeline.nircam.steps.bkg import bkg_step
    shutil.copyfile(pristine, workfile)
    t0 = time.time()
    bkg_step(workfile, None, step_config, overwrite=True)
    step_s = time.time() - t0
    with fits.open(workfile) as hdul:
        sci_out = hdul['SCI'].data.astype(np.float64)
        srcmask = hdul['SRCMASK'].data.astype(bool)
    correction = scene.image.astype(np.float64) - sci_out
    return correction, srcmask, step_s


def plot_scene(path, scene):
    """Truth layers: image, galaxy plane, ICL plane, sky. The image and
    galaxy panels share the step-plot stretch; ICL and sky get their own
    (annotated) since their amplitudes are far below it."""
    sig = scene.sigma
    med = float(np.median(scene.image[scene.valid]))
    panels = [
        (scene.image - med, f'image − median [{-2:.0f}σ, {10:.0f}σ]',
         -2 * sig, 10 * sig, 'gray'),
        (scene.galaxies, f'galaxies truth [{-2:.0f}σ, {10:.0f}σ]',
         -2 * sig, 10 * sig, 'gray'),
        (scene.icl, f'ICL truth [0, {scene.icl.max() / sig:.1f}σ]',
         0, max(scene.icl.max(), 1e-6), 'magma'),
        (scene.sky - scene.sky.mean(), 'sky − mean (gradient)',
         None, None, 'RdBu_r'),
    ]
    if scene.stripes.any():
        panels.append((scene.stripes, 'stripes truth (amp DC + 1/f)',
                       None, None, 'RdBu_r'))
    fig, axes = plt.subplots(1, len(panels),
                             figsize=(4.8 * len(panels), 4.6))
    for ax, (img, title, vmin, vmax, cmap) in zip(axes, panels):
        im = ax.imshow(img[::2, ::2], vmin=vmin, vmax=vmax, cmap=cmap,
                       origin='lower')
        ax.set_title(title, fontsize=10)
        ax.set_xticks([]), ax.set_yticks([])
        fig.colorbar(im, ax=ax, fraction=0.046)
    fig.suptitle(f'scene {scene.preset}-{scene.channel}-s{scene.seed}')
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_images(path, tag, scene, correction):
    """Before / after / subtracted background, all three on ONE matched
    linear stretch (−2σ, +10σ around each panel's median) so structure is
    directly comparable across panels and arms."""
    sig = scene.sigma
    before = scene.image.astype(np.float64)
    after = before - correction
    fig, axes = plt.subplots(1, 3, figsize=(16, 5.0))
    panels = [(before, 'before (input image)'),
              (after, 'after bkg step'),
              (correction, 'subtracted bkg (correction)')]
    for ax, (img, title) in zip(axes, panels):
        disp = img - np.median(img[scene.valid])
        im = ax.imshow(disp[::2, ::2], vmin=-2 * sig, vmax=10 * sig,
                       cmap='gray', origin='lower')
        ax.set_title(title)
        ax.set_xticks([]), ax.set_yticks([])
    fig.colorbar(im, ax=axes, fraction=0.02,
                 label=f'− median, linear [−2σ, +10σ], σ={sig}')
    fig.suptitle(tag)
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_cell(path, tag, scene, maps, rows, bowls, summary):
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    ds = 4
    vmax = max(3 * summary['sky_rms_sigma'] * scene.sigma, 1e-4)
    im = axes[0].imshow(maps['E_gal'][::ds, ::ds], vmin=-vmax, vmax=vmax,
                        cmap='RdBu_r', origin='lower')
    axes[0].set_title(f'E_gal (galaxy-attributable, vmax={vmax:.2e})')
    fig.colorbar(im, ax=axes[0], fraction=0.046)

    for i, (radii, prof) in bowls.items():
        axes[1].plot(radii, prof / scene.sigma, lw=1)
    axes[1].axhline(0, color='k', lw=0.5)
    axes[1].set_xlabel('r [px]')
    axes[1].set_ylabel('median(E_gal) [sigma]')
    axes[1].set_title('bowls: brightest galaxies')

    if rows:
        re_v = np.array([r['r_e'] for r in rows])
        fr = np.array([r['frac'] for r in rows])
        c = np.where(np.array([r['compact'] for r in rows]), 'C0', 'C3')
        axes[2].scatter(re_v, 100 * fr, s=8, c=c, alpha=0.6)
    axes[2].axhline(0, color='k', lw=0.5)
    axes[2].axhline(0.05, color='r', ls=':', lw=1)
    axes[2].axhline(-0.05, color='r', ls=':', lw=1)
    axes[2].set_xlabel('r_e [px]')
    axes[2].set_ylabel('aperture flux loss [%]')
    axes[2].set_title('per-source loss (blue=compact, red=extended)')

    fig.suptitle(tag)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--out', default='out')
    ap.add_argument('--preset', default='cluster',
                    choices=['blank', 'cluster', 'both'])
    ap.add_argument('--channels', default='sw')
    ap.add_argument('--seeds', type=int, default=1)
    ap.add_argument('--boxes', default='16,32,64')
    ap.add_argument('--dilates', default='0,20,40,80,150')
    ap.add_argument('--reject', default='on', choices=['on', 'off', 'both'])
    ap.add_argument('--no-detrend', action='store_true',
                    help='disable the conditioning detrend in all arms '
                         '(legacy-chain reference)')
    ap.add_argument('--inject-1f', action='store_true',
                    help='inject per-amp DC offsets + 1/f banding + column '
                         'stripes into the scene (stripes truth plane)')
    ap.add_argument('--estimator', default='none', choices=['none', 'gp',
                                                            'median'])
    ap.add_argument('--n-iter', type=int, default=None)
    ap.add_argument('--n-gal', type=int, default=None)
    ap.add_argument('--quick', action='store_true',
                    help='512-row frames, 1 seed, boxes=32, dilates=0,40')
    ap.add_argument('--keep-fits', action='store_true')
    args = ap.parse_args()

    shape = (2048, 2048)
    if args.quick:
        shape = (512, 2048)
        args.boxes, args.dilates, args.seeds = '32', '0,40', 1
    n_gal = args.n_gal or (150 if args.quick else 500)

    presets = ['blank', 'cluster'] if args.preset == 'both' else [args.preset]
    channels = args.channels.split(',')
    boxes = [int(b) for b in args.boxes.split(',')]
    dilates = [int(d) for d in args.dilates.split(',')]
    rejects = {'on': [True], 'off': [False], 'both': [True, False]}[args.reject]

    det = not args.no_detrend
    arms = [dict(name='off', subtract_2d=False, detrend=det)]
    for box in boxes:
        for dil in dilates:
            for rej in rejects:
                arms.append(dict(
                    name=f'b{box}_d{dil}_' + ('rej' if rej else 'norej'),
                    subtract_2d=True, box=box, dilate=dil, reject=rej,
                    detrend=det))

    os.makedirs(args.out, exist_ok=True)
    workdir = os.path.join(args.out, 'work')
    os.makedirs(workdir, exist_ok=True)
    defaults = load_bkg_defaults()

    cell_rows, source_rows = [], []
    n_total = len(presets) * len(channels) * args.seeds * len(arms)
    done = 0
    for preset in presets:
        for ch in channels:
            for seed in range(args.seeds):
                print(f'== scene {preset}-{ch}-s{seed}: building '
                      f'({n_gal} galaxies, {shape[0]}x{shape[1]})', flush=True)
                scene = make_scene(preset=preset, shape=shape, channel=ch,
                                   seed=seed, n_gal=n_gal,
                                   inject_1f=args.inject_1f)
                pristine = os.path.join(
                    workdir, f'scene_{preset}_{ch}_s{seed}.fits')
                save_model(scene, pristine)
                workfile = os.path.join(workdir, 'run.fits')
                plot_scene(os.path.join(
                    args.out, f'scene_{preset}-{ch}-s{seed}.png'), scene)

                for arm in arms:
                    tag = f'{preset}-{ch}-s{seed}-{arm["name"]}'
                    done += 1
                    print(f'-- [{done}/{n_total}] {tag}', flush=True)
                    sc = build_step_config(defaults, arm, args.n_iter,
                                           args.estimator)
                    correction, srcmask, step_s = run_cell(
                        pristine, workfile, scene, sc)
                    summary, rows, bowls, maps = evaluate(correction, scene,
                                                          srcmask)
                    plot_cell(os.path.join(args.out, f'{tag}.png'),
                              tag, scene, maps, rows, bowls, summary)
                    plot_images(os.path.join(args.out, f'{tag}_images.png'),
                                tag, scene, correction)

                    cell = dict(preset=preset, channel=ch, seed=seed,
                                arm=arm['name'],
                                box=arm.get('box', ''),
                                dilate=arm.get('dilate', ''),
                                reject=arm.get('reject', ''),
                                detrend=arm.get('detrend', ''),
                                step_s=round(step_s, 1),
                                sky_rms_sigma=summary['sky_rms_sigma'],
                                amp_seam_sigma=summary['amp_seam_sigma'],
                                icl_removed_frac=summary['icl_removed_frac'],
                                masked_frac=summary['masked_frac'])
                    for grp in ('compact', 'extended', 'bright'):
                        s = summary[grp]
                        cell[f'{grp}_n'] = s['n']
                        cell[f'{grp}_med_pct'] = 100 * s['med']
                        cell[f'{grp}_p95_pct'] = 100 * s['p95']
                        cell[f'{grp}_worst_pct'] = 100 * s['worst']
                    cell_rows.append(cell)
                    for r in rows:
                        source_rows.append({**cell, **r})
                    print(f'   compact med={cell["compact_med_pct"]:+.4f}% '
                          f'p95={cell["compact_p95_pct"]:.4f}% | '
                          f'extended med={cell["extended_med_pct"]:+.4f}% | '
                          f'bright med={cell["bright_med_pct"]:+.4f}% '
                          f'worst={cell["bright_worst_pct"]:+.4f}% | '
                          f'icl={summary["icl_removed_frac"]:.2f} '
                          f'sky_rms={summary["sky_rms_sigma"]:.3f}s '
                          f'seam={summary["amp_seam_sigma"]:.3f}s '
                          f'mask={summary["masked_frac"]:.2f} '
                          f'({step_s:.0f}s)', flush=True)

                if not args.keep_fits:
                    os.remove(pristine)

    if not args.keep_fits and os.path.exists(
            os.path.join(workdir, 'run.fits')):
        os.remove(os.path.join(workdir, 'run.fits'))

    for name, data in (('results_cells.csv', cell_rows),
                       ('results_sources.csv', source_rows)):
        if not data:
            continue
        with open(os.path.join(args.out, name), 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            w.writeheader()
            w.writerows(data)

    with open(os.path.join(args.out, 'summary.md'), 'w') as f:
        f.write('# bkg2d synthetic sweep\n\n')
        f.write(f'estimator={args.estimator}, n_gal={n_gal}, '
                f'shape={shape}\n\n')
        cols = ['preset', 'channel', 'seed', 'arm', 'compact_med_pct',
                'compact_p95_pct', 'compact_worst_pct', 'extended_med_pct',
                'extended_worst_pct', 'bright_med_pct', 'bright_worst_pct',
                'icl_removed_frac', 'sky_rms_sigma', 'amp_seam_sigma',
                'masked_frac', 'step_s']
        f.write('| ' + ' | '.join(cols) + ' |\n')
        f.write('|' + '---|' * len(cols) + '\n')
        for c in cell_rows:
            f.write('| ' + ' | '.join(
                (f'{c[k]:.4f}' if isinstance(c[k], float) else str(c[k]))
                for k in cols) + ' |\n')
    print(f'wrote {args.out}/summary.md '
          f'({len(cell_rows)} cells, {len(source_rows)} source rows)')


if __name__ == '__main__':
    main()
