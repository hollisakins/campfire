#!/usr/bin/env python
"""Real-exposure bkg2d parameter sweep: score BACKGROUND FLATNESS, no truth.

Runs the real ``bkg_step`` repeatedly on snapshot pre-bkg canonical exposures
(see the reset/edge rebuild in the session log) across a grid of
box_size x extra_dilate x reject, plus a subtract_2d=off GP baseline. There
is no truth plane on real data, so each cell is scored on flatness of the
masked residual:

  - amp_seam:   max |median step| across cols 512/1024/1536 (sigma units) —
                the GP/mesh interplay pathology
  - block_rms:  std of 32-px block medians of the masked sky (sigma units) —
                large-scale flatness, noise-free
  - row_rms /
    col_rms:    std of per-row / per-column masked medians (sigma) —
                residual striping
  - sky_sigma:  mad-std of the masked residual (MJy/sr) — reference scale

Sweep sizes are given in NATIVE pixels of the exposures being tested (LW
here); the config box_size/extra_dilate are SW-scale, so config = 2x native
for LW. Usage (campfire env, this directory):

    python run_real_sweep.py --pristine /tmp/rj0911_prebkg \\
        --exposures jw06882025001_04101_00004_nrcblong,jw06882025001_04101_00001_nrcalong \\
        --out out_real --processes 4
"""

import argparse
import csv
import itertools
import multiprocessing as mp
import os
import shutil
import tempfile
import time
import tomllib

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from astropy.io import fits
from astropy.stats import mad_std
from scipy.ndimage import distance_transform_edt

AMP_BOUNDS = (512, 1024, 1536)


def load_bkg_defaults():
    import campfire_pipeline
    path = os.path.join(os.path.dirname(campfire_pipeline.__file__),
                        'data', 'config_default.toml')
    with open(path, 'rb') as f:
        return tomllib.load(f)['nircam']['bkg']


def build_step_config(defaults, arm):
    det = {**defaults.get('detrend', {}), 'enabled': arm.get('detrend', True)}
    if arm.get('detrend_box'):
        det['box_size'] = arm['detrend_box'] * 2   # native -> SW-scale config
    sc = {
        'n_iterations': defaults.get('n_iterations', 3),
        'plot': False,
        'mask': dict(defaults['mask']),
        'pedestal': dict(defaults['pedestal']),
        'striping': dict(defaults['striping']),
        'variance': dict(defaults['variance']),
        'detrend': det,
        'subtract_2d': arm['subtract_2d'],
    }
    if arm['subtract_2d']:
        b2d = dict(defaults['bkg2d'])
        # arm sizes are NATIVE px on the test exposures (LW): the step
        # channel-scales config by 0.5 for LW, so config = 2x native.
        b2d.update(box_size=arm['box'] * 2, extra_dilate=arm['dilate'] * 2,
                   reject=arm['reject'])
        sc['bkg2d'] = b2d
    return sc


def flatness_metrics(sci, srcmask, dq, grow_px=5):
    """Flatness metrics on the masked residual of one corrected exposure."""
    bad = (np.bitwise_and(dq, 1) != 0) | ~np.isfinite(sci) | (sci == 0)
    src = srcmask.astype(bool)
    if grow_px > 0:
        src = distance_transform_edt(~src) <= grow_px
    m = src | bad                      # True = exclude
    resid = np.where(m, np.nan, sci.astype(np.float64))
    sig = mad_std(resid, ignore_nan=True)

    seams = []
    for b in AMP_BOUNDS:
        left = np.nanmedian(resid[:, b - 16:b - 2])
        right = np.nanmedian(resid[:, b + 2:b + 16])
        if np.isfinite(left) and np.isfinite(right):
            seams.append(abs(right - left))
    amp_seam = max(seams) if seams else np.nan

    H, W = resid.shape
    bs = 32
    hb, wb = H // bs, W // bs
    blocks = np.nanmedian(
        resid[:hb * bs, :wb * bs].reshape(hb, bs, wb, bs).transpose(
            0, 2, 1, 3).reshape(hb, wb, -1), axis=2)
    block_rms = np.nanstd(blocks)

    row_rms = np.nanstd(np.nanmedian(resid, axis=1))
    col_rms = np.nanstd(np.nanmedian(resid, axis=0))

    return dict(sky_sigma=sig, amp_seam_sigma=amp_seam / sig,
                block_rms_sigma=block_rms / sig,
                row_rms_sigma=row_rms / sig, col_rms_sigma=col_rms / sig), resid


