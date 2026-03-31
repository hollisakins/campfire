"""
Objects table rebuild via position cross-matching.

Clusters targets within ~0.2 arcsec using friends-of-friends and populates
the objects table with aggregate properties. Designed for per-field
wipe-and-rebuild — the objects table carries no user-editable state.

The clustering algorithm uses astropy's search_around_sky for vectorized
pair-finding and a Union-Find structure for connected components.
"""

from collections import defaultdict
from datetime import datetime, timezone

from astropy.coordinates import SkyCoord, search_around_sky
import astropy.units as u
import numpy as np
from supabase import Client


MATCH_RADIUS_ARCSEC = 0.2
BATCH_SIZE = 500


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
            self.parent[x] = self.parent[self.parent[x]]
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

    ra_h = coord.ra.hms
    ra_str = f"{int(ra_h.h):02d}{int(ra_h.m):02d}{ra_h.s:05.2f}"

    dec_d = coord.dec.dms
    sign = '+' if dec_deg >= 0 else '-'
    dec_str = f"{int(abs(dec_d.d)):02d}{int(abs(dec_d.m)):02d}{abs(dec_d.s):04.1f}"

    return f"J{ra_str}{sign}{dec_str}"


# ---------------------------------------------------------------------------
# Data fetching (Supabase client)
# ---------------------------------------------------------------------------

