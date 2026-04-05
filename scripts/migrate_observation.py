#!/usr/bin/env python3
"""
Migrate inspection data when observations are redefined (split or merged).

Cross-matches old objects to new objects by RA/Dec, then propagates inspection
fields that the deploy-time crossmatch doesn't handle: redshift_inspected,
non-Secure quality values, spectral_features, object_flags, dq_flags, and
comments.

Handles both 1:N splits (one old → many new) and N:1 merges (many old → one new)
with conflict resolution for the merge case.

Usage:
    # 1:N split (OCEANS)
    python scripts/migrate_observation.py \
        --old-obs oceans \
        --new-obs oceans_p2,oceans_p3,oceans_p4,oceans_p5 \
        --dry-run

    # N:1 merge (RUBIES)
    python scripts/migrate_observation.py \
        --old-obs rubies_uds_p11,rubies_uds_p12,rubies_uds_p13 \
        --new-obs rubies_uds_p1 \
        --dry-run

    # Apply
    python scripts/migrate_observation.py \
        --old-obs rubies_uds_p11,rubies_uds_p12,rubies_uds_p13 \
        --new-obs rubies_uds_p1

    # Delete old observations after verifying migration
    python scripts/migrate_observation.py \
        --old-obs rubies_uds_p11,rubies_uds_p12,rubies_uds_p13 \
        --new-obs rubies_uds_p1 \
        --delete-old
"""

import argparse
from collections import defaultdict
import sys

from astropy.coordinates import SkyCoord, search_around_sky
import astropy.units as u

from campfire_deploy.config import load_config as load_deploy_config
from campfire_deploy.r2 import get_r2_client
from supabase import create_client, Client


MATCH_RADIUS_ARCSEC = 0.2

QUALITY_LABELS = {
    0: 'Not Inspected',
    1: 'Impossible',
    2: 'Unlikely',
    3: 'Probable',
    4: 'Secure',
}


def fetch_objects(client: Client, observation: str) -> list[dict]:
    """Fetch all targets for an observation, paginating past the 1000-row limit."""
    fields = (
        'id, target_id, observation, ra, dec, '
        'redshift_auto, redshift_inspected, redshift_quality, '
        'spectral_features, object_flags, dq_flags, '
        'last_inspected_at, last_inspected_by'
    )
    page_size = 1000
    all_data = []
    offset = 0
    while True:
        resp = (
            client.table('targets')
            .select(fields)
            .eq('observation', observation)
            .range(offset, offset + page_size - 1)
            .execute()
        )
        all_data.extend(resp.data)
        if len(resp.data) < page_size:
            break
        offset += page_size
    return all_data


def fetch_comments(client: Client, target_ids: list[int]) -> list[dict]:
    """Fetch all comments linked to the given target integer IDs."""
    if not target_ids:
        return []
    all_comments = []
    batch_size = 500
    for i in range(0, len(target_ids), batch_size):
        batch = target_ids[i:i + batch_size]
        resp = (
            client.table('comments')
            .select('id, target_id, user_id, content, created_at, edited_at, is_deleted')
            .in_('target_id', batch)
            .execute()
        )
        all_comments.extend(resp.data)
    return all_comments


def crossmatch(old_objects: list[dict], new_objects: list[dict], radius_arcsec: float):
    """
    Cross-match old objects to new objects by RA/Dec.

    Uses search_around_sky to find ALL matches within the radius,
    handling both 1:N (split) and N:1 (merge) cases.

    Returns:
        matches: list of (old_obj, new_obj, sep_arcsec) tuples
        unmatched_old: list of old objects with no new match
        unmatched_new: list of new objects with no old match
    """
    if not old_objects or not new_objects:
        return [], old_objects, new_objects

    old_coords = SkyCoord(
        ra=[o['ra'] for o in old_objects] * u.deg,
        dec=[o['dec'] for o in old_objects] * u.deg,
    )
    new_coords = SkyCoord(
        ra=[o['ra'] for o in new_objects] * u.deg,
        dec=[o['dec'] for o in new_objects] * u.deg,
    )

    # Find all pairs within the radius
    # Module-level function: idx1 → old_coords, idx2 → new_coords
    old_idx, new_idx, sep2d, _ = search_around_sky(
        old_coords, new_coords, radius_arcsec * u.arcsec,
    )

    matches = []
    matched_old_idx = set()
    matched_new_idx = set()

    for i, j, sep in zip(old_idx, new_idx, sep2d.arcsec):
        matches.append((old_objects[int(i)], new_objects[int(j)], float(sep)))
        matched_old_idx.add(int(i))
        matched_new_idx.add(int(j))

    unmatched_old = [o for i, o in enumerate(old_objects) if i not in matched_old_idx]
    unmatched_new = [o for i, o in enumerate(new_objects) if i not in matched_new_idx]

    return matches, unmatched_old, unmatched_new


