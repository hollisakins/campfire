"""NIRSpec flag pull-back: DB nirspec_source_review → reference/nirspec/<obs>/ TOMLs.

The cloud→local half of the NIRSpec nods review loop (design §4.3/§7, P7). Reads the
web-editable ``nirspec_source_review`` table (one row per
``(observation, exposure_root, source_id)``) and materializes its two jsonb flag
channels into the two local TOMLs that pipeline stage 2 already consumes:

- ``stuck_shutters``  ([1,2,3]) → ``reference/nirspec/<obs>/stuck_closed_shutters.toml``
- ``bkg_overrides``   ({"3":[1]}) → ``reference/nirspec/<obs>/nodded_background_overrides.toml``

The P6 jsonb shapes were stored to mirror these TOMLs 1:1, so the DB→TOML mapping is
``toml[exposure_root][str(source_id)] = <jsonb>`` with no transform.

Two behaviors (design §4.3):

- **Background overrides — local-first merge.** The bkg TOML is hand-edited locally as
  well as set on the web, so the pull never removes or changes a local entry: it adds
  DB entries for (root, source) keys the file does not have, and keeps (and reports)
  local entries that differ from the DB. Local entries reach the DB on deploy
  (``annotation_push``). A full overwrite here once wiped every hand-set override.
- **Stuck shutters — authority merge, hand > web > auto.** The stuck TOML is written by
  three sources (hand edits, this pull = "web", and the pipeline auto-detector). Because
  hand and web are both manual identification, the pull *merges* rather than overwrites,
  reading the ``# hand`` / ``# web`` / ``# auto`` provenance tags back out of the existing
  file (``stuck_shutters.load_stuck_shutters_tagged``) so a re-pull preserves hand entries,
  refreshes web entries from the DB (keeping, and reporting, ones the DB no longer flags:
  a pull never deletes local config), and lets auto fill gaps. The shared pipeline writer (``write_stuck_shutters_toml``) emits the tags, so
  the file the pull writes and the file the pipeline auto-detector writes are byte-compatible.

Both writes are atomic (tmp + rename). These flags are DB-resident and never registered
in ``storage_objects``.
"""
from __future__ import annotations

from pathlib import Path

from campfire.deploy.nircam_masks import _utcnow_iso
from campfire.deploy.supabase import get_supabase_client


def _ref_dir(obs):
    """reference/nirspec/<obs>/ — equals observation.reference_dir, so the pull writes
    exactly where the pipeline reads."""
    from campfire_layout import Scope, reference_dir
    from campfire.deploy.nircam import _resolve_campfire_root
    return reference_dir('nirspec', Scope(obs=obs), root=_resolve_campfire_root())


# ---------------------------------------------------------------------------
# Stuck shutters — authority merge (hand > web > auto)
# ---------------------------------------------------------------------------

