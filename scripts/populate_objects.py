#!/usr/bin/env python3
"""
Populate the objects table by cross-matching targets within ~0.2 arcsec.

Uses friends-of-friends clustering: targets within the match radius are
grouped into the same object, transitively. Each object gets an IAU-style
coordinate name from its centroid.

This script is idempotent — it clears all existing object assignments
and rebuilds from scratch. Safe because objects have no user-editable data.

Usage:
    # Dry run (print stats, no writes)
    python scripts/populate_objects.py --dry-run

    # Populate local Supabase
    python scripts/populate_objects.py

    # Custom radius, single field
    python scripts/populate_objects.py --radius 0.3 --field cosmos

    # Against production (careful!)
    python scripts/populate_objects.py --prod
"""

import argparse
from collections import defaultdict
import sys

from astropy.coordinates import SkyCoord, search_around_sky
import astropy.units as u
import numpy as np
import psycopg2
import psycopg2.extras


MATCH_RADIUS_ARCSEC = 0.2

QUALITY_LABELS = {
    0: 'Not Inspected',
    1: 'Impossible',
    2: 'Unlikely',
    3: 'Probable',
    4: 'Secure',
}

BATCH_SIZE = 500

# Local Supabase Postgres
LOCAL_DSN = 'postgresql://postgres:postgres@127.0.0.1:54322/postgres'


# ---------------------------------------------------------------------------
# Union-Find for friends-of-friends clustering
# ---------------------------------------------------------------------------

class UnionFind:
    """Disjoint set with path compression and union by rank."""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]  # path compression
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


# ---------------------------------------------------------------------------
# IAU coordinate name
# ---------------------------------------------------------------------------

def generate_iau_name(ra_deg: float, dec_deg: float) -> str:
    """Generate IAU-style name: JHHMMSS.ss+DDMMSS.s from centroid coords."""
    coord = SkyCoord(ra=ra_deg, dec=dec_deg, unit='deg')

    # RA in hours
    ra_h = coord.ra.hms
    ra_str = f"{int(ra_h.h):02d}{int(ra_h.m):02d}{ra_h.s:05.2f}"

    # Dec in degrees
    dec_d = coord.dec.dms
    sign = '+' if dec_deg >= 0 else '-'
    dec_str = f"{int(abs(dec_d.d)):02d}{int(abs(dec_d.m)):02d}{abs(dec_d.s):04.1f}"

    return f"J{ra_str}{sign}{dec_str}"


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_all_targets(conn, field: str | None = None) -> list[dict]:
    """Fetch all targets via direct Postgres query."""
    query = """
        SELECT id, target_id, ra, dec, field, program_slug, observation,
               redshift::double precision, redshift_quality, max_snr, max_exposure_time
        FROM targets
    """
    params = []
    if field:
        query += " WHERE field = %s"
        params.append(field)
    query += " ORDER BY id"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def fetch_spectra_metadata(conn, field: str | None = None) -> dict[str, list[dict]]:
    """Fetch grating, S/N, exposure_time per target_id."""
    query = """
        SELECT s.target_id, s.grating, s.signal_to_noise, s.exposure_time
        FROM spectra s
    """
    params = []
    if field:
        query += " JOIN targets t ON t.target_id = s.target_id WHERE t.field = %s"
        params.append(field)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        result: dict[str, list[dict]] = defaultdict(list)
        for row in cur:
            result[row['target_id']].append(dict(row))
        return result


# ---------------------------------------------------------------------------
# Cross-matching
# ---------------------------------------------------------------------------

def cluster_targets(
    targets: list[dict],
    radius_arcsec: float,
) -> list[list[int]]:
    """
    Friends-of-friends clustering on target positions.

    Returns list of groups, where each group is a list of indices into targets.
    """
    if not targets:
        return []

    coords = SkyCoord(
        ra=[t['ra'] for t in targets] * u.deg,
        dec=[t['dec'] for t in targets] * u.deg,
    )

    # Self-match: find all pairs within radius
    idx1, idx2, _, _ = search_around_sky(
        coords, coords, radius_arcsec * u.arcsec,
    )

    # Union-Find to get connected components
    uf = UnionFind(len(targets))
    for i, j in zip(idx1, idx2):
        i_int, j_int = int(i), int(j)
        if i_int != j_int:
            uf.union(i_int, j_int)

    # Collect groups
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(targets)):
        groups[uf.find(i)].append(i)

    return list(groups.values())


