"""
Persistent-objects reconciliation (Phase C of the objects migration).

Replaces the wipe-and-rebuild path (`objects.rebuild_field_objects`) with an
incremental reconciler that preserves inspection state, comments, list
membership, and photometry across deploys. The legacy rebuild remains as an
escape hatch (`campfire deploy objects rebuild --force`).

The matching algorithm is membership-based: each existing object's previous
member targets are followed into the new FoF clusters. Position matching is
used only as a fallback to "revive" inactive objects when a new cluster
appears at a previously-occupied sky position.

Splits/merges are detected, surfaced to the operator, and applied via an
id-reuse strategy: the inheriting daughter (split) or surviving parent
(merge) keeps its existing object.id, so its inspection state, comments,
list memberships, and photometry stay attached without any per-row migration.
The other daughters/losers are inserted as new objects (split) or
soft-deleted with associated state migrated to the survivor (merge).

See `docs/design-objects-migration.md` for the full design.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Iterable

import click
import numpy as np
from astropy.coordinates import SkyCoord
import astropy.units as u
from supabase import Client

from campfire.deploy.objects import (
    BATCH_SIZE,
    cluster_targets,
    fetch_field_targets,
    fetch_spectra_metadata,
    generate_iau_name,
)


POSITION_MATCH_RADIUS_ARCSEC = 0.3
DEFAULT_FOF_RADIUS_ARCSEC = 0.2


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------


@dataclass
class ClusterAggregates:
    """Aggregates computed from a cluster's member targets and spectra."""
    object_id: str  # IAU name from centroid
    ra: float
    dec: float
    n_targets: int
    n_spectra: int
    programs: list[str]
    gratings: list[str]
    observations: list[str]
    max_snr: float | None
    max_exposure_time: float | None
    member_target_db_ids: list[int]
    member_target_ids: set[str]


@dataclass
class Update:
    """A 1:1 match: cluster updates an existing object in place."""
    cluster_idx: int
    object: dict
    aggregates: ClusterAggregates
    staleness_reason: str | None  # 'new_target' | 'membership_changed' | 'reprocessed' | None


@dataclass
class Insert:
    """A new cluster with no existing-object link — insert a new row."""
    cluster_idx: int
    aggregates: ClusterAggregates


@dataclass
class Revival:
    """A new cluster matched to an inactive object by position. Reactivate the orphan."""
    cluster_idx: int
    object: dict  # the inactive object being revived
    aggregates: ClusterAggregates


@dataclass
class Split:
    """An existing object's members went to multiple clusters."""
    object: dict
    daughters: list[ClusterAggregates]  # one per cluster
    daughter_cluster_indices: list[int]
    inheritor_index: int  # index into daughters of the inheriting daughter


@dataclass
class Merge:
    """A cluster's members came from multiple existing objects."""
    cluster_idx: int
    aggregates: ClusterAggregates
    survivor: dict
    losers: list[dict]


@dataclass
class Orphan:
    """An existing active object lost all its members. Soft-delete."""
    object: dict


@dataclass
class ComplexOverlap:
    """A cluster that is simultaneously a split-daughter and a merge-sink.

    Topologically ambiguous: the cluster inherits (part of) one object via a
    split while also absorbing (all or part of) one or more additional objects
    via a merge. An id-reuse policy that favors one model silently drops data
    from the other, so these cases must be resolved by the operator.
    """
    cluster_idx: int
    aggregates: ClusterAggregates
    # Object whose members were split across this cluster and others.
    split_source: dict
    # Other sibling cluster indices from the same split.
    split_sibling_cluster_indices: list[int]
    # Additional existing objects whose members landed in this cluster.
    merge_sources: list[dict]


@dataclass
class Proposals:
    updates: list[Update] = dc_field(default_factory=list)
    inserts: list[Insert] = dc_field(default_factory=list)
    revivals: list[Revival] = dc_field(default_factory=list)
    splits: list[Split] = dc_field(default_factory=list)
    merges: list[Merge] = dc_field(default_factory=list)
    orphans: list[Orphan] = dc_field(default_factory=list)
    complex_overlaps: list[ComplexOverlap] = dc_field(default_factory=list)
    # No-op 1:1 matches: cluster and existing object have identical aggregates
    # and no staleness signal. Skipped in apply_proposals to avoid bumping
    # updated_at on every row (which would bloat incremental sync cursors).
    unchanged: int = 0


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------


