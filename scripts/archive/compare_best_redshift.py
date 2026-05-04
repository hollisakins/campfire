#!/usr/bin/env python3
"""
Compare legacy ``objects.best_redshift`` (frozen after PR #99) against the new
``objects.redshift`` generated column.

PR #99 dropped the trigger that maintained ``best_redshift`` but kept the
column, so its stored values reflect the pre-migration consensus. The new
``redshift`` column is derived from ``redshift_inspected`` / ``redshift_auto``
/ ``redshift_quality`` at the object level, with ``redshift_auto`` now picked
by grating-priority (PRISM > medium > high-res) rather than per-target.

Usage:
    python scripts/compare_best_redshift.py
    python scripts/compare_best_redshift.py --threshold 0.05
    python scripts/compare_best_redshift.py --output diffs.csv
    python scripts/compare_best_redshift.py --include-null-new
    python scripts/compare_best_redshift.py --local
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

# Make the unified campfire package importable without requiring an editable
# install, mirroring redeploy_redshifts.py.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client

PAGE_SIZE = 1000

COLUMNS = (
    "id, object_id, field, ra, dec, "
    "best_redshift, best_redshift_quality, "
    "redshift, redshift_auto, redshift_inspected, redshift_quality, "
    "staleness_reason, last_inspected_at, last_data_change_at, "
    "n_targets, n_spectra, gratings"
)


def needs_review(row: dict) -> bool:
    """Mirror StalenessBadge logic (web/components/spectra/StalenessBadge.tsx).

    The UI shows a "Needs Review" badge when all three hold:
      1. staleness_reason is set
      2. last_inspected_at is set (i.e. it has actually been inspected)
      3. last_data_change_at is later than last_inspected_at
    """
    reason = row.get("staleness_reason")
    last_inspected = row.get("last_inspected_at")
    last_data_change = row.get("last_data_change_at")
    if not reason or not last_inspected:
        return False
    if not last_data_change:
        return False
    return last_data_change > last_inspected


def fetch_all_objects(supabase) -> list[dict]:
    """Page through objects where best_redshift is set."""
    rows: list[dict] = []
    offset = 0
    while True:
        resp = (
            supabase.table("objects")
            .select(COLUMNS)
            .not_.is_("best_redshift", "null")
            .order("id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        print(f"  fetched {len(rows)} objects...", file=sys.stderr)
    return rows


def classify(row: dict, threshold: float, include_null_new: bool):
    old = row.get("best_redshift")
    new = row.get("redshift")

    if old is None:
        return None  # should not happen — filtered out server-side

    if new is None:
        if include_null_new:
            return ("null_new", float("inf"))
        return None

    dz = float(new) - float(old)
    if abs(dz) > threshold:
        return ("diff", dz)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--threshold", type=float, default=0.1,
        help="|redshift - best_redshift| threshold for flagging (default 0.1).",
    )
    parser.add_argument(
        "--include-null-new", action="store_true",
        help="Also report objects whose new redshift is NULL (e.g. quality=1 Impossible).",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="Optional CSV path to write the flagged rows to.",
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Query local Supabase instead of production.",
    )
    parser.add_argument(
        "--config", type=str, default=None,
        help="Override deploy config TOML path.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Stop after printing N flagged rows to the terminal (CSV still gets all).",
    )
    args = parser.parse_args()

    config = load_config(args.config, local=args.local)
    supabase = get_supabase_client(config)

    target_url = config["supabase"]["url"]
    print(f"Querying {target_url} ...")

    rows = fetch_all_objects(supabase)
    print(f"Fetched {len(rows)} objects with best_redshift IS NOT NULL.")

    flagged: list[tuple[dict, str, float]] = []
    for row in rows:
        verdict = classify(row, args.threshold, args.include_null_new)
        if verdict is None:
            continue
        kind, dz = verdict
        flagged.append((row, kind, dz))

    # Sort: largest absolute delta first, null_new at the top
    flagged.sort(key=lambda x: (x[1] != "null_new", -abs(x[2]) if x[2] != float("inf") else 0))

    print()
    print(
        f"Flagged {len(flagged)} / {len(rows)} objects "
        f"(|dz| > {args.threshold}"
        f"{', including NULL-new' if args.include_null_new else ''})."
    )

    if not flagged:
        return

    # Terminal report
    hdr = (
        f"{'id':>7} {'object_id':<22} {'field':<18} "
        f"{'old_bz':>8} {'old_q':>5} "
        f"{'new_z':>8} {'new_q':>5} {'dz':>8} {'z_auto':>8} "
        f"{'z_insp':>8} {'rev':<4} {'staleness':<22}"
    )
    print()
    print(hdr)
    print("-" * len(hdr))

    printed = 0
    for row, kind, dz in flagged:
        if args.limit is not None and printed >= args.limit:
            remaining = len(flagged) - printed
            print(f"... {remaining} more rows not shown (raise --limit or use --output).")
            break

        def fmt(v, spec=">8.4f"):
            return format(v, spec) if v is not None else "—".rjust(8)

        dz_str = "NULL" if kind == "null_new" else f"{dz:>+8.4f}"
        review = "YES" if needs_review(row) else ""
        print(
            f"{row['id']:>7} {str(row.get('object_id') or ''):<22} "
            f"{str(row.get('field') or ''):<18} "
            f"{fmt(row.get('best_redshift')):>8} "
            f"{(row.get('best_redshift_quality') if row.get('best_redshift_quality') is not None else '—')!s:>5} "
            f"{fmt(row.get('redshift')):>8} "
            f"{(row.get('redshift_quality') if row.get('redshift_quality') is not None else '—')!s:>5} "
            f"{dz_str:>8} "
            f"{fmt(row.get('redshift_auto')):>8} "
            f"{fmt(row.get('redshift_inspected')):>8} "
            f"{review:<4} "
            f"{str(row.get('staleness_reason') or ''):<22}"
        )
        printed += 1

    if args.output:
        fieldnames = [
            "id", "object_id", "field", "ra", "dec",
            "best_redshift", "best_redshift_quality",
            "redshift", "redshift_auto", "redshift_inspected", "redshift_quality",
            "delta_z", "kind", "needs_review",
            "staleness_reason", "last_inspected_at", "last_data_change_at",
            "n_targets", "n_spectra", "gratings",
        ]
        with open(args.output, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row, kind, dz in flagged:
                out = {k: row.get(k) for k in fieldnames if k in row}
                out["delta_z"] = "" if kind == "null_new" else f"{dz:.6f}"
                out["kind"] = kind
                out["needs_review"] = "yes" if needs_review(row) else "no"
                w.writerow(out)
        print()
        print(f"Wrote {len(flagged)} rows to {args.output}")


if __name__ == "__main__":
    main()