def merge_pulled_stuck_shutters(existing_tagged, db_web):
    """Resolve the stuck-shutter authority merge for the pull.

    Pure function (no DB / no I/O) so the ranking rules are unit-testable.

    Parameters
    ----------
    existing_tagged : dict
        ``{(root, source_id_str): ([shutters], tag)}`` from
        ``load_stuck_shutters_tagged`` (tag ∈ {'hand','web','auto'}).
    db_web : dict
        ``{(root, source_id_str): [shutters]}`` — the web tier from the DB
        (empty lists already filtered out by the caller).

    Returns
    -------
    data : dict
        ``{root: {source_id_str: [shutters]}}`` ready for ``write_stuck_shutters_toml``.
    provenance : dict
        ``{(root, source_id_str): tag}`` — the tag to emit per surviving entry.
    stats : dict
        Counts: ``hand``, ``auto``, ``web``, ``hand_protected``, ``web_kept``.

    Rules (net realized ranking hand > web > auto):
      1. every hand entry is kept, never overwritten;
      2. auto entries are kept unless a hand entry holds the key;
      3. web (DB) overlays auto and prior web, but a hand entry protects its key;
      4. a prior web entry absent from the DB (and not hand-held) is kept: a pull
         never deletes local config. Clear it locally to remove it.
    """
    merged = {}       # (root, sid_str) -> [shutters]
    provenance = {}   # (root, sid_str) -> tag
    stats = {'hand': 0, 'auto': 0, 'web': 0, 'hand_protected': 0, 'web_kept': 0}

    hand_keys = {k for k, (_sh, tag) in existing_tagged.items() if tag == 'hand'}

    # 1. hand entries — always retained
    for k, (sh, tag) in existing_tagged.items():
        if tag == 'hand':
            merged[k] = sh
            provenance[k] = 'hand'
            stats['hand'] += 1

    # 2. auto entries — retained unless a hand entry holds the key
    for k, (sh, tag) in existing_tagged.items():
        if tag == 'auto' and k not in hand_keys:
            merged[k] = sh
            provenance[k] = 'auto'
            stats['auto'] += 1

    # 3. web (DB) overlay — wins over auto/prior-web, yields to hand
    for k, shutters in db_web.items():
        if k in hand_keys:
            stats['hand_protected'] += 1
            continue
        merged[k] = shutters
        provenance[k] = 'web'
        stats['web'] += 1

    # 4. prior web entries the DB no longer flags — kept (a pull never deletes local config)
    for k, (sh, tag) in existing_tagged.items():
        if tag == 'web' and k not in db_web and k not in hand_keys:
            merged[k] = sh
            provenance[k] = 'web'
            stats['web_kept'] += 1

    data = {}
    for (root, sid_str), shutters in merged.items():
        data.setdefault(root, {})[sid_str] = shutters
    return data, provenance, stats


def pull_stuck_shutters(obs, config, dry_run=False):
    """Merge nirspec_source_review.stuck_shutters (web tier) into the local TOML.

    Authority merge hand > web > auto; atomic write; reuses the pipeline's
    tag-emitting ``write_stuck_shutters_toml`` so the file stays byte-compatible
    with what the auto-detector writes.
    """
    from campfire.deploy import require_pipeline
    require_pipeline("Pulling stuck shutters (byte-compatible TOML writer)")
    from campfire_pipeline.nirspec.stuck_shutters import (
        load_stuck_shutters_tagged, write_stuck_shutters_toml,
    )

    stuck_file = _ref_dir(obs) / 'stuck_closed_shutters.toml'

    client = get_supabase_client(config)
    resp = (client.table('nirspec_source_review')
            .select('exposure_root, source_id, stuck_shutters')
            .eq('observation', obs)
            .not_.is_('stuck_shutters', 'null')
            .execute())
    rows = resp.data or []

    db_web = {}
    n_empty = 0
    for row in rows:
        shutters = row.get('stuck_shutters') or []
        if not shutters:
            n_empty += 1
            continue
        db_web[(row['exposure_root'], str(row['source_id']))] = list(shutters)

    existing_tagged = load_stuck_shutters_tagged(str(stuck_file))
    data, provenance, stats = merge_pulled_stuck_shutters(existing_tagged, db_web)

    print(f"Observation: {obs}")
    print(f"DB web stuck-shutter entries: {len(db_web)} ({n_empty} empty skipped)")
    print(f"Merged: {stats['hand']} hand, {stats['web']} web, {stats['auto']} auto"
          f" — {stats['hand_protected']} web overlays blocked by hand,"
          f" {stats['web_kept']} web entries kept that the DB no longer has")

    if dry_run:
        print(f"\nDry run — would write {stuck_file}")
        return

    stuck_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(stuck_file) + '.tmp'
    write_stuck_shutters_toml(data, tmp, obs, provenance=provenance)
    Path(tmp).replace(stuck_file)
    print(f"\nWrote {stuck_file}")


# ---------------------------------------------------------------------------
# Background overrides — local-first merge
# ---------------------------------------------------------------------------

