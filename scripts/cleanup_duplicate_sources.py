#!/usr/bin/env python3
"""
Remove duplicate targets caused by multiple catalog IDs for the same
physical source in MSA metadata files.

Duplicates are identified using the same algorithm as the pipeline fix
in campfire_pipeline.nirspec.metafile: within each slitlet, source IDs
with positions < 0.1" apart are the same source.  The higher ID is
dropped; the lower is kept.

Before deletion the script transfers any inspection data (redshift
quality, flags) from the target being deleted to the kept target.

Uses the same Supabase client as the deploy CLI, so credentials are
resolved identically (env vars or $CAMPFIRE_ROOT/config/deploy.toml).
Works for both local and remote Supabase with no changes.

Usage:
    # Dry run — report what would be deleted, no DB changes
    python scripts/cleanup_duplicate_sources.py --dry-run

    # Execute against configured Supabase (local or remote)
    python scripts/cleanup_duplicate_sources.py

    # Single observation
    python scripts/cleanup_duplicate_sources.py --obs rubies_uds_p21

    # Post-deletion verification
    python scripts/cleanup_duplicate_sources.py --verify

    # Skip objects table rebuild
    python scripts/cleanup_duplicate_sources.py --skip-objects-rebuild
"""

import argparse
import glob
import os
import sys
from collections import defaultdict

import numpy as np
from astropy.io import fits
from astropy.table import Table
from supabase import Client

# Add deploy package to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'deploy'))
from campfire_deploy.config import load_config, resolve_products_dir
from campfire_deploy.supabase import (
    get_supabase_client,
    refresh_filter_options,
    refresh_programs_overview,
)


DUPLICATE_SEPARATION_ARCSEC = 0.1
BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Duplicate detection (mirrors MetaFile._duplicate_source_ids)
# ---------------------------------------------------------------------------

def find_duplicates_in_msa(msa_file):
    """Find duplicate source ID pairs in an MSA metadata file.

    Returns list of (kept_id, dropped_id, separation_arcsec) tuples.
    """
    with fits.open(msa_file) as hdul:
        shutters = Table(hdul[2].data)
        sources = Table(hdul[3].data)

    # Build source_id -> (ra, dec) lookup
    src_pos = {}
    for row in sources:
        sid = int(row['source_id'])
        if sid > 0:
            src_pos[sid] = (float(row['ra']), float(row['dec']))

    pairs = []
    seen = set()
    threshold = DUPLICATE_SEPARATION_ARCSEC

    for meta_id in np.unique(shutters['msa_metadata_id']):
        meta_rows = shutters[shutters['msa_metadata_id'] == meta_id]
        meta_rows = meta_rows[meta_rows['source_id'] > 0]

        slitlet_sources = defaultdict(set)
        for row in meta_rows:
            slitlet_sources[int(row['slitlet_id'])].add(int(row['source_id']))

        for sids in slitlet_sources.values():
            if len(sids) < 2:
                continue
            sids_sorted = sorted(sids)
            for i in range(len(sids_sorted)):
                for j in range(i + 1, len(sids_sorted)):
                    s1, s2 = sids_sorted[i], sids_sorted[j]
                    if (s1, s2) in seen:
                        continue
                    if s1 not in src_pos or s2 not in src_pos:
                        continue
                    ra1, dec1 = src_pos[s1]
                    ra2, dec2 = src_pos[s2]
                    cos_dec = np.cos(np.deg2rad((dec1 + dec2) / 2))
                    dra = (ra1 - ra2) * cos_dec * 3600
                    ddec = (dec1 - dec2) * 3600
                    sep = np.sqrt(dra**2 + ddec**2)
                    if sep < threshold:
                        pairs.append((s1, s2, sep))
                        seen.add((s1, s2))

    return pairs


def discover_all_duplicates(products_dir, obs_filter=None):
    """Scan all observations and return {obs_name: [(kept, dropped, sep)]}."""
    results = {}
    obs_dirs = sorted(glob.glob(os.path.join(products_dir, '*')))

    for obs_dir in obs_dirs:
        if not os.path.isdir(obs_dir):
            continue
        obs_name = os.path.basename(obs_dir)
        if obs_filter and obs_name != obs_filter:
            continue

        msa_files = glob.glob(os.path.join(obs_dir, '*_msa.fits'))
        if not msa_files:
            continue

        # All MSA files for an observation have the same content;
        # use the first one found.
        pairs = find_duplicates_in_msa(msa_files[0])
        if pairs:
            results[obs_name] = pairs

    return results


# ---------------------------------------------------------------------------
# Database queries (Supabase client)
# ---------------------------------------------------------------------------

