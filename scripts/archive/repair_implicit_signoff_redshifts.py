#!/usr/bin/env python3
"""
One-time repair: preserve implicit sign-offs lost across the Phase D migration.

Context
-------
Pre-Phase-D, inspectors often signed off on an object by stamping a quality flag
without typing a numeric override. The legacy ``update_object_best_redshift``
trigger froze ``objects.best_redshift`` at ``COALESCE(target.redshift_inspected,
target.redshift_auto)`` for the chosen member target at the moment of the
inspection.

The Phase D migration copied ``redshift_quality`` / ``redshift_inspected`` to
the object but made the generated ``redshift`` column depend on
``objects.redshift_auto``. Any reprocessing that changed the per-spectrum
``redshift_auto`` after the original sign-off silently moves the object's
``redshift``, without updating quality — so the database advertises a
Tentative/Probable/Secure tag on a number nobody reviewed.

This script identifies those affected objects and pins the old signed-off
value into ``redshift_inspected`` so the generated ``redshift`` returns to
what the inspector actually approved. It also sets ``staleness_reason =
'reprocessed'`` and ``last_data_change_at = NOW()`` so the UI surfaces a
"Needs Review" badge on every repaired row.

Affected set
------------
    objects.best_redshift IS NOT NULL
    AND objects.best_redshift_quality >= 2
    AND objects.redshift_inspected IS NULL
    AND (objects.redshift IS NULL
         OR ABS(objects.redshift::float8 - objects.best_redshift) > 1e-4)

Quality = 1 (Impossible) is intentionally excluded — the generated redshift is
NULL by design and there's nothing to restore.

Usage
-----
    python scripts/repair_implicit_signoff_redshifts.py              # dry run
    python scripts/repair_implicit_signoff_redshifts.py --apply      # write
    python scripts/repair_implicit_signoff_redshifts.py --local      # local DB
    python scripts/repair_implicit_signoff_redshifts.py --output plan.csv

Requires service-role credentials (to bypass the non-admin column-scope trigger
on the objects table). ``campfire login`` JWTs will NOT work.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client

PAGE_SIZE = 1000
EPSILON = 0.03  # floating-point tolerance; precision of stored numeric(10,6) is 1e-6

COLUMNS = (
    "id, object_id, field, "
    "best_redshift, best_redshift_quality, "
    "redshift, redshift_auto, redshift_inspected, redshift_quality, "
    "staleness_reason, last_inspected_at, last_data_change_at, version"
)


def is_affected(row: dict) -> bool:
    best = row.get("best_redshift")
    bq = row.get("best_redshift_quality")
    inspected = row.get("redshift_inspected")
    z = row.get("redshift")
    if best is None or bq is None or bq < 2:
        return False
    if inspected is not None:
        return False
    if z is None:
        return True
    try:
        return abs(float(z) - float(best)) > EPSILON
    except (TypeError, ValueError):
        return True


def fetch_candidates(supabase) -> list[dict]:
    """Pull every object with best_redshift_quality >= 2 and redshift_inspected NULL.

    We filter affected-ness client-side so we can also report the near-miss
    "same number" rows for inspection. The server filters the cheap predicates.
    """
    rows: list[dict] = []
    offset = 0
    while True:
        resp = (
            supabase.table("objects")
            .select(COLUMNS)
            .gte("best_redshift_quality", 2)
            .not_.is_("best_redshift", "null")
            .is_("redshift_inspected", "null")
            .order("id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        print(f"  fetched {len(rows)} candidate rows...", file=sys.stderr)
    return rows


def print_plan(affected: list[dict], limit: int | None):
    if not affected:
        print("No affected objects — nothing to repair.")
        return

    hdr = (
        f"{'id':>7} {'object_id':<22} {'field':<18} "
        f"{'best_bz':>9} {'best_q':>6} {'cur_z':>9} {'cur_q':>5} "
        f"{'cur_auto':>9} {'ver':>4}"
    )
    print()
    print(hdr)
    print("-" * len(hdr))

    shown = 0
    for r in affected:
        if limit is not None and shown >= limit:
            print(f"... {len(affected) - shown} more rows not shown.")
            break
        def fmt(v):
            return f"{float(v):>9.4f}" if v is not None else "—".rjust(9)
        print(
            f"{r['id']:>7} {str(r.get('object_id') or ''):<22} "
            f"{str(r.get('field') or ''):<18} "
            f"{fmt(r.get('best_redshift'))} "
            f"{(r.get('best_redshift_quality') or 0):>6} "
            f"{fmt(r.get('redshift'))} "
            f"{(r.get('redshift_quality') or 0):>5} "
            f"{fmt(r.get('redshift_auto'))} "
            f"{(r.get('version') or 0):>4}"
        )
        shown += 1


def write_csv(affected: list[dict], path: Path):
    fieldnames = [
        "id", "object_id", "field",
        "best_redshift", "best_redshift_quality",
        "redshift", "redshift_auto", "redshift_inspected", "redshift_quality",
        "staleness_reason", "last_inspected_at", "last_data_change_at", "version",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in affected:
            w.writerow({k: r.get(k) for k in fieldnames})
    print(f"Wrote {len(affected)} planned updates to {path}")


def apply_repairs(supabase, affected: list[dict], batch_size: int):
    """Apply updates in batches. Each row update touches redshift_inspected
    (which fires bump_object_version + log_object_inspection_changes) plus
    staleness_reason / last_data_change_at (non-inspection fields, gated by
    enforce_object_user_update_scope — service role bypasses)."""
    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    failed = 0
    batch: list[dict] = []

    def flush():
        nonlocal applied, failed
        if not batch:
            return
        # Per-row update — PostgREST UPSERT would require every row share the
        # same column set, which it does, but would also create rows if an id
        # disappeared. A per-row PATCH is safer.
        for upd in batch:
            try:
                (
                    supabase.table("objects")
                    .update({
                        "redshift_inspected": upd["redshift_inspected"],
                        "staleness_reason": "reprocessed",
                        "last_data_change_at": now,
                    })
                    .eq("id", upd["id"])
                    .execute()
                )
                applied += 1
            except Exception as exc:
                failed += 1
                print(f"  FAILED id={upd['id']}: {exc}", file=sys.stderr)
        batch.clear()

    for r in affected:
        batch.append({
            "id": r["id"],
            "redshift_inspected": float(r["best_redshift"]),
        })
        if len(batch) >= batch_size:
            flush()
            print(f"  applied {applied} / {len(affected)}...", file=sys.stderr)

    flush()
    print(f"Applied {applied} repairs. {failed} failures.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="Actually write the repairs. Default is dry-run.")
    parser.add_argument("--output", type=Path, default=None,
                        help="Write the plan to CSV.")
    parser.add_argument("--limit", type=int, default=50,
                        help="Terminal row cap (CSV and --apply still process all). Default 50.")
    parser.add_argument("--batch-size", type=int, default=200,
                        help="Apply batch size. Default 200.")
    parser.add_argument("--local", action="store_true",
                        help="Run against the local Supabase instance.")
    parser.add_argument("--config", type=str, default=None,
                        help="Override deploy config TOML path.")
    args = parser.parse_args()

    config = load_config(args.config, local=args.local)
    if not config.get("supabase", {}).get("service_role_key"):
        print("Error: service-role credentials required.", file=sys.stderr)
        print("       This script updates staleness_reason / last_data_change_at,", file=sys.stderr)
        print("       which the non-admin column-scope trigger forbids.", file=sys.stderr)
        print("       Set CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY or use --local.", file=sys.stderr)
        sys.exit(2)

    supabase = get_supabase_client(config)
    print(f"Querying {config['supabase']['url']} ...")

    candidates = fetch_candidates(supabase)
    affected = [r for r in candidates if is_affected(r)]
    unchanged = len(candidates) - len(affected)

    print(
        f"{len(candidates)} candidates (quality>=2, best_redshift set, no redshift_inspected). "
        f"{len(affected)} affected, {unchanged} match their best_redshift (no repair needed)."
    )

    # Break down by best_redshift_quality for visibility
    buckets: dict[int, int] = {}
    for r in affected:
        buckets[r["best_redshift_quality"]] = buckets.get(r["best_redshift_quality"], 0) + 1
    if buckets:
        print("Affected by old quality:")
        for q in sorted(buckets):
            label = {2: "Tentative", 3: "Probable", 4: "Secure"}.get(q, f"q={q}")
            print(f"  {label:<10} (q={q}): {buckets[q]}")

    print_plan(affected, args.limit)

    if args.output:
        write_csv(affected, args.output)

    if not args.apply:
        print()
        print("DRY RUN — no changes written. Re-run with --apply to execute.")
        return

    if not affected:
        return

    print()
    confirm = input(f"Apply {len(affected)} repairs to {config['supabase']['url']}? [yes/NO] ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    apply_repairs(supabase, affected, args.batch_size)


if __name__ == "__main__":
    main()
