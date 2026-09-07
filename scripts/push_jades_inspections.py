#!/usr/bin/env python3
"""
Push JADES catalog redshift inspections into the CAMPFIRE database.

Variant of ``push_inspections.py`` (the AnthonyBot push) adapted for the
JADES team's compiled inspection catalog (CSV: ``RA, Dec, z_Spec,
z_Spec_flag``). The JADES flags map 1:1 onto CAMPFIRE's
``REDSHIFT_QUALITY`` enum (1=Impossible, 2=Tentative, 3=Probable,
4=Secure).

Policy: only *confirmations* propagate automatically. A row is written iff

  - the JADES flag is good (>= MIN_GOOD_FLAG, i.e. Probable or Secure), AND
  - the object has a ``redshift_auto``, AND
  - |z_jades - z_auto| <= REDSHIFT_DISAGREEMENT_THRESHOLD.

Confirmed rows are written with the JADES flag as quality and
``redshift_inspected = NULL``, so the ``pin_redshift_on_signoff`` trigger
copies ``redshift_auto`` into ``redshift_inspected`` with
``inspected_used_auto = true`` (auto-pinned, not a typed override).

Everything else that matched a not-yet-inspected object is exported to
``jades_manual_review.csv`` with a reason column instead of being written:
  - ``disagreement``     good flag but |dz| > threshold
  - ``no_z_auto``        good flag but nothing of ours to confirm against
  - ``tentative_flag``   flag 2 (JADES themselves are ~50% confident)
  - ``impossible_flag``  flag 1 (JADES saw no z; often our auto-fit has one
                         — a human should arbitrate before we null it out)

Other details:
  - Matching is scoped to objects belonging to the JADES program
    (``jades``) instead of the whole objects catalog: a
    JADES catalog z landing on an unrelated program's object is more likely
    coincidence than confirmation.
  - Rows with flag >= 2 but no finite z are skipped at parse and reported.

Kept from the original: positional dedup of the input with internal-conflict
detection, skip-already-inspected (never clobber a human inspection,
``redshift_quality > 0``), AnthonyBot JWT impersonation for audit
attribution, ``--dry-run`` / ``--local``, and CSV reports written next to
the input (on dry runs too, so the push can be iterated before anything
permanent happens).

Usage
-----
    python scripts/push_jades_inspections.py --input ~/Downloads/JADES_for_Campfire.csv --dry-run
    python scripts/push_jades_inspections.py --input ~/Downloads/JADES_for_Campfire.csv
    python scripts/push_jades_inspections.py --input table.csv --local
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import os
import tomllib

import numpy as np
import jwt  # PyJWT
from astropy.coordinates import SkyCoord
import astropy.units as u

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client


MATCH_RADIUS_ARCSEC = 0.2
REDSHIFT_DISAGREEMENT_THRESHOLD = 0.1
MIN_GOOD_FLAG = 3  # only Probable/Secure JADES flags auto-propagate
INSPECTOR_NAME = "AnthonyBot"

# jades_inprep was merged into the public jades program on 2026-09-07
# (scripts/merge_jades_programs.py); one slug covers everything now.
JADES_PROGRAM_SLUGS = ["jades"]

# JADES z_Spec_flag → CAMPFIRE redshift_quality (identical semantics):
# 1=Impossible, 2=Tentative, 3=Probable, 4=Secure.
VALID_FLAGS = {1, 2, 3, 4}

PAGE_SIZE = 1000

# Local Supabase JWT secret — well-known, baked into the Supabase CLI defaults.
_LOCAL_JWT_SECRET = "super-secret-jwt-token-with-at-least-32-characters-long"


def _read_jwt_secret_from_toml(config_path: str | None) -> str | None:
    """Direct TOML lookup for [supabase].jwt_secret.

    Works around the env-var-shadows-TOML behavior in
    campfire.deploy.config.load_config (top-level section merge).
    """
    candidates: list[Path] = []
    if config_path:
        candidates.append(Path(config_path))
    else:
        root = os.environ.get("CAMPFIRE_ROOT")
        if root:
            candidates.append(Path(root) / "config" / "deploy.toml")
    for path in candidates:
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)
            secret = data.get("supabase", {}).get("jwt_secret")
            if secret:
                return secret
    return None


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_inspector_uuid(supabase) -> str | None:
    """Look up the inspector bot's user_id."""
    result = (
        supabase.table("user_profiles")
        .select("user_id")
        .eq("full_name", INSPECTOR_NAME)
        .execute()
    )
    return result.data[0]["user_id"] if result.data else None