def _serialize_bkg_overrides(data, obs_name, generated_at=None):
    """Render ``{root: {sid_str: {nod_str: [bkg nods]}}}`` as the nodded-background
    override TOML, using inline tables (``12345 = {3 = [1]}``) that
    ``observation.bkg_overrides``'s ``toml.load`` reads back verbatim.

    An empty bkg list for a nod (``{"3": []}`` → ``3 = []``) is meaningful — it means
    "exclude this nod" (CFP_BKG='excluded:override' in stage2) — and is preserved.
    """
    generated_at = generated_at or _utcnow_iso()
    lines = [
        f'# nodded background overrides for {obs_name}',
        '# format is like the stuck closed shutter list, e.g., there\'s',
        '# a table for each "root" file name, which',
        '# consists of obs/visit/config, e.g. "jw06368001001_03101"',
        '# for each source ID, the background shutters to use for',
        '# each nod should be given as a key-value pair in the table;',
        '# for example:',
        '# [jw06368001001_03101]',
        '#     12345 = {3=[1]}',
        '# (for source 12345, only use nod 1 as background for nod 3)',
        '#',
        '# NOTE: nod numbers are the exposure sequence numbers from the',
        '# FITS filenames (the 3rd underscore-delimited segment), NOT',
        '# sequential indices. If a TACONFIRM exposure is 00001, the',
        '# first science nod will be 2, not 1.',
        '#',
        f'# Generated by campfire deploy nirspec pull-bkg-overrides at {generated_at}',
        '# Local entries are kept on every pull (DB entries are only added) and pushed to the DB on deploy.',
        '',
    ]

    for root in sorted(data.keys()):
        lines.append(f'[{root}]')
        for sid_str in sorted(data[root].keys(), key=int):
            nodmap = data[root][sid_str]
            inner = ', '.join(
                f'{nod} = [{", ".join(str(n) for n in nodmap[nod])}]'
                for nod in sorted(nodmap.keys(), key=int)
            )
            lines.append(f'    {sid_str} = {{{inner}}}')
        lines.append('')

    return '\n'.join(lines) + '\n'


def read_bkg_overrides(path):
    """``{root: {sid_str: {nod_str: [nods]}}}`` from a local override TOML ({} if absent),
    read the way ``observation.bkg_overrides`` reads it."""
    import toml
    path = Path(path)
    if not path.exists():
        return {}
    raw = toml.load(str(path))
    return {str(root): {str(sid): {str(nod): [int(n) for n in nods] for nod, nods in nodmap.items()}
                        for sid, nodmap in table.items()}
            for root, table in raw.items() if isinstance(table, dict)}


def merge_pulled_bkg_overrides(local, db):
    """Local-first merge of background overrides. Pure, for unit tests.

    Every local (root, source) entry is kept as it is; a DB entry is added only where
    the file has none. Returns ``(merged, stats)`` with counts ``local``, ``added``,
    ``same`` and ``conflict`` (local differs from the DB; local kept)."""
    merged = {root: {sid: dict(nm) for sid, nm in tab.items()} for root, tab in local.items()}
    stats = {'local': sum(len(t) for t in local.values()), 'added': 0, 'same': 0, 'conflict': 0}
    conflicts = []
    for root, tab in db.items():
        for sid, nodmap in tab.items():
            mine = local.get(root, {}).get(sid)
            if mine is None:
                merged.setdefault(root, {})[sid] = nodmap
                stats['added'] += 1
            elif mine == nodmap:
                stats['same'] += 1
            else:
                stats['conflict'] += 1
                conflicts.append((root, sid, mine, nodmap))
    stats['conflicts'] = conflicts
    return merged, stats


