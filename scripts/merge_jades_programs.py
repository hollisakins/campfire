#!/usr/bin/env python3
"""
Merge the proprietary ``jades_inprep`` program into the public ``jades``
program (issue: JADES goes public now that the team's inspected redshifts
have been pushed).

There is no sanctioned program-rename command — the config plane treats a
rename as retire-old + push-new — so this one-off script performs the data
moves directly, in FK-safe order:

  1. Pre-flight report of everything that references ``jades_inprep``.
  2. Update the merged ``jades`` programs row metadata (name/description/
     cycle; it is already public). PID display is derived from
     ``observations`` by ``mv_programs_overview``, nothing to store.
  3. observations.program_slug: jades_inprep -> jades (25 rows).
  4. targets.program_slug:      jades_inprep -> jades (~5,066 rows).
  5. Access cleanup: delete user_program_access grants for jades_inprep
     (redundant once public) and rewrite any access_codes/pending_invites
     program_slugs arrays containing the old slug.
  6. Soft-retire the now-empty jades_inprep row (sets programs.retired_at,
     same as ``campfire config retire programs jades_inprep``). The row is
     kept — FKs to programs(slug) have no ON UPDATE CASCADE and the config
     plane never deletes rows.

NOT done here (run separately):
  - ``campfire deploy objects reconcile --field goods-s / goods-n`` to
    rebuild objects.programs[]/observations[]/search_text and refresh the
    materialized views.
  - The reduction machine's observations.toml still says
    ``program = 'jades_inprep'`` for these observations and the pipeline
    ECSV summaries bake that slug into their metadata: fix the TOML and
    re-run ``cfpipe nirspec summary --obs <name>`` there before any
    redeploy, or the redeploy resurrects the old slug in targets.

Usage
-----
    python scripts/merge_jades_programs.py --dry-run
    python scripts/merge_jades_programs.py
    python scripts/merge_jades_programs.py --local
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client


OLD_SLUG = "jades_inprep"
NEW_SLUG = "jades"

# Merged program metadata for the surviving `jades` row. The old row said
# "JADES origins field" (cycle-2 PID 3215 only); the merged program spans
# the cycle-1 GTO PIDs too, so present it as the full survey.
MERGED_METADATA = {
    "program_name": "JADES",
    "pi_name": "D. Eisenstein",
    "description": "JWST Advanced Deep Extragalactic Survey",
    "cycle": 1,
}

PAGE_SIZE = 1000


def count(sb, table: str, column: str, value: str) -> int:
    r = (sb.table(table).select("*", count="exact", head=True)
         .eq(column, value).execute())
    return r.count or 0


def rewrite_slug_array(arr: list[str]) -> list[str]:
    """Replace OLD_SLUG with NEW_SLUG, dedupe preserving order."""
    out: list[str] = []
    for s in arr:
        s = NEW_SLUG if s == OLD_SLUG else s
        if s not in out:
            out.append(s)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge jades_inprep into the public jades program."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; do not write to the database")
    parser.add_argument("--local", action="store_true",
                        help="Run against the local Supabase instance")
    parser.add_argument("--config", type=str, default=None,
                        help="Override deploy config TOML path")
    args = parser.parse_args()

    config = load_config(args.config, local=args.local,
                         service_role=not args.local)
    sb = get_supabase_client(config)
    print(f"Using Supabase: {config['supabase']['url']}")
    if args.dry_run:
        print("*** DRY RUN — no changes will be made ***")

    # ------------------------------------------------------------------
    # 1. Pre-flight
    # ------------------------------------------------------------------
    progs = {p["slug"]: p for p in
             sb.table("programs").select("*")
             .in_("slug", [OLD_SLUG, NEW_SLUG]).execute().data}
    if NEW_SLUG not in progs:
        print(f"Error: surviving program '{NEW_SLUG}' not found.")
        sys.exit(1)
    if OLD_SLUG not in progs:
        print(f"Error: '{OLD_SLUG}' not found — nothing to merge.")
        sys.exit(1)
    if not progs[NEW_SLUG]["is_public"]:
        print(f"Error: '{NEW_SLUG}' is not public; expected it to be.")
        sys.exit(1)
    if progs[OLD_SLUG]["retired_at"]:
        print(f"Note: '{OLD_SLUG}' is already retired "
              f"({progs[OLD_SLUG]['retired_at']}); continuing.")

    n_obs = count(sb, "observations", "program_slug", OLD_SLUG)
    n_targets = count(sb, "targets", "program_slug", OLD_SLUG)
    upa = (sb.table("user_program_access").select("user_id, program_slug")
           .eq("program_slug", OLD_SLUG).execute().data)
    upa_new = (sb.table("user_program_access").select("user_id")
               .eq("program_slug", NEW_SLUG).execute().data)

    codes = (sb.table("access_codes").select("id, program_slugs")
             .cs("program_slugs", [OLD_SLUG]).execute().data)
    invites = (sb.table("pending_invites").select("id, program_slugs")
               .cs("program_slugs", [OLD_SLUG]).execute().data)

    print("\n--- Pre-flight ---")
    print(f"  observations on {OLD_SLUG}:        {n_obs}")
    print(f"  targets on {OLD_SLUG}:             {n_targets}")
    print(f"  user_program_access grants:        {len(upa)} "
          f"(users: {[u['user_id'][:8] for u in upa]})")
    print(f"  access_codes carrying the slug:    {len(codes)}")
    print(f"  pending_invites carrying the slug: {len(invites)}")

    if args.dry_run:
        print("\nWould apply, in order:")
        print(f"  1. programs[{NEW_SLUG}] metadata -> {MERGED_METADATA}")
        print(f"  2. observations: {n_obs} rows -> program_slug='{NEW_SLUG}'")
        print(f"  3. targets: {n_targets} rows -> program_slug='{NEW_SLUG}'")
        print(f"  4. delete {len(upa)} user_program_access rows for {OLD_SLUG}")
        for c in codes:
            print(f"     access_codes id={c['id']}: {c['program_slugs']} -> "
                  f"{rewrite_slug_array(c['program_slugs'])}")
        for i in invites:
            print(f"     pending_invites id={i['id']}: {i['program_slugs']} -> "
                  f"{rewrite_slug_array(i['program_slugs'])}")
        print(f"  5. retire programs[{OLD_SLUG}] (set retired_at)")
        print("\nThen run: campfire deploy objects reconcile for goods-s, goods-n")
        return

    # ------------------------------------------------------------------
    # 2. Merged program metadata
    # ------------------------------------------------------------------
    sb.table("programs").update(MERGED_METADATA).eq("slug", NEW_SLUG).execute()
    print(f"\nUpdated programs[{NEW_SLUG}] metadata: {MERGED_METADATA}")

    # ------------------------------------------------------------------
    # 3 + 4. Repoint observations, then targets
    # ------------------------------------------------------------------
    moved_obs = (sb.table("observations")
                 .update({"program_slug": NEW_SLUG})
                 .eq("program_slug", OLD_SLUG).execute().data)
    print(f"observations repointed: {len(moved_obs)}")

    # One UPDATE over ~5k rows exceeds the PostgREST statement timeout, so
    # move targets in id batches. Atomic-per-batch; the whole loop is
    # idempotent (re-running picks up whatever is still on OLD_SLUG).
    moved = 0
    while True:
        ids = [t["id"] for t in
               (sb.table("targets").select("id")
                .eq("program_slug", OLD_SLUG).limit(500).execute().data)]
        if not ids:
            break
        (sb.table("targets")
         .update({"program_slug": NEW_SLUG})
         .in_("id", ids).execute())
        moved += len(ids)
        print(f"  targets repointed: {moved} ...", flush=True)
    print(f"targets repointed:      {moved}")

    # ------------------------------------------------------------------
    # 5. Access cleanup
    # ------------------------------------------------------------------
    if upa:
        deleted = (sb.table("user_program_access").delete()
                   .eq("program_slug", OLD_SLUG).execute().data)
        print(f"user_program_access rows deleted: {len(deleted)} "
              f"(program is public now)")

    for c in codes:
        sb.table("access_codes").update(
            {"program_slugs": rewrite_slug_array(c["program_slugs"])}
        ).eq("id", c["id"]).execute()
    for i in invites:
        sb.table("pending_invites").update(
            {"program_slugs": rewrite_slug_array(i["program_slugs"])}
        ).eq("id", i["id"]).execute()
    if codes or invites:
        print(f"slug arrays rewritten: {len(codes)} access_codes, "
              f"{len(invites)} pending_invites")

    # ------------------------------------------------------------------
    # 6. Retire the empty program
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc).isoformat()
    sb.table("programs").update({"retired_at": now}).eq("slug", OLD_SLUG).execute()
    print(f"programs[{OLD_SLUG}] retired at {now}")

    # ------------------------------------------------------------------
    # Post-check
    # ------------------------------------------------------------------
    print("\n--- Post-check ---")
    print(f"  observations on {OLD_SLUG}: {count(sb, 'observations', 'program_slug', OLD_SLUG)} (want 0)")
    print(f"  targets on {OLD_SLUG}:      {count(sb, 'targets', 'program_slug', OLD_SLUG)} (want 0)")
    print(f"  observations on {NEW_SLUG}: {count(sb, 'observations', 'program_slug', NEW_SLUG)}")
    print(f"  targets on {NEW_SLUG}:      {count(sb, 'targets', 'program_slug', NEW_SLUG)}")
    print("\nNext: campfire deploy objects reconcile --field goods-s / goods-n")
    print("      (rebuilds objects.programs[]/search_text, refreshes matviews)")
    print("Reminder: fix observations.toml + regenerate ECSV summaries on the")
    print("reduction machine before any JADES redeploy.")


if __name__ == "__main__":
    main()