def build_objects(
    targets: list[dict],
    groups: list[list[int]],
    spectra_map: dict[str, list[dict]],
) -> list[dict]:
    """Build object records from clustered targets."""
    objects = []

    for indices in groups:
        members = [targets[i] for i in indices]

        # Centroid
        ra_centroid = float(np.mean([m['ra'] for m in members]))
        dec_centroid = float(np.mean([m['dec'] for m in members]))

        # IAU name
        object_id = generate_iau_name(ra_centroid, dec_centroid)

        # Field (should be uniform)
        field = members[0]['field']

        # Aggregate spectra info
        all_gratings = set()
        total_spectra = 0
        for m in members:
            spec_rows = spectra_map.get(m['target_id'], [])
            total_spectra += len(spec_rows)
            for s in spec_rows:
                all_gratings.add(s['grating'])

        # Max SNR and exposure time across all member spectra
        all_snr = [
            s['signal_to_noise']
            for m in members
            for s in spectra_map.get(m['target_id'], [])
            if s.get('signal_to_noise') is not None
        ]
        all_exp = [
            s['exposure_time']
            for m in members
            for s in spectra_map.get(m['target_id'], [])
            if s.get('exposure_time') is not None
        ]

        # Best redshift: highest quality, tiebreak by max_snr
        valid_members = [
            m for m in members
            if m.get('redshift') is not None
        ]
        if valid_members:
            best = max(
                valid_members,
                key=lambda m: (
                    m.get('redshift_quality') or 0,
                    m.get('max_snr') or 0,
                ),
            )
            best_redshift = float(best['redshift'])
            best_quality = best.get('redshift_quality') or 0
        else:
            best_redshift = None
            best_quality = max(
                (m.get('redshift_quality') or 0 for m in members),
                default=0,
            )

        obj = {
            'object_id': object_id,
            'field': field,
            'ra': ra_centroid,
            'dec': dec_centroid,
            'n_targets': len(members),
            'n_spectra': total_spectra,
            'programs': sorted(set(m['program_slug'] for m in members)),
            'gratings': sorted(all_gratings),
            'max_snr': max(all_snr) if all_snr else None,
            'max_exposure_time': max(all_exp) if all_exp else None,
            'best_redshift': best_redshift,
            'best_redshift_quality': best_quality,
            '_member_db_ids': [m['id'] for m in members],
        }
        objects.append(obj)

    # Handle object_id collisions (rare: two groups with same centroid name)
    seen: dict[str, int] = {}
    for obj in objects:
        oid = obj['object_id']
        if oid in seen:
            seen[oid] += 1
            obj['object_id'] = f"{oid}_{seen[oid]}"
        else:
            seen[oid] = 0

    return objects


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def clear_existing(conn) -> None:
    """Clear all object_id FKs on targets, then delete all objects."""
    print("  Clearing existing object assignments...")
    with conn.cursor() as cur:
        cur.execute("UPDATE targets SET object_id = NULL WHERE object_id IS NOT NULL")
        cur.execute("DELETE FROM objects")
    conn.commit()
    print("  Done")