def pull_bkg_overrides(obs, config, dry_run=False):
    """Merge nirspec_source_review.bkg_overrides into the local override TOML.

    Local-first (``merge_pulled_bkg_overrides``): local entries are never removed or
    changed; the file is rewritten only when the DB adds entries, and the previous
    file is kept alongside as ``.prev``. Atomic write.
    """
    bkg_file = _ref_dir(obs) / 'nodded_background_overrides.toml'

    client = get_supabase_client(config)
    resp = (client.table('nirspec_source_review')
            .select('exposure_root, source_id, bkg_overrides')
            .eq('observation', obs)
            .not_.is_('bkg_overrides', 'null')
            .execute())
    rows = resp.data or []

    db = {}
    for row in rows:
        overrides = row.get('bkg_overrides') or {}
        if not overrides:
            continue
        db.setdefault(row['exposure_root'], {})[str(row['source_id'])] = {
            str(nod): [int(n) for n in nods] for nod, nods in overrides.items()
        }

    try:
        local = read_bkg_overrides(bkg_file)
    except (ValueError, TypeError, AttributeError) as e:
        # Unreadable local file: keep it as it is (local-first) and carry on.
        print(f"Observation: {obs}")
        print(f"  kept local {bkg_file} unchanged: cannot read it ({e}); "
              f"{sum(len(t) for t in db.values())} DB override(s) not merged")
        return
    merged, stats = merge_pulled_bkg_overrides(local, db)

    print(f"Observation: {obs}")
    print(f"Background overrides: {stats['local']} local kept, {stats['added']} added from the DB, "
          f"{stats['conflict']} local entries differ from the DB (local kept)")
    for root, sid, mine, theirs in stats['conflicts']:
        print(f"  kept local {root} {sid} = {mine} (DB has {theirs})")

    if stats['added'] == 0 and bkg_file.exists():
        return
    if dry_run:
        print(f"\nDry run — would write {bkg_file}")
        return

    bkg_file.parent.mkdir(parents=True, exist_ok=True)
    if bkg_file.exists():
        bkg_file.with_suffix(bkg_file.suffix + '.prev').write_text(bkg_file.read_text())
    tmp = bkg_file.with_suffix(bkg_file.suffix + '.tmp')
    tmp.write_text(_serialize_bkg_overrides(merged, obs))
    tmp.replace(bkg_file)
    print(f"\nWrote {bkg_file}")


# ---------------------------------------------------------------------------
# Push: local TOMLs -> nirspec_source_review (on deploy)
# ---------------------------------------------------------------------------

def _fetch_review_rows(sb, obs, column):
    rows, start = [], 0
    while True:
        r = (sb.table('nirspec_source_review')
             .select(f'exposure_root, source_id, {column}')
             .eq('observation', obs)
             .range(start, start + 999)
             .execute()).data or []
        rows += r
        start += 1000
        if len(r) < 1000:
            return {(row['exposure_root'], str(row['source_id'])): row.get(column) for row in rows}


def plan_review_push(local, db, column):
    """Rows to upsert so the DB carries every local entry. Pure, for unit tests.

    ``local`` and ``db`` map ``(root, sid_str)`` to the column value. A local entry is
    pushed when the DB lacks it or holds a different value (local config wins on
    deploy). DB entries with no local counterpart are left alone; a push never
    deletes."""
    out = []
    for (root, sid), value in sorted(local.items()):
        if db.get((root, sid)) != value:
            out.append({'exposure_root': root, 'source_id': int(sid), column: value})
    return out


def push_review_flags(sb, obs, dry_run=False):
    """Push the local background overrides and hand/web stuck shutters of ``obs`` to
    ``nirspec_source_review`` (upsert on (observation, exposure_root, source_id),
    touching only the pushed column, so web notes and the other flag survive).
    Auto-detected stuck shutters stay local: the pipeline regenerates them."""
    from campfire_pipeline.nirspec.stuck_shutters import load_stuck_shutters_tagged

    ref = _ref_dir(obs)
    bkg_local = {(root, sid): nodmap
                 for root, tab in read_bkg_overrides(ref / 'nodded_background_overrides.toml').items()
                 for sid, nodmap in tab.items()}
    stuck_local = {k: list(sh) for k, (sh, tag) in
                   load_stuck_shutters_tagged(str(ref / 'stuck_closed_shutters.toml')).items()
                   if tag in ('hand', 'web')}
    n = 0
    for column, local in (('bkg_overrides', bkg_local), ('stuck_shutters', stuck_local)):
        if not local:
            continue
        rows = plan_review_push(local, _fetch_review_rows(sb, obs, column), column)
        for row in rows:
            row['observation'] = obs
        if rows and not dry_run:
            for i in range(0, len(rows), 500):
                (sb.table('nirspec_source_review')
                 .upsert(rows[i:i + 500], on_conflict='observation,exposure_root,source_id')
                 .execute())
        print(f"  {'Would push' if dry_run else 'Pushed'} {len(rows)} local {column} "
              f"entr{'y' if len(rows) == 1 else 'ies'} to nirspec_source_review "
              f"({len(local) - len(rows)} already in the DB)")
        n += len(rows)
    return n
