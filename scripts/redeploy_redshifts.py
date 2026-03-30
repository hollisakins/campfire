#!/usr/bin/env python3
"""
Recompute redshift consensus and redeploy to Supabase.

For each observation:
  1. Extract zfits from tar (if needed)
  2. Regenerate the summary ECSV with the new consensus
  3. Re-tar zfits and clean up
  4. Preview quality resets (query Supabase for inspection state)
  5. Prompt for confirmation before deploying

Usage:
    python scripts/redeploy_redshifts.py rubies_uds_p11
    python scripts/redeploy_redshifts.py rubies_uds_p11 rubies_uds_p12
    python scripts/redeploy_redshifts.py --all-rubies
    python scripts/redeploy_redshifts.py --all-gto-wide
    python scripts/redeploy_redshifts.py --batch1
    python scripts/redeploy_redshifts.py --batch1 --yes   # skip confirmation
"""

import argparse
import os
import subprocess
import sys
import tarfile
from pathlib import Path

# Add deploy package to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'deploy'))

from campfire_deploy.config import load_config
from campfire_deploy.supabase import (
    get_supabase_client,
    check_existing_objects,
    REDSHIFT_DRIFT_THRESHOLD,
)
from campfire_deploy.summary import load_summary, get_unique_objects

CAMPFIRE_ROOT = Path(os.environ.get('CAMPFIRE_ROOT', '/Users/hba423/simmons/campfire-data'))
PRODUCTS = CAMPFIRE_ROOT / 'products'
CONDA_ENV = 'jwst'

RUBIES_OBS = [
    f'rubies_uds_p{d}{s}' for d in range(1, 5) for s in range(1, 4)
] + [
    f'rubies_egs_p{d}{s}' for d in range(5, 7) for s in range(1, 4)
]

GTO_WIDE_OBS = (
    [f'gto_wide_cosmos_p{i}' for i in range(1, 6)]
    + [f'gto_wide_egs_p{i}' for i in range(1, 5)]
    + [f'gto_wide_uds_p{i}' for i in range(1, 6)]
)

BATCH1_EXTRA = ['ceers1', 'jades_gds_udeep', 'diver']