def fetch_field_targets(client: Client, field: str) -> list[dict]:
    """Fetch all targets for a field, with pagination."""
    fields = (
        'id, target_id, ra, dec, field, program_slug, observation, '
        'redshift, redshift_quality, max_snr, max_exposure_time'
    )
    all_targets = []
    page_size = 1000
    offset = 0

    while True:
        resp = (
            client.table('targets')
            .select(fields)
            .eq('field', field)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        all_targets.extend(resp.data)
        if len(resp.data) < page_size:
            break
        offset += page_size

    return all_targets


def fetch_spectra_metadata(
    client: Client,
    field: str,
) -> dict[str, list[dict]]:
    """Fetch grating, S/N, exposure_time for all spectra in a field.

    Uses PostgREST embedding via the spectra→targets FK to filter
    server-side by field, avoiding URI-length limits from large
    target_id lists.
    """
    select = 'target_id, grating, signal_to_noise, exposure_time, targets!inner(field)'
    result: dict[str, list[dict]] = defaultdict(list)
    page_size = 1000
    offset = 0

    while True:
        resp = (
            client.table('spectra')
            .select(select)
            .eq('targets.field', field)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        for row in resp.data:
            result[row['target_id']].append({
                'target_id': row['target_id'],
                'grating': row['grating'],
                'signal_to_noise': row['signal_to_noise'],
                'exposure_time': row['exposure_time'],
            })
        if len(resp.data) < page_size:
            break
        offset += page_size

    return result


def fetch_distinct_fields(client: Client) -> list[str]:
    """Fetch all distinct field values from targets table."""
    # Use a lightweight query — select field, deduplicate in Python
    all_fields = set()
    page_size = 1000
    offset = 0

    while True:
        resp = (
            client.table('targets')
            .select('field')
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        for row in resp.data:
            all_fields.add(row['field'])
        if len(resp.data) < page_size:
            break
        offset += page_size

    return sorted(all_fields)


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

    idx1, idx2, _, _ = search_around_sky(
        coords, coords, radius_arcsec * u.arcsec,
    )

    uf = UnionFind(len(targets))
    for i, j in zip(idx1, idx2):
        i_int, j_int = int(i), int(j)
        if i_int != j_int:
            uf.union(i_int, j_int)

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

        # Field (uniform within a rebuild)
        field = members[0]['field']

        # Aggregate spectra info
        all_gratings = set()
        total_spectra = 0
        for m in members:
            spec_rows = spectra_map.get(m['target_id'], [])
            total_spectra += len(spec_rows)
            for s in spec_rows:
                all_gratings.add(s['grating'])

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

def _clear_field_objects(client: Client, field: str) -> None:
    """Null target FKs and delete objects for a field."""
    # Must null FKs first (FK constraint has no ON DELETE SET NULL)
    client.table('targets').update(
        {'object_id': None},
    ).eq('field', field).not_.is_('object_id', 'null').execute()

    client.table('objects').delete().eq('field', field).execute()


def _insert_objects(
    client: Client,
    objects: list[dict],
) -> dict[str, int]:
    """Batch-insert objects, return mapping of object_id -> db id."""
    object_id_to_db_id: dict[str, int] = {}
    now = datetime.now(timezone.utc).isoformat()

    for i in range(0, len(objects), BATCH_SIZE):
        batch = objects[i:i + BATCH_SIZE]
        records = [
            {
                'object_id': obj['object_id'],
                'field': obj['field'],
                'ra': obj['ra'],
                'dec': obj['dec'],
                'n_targets': obj['n_targets'],
                'n_spectra': obj['n_spectra'],
                'programs': obj['programs'],
                'gratings': obj['gratings'],
                'max_snr': obj['max_snr'],
                'max_exposure_time': obj['max_exposure_time'],
                'best_redshift': obj['best_redshift'],
                'best_redshift_quality': obj['best_redshift_quality'],
                'updated_at': now,
            }
            for obj in batch
        ]
        resp = client.table('objects').insert(records).execute()
        for row in resp.data:
            object_id_to_db_id[row['object_id']] = row['id']

    return object_id_to_db_id


def _set_target_fks(
    client: Client,
    objects: list[dict],
    object_id_to_db_id: dict[str, int],
) -> int:
    """Set object_id FK on targets via a server-side RPC for bulk efficiency.

    Sends all (target_db_id, object_db_id) pairs to a Postgres function
    that performs the UPDATE in a single transaction, avoiding per-object
    HTTP round-trips.
    """
    pairs = []
    for obj in objects:
        db_id = object_id_to_db_id[obj['object_id']]
        for tid in obj['_member_db_ids']:
            pairs.append({'target_id': tid, 'object_id': db_id})

    if not pairs:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    total = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i:i + BATCH_SIZE]
        client.rpc('bulk_set_target_object_fks', {
            'p_pairs': batch,
            'p_updated_at': now,
        }).execute()
        total += len(batch)

    return total


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_rebuild_summary(objects: list[dict], targets: list[dict]) -> None:
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

        print(f"\n  Multi-target objects:")
        for obj in sorted(multi, key=lambda o: -o['n_targets']):
            programs = ', '.join(obj['programs'])
            print(f"    {obj['object_id']}: {obj['n_targets']} targets ({programs})")

    print()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def rebuild_field_objects(
    client: Client,
    field: str,
    *,
    radius: float = MATCH_RADIUS_ARCSEC,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Rebuild the objects table for a single field.

    Wipes existing objects for the field and rebuilds from scratch
    using friends-of-friends clustering on target positions.

    Args:
        client: Supabase client (service role)
        field: Field name to rebuild
        radius: Cross-match radius in arcseconds
        dry_run: Print stats without writing

    Returns:
        Tuple of (n_objects, n_multi_target)
    """
    # Fetch data
    print(f"  Fetching targets for field '{field}'...")
    targets = fetch_field_targets(client, field)
    print(f"    {len(targets)} targets")

    if not targets:
        print(f"  No targets in field '{field}'. Nothing to do.")
        return 0, 0

    print(f"  Fetching spectra metadata...")
    spectra_map = fetch_spectra_metadata(client, field)
    n_spectra = sum(len(v) for v in spectra_map.values())
    print(f"    {n_spectra} spectra for {len(spectra_map)} targets")

    # Cross-match
    print(f"  Clustering with radius={radius}\"...")
    groups = cluster_targets(targets, radius)

    # Build objects
    objects = build_objects(targets, groups, spectra_map)
    multi = [o for o in objects if o['n_targets'] > 1]

    if dry_run:
        print_rebuild_summary(objects, targets)
        return len(objects), len(multi)

    # Write to database
    print(f"  Clearing existing objects for field '{field}'...")
    _clear_field_objects(client, field)

    print(f"  Inserting {len(objects)} objects...")
    object_id_to_db_id = _insert_objects(client, objects)

    print(f"  Setting target FK references...")
    n_fks = _set_target_fks(client, objects, object_id_to_db_id)
    print(f"    Updated {n_fks} targets")

    print_rebuild_summary(objects, targets)

    return len(objects), len(multi)
