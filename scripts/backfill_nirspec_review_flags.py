#!/usr/bin/env python3
"""
One-time backfill: seed ``nirspec_source_review`` (the cloud home of the NIRSpec
stuck-shutter and background-override lists) from the per-deploy snapshots.

Until the lists moved onto the config plane (``campfire config push / pull``), the
only cloud copy of most hand-made stuck-shutter lists was the snapshot each deploy
stored in ``deployments.stuck_shutters``. This copies, per observation, the entries
of its LATEST deployment snapshot (the newest deployment that recorded the list;
an intermediates-only deploy records none) into ``nirspec_source_review`` wherever that table
has no stuck entry for the (exposure_root, source_id) yet, so a ``campfire config
pull`` on any machine gets the list the last deploy used.

Rules
-----
* **The review table wins.** An entry it already holds (set in the portal, or pushed
  by a deploy or a ``config push``) is never changed, whether the snapshot agrees or
  not; the report counts both cases.
* **Insert only, through the guarded write** (``nirspec_review_apply``): an entry that
  appears between this script's read and its write is refused, not overwritten.
* **Tags.** Snapshots taken before deploys recorded tags carry none, so their entries
  are stored with ``stuck_source`` NULL: provenance unknown. Unknown reads as ``hand``
  on every machine that pulls it (the conservative reading: the detector never
  replaces it), and a machine whose local file holds the same shutters under ``# auto``
  keeps its tag and sends it up on its next ``config push``. Deploys made after this
  change record the tags (``stuck_shutter_sources``) and their overrides
  (``bkg_overrides``); both are used when present.
* **Read-only unless ``--apply``.** The default prints the per-observation report and
  writes nothing. It is idempotent and resumable: a second run finds nothing to do.
* It touches no local file and no machine's sync state. Afterwards run
  ``campfire config pull`` where the lists are wanted; local entries are merged
  entry by entry, never overwritten.

The report's ``earlier`` column counts entries that an EARLIER snapshot of the same
observation holds but the latest does not (and the cloud lacks): they are not
backfilled (the latest deploy is the record of what was last used), but a non-zero
count is worth a look, since a list that shrank between deploys may have lost hand
entries. ``--list-earlier`` prints them.

Auth follows ``campfire deploy``: your ``campfire login`` session by default (you
must be an admin), or ``--service-role`` / ``--local``.

Usage:
    python scripts/backfill_nirspec_review_flags.py                    # report only
    python scripts/backfill_nirspec_review_flags.py --obs capers_uds_p3
    python scripts/backfill_nirspec_review_flags.py --apply
"""

from __future__ import annotations

import argparse
import sys

from campfire.deploy import nirspec_flags as nf
from campfire.deploy.config import load_config
from campfire.deploy.supabase import get_supabase_client

PAGE = 200   # deployment rows carry two jsonb snapshots each


def fetch_deployments(sb, observations: list[str] | None) -> dict[str, list[dict]]:
    """``{observation: [deployments, newest first]}`` for every NIRSpec deployment."""
    cols = 'id, observation, deployed_at, status, stuck_shutters, stuck_shutter_sources, bkg_overrides'
    by_obs: dict[str, list[dict]] = {}
    last = 0
    while True:
        q = (sb.table('deployments').select(cols).not_.is_('observation', 'null')
             .gt('id', last).order('id').limit(PAGE))
        if observations:
            q = q.in_('observation', observations)
        page = q.execute().data or []
        for row in page:
            by_obs.setdefault(row['observation'], []).append(row)
        if len(page) < PAGE:
            break
        last = page[-1]['id']
    for rows in by_obs.values():
        rows.sort(key=lambda r: (r.get('deployed_at') or '', r['id']), reverse=True)
    return by_obs