def fetch_existing_objects(
    client: Client, field: str
) -> tuple[list[dict], dict[int, set[str]], dict[int, set[int]]]:
    """Fetch existing objects in the field with their current member targets.

    Returns:
        (objects, members_by_object_id, member_db_ids_by_object_id)

        - `objects`: list of dicts (active + inactive), with full inspection
          state needed for split/merge tiebreaks.
        - `members_by_object_id`: {object_db_id: set of target_id strings}
        - `member_db_ids_by_object_id`: {object_db_id: set of target db ids}
    """
    objects: list[dict] = []
    page_size = 1000
    offset = 0
    select = (
        'id, object_id, field, ra, dec, is_active, '
        'redshift_quality, last_inspected_at, '
        'redshift_inspected, redshift_auto, last_inspected_by, '
        'last_data_change_at, staleness_reason, version, '
        # Aggregate columns — used by classify() to detect no-op 1:1 matches
        # and skip redundant updates (avoids bumping updated_at on every row).
        'n_targets, n_spectra, programs, gratings, observations, '
        'max_snr, max_exposure_time'
    )
    while True:
        resp = (
            client.table('objects')
            .select(select)
            .eq('field', field)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        objects.extend(resp.data or [])
        if len(resp.data or []) < page_size:
            break
        offset += page_size

    # Fetch all targets in field to build the membership reverse-index.
    members: dict[int, set[str]] = defaultdict(set)
    member_db_ids: dict[int, set[int]] = defaultdict(set)
    offset = 0
    while True:
        resp = (
            client.table('targets')
            .select('id, target_id, object_id')
            .eq('field', field)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        rows = resp.data or []
        for row in rows:
            if row['object_id'] is not None:
                members[row['object_id']].add(row['target_id'])
                member_db_ids[row['object_id']].add(row['id'])
        if len(rows) < page_size:
            break
        offset += page_size

    return objects, dict(members), dict(member_db_ids)


# ---------------------------------------------------------------------------
# Cluster aggregation
# ---------------------------------------------------------------------------


def build_cluster_aggregates(
    cluster_indices: list[int],
    targets: list[dict],
    spectra_map: dict[str, list[dict]],
) -> ClusterAggregates:
    members = [targets[i] for i in cluster_indices]

    ra_centroid = float(np.mean([m['ra'] for m in members]))
    dec_centroid = float(np.mean([m['dec'] for m in members]))

    all_gratings: set[str] = set()
    all_snr: list[float] = []
    all_exp: list[float] = []
    n_spectra = 0
    for m in members:
        for s in spectra_map.get(m['target_id'], []):
            n_spectra += 1
            if s.get('grating'):
                all_gratings.add(s['grating'])
            if s.get('signal_to_noise') is not None:
                all_snr.append(float(s['signal_to_noise']))
            if s.get('exposure_time') is not None:
                all_exp.append(float(s['exposure_time']))

    return ClusterAggregates(
        object_id=generate_iau_name(ra_centroid, dec_centroid),
        ra=ra_centroid,
        dec=dec_centroid,
        n_targets=len(members),
        n_spectra=n_spectra,
        programs=sorted({m['program_slug'] for m in members}),
        gratings=sorted(all_gratings),
        observations=sorted({m['observation'] for m in members}),
        max_snr=max(all_snr) if all_snr else None,
        max_exposure_time=max(all_exp) if all_exp else None,
        member_target_db_ids=[m['id'] for m in members],
        member_target_ids={m['target_id'] for m in members},
    )


def _aggregates_match_object(agg: ClusterAggregates, obj: dict) -> bool:
    """True if the cluster's aggregates equal the existing object's stored aggregates.

    Used by classify() to detect no-op 1:1 matches: the cluster has identical
    membership AND identical per-spectrum aggregates to the existing object,
    so there is nothing to write. Skipping these rows avoids bumping
    objects.updated_at (which is the cursor for incremental sync) on every
    object in the field just because one observation was redeployed.

    Array columns (programs/gratings/observations) are always sorted by
    build_cluster_aggregates() before write, so list equality is valid.
    """
    return (
        agg.object_id == obj.get('object_id')
        and agg.ra == obj.get('ra')
        and agg.dec == obj.get('dec')
        and agg.n_targets == obj.get('n_targets')
        and agg.n_spectra == obj.get('n_spectra')
        and list(agg.programs) == list(obj.get('programs') or [])
        and list(agg.gratings) == list(obj.get('gratings') or [])
        and list(agg.observations) == list(obj.get('observations') or [])
        and agg.max_snr == obj.get('max_snr')
        and agg.max_exposure_time == obj.get('max_exposure_time')
    )


def deduplicate_iau_names(items: Iterable[ClusterAggregates]) -> None:
    """Resolve IAU-name collisions by appending _1, _2, ... in place."""
    seen: dict[str, int] = {}
    for agg in items:
        oid = agg.object_id
        if oid in seen:
            seen[oid] += 1
            agg.object_id = f"{oid}_{seen[oid]}"
        else:
            seen[oid] = 0


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(
    groups: list[list[int]],
    targets: list[dict],
    existing_objects: list[dict],
    members_by_obj: dict[int, set[str]],
    spectra_map: dict[str, list[dict]],
    changed_hashes: set[tuple[str, str]],
    *,
    position_tolerance_arcsec: float = POSITION_MATCH_RADIUS_ARCSEC,
) -> Proposals:
    """Sort the (cluster × existing_object) cross-product into proposal categories.

    Algorithm:
      1. Build target → cluster map and target → existing-object map.
      2. For each existing object: which clusters did its members land in?
      3. For each cluster: which existing objects did its members come from?
      4. Classify into split / merge / update / insert / orphan.
      5. Position-match unmatched inserts against inactive objects (revival).
    """
    # Index targets by db id.
    target_by_db_id = {t['id']: t for t in targets}

    target_to_cluster: dict[int, int] = {}
    for ci, indices in enumerate(groups):
        for tidx in indices:
            target_to_cluster[targets[tidx]['id']] = ci

    # Existing object FK on each target row (may be None for new targets).
    target_to_obj: dict[int, int | None] = {
        t['id']: t.get('object_id') for t in targets
    }

    # For each existing object: the clusters its members went to.
    obj_clusters: dict[int, set[int]] = defaultdict(set)
    for tid, obj_id in target_to_obj.items():
        if obj_id is None:
            continue
        if tid in target_to_cluster:
            obj_clusters[obj_id].add(target_to_cluster[tid])

    # For each cluster: the existing objects its members came from.
    cluster_objects: dict[int, set[int]] = defaultdict(set)
    for ci, indices in enumerate(groups):
        for tidx in indices:
            tid = targets[tidx]['id']
            oid = target_to_obj.get(tid)
            if oid is not None:
                cluster_objects[ci].add(oid)

    obj_by_id = {o['id']: o for o in existing_objects}

    # Pre-compute aggregates for every cluster.
    aggs: list[ClusterAggregates] = [
        build_cluster_aggregates(g, targets, spectra_map) for g in groups
    ]
    deduplicate_iau_names(aggs)

    proposals = Proposals()
    handled_objects: set[int] = set()
    handled_clusters: set[int] = set()

    # 0) Complex overlaps — a cluster that is BOTH a split-daughter and a
    #    merge-sink. The two id-reuse models are incompatible, so auto-
    #    applying either one silently drops data from the other. Surface
    #    these to the operator and exclude them + their related objects
    #    from the split/merge passes below.
    overlap_cluster_indices: set[int] = set()
    for ci, oset in cluster_objects.items():
        if len(oset) <= 1:
            continue
        # Cluster is a merge-sink candidate. Is any origin object ALSO being
        # split across multiple clusters?
        split_origins = [oid for oid in oset if len(obj_clusters.get(oid, set())) > 1]
        if not split_origins:
            continue
        # Record one ComplexOverlap per split origin so the operator sees the
        # full topology. Typically there's exactly one split origin; more is
        # rare but possible.
        for split_oid in split_origins:
            sibling_clusters = sorted(obj_clusters[split_oid])
            merge_sources = [
                obj_by_id[oid] for oid in sorted(oset) if oid != split_oid
            ]
            proposals.complex_overlaps.append(ComplexOverlap(
                cluster_idx=ci,
                aggregates=aggs[ci],
                split_source=obj_by_id[split_oid],
                split_sibling_cluster_indices=sibling_clusters,
                merge_sources=merge_sources,
            ))
        overlap_cluster_indices.add(ci)
        # Mark every object involved (split origin + merge sources + all
        # sibling daughter clusters) as handled so downstream steps don't
        # second-guess the topology. The operator resolves it post-hoc.
        for oid in oset:
            handled_objects.add(oid)
            overlap_cluster_indices.update(obj_clusters.get(oid, set()))
    handled_clusters.update(overlap_cluster_indices)

    # 1) Splits — an existing object's members spread across multiple clusters.
    for obj_id, cset in obj_clusters.items():
        if len(cset) <= 1:
            continue
        if obj_id in handled_objects:
            continue  # claimed by a complex_overlaps entry above
        cluster_idx_list = sorted(cset)
        daughters = [aggs[ci] for ci in cluster_idx_list]
        # Inheritor: daughter with highest max_snr among those with ≥50%
        # overlap with the original member set; ties → first by cluster index.
        original_members = members_by_obj.get(obj_id, set())
        inheritor_idx = _choose_split_inheritor(daughters, original_members)
        proposals.splits.append(
            Split(
                object=obj_by_id[obj_id],
                daughters=daughters,
                daughter_cluster_indices=cluster_idx_list,
                inheritor_index=inheritor_idx,
            )
        )
        handled_objects.add(obj_id)
        handled_clusters.update(cluster_idx_list)

    # 2) Merges — a cluster's members came from multiple existing objects.
    for ci, oset in cluster_objects.items():
        if ci in handled_clusters or len(oset) <= 1:
            continue
        objs = [obj_by_id[oid] for oid in sorted(oset)]
        survivor, losers = _choose_merge_survivor(objs)
        proposals.merges.append(
            Merge(
                cluster_idx=ci,
                aggregates=aggs[ci],
                survivor=survivor,
                losers=losers,
            )
        )
        handled_clusters.add(ci)
        handled_objects.add(survivor['id'])
        handled_objects.update(o['id'] for o in losers)

    # 3) Updates — 1:1 match (cluster came from exactly one object, and that
    #    object's only landing cluster is this one).
    for ci, oset in cluster_objects.items():
        if ci in handled_clusters or len(oset) != 1:
            continue
        oid = next(iter(oset))
        if oid in handled_objects:
            continue
        if obj_clusters.get(oid) != {ci}:
            continue
        obj = obj_by_id[oid]
        agg = aggs[ci]
        staleness = _detect_staleness(
            obj=obj,
            previous_member_target_ids=members_by_obj.get(oid, set()),
            new_member_target_ids=agg.member_target_ids,
            changed_hashes=changed_hashes,
            spectra_map=spectra_map,
        )
        # Skip no-op updates: same member set (enforced by the 1:1 guard
        # above), no reprocessed spectra (staleness is None), and identical
        # aggregates. Writing these would bump updated_at on every row and
        # cause every incremental sync to re-pull the full field.
        # The object must already be active (inactive 1:1 matches need a
        # reactivation write in apply_proposals).
        if (
            staleness is None
            and obj.get('is_active')
            and _aggregates_match_object(agg, obj)
        ):
            proposals.unchanged += 1
            handled_clusters.add(ci)
            handled_objects.add(oid)
            continue
        proposals.updates.append(
            Update(
                cluster_idx=ci,
                object=obj,
                aggregates=agg,
                staleness_reason=staleness,
            )
        )
        handled_clusters.add(ci)
        handled_objects.add(oid)

    # 4) Position-match revivals — clusters with no object link, near an
    #    inactive object's centroid.
    inactive_objs = [
        o for o in existing_objects
        if not o.get('is_active') and o['id'] not in handled_objects
    ]
    if inactive_objs:
        revival_idx = _greedy_position_match(
            unmatched_clusters=[
                (ci, aggs[ci]) for ci in range(len(groups))
                if ci not in handled_clusters
            ],
            candidate_objects=inactive_objs,
            tolerance_arcsec=position_tolerance_arcsec,
        )
        for ci, obj in revival_idx:
            proposals.revivals.append(Revival(
                cluster_idx=ci,
                object=obj,
                aggregates=aggs[ci],
            ))
            handled_clusters.add(ci)
            handled_objects.add(obj['id'])

    # 5) Inserts — leftover unhandled clusters.
    for ci in range(len(groups)):
        if ci in handled_clusters:
            continue
        proposals.inserts.append(Insert(cluster_idx=ci, aggregates=aggs[ci]))

    # 6) Orphans — leftover unhandled active objects.
    for obj in existing_objects:
        if obj['id'] in handled_objects:
            continue
        if not obj.get('is_active'):
            continue  # already inactive, not a fresh orphan
        proposals.orphans.append(Orphan(object=obj))

    return proposals


def _choose_split_inheritor(
    daughters: list[ClusterAggregates],
    original_members: set[str],
) -> int:
    """Pick the daughter with max_snr that overlaps original members ≥50%."""
    eligible: list[tuple[int, float]] = []
    for i, d in enumerate(daughters):
        overlap = len(d.member_target_ids & original_members)
        if not original_members:
            ratio = 0.0
        else:
            ratio = overlap / len(original_members)
        if ratio >= 0.5:
            eligible.append((i, d.max_snr or 0.0))
    if not eligible:
        # Fall back to overall max_snr — keeps behavior defined when the
        # original membership has been completely reshuffled.
        eligible = [(i, d.max_snr or 0.0) for i, d in enumerate(daughters)]
    eligible.sort(key=lambda t: (-t[1], t[0]))
    return eligible[0][0]


def _choose_merge_survivor(objs: list[dict]) -> tuple[dict, list[dict]]:
    """Highest redshift_quality wins; tie → most recent last_inspected_at, then max_snr."""
    def key(o: dict) -> tuple:
        return (
            -(o.get('redshift_quality') or 0),
            -_iso_to_epoch(o.get('last_inspected_at')),
            -(o.get('max_snr') or 0.0),
            o['id'],
        )
    ranked = sorted(objs, key=key)
    return ranked[0], ranked[1:]


def _iso_to_epoch(iso: str | None) -> float:
    if iso is None:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _detect_staleness(
    *,
    obj: dict,
    previous_member_target_ids: set[str],
    new_member_target_ids: set[str],
    changed_hashes: set[tuple[str, str]],
    spectra_map: dict[str, list[dict]],
) -> str | None:
    """Compute staleness_reason for a 1:1 matched cluster/object pair.

    Priority (mutually exclusive): membership_changed > new_target > reprocessed > None.
    """
    added = new_member_target_ids - previous_member_target_ids
    removed = previous_member_target_ids - new_member_target_ids
    if removed or len(added) > 1:
        return 'membership_changed'
    if len(added) == 1:
        return 'new_target'
    # Membership unchanged — check for any member spectrum hash change.
    for tid in new_member_target_ids:
        for s in spectra_map.get(tid, []):
            if (tid, s['grating']) in changed_hashes:
                return 'reprocessed'
    return None


def _greedy_position_match(
    unmatched_clusters: list[tuple[int, ClusterAggregates]],
    candidate_objects: list[dict],
    tolerance_arcsec: float,
) -> list[tuple[int, dict]]:
    """Greedy distance-sorted 1:1 match between clusters and candidate objects."""
    if not unmatched_clusters or not candidate_objects:
        return []
    cluster_coords = SkyCoord(
        ra=[a.ra for _, a in unmatched_clusters] * u.deg,
        dec=[a.dec for _, a in unmatched_clusters] * u.deg,
    )
    obj_coords = SkyCoord(
        ra=[o['ra'] for o in candidate_objects] * u.deg,
        dec=[o['dec'] for o in candidate_objects] * u.deg,
    )
    pairs: list[tuple[float, int, int]] = []
    for i, c_coord in enumerate(cluster_coords):
        seps = c_coord.separation(obj_coords).arcsec
        for j, sep in enumerate(seps):
            if sep <= tolerance_arcsec:
                pairs.append((float(sep), i, j))
    pairs.sort()
    used_clusters: set[int] = set()
    used_objects: set[int] = set()
    matches: list[tuple[int, dict]] = []
    for _, i, j in pairs:
        if i in used_clusters or j in used_objects:
            continue
        used_clusters.add(i)
        used_objects.add(j)
        matches.append((unmatched_clusters[i][0], candidate_objects[j]))
    return matches


# ---------------------------------------------------------------------------
# CLI confirmation for splits/merges
# ---------------------------------------------------------------------------


def confirm_proposals(
    proposals: Proposals,
    *,
    yes: bool,
    dry_run: bool,
    abort_on_changes: bool = False,
) -> None:
    """Print split/merge proposals; require confirm unless --yes/--dry-run.

    `abort_on_changes` is set by the in-deploy caller: rather than prompting
    in the middle of a deploy run, we abort hard and tell the operator to
    resolve interactively via `campfire deploy objects reconcile`.

    ``complex_overlaps`` are ALWAYS fatal: the split+merge ambiguity cannot
    be auto-resolved without silently dropping inspection state from one
    side. They force the operator into `campfire deploy objects split` /
    `merge` resolution (or `rebuild --force` as an escape hatch).
    """
    if proposals.complex_overlaps:
        print()
        print(f"  ERROR: {len(proposals.complex_overlaps)} split+merge overlap(s):")
        for ov in proposals.complex_overlaps:
            print(f"    OVERLAP → cluster {ov.aggregates.object_id} "
                  f"({ov.aggregates.n_targets} targets)")
            print(f"      • split source: {ov.split_source['object_id']} "
                  f"(id={ov.split_source['id']}) — also spreads to "
                  f"{len(ov.split_sibling_cluster_indices) - 1} other cluster(s)")
            for src in ov.merge_sources:
                print(f"      • merge source: {src['object_id']} (id={src['id']}, "
                      f"q={src.get('redshift_quality')}, max_snr={src.get('max_snr')})")
        raise click.ClickException(
            "Split+merge overlap detected. These topologies cannot be safely "
            "auto-resolved — inspection state on one side would be silently "
            "dropped. Resolve manually with:\n"
            "    campfire deploy objects split --object <id>\n"
            "    campfire deploy objects merge --into <id> --from <id>\n"
            "Or, if the old topology is unrecoverable, use:\n"
            "    campfire deploy objects rebuild --field <name> --force"
        )

    if not proposals.splits and not proposals.merges:
        return
    print()
    print(f"  Detected {len(proposals.splits)} split(s), {len(proposals.merges)} merge(s):")
    for s in proposals.splits:
        print(f"    SPLIT {s.object['object_id']} (id={s.object['id']}) → {len(s.daughters)} daughters")
        for i, d in enumerate(s.daughters):
            tag = " ← inherits state" if i == s.inheritor_index else ""
            print(f"      • {d.object_id}: {d.n_targets} targets, max_snr={d.max_snr}{tag}")
    for m in proposals.merges:
        print(f"    MERGE → {m.aggregates.object_id} ({m.aggregates.n_targets} targets)")
        print(f"      • survivor:  {m.survivor['object_id']} (id={m.survivor['id']}, "
              f"q={m.survivor.get('redshift_quality')}, max_snr={m.survivor.get('max_snr')})")
        for loser in m.losers:
            print(f"      • soft-deleted: {loser['object_id']} (id={loser['id']}, "
                  f"q={loser.get('redshift_quality')}, max_snr={loser.get('max_snr')})")
    if dry_run or yes:
        return
    if abort_on_changes:
        raise click.ClickException(
            "Splits/merges detected during deploy. Resolve them interactively with:\n"
            "    campfire deploy objects reconcile --field <name>\n"
            "Then re-run the deploy."
        )
    if not sys.stdin.isatty():
        raise click.ClickException(
            "Splits/merges require interactive confirmation. "
            "Pass --yes to skip the prompt in non-interactive mode."
        )
    click.confirm("Apply these split/merge changes?", abort=True)


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_proposals(
    client: Client, field: str, proposals: Proposals,
) -> tuple[dict[str, int], set[int]]:
    """Execute the reconciliation against Supabase.

    Returns:
        (stats, changed_object_db_ids). The set contains the union of
        object DB ids that were inserted, revived, updated, split (parent
        plus daughters), or merged (survivor). Soft-deleted orphans and
        merge losers are excluded — downstream consumers (e.g. photometry)
        don't need to re-process them.
    """
    if proposals.complex_overlaps:
        raise RuntimeError(
            f"apply_proposals called with {len(proposals.complex_overlaps)} "
            "unresolved complex_overlaps. confirm_proposals should have "
            "aborted before reaching this point."
        )
    now = datetime.now(timezone.utc).isoformat()
    stats = defaultdict(int)

    # The order matters: inserts must run before target FK updates that
    # reference them, and merge survivors must absorb associated state from
    # losers before the losers are soft-deleted.

    # Track (cluster_idx → object_db_id) for FK assignment at the end.
    cluster_to_object_db_id: dict[int, int] = {}
    # Track touched object DB ids (returned for downstream consumers).
    changed_ids: set[int] = set()

    # 1. Inserts — brand-new objects.
    if proposals.inserts:
        new_records = [
            _aggregate_to_insert_dict(p.aggregates, field, now)
            for p in proposals.inserts
        ]
        ids = _insert_objects(client, new_records)
        for p, oid in zip(proposals.inserts, ids):
            cluster_to_object_db_id[p.cluster_idx] = oid
            changed_ids.add(oid)
        stats['inserted'] += len(ids)

    # 2. Revivals — reactivate inactive objects, re-set centroid + aggregates.
    for r in proposals.revivals:
        client.table('objects').update({
            'is_active': True,
            'object_id': r.aggregates.object_id,
            'ra': r.aggregates.ra,
            'dec': r.aggregates.dec,
            'n_targets': r.aggregates.n_targets,
            'n_spectra': r.aggregates.n_spectra,
            'programs': r.aggregates.programs,
            'gratings': r.aggregates.gratings,
            'observations': r.aggregates.observations,
            'max_snr': r.aggregates.max_snr,
            'max_exposure_time': r.aggregates.max_exposure_time,
            'last_data_change_at': now,
            'staleness_reason': 'membership_changed',
            'updated_at': now,
        }).eq('id', r.object['id']).execute()
        cluster_to_object_db_id[r.cluster_idx] = r.object['id']
        changed_ids.add(r.object['id'])
        stats['revived'] += 1

    # 3. Updates — refresh aggregates on matched objects, set staleness if any.
    for u in proposals.updates:
        patch = _aggregate_to_update_dict(u.aggregates, now)
        if u.staleness_reason is not None:
            patch['last_data_change_at'] = now
            patch['staleness_reason'] = u.staleness_reason
        # Membership-based match won an inactive object back — reactivate so
        # the UI stops treating it as soft-deleted. Signal the reactivation
        # via staleness so the operator sees it.
        if not u.object.get('is_active'):
            patch['is_active'] = True
            patch['last_data_change_at'] = now
            patch['staleness_reason'] = 'membership_changed'
            stats['reactivated'] += 1
        client.table('objects').update(patch).eq('id', u.object['id']).execute()
        cluster_to_object_db_id[u.cluster_idx] = u.object['id']
        changed_ids.add(u.object['id'])
        stats['updated'] += 1
        if u.staleness_reason:
            stats[f'staleness_{u.staleness_reason}'] += 1

    # 4. Splits — id-reuse for the inheritor; insert new objects for the rest.
    for s in proposals.splits:
        original_centroid = (s.object['ra'], s.object['dec'])
        new_object_db_ids: list[int] = []
        for i, d in enumerate(s.daughters):
            ci = s.daughter_cluster_indices[i]
            if i == s.inheritor_index:
                # Reuse the existing object id; refresh aggregates + centroid.
                patch = _aggregate_to_update_dict(d, now)
                patch['last_data_change_at'] = now
                patch['staleness_reason'] = 'membership_changed'
                client.table('objects').update(patch).eq('id', s.object['id']).execute()
                cluster_to_object_db_id[ci] = s.object['id']
                new_object_db_ids.append(s.object['id'])
            else:
                rec = _aggregate_to_insert_dict(d, field, now)
                resp = client.table('objects').insert(rec).execute()
                new_id = resp.data[0]['id']
                cluster_to_object_db_id[ci] = new_id
                new_object_db_ids.append(new_id)
        # Photometry rows attached to the original object: move to whichever
        # daughter is closest in centroid.
        _migrate_split_photometry(
            client, s.object['id'], original_centroid,
            [(nid, (d.ra, d.dec)) for nid, d in zip(new_object_db_ids, s.daughters)],
        )
        changed_ids.update(new_object_db_ids)
        stats['split'] += 1
        stats['inserted'] += len(s.daughters) - 1

    # 5. Merges — id-reuse for the survivor; absorb losers' associated state.
    for m in proposals.merges:
        # Update survivor's centroid + aggregates + staleness.
        patch = _aggregate_to_update_dict(m.aggregates, now)
        patch['last_data_change_at'] = now
        patch['staleness_reason'] = 'membership_changed'
        client.table('objects').update(patch).eq('id', m.survivor['id']).execute()
        cluster_to_object_db_id[m.cluster_idx] = m.survivor['id']
        changed_ids.add(m.survivor['id'])
        # Absorb state from losers, then soft-delete them.
        for loser in m.losers:
            _absorb_loser_state(client, loser_id=loser['id'], survivor_id=m.survivor['id'])
            client.table('objects').update({
                'is_active': False,
                'last_data_change_at': now,
                'staleness_reason': 'membership_changed',
                'updated_at': now,
            }).eq('id', loser['id']).execute()
        stats['merged'] += 1
        stats['soft_deleted'] += len(m.losers)

    # 6. Orphans — soft-delete (associated state stays attached).
    for o in proposals.orphans:
        client.table('objects').update({
            'is_active': False,
            'last_data_change_at': now,
            'staleness_reason': 'membership_changed',
            'updated_at': now,
        }).eq('id', o.object['id']).execute()
        stats['soft_deleted'] += 1

    # 7. Set target FKs in bulk.
    pairs: list[dict] = []
    for proposal_list in (proposals.updates, proposals.inserts, proposals.revivals):
        for p in proposal_list:
            agg = p.aggregates
            ci = p.cluster_idx
            for tid in agg.member_target_db_ids:
                pairs.append({'target_id': tid, 'object_id': cluster_to_object_db_id[ci]})
    for s in proposals.splits:
        for i, ci in enumerate(s.daughter_cluster_indices):
            agg = s.daughters[i]
            for tid in agg.member_target_db_ids:
                pairs.append({'target_id': tid, 'object_id': cluster_to_object_db_id[ci]})
    for m in proposals.merges:
        for tid in m.aggregates.member_target_db_ids:
            pairs.append({'target_id': tid, 'object_id': cluster_to_object_db_id[m.cluster_idx]})
    if pairs:
        for i in range(0, len(pairs), BATCH_SIZE):
            chunk = pairs[i:i + BATCH_SIZE]
            client.rpc('bulk_set_target_object_fks', {
                'p_pairs': chunk,
                'p_updated_at': now,
            }).execute()
        stats['target_fks_set'] += len(pairs)

    return dict(stats), changed_ids


# ---------------------------------------------------------------------------
# Apply helpers (db side)
# ---------------------------------------------------------------------------


def _aggregate_to_insert_dict(agg: ClusterAggregates, field: str, now: str) -> dict:
    return {
        'object_id': agg.object_id,
        'field': field,
        'ra': agg.ra,
        'dec': agg.dec,
        'n_targets': agg.n_targets,
        'n_spectra': agg.n_spectra,
        'programs': agg.programs,
        'gratings': agg.gratings,
        'observations': agg.observations,
        'max_snr': agg.max_snr,
        'max_exposure_time': agg.max_exposure_time,
        'updated_at': now,
        # New objects start with no inspection state. is_active and version
        # get their column defaults (true, 1).
    }


def _aggregate_to_update_dict(agg: ClusterAggregates, now: str) -> dict:
    return {
        'object_id': agg.object_id,
        'ra': agg.ra,
        'dec': agg.dec,
        'n_targets': agg.n_targets,
        'n_spectra': agg.n_spectra,
        'programs': agg.programs,
        'gratings': agg.gratings,
        'observations': agg.observations,
        'max_snr': agg.max_snr,
        'max_exposure_time': agg.max_exposure_time,
        'updated_at': now,
    }


def _insert_objects(client: Client, records: list[dict]) -> list[int]:
    """Batch-insert objects, return ids in input order."""
    ids: list[int] = []
    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        # `.insert(...).execute()` returns rows in input order under
        # PostgREST; use object_id roundtrip to be safe.
        resp = client.table('objects').insert(batch).execute()
        # Map by object_id to handle any reordering.
        by_oid = {row['object_id']: row['id'] for row in resp.data}
        ids.extend(by_oid[r['object_id']] for r in batch)
    return ids


def _absorb_loser_state(client: Client, *, loser_id: int, survivor_id: int) -> None:
    """Migrate comments, list memberships, and photometry from loser → survivor.

    - Comments: re-point comments.object_id → survivor (concatenation).
    - List membership: union into survivor (deduped by list_id).
    - Photometry: re-point object_photometry.object_id → survivor (no dedup).
    """
    # Comments
    client.table('comments').update(
        {'object_id': survivor_id}
    ).eq('object_id', loser_id).execute()

    # List members — fetch survivor's existing list_ids first to dedup.
    survivor_lists_resp = (
        client.table('object_list_members')
        .select('list_id')
        .eq('object_id', survivor_id)
        .execute()
    )
    survivor_list_ids = {r['list_id'] for r in survivor_lists_resp.data or []}
    loser_lists_resp = (
        client.table('object_list_members')
        .select('id, list_id')
        .eq('object_id', loser_id)
        .execute()
    )
    to_move: list[int] = []
    to_delete: list[int] = []
    for r in loser_lists_resp.data or []:
        if r['list_id'] in survivor_list_ids:
            to_delete.append(r['id'])
        else:
            to_move.append(r['id'])
    if to_move:
        for i in range(0, len(to_move), BATCH_SIZE):
            chunk = to_move[i:i + BATCH_SIZE]
            client.table('object_list_members').update(
                {'object_id': survivor_id}
            ).in_('id', chunk).execute()
    if to_delete:
        for i in range(0, len(to_delete), BATCH_SIZE):
            chunk = to_delete[i:i + BATCH_SIZE]
            client.table('object_list_members').delete().in_('id', chunk).execute()

    # Photometry — re-point all rows; downstream UI may surface multiple.
    client.table('object_photometry').update(
        {'object_id': survivor_id}
    ).eq('object_id', loser_id).execute()


def _migrate_split_photometry(
    client: Client,
    original_object_id: int,
    original_centroid: tuple[float, float],
    daughters: list[tuple[int, tuple[float, float]]],
) -> None:
    """For each photometry row attached to the pre-split object, move it to
    whichever daughter centroid it's closest to."""
    resp = (
        client.table('object_photometry')
        .select('id, ra, dec')
        .eq('object_id', original_object_id)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        return
    daughter_ids = np.array([d[0] for d in daughters])
    daughter_ras = np.array([d[1][0] for d in daughters])
    daughter_decs = np.array([d[1][1] for d in daughters])
    for row in rows:
        ra = row['ra'] if row['ra'] is not None else original_centroid[0]
        dec = row['dec'] if row['dec'] is not None else original_centroid[1]
        cos_dec = np.cos(np.radians(dec))
        d2 = ((daughter_ras - ra) * cos_dec) ** 2 + (daughter_decs - dec) ** 2
        target = int(daughter_ids[int(np.argmin(d2))])
        if target != original_object_id:
            client.table('object_photometry').update(
                {'object_id': target}
            ).eq('id', row['id']).execute()


# ---------------------------------------------------------------------------
# Compute objects.redshift_auto from member spectra
# ---------------------------------------------------------------------------


def compute_object_redshift_auto(client: Client, field: str) -> int:
    """Set objects.redshift_auto from best member spectrum under grating priority.

    Wraps the SQL function of the same name. Priority tiers: PRISM > medium
    (G140M/G235M/G395M) > high-res (G140H/G235H/G395H); tiebreak on longest
    exposure_time. Objects whose members have no spectra with redshift_auto
    are nulled out.
    """
    resp = client.rpc('compute_object_redshift_auto', {'p_field': field}).execute()
    return int(resp.data) if resp.data is not None else 0


# ---------------------------------------------------------------------------
# Operator-driven split / merge (post-hoc resolution of ambiguous topologies)
# ---------------------------------------------------------------------------


def resolve_object_ref(client: Client, ref: str) -> dict:
    """Look up an object by either integer DB id or IAU ``object_id``.

    Returns the full row. Raises ``click.ClickException`` on miss / ambiguity.
    """
    fields = (
        'id, object_id, field, ra, dec, is_active, '
        'redshift_quality, last_inspected_at, max_snr, '
        'redshift_inspected, redshift_auto, last_inspected_by, '
        'last_data_change_at, staleness_reason, version'
    )
    q = client.table('objects').select(fields)
    try:
        db_id = int(ref)
        resp = q.eq('id', db_id).execute()
    except ValueError:
        resp = q.eq('object_id', ref).execute()
    rows = resp.data or []
    if not rows:
        raise click.ClickException(f"No object found for ref '{ref}'.")
    if len(rows) > 1:
        raise click.ClickException(
            f"Object ref '{ref}' is ambiguous ({len(rows)} matches). "
            "Use the integer DB id instead."
        )
    return rows[0]


def _fetch_targets_for_object(client: Client, object_db_id: int) -> list[dict]:
    """Return all currently-linked targets for an object."""
    fields = 'id, target_id, ra, dec, program_slug, observation, field'
    rows: list[dict] = []
    offset = 0
    page_size = 500
    while True:
        resp = (
            client.table('targets').select(fields)
            .eq('object_id', object_db_id)
            .order('id')
            .range(offset, offset + page_size - 1)
            .execute()
        )
        chunk = resp.data or []
        rows.extend(chunk)
        if len(chunk) < page_size:
            break
        offset += page_size
    return rows


def _aggregates_from_targets(
    targets_subset: list[dict], spectra_map: dict[str, list[dict]],
) -> ClusterAggregates:
    """Recompute ClusterAggregates for an explicit subset of targets.

    Shared shape with ``build_cluster_aggregates`` but driven by concrete
    target rows (not FoF groups).
    """
    if not targets_subset:
        raise ValueError("Cannot aggregate an empty target set.")
    ra_centroid = float(np.mean([t['ra'] for t in targets_subset]))
    dec_centroid = float(np.mean([t['dec'] for t in targets_subset]))
    all_gratings: set[str] = set()
    all_snr: list[float] = []
    all_exp: list[float] = []
    n_spectra = 0
    for t in targets_subset:
        for s in spectra_map.get(t['target_id'], []):
            n_spectra += 1
            if s.get('grating'):
                all_gratings.add(s['grating'])
            if s.get('signal_to_noise') is not None:
                all_snr.append(float(s['signal_to_noise']))
            if s.get('exposure_time') is not None:
                all_exp.append(float(s['exposure_time']))
    return ClusterAggregates(
        object_id=generate_iau_name(ra_centroid, dec_centroid),
        ra=ra_centroid,
        dec=dec_centroid,
        n_targets=len(targets_subset),
        n_spectra=n_spectra,
        programs=sorted({t['program_slug'] for t in targets_subset}),
        gratings=sorted(all_gratings),
        observations=sorted({t['observation'] for t in targets_subset}),
        max_snr=max(all_snr) if all_snr else None,
        max_exposure_time=max(all_exp) if all_exp else None,
        member_target_db_ids=[t['id'] for t in targets_subset],
        member_target_ids={t['target_id'] for t in targets_subset},
    )


def _disambiguate_iau_against_field(
    client: Client, field: str, candidate: str,
) -> str:
    """Suffix ``_1``, ``_2``, ... onto the candidate IAU name until it's
    unique within the field."""
    resp = (
        client.table('objects').select('object_id')
        .eq('field', field)
        .ilike('object_id', f"{candidate}%")
        .execute()
    )
    taken = {r['object_id'] for r in resp.data or []}
    if candidate not in taken:
        return candidate
    i = 1
    while f"{candidate}_{i}" in taken:
        i += 1
    return f"{candidate}_{i}"


def split_object(
    client: Client,
    object_ref: str,
    move_target_ids: list[str],
    *,
    dry_run: bool = False,
    yes: bool = False,
) -> dict[str, int]:
    """Split ``object_ref`` into two objects: the moved target set and the
    remainder. The remainder keeps the original DB id (and thus all
    inspection state, comments, list memberships, and photometry).

    The new object starts with a fresh row, a coordinate-derived IAU name,
    and no inspection state. Photometry attached to the original object is
    re-linked by spatial proximity to the closer of the two new centroids.
    """
    obj = resolve_object_ref(client, object_ref)
    if not obj.get('is_active'):
        raise click.ClickException(
            f"Object {obj['object_id']} (id={obj['id']}) is inactive. "
            "Reactivate it via reconcile before splitting."
        )

    targets = _fetch_targets_for_object(client, obj['id'])
    target_by_tid = {t['target_id']: t for t in targets}

    missing = [tid for tid in move_target_ids if tid not in target_by_tid]
    if missing:
        raise click.ClickException(
            f"Target(s) not linked to {obj['object_id']}: {', '.join(missing)}"
        )
    if len(move_target_ids) == 0:
        raise click.ClickException("Nothing to move (--move is empty).")
    if len(move_target_ids) >= len(targets):
        raise click.ClickException(
            "Cannot split off every target — the remainder would be empty."
        )

    move_set = {tid for tid in move_target_ids}
    moved_targets = [t for t in targets if t['target_id'] in move_set]
    keep_targets = [t for t in targets if t['target_id'] not in move_set]

    target_ids_str = [t['target_id'] for t in targets]
    spectra_map = fetch_spectra_metadata(client, target_ids_str)

    moved_agg = _aggregates_from_targets(moved_targets, spectra_map)
    keep_agg = _aggregates_from_targets(keep_targets, spectra_map)
    moved_agg.object_id = _disambiguate_iau_against_field(
        client, obj['field'], moved_agg.object_id,
    )

    print(f"  Splitting {obj['object_id']} (id={obj['id']}):")
    print(f"    Keep ({len(keep_targets)} targets) → remains id={obj['id']}, "
          f"new name {keep_agg.object_id}")
    print(f"    Move ({len(moved_targets)} targets) → new object "
          f"{moved_agg.object_id}")

    if dry_run:
        print("  Dry run — no changes.")
        return {'kept': len(keep_targets), 'moved': len(moved_targets)}
    if not yes and sys.stdin.isatty():
        click.confirm("Apply split?", abort=True)

    now = datetime.now(timezone.utc).isoformat()

    # 1) Insert the new object for moved targets.
    new_rec = _aggregate_to_insert_dict(moved_agg, obj['field'], now)
    resp = client.table('objects').insert(new_rec).execute()
    new_id = resp.data[0]['id']

    # 2) Update original with its new aggregates (kept subset).
    patch = _aggregate_to_update_dict(keep_agg, now)
    patch['last_data_change_at'] = now
    patch['staleness_reason'] = 'membership_changed'
    client.table('objects').update(patch).eq('id', obj['id']).execute()

    # 3) Re-point moved target FKs to the new object.
    pairs = [{'target_id': t['id'], 'object_id': new_id} for t in moved_targets]
    for i in range(0, len(pairs), BATCH_SIZE):
        chunk = pairs[i:i + BATCH_SIZE]
        client.rpc('bulk_set_target_object_fks', {
            'p_pairs': chunk, 'p_updated_at': now,
        }).execute()

    # 4) Re-link photometry by proximity to the closer centroid.
    _migrate_split_photometry(
        client, obj['id'], (obj['ra'], obj['dec']),
        [(obj['id'], (keep_agg.ra, keep_agg.dec)),
         (new_id, (moved_agg.ra, moved_agg.dec))],
    )

    # 5) Recompute objects.redshift_auto for the field.
    compute_object_redshift_auto(client, obj['field'])

    print(f"  Done. new_id={new_id}")
    return {'kept': len(keep_targets), 'moved': len(moved_targets),
            'new_object_db_id': new_id}


def merge_objects(
    client: Client,
    survivor_ref: str,
    source_refs: list[str],
    *,
    dry_run: bool = False,
    yes: bool = False,
) -> dict[str, int]:
    """Merge each ``source_refs`` object into ``survivor_ref``.

    The survivor keeps its DB id (and inspection state). Each source's
    comments, list memberships, and photometry are absorbed via
    ``_absorb_loser_state`` and its targets re-pointed; the source is
    then soft-deleted.
    """
    survivor = resolve_object_ref(client, survivor_ref)
    if not survivor.get('is_active'):
        raise click.ClickException(
            f"Survivor {survivor['object_id']} (id={survivor['id']}) is "
            "inactive. Reactivate it via reconcile before merging into it."
        )
    if not source_refs:
        raise click.ClickException("No --from sources given.")

    sources = [resolve_object_ref(client, r) for r in source_refs]
    for s in sources:
        if s['id'] == survivor['id']:
            raise click.ClickException(
                f"--from {s['object_id']} is the same as --into; "
                "cannot merge an object into itself."
            )
        if s['field'] != survivor['field']:
            raise click.ClickException(
                f"Cross-field merge rejected: {s['object_id']} is in "
                f"{s['field']} but survivor is in {survivor['field']}."
            )

    # Recompute combined aggregates.
    all_object_db_ids = [survivor['id']] + [s['id'] for s in sources]
    targets: list[dict] = []
    for oid in all_object_db_ids:
        targets.extend(_fetch_targets_for_object(client, oid))
    target_ids_str = [t['target_id'] for t in targets]
    spectra_map = fetch_spectra_metadata(client, target_ids_str)
    combined_agg = _aggregates_from_targets(targets, spectra_map)
    # Keep the survivor's existing object_id to avoid renaming URLs.
    combined_agg.object_id = survivor['object_id']

    print(f"  Merging into survivor {survivor['object_id']} (id={survivor['id']}):")
    for s in sources:
        print(f"    + {s['object_id']} (id={s['id']}, q={s.get('redshift_quality')})")
    print(f"    Combined: {combined_agg.n_targets} targets, "
          f"{combined_agg.n_spectra} spectra, centroid "
          f"({combined_agg.ra:.6f}, {combined_agg.dec:.6f})")

    if dry_run:
        print("  Dry run — no changes.")
        return {'survivor_id': survivor['id'], 'absorbed': len(sources)}
    if not yes and sys.stdin.isatty():
        click.confirm("Apply merge?", abort=True)

    now = datetime.now(timezone.utc).isoformat()

    # 1) Update survivor with combined aggregates + staleness.
    patch = _aggregate_to_update_dict(combined_agg, now)
    patch['last_data_change_at'] = now
    patch['staleness_reason'] = 'membership_changed'
    client.table('objects').update(patch).eq('id', survivor['id']).execute()

    # 2) For each source: absorb state, re-point targets, soft-delete.
    for src in sources:
        _absorb_loser_state(client, loser_id=src['id'], survivor_id=survivor['id'])
        src_targets = _fetch_targets_for_object(client, src['id'])
        if src_targets:
            pairs = [{'target_id': t['id'], 'object_id': survivor['id']}
                     for t in src_targets]
            for i in range(0, len(pairs), BATCH_SIZE):
                chunk = pairs[i:i + BATCH_SIZE]
                client.rpc('bulk_set_target_object_fks', {
                    'p_pairs': chunk, 'p_updated_at': now,
                }).execute()
        client.table('objects').update({
            'is_active': False,
            'last_data_change_at': now,
            'staleness_reason': 'membership_changed',
            'updated_at': now,
        }).eq('id', src['id']).execute()

    # 3) Recompute objects.redshift_auto for the field.
    compute_object_redshift_auto(client, survivor['field'])

    print(f"  Done. survivor_id={survivor['id']}")
    return {'survivor_id': survivor['id'], 'absorbed': len(sources)}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_summary(field: str, proposals: Proposals, stats: dict[str, int] | None) -> None:
    n_clusters = (
        proposals.unchanged
        + len(proposals.updates) + len(proposals.inserts) + len(proposals.revivals)
        + sum(len(s.daughters) for s in proposals.splits)
        + len(proposals.merges)
    )
    print()
    print(f"  Reconciliation summary ({field}):")
    print(f"    Clusters:        {n_clusters}")
    print(f"    Unchanged:       {proposals.unchanged}")
    print(f"    Updates:         {len(proposals.updates)}")
    print(f"    Inserts:         {len(proposals.inserts)}")
    print(f"    Revivals:        {len(proposals.revivals)}")
    print(f"    Splits:          {len(proposals.splits)}")
    print(f"    Merges:          {len(proposals.merges)}")
    print(f"    Orphans:         {len(proposals.orphans)}")
    if proposals.complex_overlaps:
        print(f"    Overlaps:        {len(proposals.complex_overlaps)} (split+merge — operator must resolve)")
    if stats:
        for k in (
            'staleness_new_target',
            'staleness_membership_changed',
            'staleness_reprocessed',
        ):
            if stats.get(k):
                print(f"    {k}: {stats[k]}")


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


def reconcile_field_objects(
    client: Client,
    field: str,
    *,
    radius: float = DEFAULT_FOF_RADIUS_ARCSEC,
    dry_run: bool = False,
    yes: bool = False,
    abort_on_split_merge: bool = False,
    changed_hashes: set[tuple[str, str]] | None = None,
) -> tuple[int, dict[str, int], set[int]]:
    """Incrementally reconcile the objects table for a field.

    Args:
        client: Supabase client (service role).
        field: Field name.
        radius: FoF clustering radius in arcseconds.
        dry_run: Print plan without writing.
        yes: Skip interactive split/merge confirmation prompts.
        changed_hashes: Set of (target_id, grating) pairs whose spectrum
            file_hash changed in this deploy. Used to set
            staleness_reason='reprocessed'. Pass None or empty set for
            standalone reconcile (no upsert ran).

    Returns:
        (n_clusters, stats_dict, changed_object_db_ids). The third element is
        the union of object DB ids touched (inserted, revived, updated, split,
        merged-survivor) — useful for downstream consumers like photometry
        deploy that only need to re-process changed objects. Empty on dry_run
        and on no-targets early returns.
    """
    if changed_hashes is None:
        changed_hashes = set()

    print(f"  Fetching targets for field '{field}'...")
    targets = fetch_field_targets(client, field)
    print(f"    {len(targets)} targets")

    print(f"  Fetching existing objects + membership...")
    existing_objects, members_by_obj, _ = fetch_existing_objects(client, field)
    print(f"    {len(existing_objects)} existing objects ({sum(1 for o in existing_objects if o['is_active'])} active)")

    if not targets:
        if dry_run:
            print("  Dry run: would soft-delete all objects.")
            return 0, {}, set()
        if existing_objects:
            print(f"  No targets remain — soft-deleting {len(existing_objects)} object(s).")
            now = datetime.now(timezone.utc).isoformat()
            for obj in existing_objects:
                if obj.get('is_active'):
                    client.table('objects').update({
                        'is_active': False,
                        'last_data_change_at': now,
                        'staleness_reason': 'membership_changed',
                        'updated_at': now,
                    }).eq('id', obj['id']).execute()
        return 0, {'soft_deleted': sum(1 for o in existing_objects if o.get('is_active'))}, set()

    print(f"  Fetching spectra metadata...")
    target_ids_str = [t['target_id'] for t in targets]
    spectra_map = fetch_spectra_metadata(client, target_ids_str)
    n_spectra = sum(len(v) for v in spectra_map.values())
    print(f"    {n_spectra} spectra for {len(spectra_map)} targets")

    print(f"  Clustering with radius={radius}\"...")
    groups = cluster_targets(targets, radius)
    print(f"    {len(groups)} clusters")

    proposals = classify(
        groups=groups,
        targets=targets,
        existing_objects=existing_objects,
        members_by_obj=members_by_obj,
        spectra_map=spectra_map,
        changed_hashes=changed_hashes,
    )

    print_summary(field, proposals, stats=None)

    if dry_run:
        confirm_proposals(proposals, yes=True, dry_run=True)
        return len(groups), {}, set()

    confirm_proposals(
        proposals, yes=yes, dry_run=False,
        abort_on_changes=abort_on_split_merge,
    )

    print(f"  Applying...")
    stats, changed_ids = apply_proposals(client, field, proposals)

    print(f"  Computing object redshift_auto from member spectra...")
    n_recomputed = compute_object_redshift_auto(client, field)
    stats['redshift_auto_set'] = n_recomputed
    print(f"    Updated {n_recomputed} objects")

    return len(groups), stats, changed_ids
