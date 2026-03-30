#!/usr/bin/env python3
"""Repair corrupted MSAMETFL/OGMETFL headers in RUBIES rate files.

Usage:
    python scripts/repair_msametfl.py --dry-run   # preview changes
    python scripts/repair_msametfl.py              # apply fixes
"""

import argparse
import glob
import os
from astropy.io import fits

MAPPING_FILE = os.path.expanduser('~/Downloads/rubies_MSAMETFL_mapping.txt')
PRODUCTS_DIR = os.path.expanduser('~/simmons/campfire-data/products')


def load_mapping(path):
    """Load rate_file -> MSAMETFL mapping, stripping .gz from rate file names."""
    mapping = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rate_gz, msametfl = line.split()
            # On-disk rate files don't have .gz
            rate = rate_gz.replace('.fits.gz', '.fits')
            mapping[rate] = msametfl
    return mapping


def main():
    parser = argparse.ArgumentParser(description='Repair MSAMETFL headers')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing')
    args = parser.parse_args()

    mapping = load_mapping(MAPPING_FILE)
    print(f"Loaded {len(mapping)} entries from mapping file")

    # Find all RUBIES products directories
    rubies_dirs = sorted(glob.glob(os.path.join(PRODUCTS_DIR, 'rubies_*')))
    print(f"Found {len(rubies_dirs)} RUBIES products directories")

    fixed = 0
    skipped = 0
    not_found = 0

    for products_dir in rubies_dirs:
        rate_files = glob.glob(os.path.join(products_dir, '*_rate.fits'))
        if not rate_files:
            continue

        for rate_file in sorted(rate_files):
            basename = os.path.basename(rate_file)
            correct_msametfl = mapping.get(basename)
            if correct_msametfl is None:
                not_found += 1
                continue

            with fits.open(rate_file) as rf:
                current = rf[0].header['MSAMETFL']
                ogmetfl = rf[0].header.get('OGMETFL', '')

            needs_fix = (current != correct_msametfl) or (ogmetfl and ogmetfl != correct_msametfl)

            if not needs_fix:
                skipped += 1
                continue

            obs_name = os.path.basename(products_dir)
            print(f"  [{obs_name}] {basename}")
            print(f"    MSAMETFL: {current} -> {correct_msametfl}")
            if ogmetfl:
                print(f"    OGMETFL:  {ogmetfl} -> {correct_msametfl}")

            if not args.dry_run:
                with fits.open(rate_file, mode='update') as rf:
                    rf[0].header['MSAMETFL'] = correct_msametfl
                    if 'OGMETFL' in rf[0].header:
                        rf[0].header['OGMETFL'] = correct_msametfl
                    rf.flush()

            fixed += 1

    action = "Would fix" if args.dry_run else "Fixed"
    print(f"\n{action} {fixed} files, skipped {skipped} (already correct), "
          f"{not_found} not in mapping")


if __name__ == '__main__':
    main()