def is_inspected(obj: dict) -> bool:
    """Check if an object has any inspection data."""
    return (
        obj['redshift_quality'] > 0
        or obj['redshift_inspected'] is not None
        or obj['spectral_features'] > 0
        or obj['object_flags'] > 0
        or obj['dq_flags'] > 0
    )


def resolve_merge_updates(old_objs_with_sep: list[tuple[dict, float]], new_obj: dict):
    """
    Resolve inspection data from multiple old objects into one update for a new object.

    Strategy:
      - Pick the "best" old object: highest redshift_quality, then most recent inspection
      - Copy scalar fields (quality, redshift_inspected, timestamps) from the best
      - OR bitmask flags from ALL inspected old objects (features seen in any pointing are real)
      - If multiple old objects have DIFFERENT redshift_inspected values, flag as conflict
        (skip quality/redshift propagation — needs manual re-inspection)

    Returns:
        (updates dict or None, conflict_info or None)
    """
    inspected = [(obj, sep) for obj, sep in old_objs_with_sep if is_inspected(obj)]
    if not inspected:
        return None, None

    # Check if the new object already has everything
    if new_obj['redshift_quality'] > 0 and new_obj['spectral_features'] > 0:
        # Already has quality and flags — likely already propagated
        return None, None

    # Sort by quality (desc), then last_inspected_at (desc, None last)
    def sort_key(item):
        obj = item[0]
        q = obj['redshift_quality']
        t = obj['last_inspected_at'] or ''
        return (-q, '' if t else 'zzz', t if t else '')

    inspected.sort(key=sort_key)
    best_obj = inspected[0][0]

    # Check for redshift_inspected conflicts
    manual_redshifts = set()
    for obj, _ in inspected:
        if obj['redshift_inspected'] is not None:
            manual_redshifts.add(float(obj['redshift_inspected']))

    has_z_conflict = len(manual_redshifts) > 1

    # OR all bitmask flags together
    merged_sf = 0
    merged_of = 0
    merged_dq = 0
    for obj, _ in inspected:
        merged_sf |= obj['spectral_features']
        merged_of |= obj['object_flags']
        merged_dq |= obj['dq_flags']

    updates = {}
    conflict_info = None

    if has_z_conflict:
        # Multiple conflicting manual redshifts — don't propagate quality or redshift,
        # flag for manual re-inspection
        conflict_info = {
            'type': 'redshift_conflict',
            'values': sorted(manual_redshifts),
            'sources': [obj['target_id'] for obj, _ in inspected if obj['redshift_inspected'] is not None],
        }
        # Still propagate flags — those are additive and observation-independent
    else:
        # No conflict — propagate quality + redshift from best
        if best_obj['redshift_quality'] > 0 and new_obj['redshift_quality'] == 0:
            updates['redshift_quality'] = best_obj['redshift_quality']
            updates['last_inspected_at'] = best_obj['last_inspected_at']
            updates['last_inspected_by'] = best_obj['last_inspected_by']
        if best_obj['redshift_inspected'] is not None and new_obj['redshift_inspected'] is None:
            updates['redshift_inspected'] = best_obj['redshift_inspected']

    # Propagate merged flags
    if merged_sf > 0 and new_obj['spectral_features'] == 0:
        updates['spectral_features'] = merged_sf
    if merged_of > 0 and new_obj['object_flags'] == 0:
        updates['object_flags'] = merged_of
    if merged_dq > 0 and new_obj['dq_flags'] == 0:
        updates['dq_flags'] = merged_dq

    return (updates if updates else None), conflict_info


def print_quality_breakdown(label: str, objects: list[dict]):
    """Print a breakdown of redshift_quality values."""
    counts = {}
    for obj in objects:
        q = obj['redshift_quality']
        counts[q] = counts.get(q, 0) + 1
    print(f"  {label}:")
    for q in sorted(counts):
        print(f"    {QUALITY_LABELS.get(q, f'Unknown({q})')}: {counts[q]}")