def insert_objects(conn, objects: list[dict]) -> dict[str, int]:
    """Insert objects and return mapping of object_id -> db id."""
    print(f"  Inserting {len(objects)} objects...")
    object_id_to_db_id: dict[str, int] = {}

    with conn.cursor() as cur:
        for obj in objects:
            cur.execute("""
                INSERT INTO objects (object_id, field, ra, dec, n_targets, n_spectra,
                                     programs, gratings, max_snr, max_exposure_time,
                                     best_redshift, best_redshift_quality)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                obj['object_id'], obj['field'], obj['ra'], obj['dec'],
                obj['n_targets'], obj['n_spectra'],
                obj['programs'], obj['gratings'],
                obj['max_snr'], obj['max_exposure_time'],
                obj['best_redshift'], obj['best_redshift_quality'],
            ))
            db_id = cur.fetchone()[0]
            object_id_to_db_id[obj['object_id']] = db_id

    conn.commit()
    print(f"  Inserted {len(object_id_to_db_id)} objects")
    return object_id_to_db_id


def set_target_fks(conn, objects: list[dict], object_id_to_db_id: dict[str, int]) -> None:
    """Set object_id FK on targets."""
    print("  Setting target FK references...")
    updates = []
    for obj in objects:
        db_id = object_id_to_db_id[obj['object_id']]
        for target_db_id in obj['_member_db_ids']:
            updates.append((db_id, target_db_id))

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            "UPDATE targets SET object_id = %s WHERE id = %s",
            updates,
        )
    conn.commit()
    print(f"  Updated {len(updates)} targets")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(objects: list[dict], targets: list[dict]) -> None:
    """Print cross-match summary statistics."""
    n_objects = len(objects)
    n_targets = len(targets)
    multi = [o for o in objects if o['n_targets'] > 1]
    n_multi = len(multi)

    print(f"\n{'='*60}")
    print(f"Cross-match summary")
    print(f"{'='*60}")
    print(f"  Total targets:          {n_targets}")
    print(f"  Total objects:          {n_objects}")
    print(f"  Singletons:             {n_objects - n_multi}")
    print(f"  Multi-target objects:   {n_multi}")

    if multi:
        sizes = [o['n_targets'] for o in multi]
        print(f"  Max group size:         {max(sizes)}")
        print(f"  Mean group size:        {np.mean(sizes):.1f}")

    # Per-field breakdown
    fields: dict[str, dict] = defaultdict(lambda: {'objects': 0, 'multi': 0})
    for obj in objects:
        fields[obj['field']]['objects'] += 1
        if obj['n_targets'] > 1:
            fields[obj['field']]['multi'] += 1

    print(f"\n  Per-field breakdown:")
    for fname in sorted(fields):
        f = fields[fname]
        print(f"    {fname}: {f['objects']} objects ({f['multi']} multi-target)")

    # List multi-target objects
    if multi:
        print(f"\n  Multi-target objects:")
        for obj in sorted(multi, key=lambda o: -o['n_targets']):
            programs = ', '.join(obj['programs'])
            print(f"    {obj['object_id']}: {obj['n_targets']} targets ({programs})")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Populate objects table via position cross-matching',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print stats without writing to database',
    )
    parser.add_argument(
        '--radius', type=float, default=MATCH_RADIUS_ARCSEC,
        help=f'Match radius in arcseconds (default: {MATCH_RADIUS_ARCSEC})',
    )
    parser.add_argument(
        '--field', type=str, default=None,
        help='Limit to a single field (for testing)',
    )
    parser.add_argument(
        '--dsn', type=str, default=LOCAL_DSN,
        help=f'Postgres connection string (default: {LOCAL_DSN})',
    )
    args = parser.parse_args()

    # Connect directly to Postgres
    print(f"Connecting to {args.dsn.split('@')[1] if '@' in args.dsn else args.dsn}...")
    conn = psycopg2.connect(args.dsn)

    # Fetch data
    print("Fetching targets...")
    targets = fetch_all_targets(conn, field=args.field)
    print(f"  Found {len(targets)} targets")

    if not targets:
        print("No targets found. Nothing to do.")
        conn.close()
        return

    print("Fetching spectra...")
    spectra_map = fetch_spectra_metadata(conn, field=args.field)
    n_spectra = sum(len(v) for v in spectra_map.values())
    print(f"  Found {n_spectra} spectra for {len(spectra_map)} targets")

    # Cross-match
    print(f"Cross-matching with radius={args.radius}\"...")
    groups = cluster_targets(targets, args.radius)

    # Build objects
    objects = build_objects(targets, groups, spectra_map)

    # Report
    print_summary(objects, targets)

    if args.dry_run:
        print("Dry run — no database changes made.")
        conn.close()
        return

    # Write to database
    print("Writing to database...")
    clear_existing(conn)
    object_id_to_db_id = insert_objects(conn, objects)
    set_target_fks(conn, objects, object_id_to_db_id)

    conn.close()
    print("Done!")


if __name__ == '__main__':
    main()