def run(cmd, check=True):
    """Run a shell command, streaming output."""
    result = subprocess.run(cmd, shell=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed: {cmd}")
    return result.returncode == 0


def regenerate_summary(obs_name):
    """Extract zfits if tarred, regenerate summary ECSV, re-tar."""
    obs_dir = PRODUCTS / obs_name
    if not obs_dir.is_dir():
        print(f"  [{obs_name}] SKIP — directory not found")
        return False

    tar_path = obs_dir / f'{obs_name}_zfit.tar.gz'
    loose_exist = any(obs_dir.glob('*_zfit.fits'))
    extracted = False

    # Extract if needed
    if tar_path.exists() and not loose_exist:
        print(f"  [{obs_name}] Extracting zfits from tar...")
        with tarfile.open(tar_path, 'r:gz') as tf:
            tf.extractall(path=obs_dir)
        extracted = True
    elif tar_path.exists() and loose_exist:
        print(f"  [{obs_name}] WARNING: tar + loose zfits both exist. Using loose files.")

    if not any(obs_dir.glob('*_zfit.fits')):
        print(f"  [{obs_name}] SKIP — no zfit files found")
        return False

    # Regenerate summary
    print(f"  [{obs_name}] Recomputing summary...")
    ok = run(f'conda run -n {CONDA_ENV} cfpipe nirspec summary --obs {obs_name}', check=False)
    if not ok:
        print(f"  [{obs_name}] FAIL — summary generation failed")
        if extracted:
            for f in obs_dir.glob('*_zfit.fits'):
                f.unlink()
        return False

    # Re-tar if we extracted
    if extracted:
        print(f"  [{obs_name}] Re-tarring zfits...")
        zfit_files = sorted(obs_dir.glob('*_zfit.fits'))
        tmp_tar = tar_path.with_suffix('.tmp.tar.gz')
        with tarfile.open(tmp_tar, 'w:gz') as tf:
            for f in zfit_files:
                tf.add(f, arcname=f.name)
        tmp_tar.rename(tar_path)
        for f in zfit_files:
            f.unlink()

    return True


def preview_resets(client, obs_name):
    """Query Supabase and return list of objects that would have quality reset."""
    obs_dir = PRODUCTS / obs_name
    try:
        summary = load_summary(obs_dir, obs_name)
    except SystemExit:
        return []

    objects = get_unique_objects(summary)
    if not objects:
        return []

    existing = check_existing_objects(client, [o['object_id'] for o in objects])

    resets = []
    for obj in objects:
        oid = obj['object_id']
        if oid not in existing:
            continue
        old = existing[oid]
        if (
            old['redshift_quality'] == 4
            and old['redshift_inspected'] is None
            and old['redshift_auto'] is not None
            and obj['redshift_best'] is not None
            and abs(float(old['redshift_auto']) - float(obj['redshift_best'])) > REDSHIFT_DRIFT_THRESHOLD
        ):
            resets.append({
                'object_id': oid,
                'z_old': float(old['redshift_auto']),
                'z_new': float(obj['redshift_best']),
                'dz': abs(float(old['redshift_auto']) - float(obj['redshift_best'])),
            })
    return resets


def deploy(obs_name):
    """Deploy to Supabase (supabase-only, no R2)."""
    return run(
        f'conda run -n {CONDA_ENV} cfdeploy --obs {obs_name} --supabase-only --auto-approve',
        check=False,
    )


def main():
    parser = argparse.ArgumentParser(description='Recompute redshift consensus and redeploy.')
    parser.add_argument('obs', nargs='*', help='Observation name(s)')
    parser.add_argument('--all-rubies', action='store_true')
    parser.add_argument('--all-gto-wide', action='store_true')
    parser.add_argument('--batch1', action='store_true')
    parser.add_argument('--yes', '-y', action='store_true', help='Skip confirmation prompt')
    args = parser.parse_args()

    obs_list = list(args.obs)
    if args.all_rubies:
        obs_list.extend(RUBIES_OBS)
    if args.all_gto_wide:
        obs_list.extend(GTO_WIDE_OBS)
    if args.batch1:
        obs_list.extend(RUBIES_OBS + GTO_WIDE_OBS + BATCH1_EXTRA)

    if not obs_list:
        parser.print_help()
        sys.exit(1)

    print(f"=== Redeploy redshifts ===")
    print(f"Observations: {len(obs_list)}")
    print()

    # Phase 1: Regenerate all summaries
    print("--- Phase 1: Regenerate summaries ---")
    succeeded = []
    for obs_name in obs_list:
        if regenerate_summary(obs_name):
            succeeded.append(obs_name)
    print(f"\nRegenerated {len(succeeded)}/{len(obs_list)} summaries.")

    if not succeeded:
        print("Nothing to deploy.")
        return

    # Phase 2: Preview quality resets
    print("\n--- Phase 2: Preview quality resets ---")
    config = load_config(None)
    client = get_supabase_client(config)

    all_resets = []
    for obs_name in succeeded:
        resets = preview_resets(client, obs_name)
        n = len(resets)
        if n > 0:
            print(f"  [{obs_name}] {n} quality reset(s)")
            for r in resets:
                all_resets.append({**r, 'obs': obs_name})
        else:
            print(f"  [{obs_name}] no resets")

    print(f"\nTotal quality resets: {len(all_resets)}")
    if all_resets:
        print(f"\n{'Object ID':<40} {'z_old':>8} {'z_new':>8} {'dz':>8}")
        print(f"{'-'*40} {'-'*8} {'-'*8} {'-'*8}")
        for r in sorted(all_resets, key=lambda x: -x['dz']):
            print(f"{r['object_id']:<40} {r['z_old']:8.4f} {r['z_new']:8.4f} {r['dz']:8.4f}")

    # Phase 3: Confirm and deploy
    print(f"\n--- Phase 3: Deploy {len(succeeded)} observations ---")
    if not args.yes:
        resp = input(f"Proceed with live deploy? [y/N] ").strip().lower()
        if resp != 'y':
            print("Aborted. Summaries were regenerated but not deployed.")
            return

    n_ok = 0
    for obs_name in succeeded:
        print(f"  [{obs_name}] Deploying...")
        if deploy(obs_name):
            n_ok += 1
        else:
            print(f"  [{obs_name}] FAIL")

    print(f"\n=== Done: deployed {n_ok}/{len(succeeded)} observations ===")


if __name__ == '__main__':
    main()