def run_migration(
    client: Client,
    old_obs_list: list[str],
    new_obs_list: list[str],
    *,
    dry_run: bool = True,
    match_radius: float = MATCH_RADIUS_ARCSEC,
):
    # ── Fetch objects ──
    all_old_objects = []
    for obs in old_obs_list:
        print(f"Fetching old objects (observation = '{obs}')...")
        objs = fetch_objects(client, obs)
        print(f"  Found {len(objs)} objects")
        all_old_objects.extend(objs)

    all_new_objects = []
    for obs in new_obs_list:
        print(f"Fetching new objects (observation = '{obs}')...")
        objs = fetch_objects(client, obs)
        print(f"  Found {len(objs)} objects")
        all_new_objects.extend(objs)

    if not all_old_objects:
        print("\nNo old objects found — nothing to migrate.")
        return
    if not all_new_objects:
        print("\nNo new objects found — deploy new observations first.")
        return

    # ── Cross-match ──
    print(f"\nCross-matching (radius = {match_radius}\")")
    matches, unmatched_old, unmatched_new = crossmatch(
        all_old_objects, all_new_objects, match_radius,
    )
    print(f"  Pairs matched:  {len(matches)}")
    print(f"  Unmatched old:  {len(unmatched_old)}")
    print(f"  Unmatched new:  {len(unmatched_new)}")

    # ── Merge statistics ──
    # Group matches by new object to detect N:1 merges
    new_to_old: dict[str, list[tuple[dict, float]]] = defaultdict(list)
    for old_obj, new_obj, sep in matches:
        new_to_old[new_obj['target_id']].append((old_obj, sep))

    n_one_to_one = sum(1 for v in new_to_old.values() if len(v) == 1)
    n_many_to_one = sum(1 for v in new_to_old.values() if len(v) > 1)
    max_merge = max((len(v) for v in new_to_old.values()), default=0)

    print(f"\n  Unique new objects matched: {len(new_to_old)}")
    print(f"    1:1 matches: {n_one_to_one}")
    print(f"    N:1 merges:  {n_many_to_one} (max {max_merge} old → 1 new)")

    # ── Quality breakdown ──
    print()
    print_quality_breakdown('Old objects', all_old_objects)
    print_quality_breakdown('New objects (all)', all_new_objects)

    # ── Crossmatch propagation audit ──
    auto_secured = [o for o in all_new_objects
                    if o['redshift_quality'] == 4 and o['last_inspected_by'] is None]
    print(f"\n  Deploy auto-secured (crossmatch on deploy): {len(auto_secured)}")

    # ── Build per-new-object update plan ──
    # Build a lookup from new object_id to new object dict
    new_obj_map = {o['target_id']: o for o in all_new_objects}

    propagation_plan = []  # (new_obj, updates, contributing_old_ids)
    conflicts = []         # (new_obj, conflict_info)
    already_done = 0
    no_inspection = 0

    for new_oid, old_objs_with_sep in new_to_old.items():
        new_obj = new_obj_map[new_oid]
        updates, conflict_info = resolve_merge_updates(old_objs_with_sep, new_obj)

        if conflict_info:
            conflicts.append((new_obj, conflict_info))

        if updates:
            old_ids = [obj['target_id'] for obj, _ in old_objs_with_sep if is_inspected(obj)]
            propagation_plan.append((new_obj, updates, old_ids))
        elif any(is_inspected(obj) for obj, _ in old_objs_with_sep):
            already_done += 1
        else:
            no_inspection += 1

    # Also count new objects with no old match
    no_inspection += len(unmatched_new)

    print(f"\n── Migration plan ──")
    print(f"  Already propagated (no action needed): {already_done}")
    print(f"  Not inspected (no action needed):      {no_inspection}")
    print(f"  Need propagation:                      {len(propagation_plan)}")
    if conflicts:
        print(f"  Redshift conflicts (need re-inspection): {len(conflicts)}")

    if propagation_plan:
        field_counts = {}
        for _, updates, _ in propagation_plan:
            for field in updates:
                field_counts[field] = field_counts.get(field, 0) + 1

        print(f"\n  Fields to propagate:")
        for field, count in sorted(field_counts.items()):
            print(f"    {field}: {count}")

        print(f"\n  Details:")
        for new_obj, updates, old_ids in propagation_plan[:20]:
            new_q = QUALITY_LABELS.get(new_obj['redshift_quality'], '?')
            fields = ', '.join(updates.keys())
            old_str = ', '.join(old_ids[:3])
            if len(old_ids) > 3:
                old_str += f' +{len(old_ids)-3}'
            print(f"    {old_str} -> {new_obj['target_id']} [{fields}]")
            print(f"      new before: q={new_q}, z_insp={new_obj['redshift_inspected']}, "
                  f"sf={new_obj['spectral_features']}, of={new_obj['object_flags']}, dq={new_obj['dq_flags']}")
            print(f"      will set: {updates}")
        if len(propagation_plan) > 20:
            print(f"    ... and {len(propagation_plan) - 20} more")

    if conflicts:
        print(f"\n  Redshift conflicts (skipped, need manual re-inspection):")
        for new_obj, info in conflicts[:10]:
            z_vals = ', '.join(f'{z:.6f}' for z in info['values'])
            sources = ', '.join(info['sources'][:3])
            print(f"    {new_obj['target_id']}: z_inspected = [{z_vals}] from [{sources}]")
        if len(conflicts) > 10:
            print(f"    ... and {len(conflicts) - 10} more")

    # ── Check for comments ──
    old_int_ids = [o['id'] for o in all_old_objects if is_inspected(o)]
    old_comments = fetch_comments(client, old_int_ids)

    # Build old_int_id -> new_int_id map (for N:1, multiple old ids map to same new id)
    id_remap: dict[int, int] = {}
    for old_obj, new_obj, sep in matches:
        id_remap[old_obj['id']] = new_obj['id']

    if old_comments:
        print(f"\n  Comments on old objects: {len(old_comments)}")
        remappable = [c for c in old_comments if c['target_id'] in id_remap]
        orphaned = [c for c in old_comments if c['target_id'] not in id_remap]
        print(f"    Can remap to new objects: {len(remappable)}")
        if orphaned:
            print(f"    Orphaned (old object unmatched): {len(orphaned)}")
    else:
        print(f"\n  Comments on old objects: 0")

    # ── Unmatched inspected old objects (concerning) ──
    unmatched_inspected = [o for o in unmatched_old if is_inspected(o)]
    if unmatched_inspected:
        print(f"\n  WARNING: {len(unmatched_inspected)} inspected old objects have no match:")
        for obj in unmatched_inspected[:10]:
            q = QUALITY_LABELS.get(obj['redshift_quality'], '?')
            print(f"    {obj['target_id']} (q={q}, z_insp={obj['redshift_inspected']})")
        if len(unmatched_inspected) > 10:
            print(f"    ... and {len(unmatched_inspected) - 10} more")

    # ── Apply ──
    if dry_run:
        print(f"\n{'='*50}")
        print("DRY RUN — no changes made")
        print(f"{'='*50}")
        return

    remappable_comments = [c for c in old_comments if c['target_id'] in id_remap] if old_comments else []

    if not propagation_plan and not remappable_comments:
        print("\nNothing to propagate — all inspection data already transferred.")
        return

    # Confirm
    total_actions = len(propagation_plan) + len(remappable_comments)
    resp = input(f"\nApply {total_actions} updates? [y/N]: ")
    if resp.lower() != 'y':
        print("Aborted.")
        return

    # Propagate inspection fields
    updated = 0
    for new_obj, updates, old_ids in propagation_plan:
        try:
            client.table('targets').update(updates).eq('id', new_obj['id']).execute()
            updated += 1
        except Exception as e:
            print(f"  Error updating {new_obj['target_id']}: {e}")

    print(f"  Updated {updated}/{len(propagation_plan)} objects")

    # Remap comments
    if remappable_comments:
        remapped = 0
        for comment in remappable_comments:
            new_int_id = id_remap.get(comment['target_id'])
            if new_int_id is None:
                continue
            try:
                client.table('comments').update(
                    {'target_id': new_int_id}
                ).eq('id', comment['id']).execute()
                remapped += 1
            except Exception as e:
                print(f"  Error remapping comment {comment['id']}: {e}")

        print(f"  Remapped {remapped} comments")

    print("\nMigration complete.")


