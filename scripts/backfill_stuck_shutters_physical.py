#!/usr/bin/env python3
"""
One-time backfill: nirspec_source_review.stuck_shutters (per-source legacy ordinal
lists) -> nirspec_stuck_shutters (one row per physical MSA shutter).

The old column holds, per (observation, exposure_root, source_id), the 1-indexed
positions of the stuck shutters among the source's sorted slitlet columns. Each
list is mapped through the root's ORIGINAL metafile, found locally the way stage 2a
finds it (the first rate file of the root under products/nirspec/<obs>/: its
OGMETFL, else MSAMETFL, and MSAMETID), with
``campfire_pipeline.nirspec.stuck_list.legacy_to_physical``.

- Origin: the old rows mix pushed manual entries and web edits with no origin
  recorded. A shutter the local reference/nirspec/<obs>/stuck_closed_shutters.toml
  resolves as manual (stuck_list, physical or legacy ``# hand`` entries) becomes
  'manual', every other one 'web'.
- Two sources sharing a slitlet can name the same physical shutter: one row per
  shutter. When one source lists it stuck and another source whose slitlet holds it
  does not, the conflict is reported and the row is stuck = true.
- A row whose root has no local metafile, whose source the metafile does not hold,
  or whose ordinal is out of range is reported and skipped (never dropped silently).
- Insert-only: a shutter that already has a row is left alone (reported when it
  differs), so a rerun changes nothing and never overwrites a later web edit. The
  old column is not touched.

It also fills ``spectrum_exposures.msa_shutters`` (the source's slitlet in physical
shutter keys, which the web nods review labels its shutters with) for rows deployed
before the column existed, with the same function deploy uses
(``campfire.deploy.spectrum_grid.slitlet_shutter_map``). Only NULL values are
filled; a pair whose root has no local metafile is reported and left NULL.

Dry run by default (prints the plan); ``--apply`` writes. ``--part stuck`` /
``--part shutters`` runs one half only. Needs the products (rate files +
metafiles) and reference TOMLs of the observations locally, and the pipeline
package (``campfire_pipeline``) importable.

Usage:
    CAMPFIRE_DEPLOY_MODE=service-role python scripts/backfill_stuck_shutters_physical.py [--obs NAME ...] [--part stuck|shutters] [--apply]
    # against the local stack:
    CAMPFIRE_LOCAL_SUPABASE_URL=http://127.0.0.1:<port> python scripts/backfill_stuck_shutters_physical.py --local
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

PAGE = 1000
TABLE = 'nirspec_stuck_shutters'


# ---------------------------------------------------------------------------
# The plan (pure: no DB, no files; the mapping is passed in)
# ---------------------------------------------------------------------------

def shutter_key(q, r, c) -> str:
    return f'q{int(q)}r{int(r)}c{int(c)}'


@dataclass
class Plan:
    rows: list[dict] = field(default_factory=list)            # to insert
    skipped: list[tuple] = field(default_factory=list)        # (root, sid, ordinals, reason)
    conflicts: list[tuple] = field(default_factory=list)      # (root, key, stuck_sids, open_sids)
    existing_same: int = 0
    existing_diff: list[tuple] = field(default_factory=list)  # (root, key, db row, planned row)
    empty: int = 0

    def origin_counts(self) -> dict:
        out = {'manual': 0, 'web': 0}
        for r in self.rows:
            out[r['origin']] += 1
        return out


def plan_observation(obs, review_rows, to_physical, coverage, manual_keys, existing_rows) -> Plan:
    """The rows ``obs`` needs in nirspec_stuck_shutters.

    ``review_rows``: ``[{exposure_root, source_id, stuck_shutters}]`` (the old column).
    ``to_physical(root, sid, ordinals)`` -> set of ``(q, r, c)`` the ordinals name;
    ``coverage(root, sid)`` -> set of ``(q, r, c)`` of the source's whole slitlet; both
    raise when the root's metafile or the source is missing or an ordinal is out of
    range. ``manual_keys``: ``{(root, "q..r..c..")}`` the local TOML resolves as manual.
    ``existing_rows``: the observation's current nirspec_stuck_shutters rows.
    """
    plan = Plan()
    stuck_by: dict[tuple, set] = {}
    covered_by: dict[tuple, set] = {}
    for row in sorted(review_rows, key=lambda r: (r['exposure_root'], int(r['source_id']))):
        root, sid = row['exposure_root'], int(row['source_id'])
        ordinals = [int(k) for k in (row.get('stuck_shutters') or [])]
        if not ordinals:
            plan.empty += 1
            continue
        try:
            keys = to_physical(root, sid, ordinals)
            slit = coverage(root, sid)
        except Exception as e:  # noqa: BLE001 - every failure is reported, never dropped
            plan.skipped.append((root, sid, ordinals, f'{type(e).__name__}: {e}'))
            continue
        for k in keys:
            stuck_by.setdefault((root, shutter_key(*k)), set()).add(sid)
        for k in slit:
            covered_by.setdefault((root, shutter_key(*k)), set()).add(sid)

    existing = {(r['exposure_root'], r['shutter']): r for r in existing_rows}
    for (root, key) in sorted(stuck_by):
        sids = sorted(stuck_by[(root, key)])
        open_sids = sorted(covered_by.get((root, key), set()) - set(sids))
        if open_sids:
            plan.conflicts.append((root, key, sids, open_sids))
        want = {'observation': obs, 'exposure_root': root, 'shutter': key,
                'origin': 'manual' if (root, key) in manual_keys else 'web', 'stuck': True,
                'note': 'backfill: source ' + ', '.join(str(s) for s in sids)}
        cur = existing.get((root, key))
        if cur is None:
            plan.rows.append(want)
        elif cur['origin'] == want['origin'] and bool(cur['stuck']) is True:
            plan.existing_same += 1
        else:
            plan.existing_diff.append((root, key, cur, want))
    return plan


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def fetch_review_rows(sb, obs_names) -> dict[str, list[dict]]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = (sb.table('nirspec_source_review')
             .select('id, observation, exposure_root, source_id, stuck_shutters')
             .not_.is_('stuck_shutters', 'null').order('id').range(offset, offset + PAGE - 1))
        if obs_names:
            q = q.in_('observation', list(obs_names))
        page = q.execute().data or []
        rows.extend(page)
        if len(page) < PAGE:
            break
        offset += PAGE
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r['observation'], []).append(r)
    return out


def observation_context(obs, roots):
    """``(to_physical, coverage, manual_keys, problem)`` for one observation from the
    local products + reference TOML. ``problem`` names why nothing can be mapped."""
    from campfire_layout import Scope, dir_for, reference_dir
    from campfire_pipeline.nirspec import stuck_list
    from campfire.deploy.discover import discover_rate_files

    obs_dir = Path(dir_for('nirspec_spec', Scope(obs=obs)))
    ref_file = Path(reference_dir('nirspec', Scope(obs=obs))) / 'stuck_closed_shutters.toml'
    physical, legacy = stuck_list.read_file(str(ref_file))
    need = set(roots) | set(legacy)
    rates = [str(p) for p in discover_rate_files(obs_dir)] if obs_dir.is_dir() else []
    metafiles = stuck_list.root_metafiles(
        SimpleNamespace(rate_files=rates, workspace_dir=str(obs_dir)), roots=need) if rates else {}

    resolved = stuck_list.resolve(physical, legacy, metafiles)
    manual_keys = {(root, stuck_list.shutter_key(*k)) for root, ent in resolved.items()
                   for k, e in ent.items() if e.origin == 'manual'}

    def _mf(root):
        mf = metafiles.get(root)
        if mf is None:
            raise LookupError(f'no metafile for {root} under {obs_dir}')
        return mf

    def to_physical(root, sid, ordinals):
        return stuck_list.legacy_to_physical(_mf(root), sid, ordinals)

    def coverage(root, sid):
        return set(stuck_list.row_keys(_mf(root).rows_for_source(int(sid))))

    problems = []
    if not rates:
        problems.append(f'no rate files under {obs_dir} (nothing can be mapped)')
    if not ref_file.exists():
        problems.append(f'no {ref_file} (every row will be origin web)')
    return to_physical, coverage, manual_keys, '; '.join(problems) or None


def plan_shutter_maps(pairs, shutter_map) -> tuple[dict, list]:
    """``({(root, sid): [keys]}, [(root, sid) without a map])`` for the
    ``spectrum_exposures`` pairs whose ``msa_shutters`` is NULL (pure)."""
    updates, missing = {}, []
    for pair in sorted(pairs):
        keys = shutter_map.get(pair)
        if keys:
            updates[pair] = list(keys)
        else:
            missing.append(pair)
    return updates, missing


def fetch_unmapped_pairs(sb, obs_names) -> dict[str, set]:
    """``{observation: {(exposure_root, source_id)}}`` of spectrum_exposures rows whose
    ``msa_shutters`` is NULL."""
    out: dict[str, set] = {}
    offset = 0
    while True:
        q = (sb.table('spectrum_exposures')
             .select('id, observation, exposure_root, source_id')
             .is_('msa_shutters', 'null').order('id').range(offset, offset + PAGE - 1))
        if obs_names:
            q = q.in_('observation', list(obs_names))
        page = q.execute().data or []
        for r in page:
            out.setdefault(r['observation'], set()).add((r['exposure_root'], int(r['source_id'])))
        if len(page) < PAGE:
            break
        offset += PAGE
    return out


def backfill_shutter_maps(sb, obs_names, apply: bool) -> tuple[int, int]:
    """Fill NULL ``spectrum_exposures.msa_shutters``; returns (pairs filled, pairs left)."""
    from campfire_layout import Scope, dir_for
    from campfire.deploy.spectrum_grid import slitlet_shutter_map

    by_obs = fetch_unmapped_pairs(sb, obs_names)
    print(f"\n{sum(len(v) for v in by_obs.values())} (root, source) pair(s) with msa_shutters NULL "
          f"in {len(by_obs)} observation(s)")
    filled = left = 0
    for obs in sorted(by_obs):
        obs_dir = Path(dir_for('nirspec_spec', Scope(obs=obs)))
        smap = slitlet_shutter_map(obs_dir, by_obs[obs]) if obs_dir.is_dir() else {}
        updates, missing = plan_shutter_maps(by_obs[obs], smap)
        print(f"{obs}: {'fill' if apply else 'would fill'} {len(updates)} pair(s); "
              f"no local metafile / source for {len(missing)}")
        for root, sid in missing:
            print(f"  NO MAP {root} source {sid}")
        if apply:
            for (root, sid), keys in updates.items():
                (sb.table('spectrum_exposures').update({'msa_shutters': keys})
                 .eq('observation', obs).eq('exposure_root', root).eq('source_id', sid)
                 .is_('msa_shutters', 'null').execute())
        filled += len(updates)
        left += len(missing)
    return filled, left


def report(obs, plan: Plan, apply: bool) -> None:
    oc = plan.origin_counts()
    print(f"{obs}: {'insert' if apply else 'would insert'} {len(plan.rows)} "
          f"(manual {oc['manual']}, web {oc['web']}); already present {plan.existing_same}; "
          f"differing {len(plan.existing_diff)}; conflicts {len(plan.conflicts)}; skipped {len(plan.skipped)}")
    for root, sid, ordinals, why in plan.skipped:
        print(f"  SKIPPED {root} source {sid} = {ordinals}: {why}")
    for root, key, sids, open_sids in plan.conflicts:
        print(f"  CONFLICT {root} {key}: stuck for source(s) {sids}, not for {open_sids}; stuck = true")
    for root, key, cur, want in plan.existing_diff:
        print(f"  LEFT ALONE {root} {key}: DB origin={cur['origin']} stuck={cur['stuck']}, "
              f"backfill would be origin={want['origin']} stuck=true")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--obs', action='append', help='only this observation (repeatable)')
    ap.add_argument('--apply', action='store_true', help='write the rows (default: dry run)')
    ap.add_argument('--local', action='store_true', help='the local Supabase stack')
    ap.add_argument('--part', choices=('stuck', 'shutters', 'both'), default='both',
                    help='stuck: the stuck-shutter rows; shutters: spectrum_exposures.msa_shutters')
    args = ap.parse_args()

    from campfire.deploy.config import load_config
    from campfire.deploy.nirspec_flags import fetch_stuck_rows
    from campfire.deploy.supabase import get_supabase_client

    config = load_config(local=args.local, service_role=not args.local)
    sb = get_supabase_client(config)

    if args.part in ('shutters', 'both'):
        filled, left = backfill_shutter_maps(sb, args.obs, args.apply)
        print(f"{'Filled' if args.apply else 'Would fill'} msa_shutters for {filled} pair(s); left NULL {left}")
        if args.part == 'shutters':
            if not args.apply:
                print("(dry run: nothing written; rerun with --apply)")
            return 0

    by_obs = fetch_review_rows(sb, args.obs)
    print(f"\n{sum(len(v) for v in by_obs.values())} nirspec_source_review rows with stuck_shutters "
          f"in {len(by_obs)} observation(s)")

    totals = {'rows': 0, 'manual': 0, 'web': 0, 'same': 0, 'diff': 0, 'conflicts': 0, 'skipped': 0}
    for obs in sorted(by_obs):
        rows = by_obs[obs]
        to_physical, coverage, manual_keys, problem = observation_context(
            obs, {r['exposure_root'] for r in rows})
        if problem:
            print(f"{obs}: {problem}")
        plan = plan_observation(obs, rows, to_physical, coverage, manual_keys, fetch_stuck_rows(sb, obs))
        report(obs, plan, args.apply)
        if args.apply and plan.rows:
            for i in range(0, len(plan.rows), 500):
                (sb.table(TABLE)
                 .upsert(plan.rows[i:i + 500], on_conflict='observation,exposure_root,shutter',
                         ignore_duplicates=True)
                 .execute())
        oc = plan.origin_counts()
        totals['rows'] += len(plan.rows)
        totals['manual'] += oc['manual']
        totals['web'] += oc['web']
        totals['same'] += plan.existing_same
        totals['diff'] += len(plan.existing_diff)
        totals['conflicts'] += len(plan.conflicts)
        totals['skipped'] += len(plan.skipped)

    print(f"\n{'Inserted' if args.apply else 'Would insert'} {totals['rows']} row(s): "
          f"manual {totals['manual']}, web {totals['web']}; already present {totals['same']}; "
          f"differing (left alone) {totals['diff']}; conflicts {totals['conflicts']}; "
          f"skipped source rows {totals['skipped']}")
    if not args.apply:
        print("(dry run: nothing written; rerun with --apply)")
    return 1 if totals['skipped'] else 0


if __name__ == '__main__':
    sys.exit(main())
