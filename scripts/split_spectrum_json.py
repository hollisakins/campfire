#!/usr/bin/env python3
"""
One-time backfill: emit the 1-D sidecar (``<stem>_spec_1d.json``) next to every
deployed spectrum JSON that lacks one (perf T2-D2, #508).

New deploys write both files; this splits the existing ``*_spec.json`` objects
cloud-side so the web can paint the primary trace before the 2-D S/N payload.
For each active ``spectrum_json`` registry row on OSN without a
``spectrum_1d_json`` sibling row: GET the full JSON, keep the 1-D keys
(campfire.deploy.generate.spectrum_1d_payload), PUT the sibling, then register
it (bytes land before the hash is registered) inheriting the source row's
deployment_id / cfpipe_version / uploaded_by.

Needs service-role Supabase auth and OSN *write* credentials (CAMPFIRE_S3_OSN_*).

Usage:
    CAMPFIRE_DEPLOY_MODE=service-role python scripts/split_spectrum_json.py [--dry-run] [--workers 8] [--obs NAME] [--limit N]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from tqdm import tqdm

from campfire.deploy.backend import make_s3_client, resolve_backend
from campfire.deploy.config import load_config
from campfire.deploy.generate import spectrum_1d_payload
from campfire.deploy.registry import row_for_key, upsert_storage_objects
from campfire.deploy.supabase import get_supabase_client
from campfire_layout import derive_sibling

PAGE = 1000
REGISTER_BATCH = 200


def fetch_registry(sb, product_type: str, obs: str | None) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = (sb.table('storage_objects')
             .select('storage_key, deployment_id, cfpipe_version, uploaded_by')
             .eq('product_type', product_type).eq('status', 'active').eq('backend', 'osn')
             .order('id').range(offset, offset + PAGE - 1))
        if obs:
            q = q.eq('observation', obs)
        page = q.execute().data or []
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += PAGE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--obs', help='only this observation')
    ap.add_argument('--limit', type=int, help='stop after N sidecars (smoke test)')
    args = ap.parse_args()

    config = load_config(service_role=True)
    sb = get_supabase_client(config)
    bcfg = resolve_backend(config, 'osn')
    s3 = make_s3_client(bcfg, max_pool_connections=max(8, args.workers))

    sources = fetch_registry(sb, 'spectrum_json', args.obs)
    have_1d = {r['storage_key'] for r in fetch_registry(sb, 'spectrum_1d_json', args.obs)}
    todo = [r for r in sources if derive_sibling(r['storage_key'], 'spectrum_1d_json') not in have_1d]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(sources)} spectrum JSONs on OSN, {len(have_1d)} already split, {len(todo)} to do")
    if not todo or args.dry_run:
        if args.dry_run:
            for r in todo[:5]:
                print("  ", r['storage_key'], '->', derive_sibling(r['storage_key'], 'spectrum_1d_json'))
            print("(dry run: nothing written)")
        return 0

    def work(src: dict) -> tuple[dict | None, str]:
        key = src['storage_key']
        key_1d = derive_sibling(key, 'spectrum_1d_json')
        try:
            body = s3.get_object(Bucket=bcfg.bucket, Key=key)['Body'].read()
            payload = json.dumps(spectrum_1d_payload(json.loads(body)), allow_nan=False).encode()
            s3.put_object(Bucket=bcfg.bucket, Key=key_1d, Body=payload, ContentType='application/json')
        except Exception as e:  # noqa: BLE001
            return None, f"{key}: {e}"
        row = row_for_key(
            key_1d, backend='osn',
            content_hash='sha256:' + hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload), content_type='application/json',
            deployment_id=src.get('deployment_id'), uploaded_by=src.get('uploaded_by'),
            cfpipe_version=src.get('cfpipe_version'),
        )
        return row, 'ok'

    pending: list[dict] = []
    errors: list[str] = []
    registered = 0

    def flush() -> None:
        nonlocal pending, registered
        if pending:
            registered += upsert_storage_objects(sb, pending)
            pending = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(work, r) for r in todo]
        for fut in tqdm(as_completed(futures), total=len(futures), unit='file'):
            row, status = fut.result()
            if row is None:
                errors.append(status)
                continue
            pending.append(row)
            if len(pending) >= REGISTER_BATCH:
                flush()
    flush()

    print(f"Registered {registered} 1-D sidecars; {len(errors)} errors")
    for e in errors[:10]:
        print("  ", e)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