def create_impersonation_token(jwt_secret: str, user_uuid: str) -> str:
    """Sign a JWT with sub=user_uuid, role=service_role.

    PostgREST runs the request as service_role (bypassing RLS and the
    column-scope trigger), but ``auth.uid()`` resolves to the bot inside
    triggers, so the audit log attributes the change to it.
    """
    now = int(time.time())
    payload = {
        "sub": user_uuid,
        "role": "service_role",
        "iss": "supabase",
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(payload, jwt_secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Input parsing + positional dedup
# ---------------------------------------------------------------------------

def read_input(path: Path):
    """Read the JADES CSV into parallel arrays.

    Returns (ra, dec, z, flag, skipped_rows) where skipped_rows are rows with
    an out-of-range flag, unparseable coords, or flag >= 2 without a finite z.
    """
    ras: list[float] = []
    decs: list[float] = []
    zs: list[float] = []
    flags: list[int] = []
    skipped: list[dict] = []

    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        required = {"RA", "Dec", "z_Spec", "z_Spec_flag"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            print(f"Error: missing columns in input CSV: {missing}")
            sys.exit(1)

        for i, row in enumerate(reader, start=2):  # line number incl. header
            try:
                ra = float(row["RA"])
                dec = float(row["Dec"])
                z = float(row["z_Spec"])  # nan parses fine
                flag = int(row["z_Spec_flag"])
            except ValueError:
                skipped.append({"line": i, "reason": "unparseable", **row})
                continue
            if flag not in VALID_FLAGS:
                skipped.append({"line": i, "reason": f"flag={flag} out of range", **row})
                continue
            if flag >= 2 and not np.isfinite(z):
                skipped.append({"line": i, "reason": f"flag={flag} but z is not finite", **row})
                continue
            ras.append(ra)
            decs.append(dec)
            zs.append(z)
            flags.append(flag)

    return (np.array(ras), np.array(decs), np.array(zs),
            np.array(flags, dtype=int), skipped)


def dedup_input(ras, decs, zs, flags, radius_arcsec: float):
    """Cluster input rows within ``radius_arcsec`` and collapse each cluster
    to a single record.

    A cluster is a conflict (skipped, reported) if its members disagree on
    flag, or on z by more than REDSHIFT_DISAGREEMENT_THRESHOLD.
    """
    coords = SkyCoord(ra=ras * u.deg, dec=decs * u.deg)
    idx1, idx2, _, _ = coords.search_around_sky(coords, radius_arcsec * u.arcsec)

    n = len(ras)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra_, rb_ = find(a), find(b)
        if ra_ != rb_:
            parent[ra_] = rb_

    for a, b in zip(idx1, idx2):
        if a != b:
            union(int(a), int(b))

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    deduped: list[dict] = []
    conflicts: list[dict] = []

    for members in clusters.values():
        m = np.array(members)
        cluster_zs = zs[m]
        cluster_flags = flags[m]

        if len(m) > 1:
            finite = cluster_zs[np.isfinite(cluster_zs)]
            z_spread = float(np.max(finite) - np.min(finite)) if len(finite) > 1 else 0.0
            flags_disagree = len(set(cluster_flags.tolist())) > 1
            if flags_disagree or z_spread > REDSHIFT_DISAGREEMENT_THRESHOLD:
                conflicts.append({
                    "ra": float(np.mean(ras[m])),
                    "dec": float(np.mean(decs[m])),
                    "n_members": int(len(m)),
                    "z_values": cluster_zs.tolist(),
                    "flags": cluster_flags.tolist(),
                    "z_spread": z_spread,
                })
                continue

        finite = cluster_zs[np.isfinite(cluster_zs)]
        deduped.append({
            "ra": float(np.median(ras[m])),
            "dec": float(np.median(decs[m])),
            "z": float(np.median(finite)) if len(finite) else float("nan"),
            "flag": int(cluster_flags[0]),
            "n_members": int(len(m)),
        })

    return deduped, conflicts


# ---------------------------------------------------------------------------
# Object catalog fetch + matching
# ---------------------------------------------------------------------------

def fetch_jades_objects(supabase) -> list[dict]:
    """Fetch active objects belonging to any JADES program slug."""
    rows: list[dict] = []
    offset = 0
    while True:
        resp = (
            supabase.table("objects")
            .select(
                "id, object_id, field, ra, dec, programs, "
                "redshift_auto, redshift_inspected, redshift_quality, "
                "is_active"
            )
            .eq("is_active", True)
            .ov("programs", JADES_PROGRAM_SLUGS)
            .order("id")
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        print(f"  fetched {len(rows)} objects ...", file=sys.stderr)
    print(f"Fetched {len(rows)} active JADES objects ({'/'.join(JADES_PROGRAM_SLUGS)}).")
    return rows


def match_to_objects(deduped: list[dict], objects: list[dict],
                     radius_arcsec: float) -> list[tuple[dict, dict | None]]:
    """For each deduped input entry, return (entry, nearest object within
    radius) or (entry, None) if no match. If several entries land on the
    same object, only the nearest keeps the match.
    """
    if not deduped:
        return []
    if not objects:
        return [(e, None) for e in deduped]

    catalog = SkyCoord(ra=np.array([o["ra"] for o in objects]) * u.deg,
                       dec=np.array([o["dec"] for o in objects]) * u.deg)
    sources = SkyCoord(ra=np.array([e["ra"] for e in deduped]) * u.deg,
                       dec=np.array([e["dec"] for e in deduped]) * u.deg)

    idx, sep, _ = sources.match_to_catalog_sky(catalog)
    sep_arcsec = sep.arcsec

    # Guard against two input sources claiming the same object: keep the
    # nearest, drop the rest (input is deduped at the same radius the object
    # catalog is grouped at, so this should be rare).
    best_for_object: dict[int, int] = {}
    for i in range(len(deduped)):
        if sep_arcsec[i] > radius_arcsec:
            continue
        j = int(idx[i])
        if j not in best_for_object or sep_arcsec[i] < sep_arcsec[best_for_object[j]]:
            best_for_object[j] = i

    matches: list[tuple[dict, dict | None]] = []
    for i, entry in enumerate(deduped):
        j = int(idx[i])
        if sep_arcsec[i] <= radius_arcsec and best_for_object.get(j) == i:
            matches.append((entry, objects[j]))
        else:
            if sep_arcsec[i] <= radius_arcsec:
                print(
                    f"  note: ({entry['ra']:.6f}, {entry['dec']:.6f}) also "
                    f"matched object {objects[j]['object_id']} but a nearer "
                    "input source claimed it — treating as unmatched"
                )
            matches.append((entry, None))
    return matches


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_inspections(supabase, input_path: Path, jwt_secret: str,
                        dry_run: bool) -> None:
    ras, decs, zs, flags, skipped_rows = read_input(input_path)
    print(f"Read {len(ras)} usable rows from {input_path} "
          f"({len(skipped_rows)} skipped at parse).")
    for s in skipped_rows[:10]:
        print(f"  skipped line {s['line']}: {s['reason']}")

    deduped, internal_conflicts = dedup_input(ras, decs, zs, flags,
                                              MATCH_RADIUS_ARCSEC)
    n_collapsed = len(ras) - len(deduped) - sum(c["n_members"] for c in internal_conflicts)
    print(
        f"Positional dedup: {len(deduped)} unique sources "
        f"(collapsed {n_collapsed} duplicates), "
        f"{len(internal_conflicts)} internal conflicts."
    )

    inspector_uuid = get_inspector_uuid(supabase)
    if not inspector_uuid:
        print(
            f"Error: no user_profiles row for '{INSPECTOR_NAME}'. "
            "Cannot proceed without an attributable inspector for the audit log."
        )
        sys.exit(1)
    print(f"Inspector: {INSPECTOR_NAME} ({inspector_uuid})")

    token = create_impersonation_token(jwt_secret, inspector_uuid)
    supabase.postgrest.auth(token)

    objects = fetch_jades_objects(supabase)
    matches = match_to_objects(deduped, objects, MATCH_RADIUS_ARCSEC)

    stats = {
        "no_match": 0,
        "skipped_existing": 0,
        "updated": {3: 0, 4: 0},
        "review": {"disagreement": 0, "no_z_auto": 0,
                   "tentative_flag": 0, "impossible_flag": 0},
    }
    manual_review: list[dict] = []
    no_match_samples: list[dict] = []

    now = datetime.now(timezone.utc).isoformat()

    def defer(entry: dict, obj: dict, reason: str) -> None:
        z_auto = obj["redshift_auto"]
        z = entry["z"]
        stats["review"][reason] += 1
        manual_review.append({
            "reason": reason,
            "object_id": obj["object_id"],
            "field": obj["field"],
            "ra": entry["ra"],
            "dec": entry["dec"],
            "z_jades": z if np.isfinite(z) else "",
            "flag_jades": entry["flag"],
            "z_auto": z_auto if z_auto is not None else "",
            "delta_z": (abs(z - z_auto)
                        if z_auto is not None and np.isfinite(z) else ""),
        })

    for entry, obj in matches:
        if obj is None:
            stats["no_match"] += 1
            if len(no_match_samples) < 20:
                no_match_samples.append(entry)
            continue

        # Never clobber a human inspection.
        if obj["redshift_quality"] > 0:
            stats["skipped_existing"] += 1
            continue

        quality = entry["flag"]
        z = entry["z"]
        z_auto = obj["redshift_auto"]

        # Confirmation-only gate: good JADES flag AND agreement with our
        # auto-fit. Everything else defers to human review.
        if quality < MIN_GOOD_FLAG:
            defer(entry, obj,
                  "impossible_flag" if quality == 1 else "tentative_flag")
            continue
        if z_auto is None:
            defer(entry, obj, "no_z_auto")
            continue
        if abs(z - z_auto) > REDSHIFT_DISAGREEMENT_THRESHOLD:
            defer(entry, obj, "disagreement")
            print(
                f"  [{obj['object_id']}] DISAGREE z_jades={z:.6f} vs "
                f"z_auto={z_auto:.6f} (dz={abs(z - z_auto):.4f}) — deferred"
            )
            continue

        # Confirmation: hand off to pin_redshift_on_signoff. Writing NULL
        # with quality >= 2 makes the trigger copy redshift_auto into
        # redshift_inspected (inspected_used_auto = true), so the displayed
        # redshift tracks the auto-fit and the UI shows it as auto-pinned
        # rather than a typed override.
        stats["updated"][quality] += 1
        print(
            f"  [{obj['object_id']}] z_jades={z:.6f} auto={z_auto} "
            f"dz={abs(z - z_auto):.4f} → q={quality} (auto-pinned)"
            f"{' [DRY RUN]' if dry_run else ''}"
        )

        if not dry_run:
            (
                supabase.table("objects")
                .update({
                    "redshift_inspected": None,
                    "redshift_quality": quality,
                    "last_inspected_at": now,
                    "last_inspected_by": inspector_uuid,
                })
                .eq("id", obj["id"])
                .execute()
            )

    # -------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------
    n_updated = sum(stats["updated"].values())
    print("\n--- Summary ---")
    print(f"  Usable input rows:             {len(ras)}")
    print(f"  Unique sources after dedup:    {len(deduped)}")
    print(f"  Internal conflicts (in file):  {len(internal_conflicts)}")
    print(f"  Matched to JADES objects:      {len(deduped) - stats['no_match']}")
    print(f"  No match:                      {stats['no_match']}")
    print(f"  Skipped (already inspected):   {stats['skipped_existing']}")
    print(f"  Confirmed q=4 Secure:          {stats['updated'][4]}")
    print(f"  Confirmed q=3 Probable:        {stats['updated'][3]}")
    print(f"  Total written:                 {n_updated}")
    print(f"  Deferred to manual review:     {len(manual_review)}")
    print(f"    disagreement:                {stats['review']['disagreement']}")
    print(f"    no_z_auto:                   {stats['review']['no_z_auto']}")
    print(f"    tentative_flag (2):          {stats['review']['tentative_flag']}")
    print(f"    impossible_flag (1):         {stats['review']['impossible_flag']}")
    if dry_run:
        print("\n  *** DRY RUN — no changes were made ***")

    # -------------------------------------------------------------------
    # CSV outputs (written on dry runs too, for iteration/review)
    # -------------------------------------------------------------------
    out_dir = input_path.parent

    if internal_conflicts:
        path = out_dir / "jades_internal_conflicts.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["ra", "dec", "n_members", "z_spread",
                            "z_values", "flags"],
            )
            w.writeheader()
            for c in internal_conflicts:
                w.writerow({
                    "ra": c["ra"],
                    "dec": c["dec"],
                    "n_members": c["n_members"],
                    "z_spread": c["z_spread"],
                    "z_values": "|".join(f"{z:.6f}" for z in c["z_values"]),
                    "flags": "|".join(map(str, c["flags"])),
                })
        print(f"  Wrote {len(internal_conflicts)} internal conflicts to: {path}")

    if manual_review:
        path = out_dir / "jades_manual_review.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["reason", "object_id", "field", "ra", "dec",
                            "z_jades", "flag_jades", "z_auto", "delta_z"],
            )
            w.writeheader()
            w.writerows(manual_review)
        print(f"  Wrote {len(manual_review)} manual-review rows to: {path}")

    if no_match_samples:
        path = out_dir / "jades_unmatched.csv"
        unmatched_all = [e for e, o in matches if o is None]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["ra", "dec", "z", "flag"])
            w.writeheader()
            for e in unmatched_all:
                w.writerow({"ra": e["ra"], "dec": e["dec"],
                            "z": e["z"], "flag": e["flag"]})
        print(f"  Wrote {len(unmatched_all)} unmatched sources to: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push JADES catalog redshift inspections into CAMPFIRE objects."
    )
    parser.add_argument("--input", type=Path, required=True,
                        help="JADES CSV (RA, Dec, z_Spec, z_Spec_flag)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; do not write to the database")
    parser.add_argument("--local", action="store_true",
                        help="Run against the local Supabase instance")
    parser.add_argument("--config", type=str, default=None,
                        help="Override deploy config TOML path")
    args = parser.parse_args()

    input_path = args.input.expanduser()
    if not input_path.exists():
        print(f"Error: input file not found: {input_path}")
        sys.exit(1)

    # This script always needs service-role (JWT impersonation writes), so
    # request that mode explicitly — post-#250, a service-role key in the
    # env/TOML is ignored unless the mode is asked for.
    config = load_config(args.config, local=args.local,
                         service_role=not args.local)

    if not config.get("supabase", {}).get("service_role_key"):
        print(
            "Error: service_role_key required (this script writes to objects "
            "via service-role JWT impersonation). Set "
            "CAMPFIRE_SUPABASE_SERVICE_ROLE_KEY or use --local."
        )
        sys.exit(2)

    if args.local:
        jwt_secret = _LOCAL_JWT_SECRET
    else:
        jwt_secret = config.get("supabase", {}).get("jwt_secret")
        if not jwt_secret:
            # load_config() top-level-merges TOML on top of env vars: when
            # CAMPFIRE_SUPABASE_* env vars are set, the entire TOML
            # [supabase] section — including jwt_secret — is dropped instead
            # of deep-merged. Fall back to a direct TOML read.
            jwt_secret = _read_jwt_secret_from_toml(args.config)
        if not jwt_secret:
            print(
                "Error: jwt_secret missing from deploy config. Add it under "
                "[supabase] in deploy.toml — find it in Supabase Dashboard → "
                "Settings → API → JWT Secret. It's needed to sign the "
                "impersonation token so audit attribution works."
            )
            sys.exit(2)

    supabase = get_supabase_client(config)
    print(f"Using Supabase: {config['supabase']['url']}")

    process_inspections(supabase, input_path, jwt_secret, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
