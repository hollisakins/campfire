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
    """Fetch all distinct field values from the materialized filter options."""
    resp = client.table('mv_filter_options').select('fields').single().execute()
    return sorted(resp.data['fields'])


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
            'observations': sorted(set(m['observation'] for m in members)),
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
    """Null target FKs and delete objects for a field, in batches.

    Large fields (e.g. COSMOS with ~10k targets) exceed Supabase's
    statement timeout when updated in a single call.  We fetch small
    batches of IDs and update/delete them individually to stay within
    the timeout.
    """
    # 1. Batch-null the target FK references
    while True:
        resp = (
            client.table('targets')
            .select('id')
            .eq('field', field)
            .not_.is_('object_id', 'null')
            .order('id')
            .limit(BATCH_SIZE)
            .execute()
        )
        if not resp.data:
            break
        ids = [row['id'] for row in resp.data]
        client.table('targets').update(
            {'object_id': None},
        ).in_('id', ids).execute()

    # 2. Batch-delete objects (ON DELETE SET NULL cascades to
    #    object_list_members, so large deletes can also time out)
    while True:
        resp = (
            client.table('objects')
            .select('id')
            .eq('field', field)
            .order('id')
            .limit(BATCH_SIZE)
            .execute()
        )
        if not resp.data:
            break
        ids = [row['id'] for row in resp.data]
        client.table('objects').delete().in_('id', ids).execute()


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
                'observations': obj['observations'],
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


def _relink_list_members(client: Client, field: str) -> dict:
    """Re-link object_list_members.object_id after object rebuild.

    List members are keyed by (ra, dec) which survive rebuilds. This
    updates the object_id FK by spatial cross-matching against the
    newly created objects (within 0.3 arcsec tolerance).

    Returns dict with keys: relinked, orphaned, orphaned_details.
    """
    resp = client.rpc('relink_list_members_for_field', {
        'p_field': field,
    }).execute()
    if isinstance(resp.data, dict):
        return resp.data
    return {'relinked': 0, 'orphaned': 0, 'orphaned_details': []}


def _relink_photometry(client: Client, field: str) -> dict:
    """Re-link object_photometry.object_id after object rebuild.

    Photometry rows are keyed by (ra, dec) which survive rebuilds. This
    updates the object_id FK by spatial cross-matching against the
    newly created objects (within 0.3 arcsec tolerance).

    Returns dict with keys: relinked, orphaned.
    """
    resp = client.rpc('relink_photometry_for_field', {
        'p_field': field,
    }).execute()
    if isinstance(resp.data, dict):
        return resp.data
    return {'relinked': 0, 'orphaned': 0}


def _sync_photometry(client: Client, field: str) -> int:
    """Sync photo_z and has_photometry from object_photometry to objects."""
    resp = client.rpc('sync_photometry_to_objects', {
        'p_field': field,
    }).execute()
    return resp.data if isinstance(resp.data, int) else 0


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

    print(f"  Re-linking list member FKs...")
    relink_result = _relink_list_members(client, field)
    n_relinked = relink_result.get('relinked', 0)
    n_orphaned = relink_result.get('orphaned', 0)
    if n_relinked:
        print(f"    Re-linked {n_relinked} list members")
    if n_orphaned:
        print(f"    WARNING: {n_orphaned} orphaned list members (no object within 0.3\"):")
        for detail in relink_result.get('orphaned_details', []):
            print(f"      - List \"{detail['list_name']}\": "
                  f"RA={detail['ra']:.6f}, Dec={detail['dec']:.6f}")

    print(f"  Re-linking photometry FKs...")
    phot_result = _relink_photometry(client, field)
    n_phot_relinked = phot_result.get('relinked', 0)
    n_phot_orphaned = phot_result.get('orphaned', 0)
    if n_phot_relinked:
        print(f"    Re-linked {n_phot_relinked} photometry rows")
    if n_phot_orphaned:
        print(f"    WARNING: {n_phot_orphaned} orphaned photometry rows")

    if n_phot_relinked:
        print(f"  Syncing photometry to objects...")
        n_synced = _sync_photometry(client, field)
        print(f"    Updated {n_synced} objects")

    print_rebuild_summary(objects, targets)

    return len(objects), len(multi)
