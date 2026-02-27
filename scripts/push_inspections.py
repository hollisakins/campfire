#!/usr/bin/env python3
"""
Push manual redshift inspections from a colleague's FITS table into the CAMPFIRE database.

Usage:
    python scripts/push_inspections.py --input /path/to/table.fits --dry-run
    python scripts/push_inspections.py --input /path/to/table.fits
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.table import Table
import astropy.units as u

try:
    import tomllib
except ImportError:
    import tomli as tomllib

import time

import jwt  # PyJWT

from supabase import create_client, Client


MATCH_RADIUS_ARCSEC = 0.2
QUALITY_SECURE = 4
REDSHIFT_DISAGREEMENT_THRESHOLD = 0.1
QUALITY_PROBABLE = 3
INSPECTOR_NAME = "AnthonyBot"


def load_config(scripts_dir: Path) -> dict:
    """Load deployment configuration from config.toml."""
    config_path = scripts_dir / "config.toml"
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}")
        print("Copy config.example.toml to config.toml and fill in your credentials.")
        sys.exit(1)
    with open(config_path, "rb") as f:
        return tomllib.load(f)


def get_inspector_uuid(supabase: Client) -> str | None:
    """Look up the UUID for the AnthonyBot user profile."""
    result = (
        supabase.table("user_profiles")
        .select("user_id")
        .eq("full_name", INSPECTOR_NAME)
        .execute()
    )
    if result.data:
        return result.data[0]["user_id"]
    return None


def create_impersonation_token(jwt_secret: str, user_uuid: str) -> str:
    """Create a JWT with the given user UUID as `sub` so auth.uid() resolves correctly."""
    payload = {
        "sub": user_uuid,
        "role": "service_role",
        "iss": "supabase",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(payload, jwt_secret, algorithm="HS256")


def fetch_objects_for_program(supabase: Client, program_id: int) -> list[dict]:
    """Fetch all objects for a given program ID, paginating past the 1000-row default."""
    PAGE_SIZE = 1000
    all_data = []
    offset = 0
    while True:
        result = (
            supabase.table("objects")
            .select("id, object_id, ra, dec, redshift_auto, redshift_inspected, redshift_quality")
            .eq("program_id", program_id)
            .range(offset, offset + PAGE_SIZE - 1)
            .execute()
        )
        all_data.extend(result.data)
        if len(result.data) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return all_data


def find_matches(row_ra: float, row_dec: float, objects: list[dict]) -> list[dict]:
    """Find objects within MATCH_RADIUS_ARCSEC of the given coordinates."""
    if not objects:
        return []

    obj_ras = np.array([o["ra"] for o in objects])
    obj_decs = np.array([o["dec"] for o in objects])

    source = SkyCoord(ra=row_ra * u.deg, dec=row_dec * u.deg)
    catalog = SkyCoord(ra=obj_ras * u.deg, dec=obj_decs * u.deg)
    seps = source.separation(catalog).arcsec

    return [obj for obj, sep in zip(objects, seps) if sep <= MATCH_RADIUS_ARCSEC]


def process_inspections(
    supabase: Client,
    input_path: Path,
    jwt_secret: str,
    dry_run: bool = True,
):
    """Main processing loop."""
    # Read the input FITS table
    table = Table.read(input_path)
    table = table[~np.isin(table['PROGRAM'], [1208, 1180, 1286, 1287, 3215, 2561])]

    print(f"Read {len(table)} rows from {input_path}")

    required_cols = {"PROGRAM", "ID", "RA", "DEC", "Z"}
    missing = required_cols - set(table.colnames)
    if missing:
        print(f"Error: Missing columns in input table: {missing}")
        sys.exit(1)

    # Look up inspector UUID
    inspector_uuid = get_inspector_uuid(supabase)
    if inspector_uuid:
        print(f"Found {INSPECTOR_NAME} user profile: {inspector_uuid}")
        # Switch auth context so auth.uid() returns this UUID in DB triggers
        token = create_impersonation_token(jwt_secret, inspector_uuid)
        supabase.postgrest.auth(token)
    else:
        print(
            f"Error: No user profile found for '{INSPECTOR_NAME}'. "
            "Cannot proceed without a valid user for audit logging.\n"
            "Create an auth user and user_profiles row with "
            f"full_name = '{INSPECTOR_NAME}'."
        )
        sys.exit(1)

    # Cache fetched objects by program_id to avoid repeated queries
    program_cache: dict[int, list[dict]] = {}

    # Track disagreements for manual review
    disagreements = []

    # Counters
    stats = {
        "matched": 0,
        "skipped_existing": 0,
        "updated_secure": 0,
        "updated_probable": 0,
        "disagreement": 0,
        "no_match": 0,
    }

    now = datetime.now(timezone.utc).isoformat()

    for row in table:
        program_id = int(row["PROGRAM"])
        msa_id = int(row["ID"])
        ra = float(row["RA"])
        dec = float(row["DEC"])
        z = float(row["Z"])

        # Fetch and cache objects for this program
        if program_id not in program_cache:
            program_cache[program_id] = fetch_objects_for_program(supabase, program_id)
            print(f"  Fetched {len(program_cache[program_id])} objects for program {program_id}")

        objects = program_cache[program_id]
        matches = find_matches(ra, dec, objects)

        if not matches:
            stats["no_match"] += 1
            print(f"  [{program_id}/{msa_id}] No match within {MATCH_RADIUS_ARCSEC}\" at ({ra:.6f}, {dec:.6f})")
            continue

        stats["matched"] += len(matches)

        for obj in matches:
            obj_id = obj["object_id"]

            # Step 3: Skip if already manually inspected (quality > 0 means someone reviewed it)
            if obj["redshift_quality"] > 0:
                stats["skipped_existing"] += 1
                print(f"  [{obj_id}] Already inspected (q={obj['redshift_quality']}), skipping")
                continue

            # Step 4: Check agreement with auto redshift
            z_auto = obj["redshift_auto"]
            disagrees = z_auto is not None and abs(z - z_auto) > REDSHIFT_DISAGREEMENT_THRESHOLD

            if disagrees and program_id != 6368:
                # Disagreement for non-6368 programs → flag for manual review
                stats["disagreement"] += 1
                disagreements.append({
                    "object_id": obj_id,
                    "z_input": z,
                    "z_auto": z_auto,
                    "delta_z": abs(z - z_auto),
                })
                print(f"  [{obj_id}] DISAGREE z_input={z:.6f} vs z_auto={z_auto:.6f} (Δz={abs(z - z_auto):.4f})")
                continue

            # Determine quality: Probable if disagrees (6368 only), else Secure
            if disagrees:
                quality = QUALITY_PROBABLE
                quality_label = "Probable"
                stats["updated_probable"] += 1
            else:
                quality = QUALITY_SECURE
                quality_label = "Secure"
                stats["updated_secure"] += 1

            delta_str = f"Δz={abs(z - z_auto):.4f}" if z_auto is not None else "no z_auto"
            print(
                f"  [{obj_id}] z={z:.6f} (auto={z_auto}, {delta_str}) → {quality_label}"
                f"{' [DRY RUN]' if dry_run else ''}"
            )

            if not dry_run:
                update_data = {
                    "redshift_inspected": round(z, 6),
                    "redshift_quality": quality,
                    "last_inspected_at": now,
                    "last_inspected_by": inspector_uuid,
                }
                supabase.table("objects").update(update_data).eq("id", obj["id"]).execute()

    # Print summary
    print("\n--- Summary ---")
    print(f"  Input rows:              {len(table)}")
    print(f"  Matched objects:         {stats['matched']}")
    print(f"  Skipped (already insp.): {stats['skipped_existing']}")
    print(f"  Updated (Secure):        {stats['updated_secure']}")
    print(f"  Updated (Probable):     {stats['updated_probable']}")
    print(f"  Disagreements (skipped): {stats['disagreement']}")
    print(f"  No match found:          {stats['no_match']}")
    if dry_run:
        print("\n  *** DRY RUN — no changes were made ***")

    # Write disagreements to CSV for manual review
    if disagreements:
        out_path = input_path.parent / "disagreements_to_inspect.csv"
        import csv
        with open(out_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["object_id", "z_input", "z_auto", "delta_z"])
            writer.writeheader()
            writer.writerows(disagreements)
        print(f"\n  Wrote {len(disagreements)} disagreements to: {out_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Push manual redshift inspections into CAMPFIRE database."
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Path to the FITS table with inspection results",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be updated without making changes",
    )
    args = parser.parse_args()

    if not args.input.exists():
        print(f"Error: Input file not found: {args.input}")
        sys.exit(1)

    scripts_dir = Path(__file__).resolve().parent
    config = load_config(scripts_dir)

    jwt_secret = config["supabase"].get("jwt_secret")
    if not jwt_secret:
        print("Error: jwt_secret not found in [supabase] section of config.toml")
        print("Find it in Supabase Dashboard → Settings → API → JWT Secret")
        sys.exit(1)

    supabase = create_client(
        config["supabase"]["url"],
        config["supabase"]["service_role_key"],
    )

    process_inspections(supabase, args.input, jwt_secret, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