def plan_observation(deployments: list[dict], cloud: dict) -> dict:
    """The backfill of one observation from its newest snapshot.

    "Newest" is the newest deployment that RECORDED the list: an intermediates-only
    (rate / exposure) deploy inserts a deployment row with no snapshot at all, and
    must not hide the full deploy before it. Each channel looks for its own newest
    snapshot (overrides are only recorded by deploys made after this change)."""
    plan = {'deployment': None, 'changes': [], 'bad': [], 'earlier': [],
            'stuck': {'snapshot': 0, 'insert': 0, 'same': 0, 'differ': 0},
            'bkg': {'snapshot': 0, 'insert': 0, 'same': 0, 'differ': 0}}
    snaps = {'stuck': {}, 'bkg': {}}
    used = None
    for channel, column in (('stuck', 'stuck_shutters'), ('bkg', 'bkg_overrides')):
        source = next((d for d in deployments if d.get(column) is not None), None)
        if source is None:
            continue
        if channel == 'stuck':
            used = source
        entries, bad = nf.snapshot_entries(source.get(column), source.get('stuck_shutter_sources'),
                                           channel=channel)
        snaps[channel] = entries
        plan['bad'] += [f"{channel} {b}" for b in bad]
        changes, stats = nf.plan_snapshot_backfill(channel, entries, cloud[channel])
        plan['changes'] += changes
        plan[channel] = {'snapshot': len(entries), **stats}
    plan['deployment'] = used
    # Stuck entries only an earlier snapshot holds (informational; never written).
    seen = set(snaps['stuck']) | {k for k, rec in cloud['stuck'].items() if rec['value'] is not None}
    for dep in deployments:
        if dep is used:
            continue
        entries, _ = nf.snapshot_entries(dep.get('stuck_shutters'), dep.get('stuck_shutter_sources'))
        for key, entry in sorted(entries.items()):
            if key not in seen:
                seen.add(key)
                plan['earlier'].append((key, entry, dep))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true',
                    help='write the missing entries (default: report only)')
    ap.add_argument('--obs', action='append', help='only this observation (repeatable)')
    ap.add_argument('--list-earlier', action='store_true',
                    help='print the entries only an earlier snapshot holds')
    ap.add_argument('--config', default=None, help='path to deploy config TOML')
    ap.add_argument('--local', action='store_true', help='use local Supabase')
    ap.add_argument('--service-role', action='store_true', help='service-role auth')
    args = ap.parse_args()

    config = load_config(args.config, local=args.local, service_role=args.service_role)
    sb = get_supabase_client(config)
    if not nf.can_sync(config, sb):
        print("This session is not an admin: nirspec_source_review cannot be read or "
              "written (run `campfire login` as an admin, or use --service-role).")
        return 1

    deployments = fetch_deployments(sb, args.obs)
    for missing in sorted(set(args.obs or []) - set(deployments)):
        print(f"! {missing}: no deployment record")
    rows = nf.fetch_cloud_rows(sb, sorted(deployments))

    header = (f"{'observation':<28} {'deploy':>7} {'deployed':<10} "
              f"{'snapshot':>8} {'in cloud':>8} {'differ':>6} {'INSERT':>6} {'earlier':>7}")
    print(header)
    print('-' * len(header))
    plans = {}
    totals = {'snapshot': 0, 'same': 0, 'differ': 0, 'insert': 0, 'earlier': 0}
    for obs in sorted(deployments):
        try:
            plan = plan_observation(deployments[obs], nf.cloud_entries(rows.get(obs, [])))
        except nf.FlagsError as e:
            print(f"{obs:<28} ! skipped: {e}")
            continue
        plans[obs] = plan
        dep = plan['deployment'] or {'id': '-'}
        n = {k: plan['stuck'][k] + plan['bkg'][k] for k in ('snapshot', 'same', 'differ', 'insert')}
        n['earlier'] = len(plan['earlier'])
        for k in totals:
            totals[k] += n[k]
        if not (n['snapshot'] or n['earlier']):
            continue    # this observation never had a list
        print(f"{obs:<28} {dep['id']:>7} {(dep.get('deployed_at') or '')[:10]:<10} "
              f"{n['snapshot']:>8} {n['same']:>8} {n['differ']:>6} {n['insert']:>6} {n['earlier']:>7}"
              + (f"   ({plan['bkg']['snapshot']} bkg)" if plan['bkg']['snapshot'] else ''))
        for bad in plan['bad']:
            print(f"    ! unreadable snapshot entry, skipped: {bad}")
        if args.list_earlier:
            for (root, sid), entry, old in plan['earlier']:
                print(f"    earlier: {root} {sid} = {entry['shutters']} "
                      f"(deployment {old['id']}, {(old.get('deployed_at') or '')[:10]})")
    print('-' * len(header))
    print(f"{'total':<28} {'':>7} {'':<10} {totals['snapshot']:>8} {totals['same']:>8} "
          f"{totals['differ']:>6} {totals['insert']:>6} {totals['earlier']:>7}")
    print("\nsnapshot = entries in the latest deployment snapshot; in cloud = already in "
          "nirspec_source_review with the same value;\ndiffer = the review table holds another "
          "value (kept); INSERT = missing there, to be added; earlier = only in an older snapshot.")

    if not args.apply:
        print(f"\nReport only. Re-run with --apply to insert {totals['insert']} "
              f"entr{'y' if totals['insert'] == 1 else 'ies'}.")
        return 0

    applied = refused = 0
    for obs, plan in plans.items():
        if not plan['changes']:
            continue
        result = nf.apply_changes(sb, obs, plan['changes'])
        applied += result['applied']
        refused += len(result['refused'])
        for r in result['refused']:
            print(f"  ! {obs} {r['channel']} {r['exposure_root']} {r['source_id']}: an entry "
                  f"appeared while writing; left as it is")
    print(f"\nInserted {applied} entr{'y' if applied == 1 else 'ies'}"
          + (f"; {refused} left alone (changed while writing)." if refused else "."))
    return 0


if __name__ == '__main__':
    sys.exit(main())