def list_r2_keys(r2_client, bucket: str, prefix: str) -> list[str]:
    """List all object keys under a prefix in R2."""
    keys = []
    continuation_token = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': prefix}
        if continuation_token:
            kwargs['ContinuationToken'] = continuation_token
        resp = r2_client.list_objects_v2(**kwargs)
        for obj in resp.get('Contents', []):
            keys.append(obj['Key'])
        if not resp.get('IsTruncated'):
            break
        continuation_token = resp['NextContinuationToken']
    return keys


def delete_r2_keys(r2_client, bucket: str, keys: list[str]) -> int:
    """Delete keys from R2 in batches of 1000 (S3 API limit)."""
    deleted = 0
    batch_size = 1000
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        r2_client.delete_objects(
            Bucket=bucket,
            Delete={'Objects': [{'Key': k} for k in batch]},
        )
        deleted += len(batch)
    return deleted


def run_delete(
    client: Client,
    config: dict,
    old_obs_list: list[str],
):
    """Delete old observation data after migration is verified."""
    r2_client = get_r2_client(config)
    bucket = config['r2']['bucket_name']

    for old_obs in old_obs_list:
        print(f"\nPreparing to delete old observation: {old_obs}")

        old_objects = fetch_objects(client, old_obs)
        inspected = [o for o in old_objects if is_inspected(o)]

        print(f"  Objects: {len(old_objects)} ({len(inspected)} inspected)")

        if inspected:
            print(f"  WARNING: {len(inspected)} objects still have inspection data.")
            print("  Run without --delete-old first to verify migration is complete.")
            resp = input("  Continue anyway? [y/N]: ")
            if resp.lower() != 'y':
                print(f"  Skipping {old_obs}.")
                continue

        # Check for remaining comments
        old_int_ids = [o['id'] for o in old_objects]
        comments = fetch_comments(client, old_int_ids)
        if comments:
            print(f"  WARNING: {len(comments)} comments still linked to old objects.")
            print("  These will be CASCADE deleted with the objects.")
            resp = input("  Continue? [y/N]: ")
            if resp.lower() != 'y':
                print(f"  Skipping {old_obs}.")
                continue

        # Inventory R2 files
        r2_prefixes = [f'spectra/{old_obs}/', f'rgb/{old_obs}/', f'sed/{old_obs}/']
        all_r2_keys = []
        for prefix in r2_prefixes:
            keys = list_r2_keys(r2_client, bucket, prefix)
            if keys:
                print(f"  R2 {prefix}: {len(keys)} files")
                all_r2_keys.extend(keys)

        object_ids = [o['target_id'] for o in old_objects]
        print(f"\n  Will delete:")
        print(f"    {len(old_objects)} objects (+ cascade: comments, audit log)")
        print(f"    Associated spectra")
        print(f"    Shutters and slit_regions for '{old_obs}'")
        print(f"    Observation record '{old_obs}'")
        if all_r2_keys:
            print(f"    {len(all_r2_keys)} R2 files")

        resp = input(f"\n  Type '{old_obs}' to confirm deletion: ")
        if resp != old_obs:
            print(f"  Skipping {old_obs}.")
            continue

        # Delete in dependency order
        print("  Deleting...")

        batch_size = 500
        for i in range(0, len(object_ids), batch_size):
            batch = object_ids[i:i + batch_size]
            client.table('spectra').delete().in_('target_id', batch).execute()
        print(f"    Deleted spectra")

        for i in range(0, len(object_ids), batch_size):
            batch = object_ids[i:i + batch_size]
            client.table('targets').delete().in_('target_id', batch).execute()
        print(f"    Deleted {len(object_ids)} targets")

        client.table('shutters').delete().eq('observation', old_obs).execute()
        print(f"    Deleted shutters")
        client.table('slit_regions').delete().eq('observation', old_obs).execute()
        print(f"    Deleted slit_regions")

        client.table('observations').delete().eq('name', old_obs).execute()
        print(f"    Deleted observation '{old_obs}'")

        if all_r2_keys:
            print(f"    Deleting {len(all_r2_keys)} R2 files...")
            deleted = delete_r2_keys(r2_client, bucket, all_r2_keys)
            print(f"    Deleted {deleted} R2 files")

    # Refresh caches once at the end
    try:
        client.rpc('refresh_filter_options').execute()
        client.rpc('refresh_programs_overview').execute()
        print("\n  Refreshed materialized views")
    except Exception as e:
        print(f"\n  Warning: Failed to refresh views: {e}")

    print("\nDeletion complete.")