INSPECTION_FIELDS = (
    'target_id, redshift_quality, redshift_inspected, '
    'spectral_features, object_flags, dq_flags, '
    'last_inspected_at, last_inspected_by'
)


def fetch_target_inspection(sb: Client, target_ids: list[str]) -> dict:
    """Fetch inspection fields for a list of target_ids."""
    if not target_ids:
        return {}

    result = {}
    for i in range(0, len(target_ids), BATCH_SIZE):
        batch = target_ids[i:i + BATCH_SIZE]
        resp = (sb.table('targets')
                .select(INSPECTION_FIELDS)
                .in_('target_id', batch)
                .execute())
        for row in resp.data:
            result[row['target_id']] = row
    return result


def count_rows(sb: Client, table: str, column: str, values: list[str]) -> int:
    """Count rows in a table matching column IN values."""
    if not values:
        return 0

    total = 0
    for i in range(0, len(values), BATCH_SIZE):
        batch = values[i:i + BATCH_SIZE]
        resp = (sb.table(table)
                .select('id', count='exact')
                .in_(column, batch)
                .execute())
        total += resp.count
    return total


# ---------------------------------------------------------------------------
# Inspection data transfer
# ---------------------------------------------------------------------------

def plan_transfers(sb: Client, pair_map: dict) -> tuple[list, list]:
    """Determine what inspection data needs transferring.

    pair_map: dict of dropped_target_id -> kept_target_id

    Returns:
        transfers: list of (kept_target_id, update_dict) to apply
        conflicts: list of (kept, dropped, reason) requiring manual review
    """
    all_ids = list(pair_map.keys()) + list(pair_map.values())
    info = fetch_target_inspection(sb, all_ids)

    transfers = []
    conflicts = []

    for dropped_tid, kept_tid in pair_map.items():
        d = info.get(dropped_tid)
        k = info.get(kept_tid)
        if not d or not k:
            continue

        d_quality = d['redshift_quality'] or 0
        k_quality = k['redshift_quality'] or 0
        d_inspected = d['last_inspected_at'] is not None

        if not d_inspected:
            continue  # nothing to transfer

        update = {}

        # Redshift quality: keep the higher quality
        if d_quality > k_quality:
            update['redshift_quality'] = d_quality
            update['redshift_inspected'] = d['redshift_inspected']
            update['last_inspected_at'] = d['last_inspected_at']
            update['last_inspected_by'] = d['last_inspected_by']
        elif d_quality == k_quality and d_quality > 0:
            # Same quality — check for conflicting redshifts
            dz = d['redshift_inspected']
            kz = k['redshift_inspected']
            if dz is not None and kz is not None and dz != kz:
                if abs(float(dz) - float(kz)) > 0.01:
                    conflicts.append((
                        kept_tid, dropped_tid,
                        f'Both quality={d_quality} but z_kept={kz}, z_dropped={dz}'
                    ))

        # Bitmask flags: OR together
        for flag_col in ('spectral_features', 'object_flags', 'dq_flags'):
            d_val = d[flag_col] or 0
            k_val = k[flag_col] or 0
            merged = d_val | k_val
            if merged != k_val:
                update[flag_col] = merged

        if update:
            transfers.append((kept_tid, update))

    return transfers, conflicts


def apply_transfers(sb: Client, transfers: list) -> None:
    """Apply inspection data transfers to kept targets."""
    for kept_tid, update in transfers:
        sb.table('targets').update(update).eq('target_id', kept_tid).execute()


# ---------------------------------------------------------------------------
# Local file cleanup
# ---------------------------------------------------------------------------

def cleanup_local_files(products_dir, duplicates, dry_run=False):
    """Delete local _spec.fits files and prune summary ECSVs.

    Does NOT touch tarred intermediates (_cal.tar.gz, etc.) — those are
    archived pipeline state and not worth the complexity of editing.
    """
    deleted_files = 0
    pruned_summaries = 0

    for obs_name, pairs in sorted(duplicates.items()):
        obs_dir = os.path.join(products_dir, obs_name)
        dropped_sids = {str(dropped) for _, dropped, _ in pairs}

        # Delete loose _spec.fits for dropped source IDs
        for spec_file in glob.glob(os.path.join(obs_dir, '*_spec.fits')):
            # Filename pattern: {obs}_{grating}_{filter}_{source_id}_spec.fits
            basename = os.path.basename(spec_file)
            # Extract source_id: last segment before _spec.fits
            parts = basename.replace('_spec.fits', '').split('_')
            sid = parts[-1]
            if sid in dropped_sids:
                if dry_run:
                    print(f"  [dry-run] rm {spec_file}")
                else:
                    os.remove(spec_file)
                deleted_files += 1

        # Prune summary ECSV
        summary_path = os.path.join(obs_dir, f'{obs_name}_summary.ecsv')
        if os.path.exists(summary_path):
            t = Table.read(summary_path)
            dropped_tids = {f'{obs_name}_{sid}' for sid in dropped_sids}
            mask = np.array([str(row) not in dropped_tids for row in t['object_id']])
            n_before = len(t)
            t = t[mask]
            n_removed = n_before - len(t)
            if n_removed > 0:
                if dry_run:
                    print(f"  [dry-run] Prune {n_removed} rows from {summary_path}")
                else:
                    t.write(summary_path, format='ascii.ecsv', overwrite=True)
                pruned_summaries += n_removed

    return deleted_files, pruned_summaries


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------

