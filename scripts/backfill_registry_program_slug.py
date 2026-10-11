#!/usr/bin/env python3
"""
Backfill ``storage_objects.program_slug`` for existing NIRSpec rows (plan P2).

The column is the row-local program scope archive rows are authorized on; live
NIRSpec rows get it too so a retain step can copy it onto the archive row
without a live join. Source of truth: ``observations.program_slug`` (every
NIRSpec registry row carries ``observation``). One PostgREST UPDATE per
observation (~230 statements, ~2k rows each), resumable: rows that already have
the slug are skipped by the filter. Nothing touches ``updated_at`` (no trigger
lists ``program_slug``), so sync clients are not disturbed.

Runs in service-role mode. Usage:

    CAMPFIRE_DEPLOY_MODE=service-role python scripts/backfill_registry_program_slug.py [--dry-run] [--obs NAME]
"""
from __future__ import annotations

import argparse
import sys

from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--obs', help='only this observation')
    args = ap.parse_args()

    config = load_config(service_role=True)
    sb = get_supabase_client(config)

    q = sb.table('observations').select('name, program_slug').order('name')
    if args.obs:
        q = q.eq('name', args.obs)
    observations = q.execute().data or []
    print(f"{len(observations)} observations")

    total = 0
    for row in observations:
        obs, slug = row['name'], row['program_slug']
        if not slug:
            print(f"  {obs}: no program_slug on the observation row, skipped")
            continue
        pending = (sb.table('storage_objects').select('id', count='exact')
                   .eq('instrument', 'nirspec').eq('observation', obs)
                   .is_('program_slug', 'null').limit(1).execute())
        n = pending.count or 0
        if n == 0:
            continue
        print(f"  {obs}: {n} rows -> {slug}" + (" (dry run)" if args.dry_run else ""))
        if not args.dry_run:
            for attempt in range(3):
                try:
                    (sb.table('storage_objects').update({'program_slug': slug})
                       .eq('instrument', 'nirspec').eq('observation', obs)
                       .is_('program_slug', 'null').execute())
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    sb = get_supabase_client(config)
        total += n
    print(f"Done: {total} rows" + (" would be" if args.dry_run else "") + " updated")
    return 0


if __name__ == '__main__':
    sys.exit(main())
