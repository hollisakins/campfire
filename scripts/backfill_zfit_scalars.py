#!/usr/bin/env python3
"""
One-time backfill: persist the zfit scalars (chi2_min, confidence) onto
``spectra`` rows from the deployed zfit JSON sidecars (perf T2-D2, #508).

New deploys write both columns from the summary ECSV; this fills rows that
predate the columns so the object page's redshift-fit summary fetches no
sidecar for them, and fills a NULL redshift_auto from the same sidecar (rows
from before per-grating redshift_auto existed; never overwrites a value).
Rows whose zfit sidecar does not exist keep NULL (no fit).

Every written row's ``updated_at`` moves (bump_spectra_updated_at), so
incremental-sync clients (``p_updated_since``) see each backfilled spectrum
as changed once; run it in one pass rather than dribbling it over days.

Runs in service-role mode against OSN (read-only GETs; only the DB is written).

Usage:
    CAMPFIRE_DEPLOY_MODE=service-role python scripts/backfill_zfit_scalars.py [--dry-run] [--workers 8] [--obs NAME]
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from campfire.deploy.backend import make_s3_client, resolve_backend
from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client
from campfire_layout import derive_sibling, parse_key, storage_key
from campfire_layout.keys import KeyScheme

PAGE = 1000


def _finite(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float('inf'), float('-inf')) else None


def fetch_rows(sb, obs: str | None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = (sb.table('spectra').select('id, fits_path, observation, redshift_auto')
             .is_('chi2_min', 'null').order('id').range(offset, offset + PAGE - 1))
        if obs:
            q = q.eq('observation', obs)
        page = q.execute().data or []
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


def zfit_key(fits_path: str) -> str:
    """Canonical zfit sidecar key for a spectra.fits_path (either scheme)."""
    pk = parse_key(derive_sibling(fits_path, 'zfit'))
    return storage_key(pk.product_type, pk.scope, pk.filename, scheme=KeyScheme.CANONICAL)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--obs', help='only this observation')
    args = ap.parse_args()

    config = load_config(service_role=True)
    sb = get_supabase_client(config)
    bcfg = resolve_backend(config, 'osn')
    s3 = make_s3_client(bcfg, max_pool_connections=max(8, args.workers))

    rows = fetch_rows(sb, args.obs)
    print(f"{len(rows)} spectra rows with chi2_min IS NULL")
    if not rows:
        return 0

    def work(row: dict) -> tuple[int, dict | None, str]:
        key = zfit_key(row['fits_path'])
        try:
            obj = s3.get_object(Bucket=bcfg.bucket, Key=key)
            data = json.load(obj['Body'])
        except s3.exceptions.NoSuchKey:
            return row['id'], None, 'no-fit'
        except Exception as e:  # noqa: BLE001
            if 'NoSuchKey' in str(e) or '404' in str(e):
                return row['id'], None, 'no-fit'
            return row['id'], None, f'error: {e}'
        patch = {'chi2_min': _finite(data.get('chi2_min')), 'confidence': _finite(data.get('confidence'))}
        # Pre-Phase-B rows carry no per-grating redshift_auto; the fit summary
        # reads the redshift from the row once the scalars are present, so fill
        # it from the same sidecar — fill only, never overwrite a deployed value.
        if row.get('redshift_auto') is None:
            z = _finite(data.get('redshift'))
            if z is not None:
                patch['redshift_auto'] = z
        if patch['chi2_min'] is None and patch['confidence'] is None:
            return row['id'], None, 'no-scalars'
        return row['id'], patch, 'ok'

    def write(rid: int, patch: dict) -> None:
        """One row UPDATE, surviving a dropped connection: PostgREST (behind
        HTTP/2) closes a connection after ~20k streams and httpx raises
        RemoteProtocolError rather than reconnecting, so rebuild the client
        and retry instead of losing the run 12 % in."""
        nonlocal sb
        for attempt in range(3):
            try:
                sb.table('spectra').update(patch).eq('id', rid).execute()
                return
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                sb = get_supabase_client(config)

    counts: dict[str, int] = {}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, r) for r in rows]
        for fut in tqdm(as_completed(futures), total=len(futures), unit='row'):
            rid, patch, status = fut.result()
            kind = status.split(':')[0]
            counts[kind] = counts.get(kind, 0) + 1
            if kind == 'error':
                errors.append(f"{rid}: {status}")
                continue
            if patch and not args.dry_run:
                write(rid, patch)

    print("Done:", ', '.join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.dry_run:
        print("(dry run: nothing written)")
    for e in errors[:10]:
        print("  ", e)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
