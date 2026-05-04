#!/usr/bin/env python3
"""
Push manual redshift inspections from a colleague's FITS table into the
CAMPFIRE database.

Phase D update
--------------
Inspection state lives on ``objects`` (not ``targets``). One physical sky
position is one ``objects`` row, even if it was observed in multiple JWST
programs. So we:

  1. Drop excluded programs from the input table.
  2. De-duplicate the input *positionally* before talking to the database
     (same source observed in multiple programs is one Anthony record). Within
     a positional cluster, if Anthony's redshifts disagree we log the cluster
     to ``anthony_internal_conflicts.csv`` and skip it — there's nothing
     sensible to push for that source.
  3. Match deduped sources to ``objects`` by sky coordinates within
     ``MATCH_RADIUS_ARCSEC``. No program filtering on the DB side: ``objects``
     is the unified catalog and coords are the durable key.
  4. Skip any object that has already been inspected (``redshift_quality > 0``).
  5. Compare Anthony's z to ``objects.redshift_auto``:
        - agree (no auto, or |Δz| ≤ threshold)              → write Secure (q=4)
        - disagree but the source was observed in PROGRAM 6368 (Anthony's PI
          program — we trust him on his own data)             → write Probable (q=3)
        - disagree elsewhere                                  → log to
          ``disagreements_to_inspect.csv``, do not write
  6. Writes go to ``objects`` with a JWT-impersonated AnthonyBot ``sub`` so
     ``log_object_inspection_changes`` attributes the audit row to him. The
     JWT carries ``role: service_role`` so the column-scope trigger and RLS
     are bypassed.

Usage
-----
    python scripts/push_inspections.py --input /path/to/table.fits --dry-run
    python scripts/push_inspections.py --input /path/to/table.fits
    python scripts/push_inspections.py --input /path/to/table.fits --local
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
from astropy.table import Table
import astropy.units as u

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python"))

from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client


MATCH_RADIUS_ARCSEC = 0.2
REDSHIFT_DISAGREEMENT_THRESHOLD = 0.1
QUALITY_SECURE = 4
QUALITY_PROBABLE = 3
INSPECTOR_NAME = "AnthonyBot"

# Programs to drop from the input table outright. These are reduced/served
# elsewhere and aren't part of the CAMPFIRE objects catalog we're updating.
EXCLUDED_PROGRAMS = {1180, 1286, 1287}

# JWST program where Anthony is the PI — on disagreement with our redshift_auto
# we trust his redshift but tag it Probable rather than Secure.
TRUSTED_PROGRAM = 6368

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
    """Look up AnthonyBot's user_id."""
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
    column-scope trigger), but ``auth.uid()`` resolves to AnthonyBot inside
    triggers, so the audit log attributes the change to him.
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
# Positional dedup of the input table
# ---------------------------------------------------------------------------

def dedup_input(table: Table, radius_arcsec: float):
    """Cluster input rows within ``radius_arcsec`` and collapse each cluster
    to a single record.

    Returns
    -------
    deduped : list[dict]
        One entry per unique sky position. Each entry carries the median
        coords/z of the cluster, plus the list of (program, msa_id) tuples
        that contributed.
    conflicts : list[dict]
        Clusters whose internal z spread exceeds
        ``REDSHIFT_DISAGREEMENT_THRESHOLD``. We don't push these — Anthony
        would need to reconcile them himself.
    """
    coords = SkyCoord(ra=np.asarray(table["RA"]) * u.deg,
                      dec=np.asarray(table["DEC"]) * u.deg)
    idx1, idx2, _, _ = coords.search_around_sky(coords, radius_arcsec * u.arcsec)

    n = len(table)
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

    ras = np.asarray(table["RA"], dtype=float)
    decs = np.asarray(table["DEC"], dtype=float)
    zs = np.asarray(table["Z"], dtype=float)
    progs = np.asarray(table["PROGRAM"], dtype=int)
    ids = np.asarray(table["ID"], dtype=int)

    deduped: list[dict] = []
    conflicts: list[dict] = []

    for members in clusters.values():
        m = np.array(members)
        cluster_zs = zs[m]
        cluster_ras = ras[m]
        cluster_decs = decs[m]
        cluster_progs = progs[m].tolist()
        cluster_ids = ids[m].tolist()

        if len(m) > 1:
            z_spread = float(np.max(cluster_zs) - np.min(cluster_zs))
            if z_spread > REDSHIFT_DISAGREEMENT_THRESHOLD:
                conflicts.append({
                    "ra": float(np.mean(cluster_ras)),
                    "dec": float(np.mean(cluster_decs)),
                    "z_values": cluster_zs.tolist(),
                    "programs": cluster_progs,
                    "ids": cluster_ids,
                    "z_spread": z_spread,
                    "n_members": int(len(m)),
                })
                continue

        deduped.append({
            "ra": float(np.median(cluster_ras)),
            "dec": float(np.median(cluster_decs)),
            "z": float(np.median(cluster_zs)),
            "n_members": int(len(m)),
            "programs": cluster_progs,
            "ids": cluster_ids,
        })

    return deduped, conflicts


