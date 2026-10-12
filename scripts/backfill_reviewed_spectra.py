#!/usr/bin/env python3
"""
One-time seeding of the needs-review state (docs/design-needs-review.md, P3,
decision D-P3-1).

The server-side ``seed_reviewed_spectra()`` pages over the signed-off objects
whose ``reviewed_spectrum_ids`` is still NULL and records, for each, the
member spectra that existed at sign-off (``created_at <= last_inspected_at``,
whatever their publication status is now) as the set the inspector saw, then
recomputes ``needs_review``. A spectrum deployed after the inspector's last
look is "unseen" and badges (46 objects in production on 2026-10-12), one
revoked since the sign-off "left" and badges; every other badge clears. Seeded objects are never revisited, so the script is resumable
and idempotent; ``--dry-run`` only counts.

Only a flag flip bumps ``objects.updated_at``, so sync clients see just the
objects whose badge changed.

Usage:
    CAMPFIRE_DEPLOY_MODE=service-role python scripts/backfill_reviewed_spectra.py [--dry-run] [--limit N]
"""

from __future__ import annotations

import argparse
import sys

PAGE = 1000


def run(sb, *, dry_run: bool = False, limit: int | None = None, page: int = PAGE,
        echo=print) -> dict:
    """Drive seed_reviewed_spectra() page by page; returns the totals."""
    totals = {'scanned': 0, 'seeded': 0, 'flipped': 0}
    after = 0
    while limit is None or totals['scanned'] < limit:
        n = page if limit is None else min(page, limit - totals['scanned'])
        res = sb.rpc('seed_reviewed_spectra', {
            'p_after_id': after, 'p_limit': n, 'p_dry_run': dry_run,
        }).execute().data or {}
        for k in totals:
            totals[k] += int(res.get(k) or 0)
        if res.get('last_id') is None:
            break
        after = int(res['last_id'])
        echo(f"  through object {after:>8,}  scanned {totals['scanned']:,}  "
             f"seeded {totals['seeded']:,}  badges flipped {totals['flipped']:,}")
    return totals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true', help='Count the pending objects; write nothing.')
    ap.add_argument('--limit', type=int, default=None, help='Seed at most N objects (resumable).')
    args = ap.parse_args()

    from campfire.deploy.config import load_config
    from campfire.deploy.supabase import get_supabase_client

    config = load_config(None)
    mode = config.get('supabase', {}).get('_auth_mode')
    if mode not in ('service_role', 'local'):
        print('Error: run with CAMPFIRE_DEPLOY_MODE=service-role (or local); the seed is server-side.')
        return 2
    sb = get_supabase_client(config)

    totals = run(sb, dry_run=args.dry_run, limit=args.limit)
    if args.dry_run:
        print(f"{totals['scanned']:,} signed-off object(s) without a reviewed set (nothing written)")
        return 0
    badged = (sb.table('objects').select('id', count='exact').eq('needs_review', True)
              .limit(1).execute().count) or 0
    print(f"Done: seeded {totals['seeded']:,} of {totals['scanned']:,} scanned, "
          f"badges flipped {totals['flipped']:,}; {badged:,} object(s) now need review.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
