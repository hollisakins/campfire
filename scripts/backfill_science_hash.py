#!/usr/bin/env python3
"""
Backfill ``science_hash`` on ``spectra`` and on the NIRSpec FITS rows of
``storage_objects`` (plan P0b, docs/design-data-releases-impl-plan.md §2).

Streams every ``_spec.fits`` / ``_lines.fits`` once from OSN (read-only GETs,
~58 GB in total for the live catalog), computes the science-content hash with
the one shared implementation (``campfire_pipeline.metadata.science_hash``) and
writes it to the matching ``spectra`` row (by ``spectrum_id``) and registry row
(by ``storage_key``) in batches of 500. Rows that already carry a hash are
skipped, so the run is resumable: kill it and start it again.

Every written ``spectra`` row's ``updated_at`` moves (``science_hash`` is in the
``bump_spectra_updated_at`` column list, decision D-P0-1), so incremental-sync
clients see each backfilled spectrum as changed once; run it in one pass.

Runs in service-role mode against OSN. Usage:

    CAMPFIRE_DEPLOY_MODE=service-role python scripts/backfill_science_hash.py \\
        [--dry-run] [--limit N] [--workers 8] [--obs NAME] [--product-type nirspec_spec|nirspec_lines]

``--dry-run --limit 50`` is the rehearsal (prints hashes, writes nothing).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from campfire.deploy.backend import make_s3_client, resolve_backend
from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client
from campfire_pipeline.metadata.science_hash import SCIENCE_HASH_VERSION, science_hash

PAGE = 1000
WRITE_BATCH = 500
PRODUCT_TYPES = ('nirspec_spec', 'nirspec_lines')


def fetch_pending(sb, product_types: tuple[str, ...], obs: str | None, limit: int | None) -> list[dict]:
    """Active NIRSpec FITS registry rows without a science hash, keyset-paged on id."""
    rows: list[dict] = []
    after = 0
    while True:
        q = (sb.table('storage_objects')
             .select('id, storage_key, spectrum_id, product_type, observation, size_bytes')
             .eq('status', 'active').eq('backend', 'osn')
             .in_('product_type', list(product_types))
             .is_('science_hash', 'null')
             .gt('id', after).order('id').limit(PAGE))
        if obs:
            q = q.eq('observation', obs)
        page = q.execute().data or []
        rows.extend(page)
        if limit and len(rows) >= limit:
            return rows[:limit]
        if len(page) < PAGE:
            return rows
        after = page[-1]['id']


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=None, help='stop after N rows (rehearsal)')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--obs', help='only this observation')
    ap.add_argument('--product-type', choices=PRODUCT_TYPES, help='only this product type')
    args = ap.parse_args()

    config = load_config(service_role=True)
    sb = get_supabase_client(config)
    bcfg = resolve_backend(config, 'osn')
    s3 = make_s3_client(bcfg, max_pool_connections=max(8, args.workers))

    types_ = (args.product_type,) if args.product_type else PRODUCT_TYPES
    rows = fetch_pending(sb, types_, args.obs, args.limit)
    total_gb = sum(int(r.get('size_bytes') or 0) for r in rows) / 1e9
    print(f"science hash v{SCIENCE_HASH_VERSION}: {len(rows)} registry rows pending ({total_gb:.1f} GB to read)")
    if not rows:
        return 0

    def work(row: dict) -> tuple[dict, str | None, str]:
        """Download to a temp file (the hasher needs seekable bytes), hash, delete."""
        fd, tmp = tempfile.mkstemp(suffix='.fits')
        os.close(fd)
        try:
            s3.download_file(bcfg.bucket, row['storage_key'], tmp)
            return row, science_hash(tmp), 'ok'
        except Exception as e:  # noqa: BLE001
            return row, None, f'error: {e}'
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def flush(batch: list[tuple[dict, str]]) -> None:
        """Write one batch: registry rows by id, spectra rows by spectrum_id.

        UPDATE, not upsert: a column-subset upsert on spectra fails NOT NULL
        before ON CONFLICT (see memory note on the partial-upsert trap)."""
        nonlocal sb
        for attempt in range(3):
            try:
                for row, h in batch:
                    sb.table('storage_objects').update({'science_hash': h}).eq('id', row['id']).execute()
                    if row['product_type'] == 'nirspec_spec' and row.get('spectrum_id'):
                        sb.table('spectra').update({'science_hash': h}).eq('spectrum_id', row['spectrum_id']).execute()
                return
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                sb = get_supabase_client(config)

    counts: dict[str, int] = {}
    errors: list[str] = []
    pending: list[tuple[dict, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, r) for r in rows]
        for fut in tqdm(as_completed(futures), total=len(futures), unit='file'):
            row, h, status = fut.result()
            kind = status.split(':')[0]
            counts[kind] = counts.get(kind, 0) + 1
            if kind == 'error':
                errors.append(f"{row['storage_key']}: {status}")
                continue
            if args.dry_run:
                if args.limit and args.limit <= 50:
                    print(f"  {row['storage_key']}  {h}")
                continue
            pending.append((row, h))
            if len(pending) >= WRITE_BATCH:
                flush(pending)
                pending = []
    if pending and not args.dry_run:
        flush(pending)

    print("Done:", ', '.join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.dry_run:
        print("(dry run: nothing written)")
    for e in errors[:10]:
        print("  ", e)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