def delete_targets(sb: Client, target_ids: list[str]) -> None:
    """Delete spectra, shutters, then targets in batches."""
    target_list = sorted(target_ids)

    # Delete spectra first (no CASCADE on this FK)
    for i in range(0, len(target_list), BATCH_SIZE):
        batch = target_list[i:i + BATCH_SIZE]
        sb.table('spectra').delete().in_('target_id', batch).execute()

    # Delete shutters (object_id column = target_id, no CASCADE)
    for i in range(0, len(target_list), BATCH_SIZE):
        batch = target_list[i:i + BATCH_SIZE]
        sb.table('shutters').delete().in_('object_id', batch).execute()

    # Delete targets (CASCADE handles comments, flag_audit_log)
    for i in range(0, len(target_list), BATCH_SIZE):
        batch = target_list[i:i + BATCH_SIZE]
        sb.table('targets').delete().in_('target_id', batch).execute()


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify(sb: Client) -> bool:
    """Print verification SQL to run after cleanup.

    PostgREST can't express LEFT JOIN or GROUP BY HAVING, so these
    checks need to be run directly against Postgres.
    """
    print("\nPost-deletion verification SQL (run via psql or Supabase SQL editor):")
    print()
    print("  -- Orphaned spectra (expect 0):")
    print("  SELECT COUNT(*) FROM spectra s")
    print("    LEFT JOIN targets t ON s.target_id = t.target_id")
    print("    WHERE t.target_id IS NULL;")
    print()
    print("  -- Remaining duplicate (object, observation) pairs (expect 0):")
    print("  SELECT o.id, t.observation, count(*)")
    print("    FROM objects o JOIN targets t ON t.object_id = o.id")
    print("    GROUP BY o.id, t.observation HAVING count(*) > 1;")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Remove duplicate targets from MSA catalog ID collisions',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Report what would be deleted without making changes',
    )
    parser.add_argument(
        '--obs', type=str, default=None,
        help='Limit to a single observation',
    )
    parser.add_argument(
        '--config', type=str, default=None,
        help='Path to deploy config TOML (default: env vars or $CAMPFIRE_ROOT/config/deploy.toml)',
    )
    parser.add_argument(
        '--local', action='store_true',
        help='Use local Supabase (http://127.0.0.1:54321)',
    )
    parser.add_argument(
        '--products-dir', type=str, default=None,
        help='Path to products directory (default: $CAMPFIRE_ROOT/products)',
    )
    parser.add_argument(
        '--skip-objects-rebuild', action='store_true',
        help='Skip rebuilding the objects table after deletion',
    )
    parser.add_argument(
        '--verify', action='store_true',
        help='Print verification SQL to run after cleanup',
    )
    args = parser.parse_args()

    products_dir = args.products_dir or str(resolve_products_dir())

    config = load_config(args.config, local=args.local)
    sb = get_supabase_client(config)
    print(f"Connected to {config['supabase']['url']}")

    if args.verify:
        verify(sb)
        return

    # ---- Step 1: Discover duplicates from MSA metadata ----
    print(f"Scanning MSA metadata in {products_dir}...")
    duplicates = discover_all_duplicates(products_dir, args.obs)

    if not duplicates:
        print("No duplicates found.")
        return

    # Build pair map: dropped_target_id -> kept_target_id
    pair_map = {}  # dropped -> kept
    all_pairs = []
    for obs_name, pairs in sorted(duplicates.items()):
        for kept_sid, dropped_sid, sep in pairs:
            kept_tid = f"{obs_name}_{kept_sid}"
            dropped_tid = f"{obs_name}_{dropped_sid}"
            pair_map[dropped_tid] = kept_tid
            all_pairs.append((obs_name, kept_sid, dropped_sid, sep))

    # ---- Step 2: Summary ----
    print(f"\n{'='*60}")
    print(f"Duplicate source summary")
    print(f"{'='*60}")
    print(f"  Observations affected: {len(duplicates)}")
    print(f"  Total pairs:          {len(pair_map)}")
    seps = [s for _, _, _, s in all_pairs]
    print(f"  Max separation:       {max(seps):.4f}\"")
    print(f"  Median separation:    {np.median(seps):.4f}\"")
    print()
    for obs_name in sorted(duplicates):
        print(f"  {obs_name}: {len(duplicates[obs_name])} pairs")

    # ---- Step 3: Validate against database ----
    print(f"\nValidating against database...")
    dropped_tids = list(pair_map.keys())
    kept_tids = list(pair_map.values())

    dropped_info = fetch_target_inspection(sb, dropped_tids)
    kept_info = fetch_target_inspection(sb, kept_tids)

    n_dropped_in_db = len(dropped_info)
    n_kept_in_db = len(kept_info)
    n_dropped_missing = len(dropped_tids) - n_dropped_in_db
    n_kept_missing = len(kept_tids) - n_kept_in_db

    print(f"  Targets to delete:    {n_dropped_in_db} in DB ({n_dropped_missing} not deployed)")
    print(f"  Kept counterparts:    {n_kept_in_db} in DB ({n_kept_missing} not deployed)")

    # Filter to only pairs where both exist in DB
    actionable = {
        d: k for d, k in pair_map.items()
        if d in dropped_info and k in kept_info
    }
    print(f"  Actionable pairs:     {len(actionable)}")

    if not actionable:
        print("\nNo actionable pairs. Nothing to do.")
        return

    # Count spectra and shutters that will be deleted
    spectra_count = count_rows(sb, 'spectra', 'target_id', list(actionable.keys()))
    shutters_count = count_rows(sb, 'shutters', 'object_id', list(actionable.keys()))
    print(f"  Spectra to delete:    {spectra_count}")
    print(f"  Shutters to delete:   {shutters_count}")

    # ---- Step 4: Plan inspection data transfers ----
    print(f"\nAuditing inspection data...")
    transfers, conflicts = plan_transfers(sb, actionable)

    n_inspected_dropped = sum(
        1 for d in actionable
        if (dropped_info[d].get('last_inspected_at') is not None)
    )
    print(f"  Dropped targets with inspection data: {n_inspected_dropped}")
    print(f"  Transfers needed:     {len(transfers)}")
    print(f"  Conflicts (manual):   {len(conflicts)}")

    if conflicts:
        print("\n  CONFLICTS requiring manual review:")
        for kept, dropped, reason in conflicts:
            print(f"    {kept} <- {dropped}: {reason}")

    if transfers:
        print(f"\n  Planned transfers:")
        for kept_tid, update in transfers:
            print(f"    {kept_tid}: {update}")

    # ---- Step 5: Execute ----
    if args.dry_run:
        # Show what local files would be cleaned up
        print(f"\nLocal file cleanup preview:")
        n_files, n_rows = cleanup_local_files(
            products_dir, duplicates, dry_run=True,
        )
        print(f"  Total: {n_files} spec files, {n_rows} summary rows")
        print(f"\nDry run complete. No changes made.")
        return

    if conflicts:
        print(f"\n{len(conflicts)} conflict(s) detected. Resolve manually before proceeding.")
        sys.exit(1)

    response = input(f"\nProceed with deleting {len(actionable)} targets, "
                     f"{spectra_count} spectra, and {shutters_count} shutters? [y/N] ")
    if response.lower() != 'y':
        print("Aborted.")
        return

    # Transfer inspection data
    if transfers:
        print(f"\nTransferring inspection data ({len(transfers)} updates)...")
        apply_transfers(sb, transfers)

    # Delete from database
    print(f"Deleting {spectra_count} spectra, {shutters_count} shutters, "
          f"and {len(actionable)} targets...")
    delete_targets(sb, list(actionable.keys()))
    print("  Done.")

    # Clean up local files (_spec.fits + summary ECSVs)
    print("\nCleaning up local files...")
    n_files, n_rows = cleanup_local_files(products_dir, duplicates)
    print(f"  Deleted {n_files} spec files, pruned {n_rows} summary rows.")

    # Rebuild objects table
    if not args.skip_objects_rebuild:
        print("\nRebuilding objects table...")
        print("  NOTE: populate_objects.py uses psycopg2 directly.")
        print("  Run separately: python scripts/populate_objects.py --dsn <dsn>")

    # Refresh materialized views
    print("\nRefreshing materialized views...")
    refresh_filter_options(sb)
    refresh_programs_overview(sb)

    # Verification hints
    verify(sb)

    print("\nCleanup complete.")


if __name__ == '__main__':
    main()
