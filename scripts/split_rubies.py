#!/usr/bin/env python3
"""
Split merged RUBIES observation products back into per-pointing directories.

Moves stage 1/2 files based on JWST visit prefixes and copies metadata files.
Stage 3 and redshift fitting must be re-run afterward.

Usage:
    # Dry run — show what would be moved
    python scripts/split_rubies.py --dry-run

    # Apply
    python scripts/split_rubies.py

    # Then re-run stage3 + zfit for each pointing:
    cfpipe nirspec run --obs rubies_uds_p11 --stages stage3,zfit --processes 4
"""

import argparse
import os
import shutil
from pathlib import Path

# Mapping: merged obs → [(per-pointing obs, visit prefix), ...]
# Derived from observations.toml file patterns
SPLIT_MAP = {
    'rubies_uds_p1': [
        ('rubies_uds_p11', 'jw04233001001'),
        ('rubies_uds_p12', 'jw04233001002'),
        ('rubies_uds_p13', 'jw04233001003'),
    ],
    'rubies_uds_p2': [
        ('rubies_uds_p21', 'jw04233002001'),
        ('rubies_uds_p22', 'jw04233002002'),
        ('rubies_uds_p23', 'jw04233002003'),
    ],
    'rubies_uds_p3': [
        ('rubies_uds_p31', 'jw04233003001'),
        ('rubies_uds_p32', 'jw04233003002'),
        ('rubies_uds_p33', 'jw04233003003'),
    ],
    'rubies_uds_p4': [
        ('rubies_uds_p41', 'jw04233004001'),
        ('rubies_uds_p42', 'jw04233004002'),
        ('rubies_uds_p43', 'jw04233004003'),
    ],
    'rubies_egs_p5': [
        ('rubies_egs_p51', 'jw04233005001'),
        ('rubies_egs_p52', 'jw04233005002'),
        ('rubies_egs_p53', 'jw04233005003'),
    ],
    'rubies_egs_p6': [
        ('rubies_egs_p61', 'jw04233006001'),
        ('rubies_egs_p62', 'jw04233006002'),
        ('rubies_egs_p63', 'jw04233006003'),
    ],
}


def get_products_dir() -> Path:
    root = os.environ.get('CAMPFIRE_ROOT')
    if not root:
        print("Error: $CAMPFIRE_ROOT not set")
        raise SystemExit(1)
    return Path(root) / 'products'


def split_observation(merged_obs: str, targets: list[tuple[str, str]], *, dry_run: bool):
    products_dir = get_products_dir()
    merged_dir = products_dir / merged_obs

    if not merged_dir.exists():
        print(f"  Skipping {merged_obs}: directory not found")
        return

    # Inventory all files
    all_files = list(merged_dir.iterdir())
    metadata_prefix = f'_{merged_obs}_'
    stuck_dir = merged_dir / 'stuck_shutters'

    for pointing_obs, visit_prefix in targets:
        target_dir = products_dir / pointing_obs
        if not dry_run:
            target_dir.mkdir(exist_ok=True)

        moved = 0
        for f in all_files:
            if f.name.startswith(visit_prefix):
                if dry_run:
                    moved += 1
                else:
                    shutil.move(str(f), str(target_dir / f.name))
                    moved += 1

        # Copy metadata files (these apply to all pointings)
        meta_copied = 0
        for f in all_files:
            if f.name.startswith(metadata_prefix) and f.is_file():
                new_name = f.name.replace(merged_obs, pointing_obs)
                if dry_run:
                    meta_copied += 1
                else:
                    shutil.copy2(str(f), str(target_dir / new_name))
                    meta_copied += 1

        # Copy stuck_shutters directory if it exists
        if stuck_dir.exists() and stuck_dir.is_dir():
            target_stuck = target_dir / 'stuck_shutters'
            if dry_run:
                print(f"    Would copy stuck_shutters/ -> {pointing_obs}/stuck_shutters/")
            else:
                if target_stuck.exists():
                    shutil.rmtree(target_stuck)
                shutil.copytree(str(stuck_dir), str(target_stuck))

        print(f"    {pointing_obs}: {moved} files moved, {meta_copied} metadata copied")


def main():
    parser = argparse.ArgumentParser(
        description='Split merged RUBIES products into per-pointing directories.',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show what would be moved without making changes',
    )
    parser.add_argument(
        '--obs', type=str, default=None,
        help='Specific merged observation to split (default: all)',
    )
    args = parser.parse_args()

    if args.dry_run:
        print("=== DRY RUN ===\n")

    obs_to_split = {args.obs: SPLIT_MAP[args.obs]} if args.obs else SPLIT_MAP

    for merged_obs, targets in obs_to_split.items():
        print(f"\n{merged_obs}:")
        split_observation(merged_obs, targets, dry_run=args.dry_run)

    if args.dry_run:
        print("\n=== DRY RUN — no files moved ===")
    else:
        print("\nDone. Now re-run stage3 + zfit for each pointing.")


if __name__ == '__main__':
    main()