def main():
    parser = argparse.ArgumentParser(
        description='Migrate inspection data between redefined observations.',
    )
    parser.add_argument(
        '--old-obs',
        required=True,
        help='Comma-separated old observation names',
    )
    parser.add_argument(
        '--new-obs',
        required=True,
        help='Comma-separated new observation names',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show stats and migration plan without making changes',
    )
    parser.add_argument(
        '--delete-old',
        action='store_true',
        help='Delete old observation data (run after verifying migration)',
    )
    parser.add_argument(
        '--match-radius',
        type=float,
        default=MATCH_RADIUS_ARCSEC,
        help=f'Cross-match radius in arcseconds (default: {MATCH_RADIUS_ARCSEC})',
    )
    args = parser.parse_args()

    old_obs_list = [s.strip() for s in args.old_obs.split(',')]
    new_obs_list = [s.strip() for s in args.new_obs.split(',')]
    config = load_deploy_config()

    client = create_client(
        config['supabase']['url'],
        config['supabase']['service_role_key'],
    )

    if args.delete_old:
        run_delete(client, config, old_obs_list)
    else:
        run_migration(
            client,
            old_obs_list,
            new_obs_list,
            dry_run=args.dry_run,
            match_radius=args.match_radius,
        )


if __name__ == '__main__':
    main()
