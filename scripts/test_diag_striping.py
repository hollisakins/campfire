#!/usr/bin/env python3
"""Iterate the NIRCam diag_striping step on a single exposure.

The full upstream chain (detector1 → image2 → striping → edge → sky) is
expensive — once per exposure is enough. This script keeps a sibling
``<rootname>.post_sky.fits`` snapshot of the canonical FITS taken just
before diag_striping, so subsequent iterations on θ / bin / strip / niter
parameters cost only the diag_striping step itself (~seconds).

Default target is the F356W exposure where the stripe-as-source masking
failure is visible in the residual:
    field    = uds
    filter   = f356w
    rootname = jw01837002019_04201_00002_nrcblong

Workflow
--------
First run (downloads CRDS refs, runs upstream, creates snapshot):
    python scripts/test_diag_striping.py --upstream

Subsequent iterations (restores snapshot, re-runs diag_striping):
    python scripts/test_diag_striping.py
    python scripts/test_diag_striping.py --bin-width 0.5 --n-iterations 3

A/B comparison of the stripe-aware SRCMASK filter:
    python scripts/test_diag_striping.py --no-unmask-stripe-aligned --suffix baseline
    python scripts/test_diag_striping.py --suffix stripemask

CLI overrides flow into the step config; whatever isn't overridden falls
back to the merged default + fields.toml config.
"""

import argparse
import os
import shutil
import sys

from astropy.io import fits

from campfire_pipeline.config import (
    get_nircam_step_config,
    load_config,
    setup_environment,
)
from campfire_pipeline.nircam.field import Field
from campfire_pipeline.nircam.orchestrate import run_step
from campfire_pipeline.nircam.steps.diag_striping import diag_striping_step

DEFAULT_ROOTNAME = 'jw01837002019_04201_00002_nrcblong'
DEFAULT_FIELD = 'uds'
DEFAULT_FILTER = 'f356w'

UPSTREAM_STEPS = [
    'detector1', 'persistence', 'wisp', 'striping',
    'image2', 'edge', 'sky',
]


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--rootname', default=DEFAULT_ROOTNAME)
    p.add_argument('--field', default=DEFAULT_FIELD)
    p.add_argument('--filter', default=DEFAULT_FILTER)
    p.add_argument('--upstream', action='store_true',
                   help='Run upstream steps (detector1 ... sky) before '
                        'diag_striping. Slow; do this once per exposure.')
    p.add_argument('--no-snapshot-restore', action='store_true',
                   help='Skip snapshot restore — run diag_striping in place '
                        'on whatever the canonical FITS currently holds.')
    # Step-config overrides — None means "fall through to merged config".
    p.add_argument('--bin-width', type=float, default=None)
    p.add_argument('--column-width', type=int, default=None)
    p.add_argument('--column-overlap', type=int, default=None)
    p.add_argument('--n-iterations', type=int, default=None)
    p.add_argument('--max-strip-delta-ratio', type=float, default=None)
    # Stripe-aware SRCMASK filter (option A from the diagonal-stripe diagnosis):
    # release connected components that are elongated along θ so a bright stripe
    # mistakenly captured by source detection doesn't disappear from the per-bin
    # median sample.
    p.add_argument('--unmask-stripe-aligned', dest='unmask_stripe_aligned',
                   action='store_true', default=None)
    p.add_argument('--no-unmask-stripe-aligned', dest='unmask_stripe_aligned',
                   action='store_false')
    p.add_argument('--stripe-aspect-min', type=float, default=None)
    p.add_argument('--stripe-angle-tol-deg', type=float, default=None)
    p.add_argument('--stripe-min-size', type=int, default=None)
    # Skip-condition overrides (set the abs_range pair to 0 to force apply).
    p.add_argument('--skip-abs-range', type=float, default=None)
    p.add_argument('--skip-abs-range-at-edge', type=float, default=None)
    p.add_argument('--skip-boundary-dist', type=float, default=None)
    p.add_argument('--suffix', default=None,
                   help='Suffix to append to the output PDF basename so '
                        'successive runs don\'t overwrite each other '
                        '(e.g. "baseline" or "stripemask").')
    return p.parse_args()


def restore_snapshot(canonical, snapshot):
    if not os.path.exists(snapshot):
        sys.exit(
            f"snapshot missing: {snapshot}\n"
            "  Re-run with --upstream once to generate it, or use "
            "--no-snapshot-restore to operate on the current canonical."
        )
    shutil.copy(snapshot, canonical)
    with fits.open(canonical, mode='update') as hdul:
        hdr = hdul[0].header
        if 'CFP_DIAG' in hdr:
            del hdr['CFP_DIAG']
        hdul.flush()


def make_snapshot(canonical, snapshot):
    if os.path.exists(snapshot):
        return  # existing snapshot is the trusted "pre-diag" state
    shutil.copy(canonical, snapshot)


def main():
    args = parse_args()

    config = load_config()
    setup_environment(config)
    field = Field.load(args.field)
    field.setup_workspace()

    canonical = os.path.join(field.filter_dir(args.filter),
                             f'{args.rootname}.fits')
    snapshot = canonical[:-5] + '.post_sky.fits'

    if args.upstream:
        for step in UPSTREAM_STEPS:
            print(f">>> upstream: {step}")
            run_step(step, field, config, filters=[args.filter],
                     n_processes=1, overwrite=False)
        if not os.path.exists(canonical):
            sys.exit(f"upstream finished but no canonical at {canonical}; "
                     "check the run output above for the exposure list.")
        make_snapshot(canonical, snapshot)
        print(f">>> snapshot: {snapshot}")
    elif not args.no_snapshot_restore:
        restore_snapshot(canonical, snapshot)
        print(f">>> restored from snapshot")

    cfg = dict(get_nircam_step_config('diag_striping', config, field))
    cfg['enabled'] = True
    overrides = {
        'bin_width': args.bin_width,
        'column_width': args.column_width,
        'column_overlap': args.column_overlap,
        'n_iterations': args.n_iterations,
        'max_strip_delta_ratio': args.max_strip_delta_ratio,
        'unmask_stripe_aligned': args.unmask_stripe_aligned,
        'stripe_aspect_min': args.stripe_aspect_min,
        'stripe_angle_tol_deg': args.stripe_angle_tol_deg,
        'stripe_min_size': args.stripe_min_size,
        'skip_abs_range': args.skip_abs_range,
        'skip_abs_range_at_edge': args.skip_abs_range_at_edge,
        'skip_boundary_dist': args.skip_boundary_dist,
    }
    for key, val in overrides.items():
        if val is not None:
            cfg[key] = val

    print(">>> diag_striping config:")
    for k in ('theta_min', 'theta_max', 'theta_coarse_step', 'theta_fine_step',
              'bin_width', 'column_width', 'column_overlap',
              'max_strip_delta_ratio', 'n_iterations', 'maxiters',
              'unmask_stripe_aligned', 'stripe_aspect_min',
              'stripe_angle_tol_deg', 'stripe_min_size',
              'skip_abs_range', 'skip_abs_range_at_edge',
              'skip_boundary_dist'):
        if k in cfg:
            print(f"      {k}: {cfg[k]}")

    diag_striping_step(canonical, field, cfg, overwrite=True, status=None)

    pdf = canonical[:-5] + '_diag_striping.pdf'
    if args.suffix:
        suffixed = canonical[:-5] + f'_diag_striping_{args.suffix}.pdf'
        if os.path.exists(pdf):
            shutil.move(pdf, suffixed)
            pdf = suffixed
    print(f">>> done. plot: {pdf}")


if __name__ == '__main__':
    main()