def run_arm(job):
    """One (exposure, arm) cell — safe to run in a worker process."""
    pristine, arm, defaults, outdir = job
    from campfire_pipeline.nircam.steps.bkg import bkg_step
    root = os.path.basename(pristine).removesuffix('.fits')
    tag = f'{root}-{arm["name"]}'
    tmpd = tempfile.mkdtemp()
    work = os.path.join(tmpd, os.path.basename(pristine))
    try:
        shutil.copyfile(pristine, work)
        sc = build_step_config(defaults, arm)
        t0 = time.time()
        bkg_step(work, None, sc, overwrite=True)
        step_s = time.time() - t0
        with fits.open(work) as hdul:
            sci = hdul['SCI'].data
            srcmask = hdul['SRCMASK'].data
            dq = hdul['DQ'].data
        met, resid = flatness_metrics(sci, srcmask, dq)
        met.update(exposure=root, arm=arm['name'],
                   box=arm.get('box', ''), dilate=arm.get('dilate', ''),
                   reject=arm.get('reject', ''),
                   detrend=arm.get('detrend', True),
                   step_s=round(step_s, 1))
        np.savez_compressed(os.path.join(outdir, f'{tag}_resid.npz'),
                            resid=resid.astype(np.float32))

        ds = 4
        v = 2 * met['sky_sigma']
        fig, ax = plt.subplots(figsize=(7, 7))
        im = ax.imshow(resid[::ds, ::ds], vmin=-v, vmax=v, cmap='RdBu_r',
                       origin='lower')
        for b in AMP_BOUNDS:
            ax.axvline(b / ds, color='k', lw=0.3, alpha=0.5)
        ax.set_title(f'{tag}\nseam={met["amp_seam_sigma"]:.3f}s '
                     f'block={met["block_rms_sigma"]:.3f}s '
                     f'row={met["row_rms_sigma"]:.3f}s', fontsize=9)
        ax.set_xticks([]), ax.set_yticks([])
        fig.colorbar(im, ax=ax, fraction=0.046)
        fig.tight_layout()
        fig.savefig(os.path.join(outdir, f'{tag}.png'), dpi=100)
        plt.close(fig)
        return met
    except Exception as e:
        return dict(exposure=root, arm=arm['name'], error=str(e))
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pristine', required=True,
                    help='dir of snapshot pre-bkg canonical exposures')
    ap.add_argument('--exposures', required=True,
                    help='comma-separated rootnames to sweep')
    ap.add_argument('--out', default='out_real')
    ap.add_argument('--boxes', default='8,16,32,64',
                    help='NATIVE px on the test exposures')
    ap.add_argument('--dilates', default='0,10,20,40', help='NATIVE px')
    ap.add_argument('--reject', default='both', choices=['on', 'off', 'both'])
    ap.add_argument('--detrend-ab', action='store_true',
                    help='run every arm with the conditioning detrend both '
                         'on (_det) and off (_nodet)')
    ap.add_argument('--detrend-boxes', default=None,
                    help='comma-separated detrend box sizes (NATIVE px) to '
                         'sweep; adds _db<n> arms')
    ap.add_argument('--processes', type=int, default=4)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    defaults = load_bkg_defaults()
    boxes = [int(b) for b in args.boxes.split(',')]
    dilates = [int(d) for d in args.dilates.split(',')]
    rejects = {'on': [True], 'off': [False],
               'both': [True, False]}[args.reject]
    detrends = [True, False] if args.detrend_ab else [True]
    dboxes = ([int(b) for b in args.detrend_boxes.split(',')]
              if args.detrend_boxes else [None])

    arms = []
    for det in detrends:
        for dbox in dboxes:
            suff = ('_det' if det else '_nodet') if args.detrend_ab else ''
            if dbox is not None:
                suff += f'_db{dbox}'
            arms.append(dict(name=f'gp_only{suff}', subtract_2d=False,
                             detrend=det, detrend_box=dbox))
            for box, dil, rej in itertools.product(boxes, dilates, rejects):
                arms.append(dict(
                    name=(f'b{box}_d{dil}_' + ('rej' if rej else 'norej')
                          + suff),
                    subtract_2d=True, box=box, dilate=dil, reject=rej,
                    detrend=det, detrend_box=dbox))

    exps = [os.path.join(args.pristine, f'{e}.fits')
            for e in args.exposures.split(',')]
    for e in exps:
        if not os.path.exists(e):
            raise SystemExit(f'missing pristine exposure: {e}')

    jobs = [(e, arm, defaults, args.out) for e in exps for arm in arms]
    print(f'{len(jobs)} cells ({len(exps)} exposures x {len(arms)} arms), '
          f'{args.processes} processes', flush=True)

    with mp.Pool(args.processes) as pool:
        results = pool.map(run_arm, jobs)

    ok = [r for r in results if 'error' not in r]
    bad = [r for r in results if 'error' in r]
    for r in bad:
        print(f'ERROR {r["exposure"]} {r["arm"]}: {r["error"]}', flush=True)

    cols = ['exposure', 'arm', 'box', 'dilate', 'reject', 'detrend',
            'amp_seam_sigma', 'block_rms_sigma', 'row_rms_sigma',
            'col_rms_sigma', 'sky_sigma', 'step_s']
    with open(os.path.join(args.out, 'results.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows({k: r.get(k, '') for k in cols} for r in ok)

    with open(os.path.join(args.out, 'summary.md'), 'w') as f:
        f.write('# bkg2d real-exposure flatness sweep\n\n')
        f.write('| ' + ' | '.join(cols[:-2]) + ' |\n')
        f.write('|' + '---|' * (len(cols) - 2) + '\n')
        for r in sorted(ok, key=lambda r: (r['exposure'],
                                           r['amp_seam_sigma'])):
            f.write('| ' + ' | '.join(
                (f'{r[k]:.4f}' if isinstance(r[k], float) else str(r[k]))
                for k in cols[:-2]) + ' |\n')
    for r in sorted(ok, key=lambda r: (r['exposure'], r['amp_seam_sigma'])):
        print(f'{r["exposure"]:40s} {r["arm"]:16s} '
              f'seam={r["amp_seam_sigma"]:.3f}s '
              f'block={r["block_rms_sigma"]:.3f}s '
              f'row={r["row_rms_sigma"]:.3f}s '
              f'col={r["col_rms_sigma"]:.3f}s', flush=True)
    print(f'wrote {args.out}/summary.md ({len(ok)} cells)')


if __name__ == '__main__':
    main()