# ---------------------------------------------------------------------------
# Object catalog fetch + matching
# ---------------------------------------------------------------------------

def fetch_active_objects(supabase) -> list[dict]:
    """Fetch every active object's coords + inspection state."""
    rows: list[dict] = []
    offset = 0
    while True:
        resp = (
            supabase.table("objects")
            .select(
                "id, object_id, field, ra, dec, "
                "redshift_auto, redshift_inspected, redshift_quality, "
                "is_active, version"
            )
            .eq("is_active", True)
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
    print(f"Fetched {len(rows)} active objects.")
    return rows


def match_to_objects(deduped: list[dict], objects: list[dict],
                     radius_arcsec: float) -> list[tuple[dict, dict | None]]:
    """For each deduped Anthony entry, return (entry, nearest object within
    radius) or (entry, None) if no match.
    """
    if not deduped:
        return []
    if not objects:
        return [(e, None) for e in deduped]

    obj_ra = np.array([o["ra"] for o in objects])
    obj_dec = np.array([o["dec"] for o in objects])
    catalog = SkyCoord(ra=obj_ra * u.deg, dec=obj_dec * u.deg)

    src_ra = np.array([e["ra"] for e in deduped])
    src_dec = np.array([e["dec"] for e in deduped])
    sources = SkyCoord(ra=src_ra * u.deg, dec=src_dec * u.deg)

    idx, sep, _ = sources.match_to_catalog_sky(catalog)
    sep_arcsec = sep.arcsec

    matches: list[tuple[dict, dict | None]] = []
    for i, entry in enumerate(deduped):
        if sep_arcsec[i] <= radius_arcsec:
            matches.append((entry, objects[int(idx[i])]))
        else:
            matches.append((entry, None))
    return matches


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_inspections(supabase, input_path: Path, jwt_secret: str,
                        dry_run: bool) -> None:
    table = Table.read(input_path)
    n_total = len(table)

    required_cols = {"PROGRAM", "ID", "RA", "DEC", "Z"}
    missing = required_cols - set(table.colnames)
    if missing:
        print(f"Error: missing columns in input table: {missing}")
        sys.exit(1)

    excluded_mask = np.isin(table["PROGRAM"], list(EXCLUDED_PROGRAMS))
    table = table[~excluded_mask]
    print(
        f"Read {n_total} rows; excluded {int(excluded_mask.sum())} from "
        f"{sorted(EXCLUDED_PROGRAMS)}; {len(table)} remain."
    )

    deduped, internal_conflicts = dedup_input(table, MATCH_RADIUS_ARCSEC)
    n_collapsed = len(table) - len(deduped) - sum(c["n_members"] for c in internal_conflicts)
    print(
        f"Positional dedup: {len(deduped)} unique sources "
        f"(collapsed {n_collapsed} cross-program duplicates), "
        f"{len(internal_conflicts)} internal-z conflicts."
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

    objects = fetch_active_objects(supabase)
    matches = match_to_objects(deduped, objects, MATCH_RADIUS_ARCSEC)

    stats = {
        "no_match": 0,
        "skipped_existing": 0,
        "updated_secure": 0,
        "updated_probable": 0,
        "disagreement": 0,
    }
    disagreements: list[dict] = []
    no_match_samples: list[dict] = []

    now = datetime.now(timezone.utc).isoformat()

    for entry, obj in matches:
        if obj is None:
            stats["no_match"] += 1
            if len(no_match_samples) < 20:
                no_match_samples.append(entry)
            continue

        if obj["redshift_quality"] > 0:
            stats["skipped_existing"] += 1
            continue

        z = entry["z"]
        z_auto = obj["redshift_auto"]
        disagrees = (
            z_auto is not None
            and abs(z - z_auto) > REDSHIFT_DISAGREEMENT_THRESHOLD
        )
        is_trusted = TRUSTED_PROGRAM in entry["programs"]

        if disagrees and not is_trusted:
            stats["disagreement"] += 1
            disagreements.append({
                "object_id": obj["object_id"],
                "field": obj["field"],
                "ra": entry["ra"],
                "dec": entry["dec"],
                "z_input": z,
                "z_auto": z_auto,
                "delta_z": abs(z - z_auto),
                "programs": "|".join(map(str, entry["programs"])),
                "msa_ids": "|".join(map(str, entry["ids"])),
            })
            print(
                f"  [{obj['object_id']}] DISAGREE z_input={z:.6f} vs "
                f"z_auto={z_auto:.6f} (Δz={abs(z - z_auto):.4f}) — flagged"
            )
            continue

        if disagrees:
            # 6368-trusted disagreement: pin Anthony's z explicitly. Trigger
            # will set inspected_used_auto = false (real override).
            quality = QUALITY_PROBABLE
            label = "Probable"
            redshift_inspected: float | None = round(z, 6)
            stats["updated_probable"] += 1
        else:
            # Agreement: hand off to pin_redshift_on_signoff. Writing NULL
            # with quality >= 2 makes the trigger copy redshift_auto into
            # redshift_inspected and set inspected_used_auto = true. Means
            # the displayed redshift tracks the auto-fit (and any future
            # reprocessing of it), and the UI flags it as auto-pinned
            # rather than a typed override.
            quality = QUALITY_SECURE
            label = "Secure"
            redshift_inspected = None
            stats["updated_secure"] += 1

        delta_str = (
            f"Δz={abs(z - z_auto):.4f}" if z_auto is not None else "no z_auto"
        )
        pin_str = (
            f"override={redshift_inspected:.6f}"
            if redshift_inspected is not None
            else "auto-pinned"
        )
        print(
            f"  [{obj['object_id']}] anthony={z:.6f} auto={z_auto} {delta_str} "
            f"→ {label} ({pin_str}){' [DRY RUN]' if dry_run else ''}"
        )

        if not dry_run:
            (
                supabase.table("objects")
                .update({
                    "redshift_inspected": redshift_inspected,
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
    print("\n--- Summary ---")
    print(f"  Input rows (post-exclusion):   {len(table)}")
    print(f"  Unique sources after dedup:    {len(deduped)}")
    print(f"  Internal-z conflicts (in file):{len(internal_conflicts)}")
    print(f"  Matched to objects:            {len(deduped) - stats['no_match']}")
    print(f"  No match in objects:           {stats['no_match']}")
    print(f"  Skipped (already inspected):   {stats['skipped_existing']}")
    print(f"  Updated Secure (q=4):          {stats['updated_secure']}")
    print(f"  Updated Probable (q=3):        {stats['updated_probable']}")
    print(f"  Disagreements (skipped):       {stats['disagreement']}")
    if dry_run:
        print("\n  *** DRY RUN — no changes were made ***")

    # -------------------------------------------------------------------
    # CSV outputs
    # -------------------------------------------------------------------
    if internal_conflicts:
        path = input_path.parent / "anthony_internal_conflicts.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["ra", "dec", "n_members", "z_spread",
                            "z_values", "programs", "ids"],
            )
            w.writeheader()
            for c in internal_conflicts:
                w.writerow({
                    "ra": c["ra"],
                    "dec": c["dec"],
                    "n_members": c["n_members"],
                    "z_spread": c["z_spread"],
                    "z_values": "|".join(f"{z:.6f}" for z in c["z_values"]),
                    "programs": "|".join(map(str, c["programs"])),
                    "ids": "|".join(map(str, c["ids"])),
                })
        print(f"  Wrote {len(internal_conflicts)} internal conflicts to: {path}")

    if disagreements:
        path = input_path.parent / "disagreements_to_inspect.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["object_id", "field", "ra", "dec",
                            "z_input", "z_auto", "delta_z",
                            "programs", "msa_ids"],
            )
            w.writeheader()
            w.writerows(disagreements)
        print(f"  Wrote {len(disagreements)} disagreements to: {path}")

    if no_match_samples:
        print(f"\n  First {len(no_match_samples)} unmatched sources:")
        for e in no_match_samples:
            print(
                f"    ({e['ra']:.6f}, {e['dec']:.6f}) z={e['z']:.4f} "
                f"programs={e['programs']}"
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Push manual redshift inspections into CAMPFIRE objects."
    )
    parser.add_argument("--input", type=Path, required=True,
                        help="FITS table with inspection results")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report only; do not write to the database")
    parser.add_argument("--local", action="store_true",
                        help="Run against the local Supabase instance")
    parser.add_argument("--config", type=str, default=None,
                        help="Override deploy config TOML path")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: input file not found: {args.input}")
        sys.exit(1)

    config = load_config(args.config, local=args.local)

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
                "AnthonyBot impersonation token so audit attribution works."
            )
            sys.exit(2)

    supabase = get_supabase_client(config)
    print(f"Using Supabase: {config['supabase']['url']}")

    process_inspections(supabase, args.input, jwt_secret, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
