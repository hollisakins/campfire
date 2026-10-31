"""NIRSpec review flags: DB <-> reference/nirspec/<obs>/ TOMLs.

The cloud<->local halves of the NIRSpec nods review loop (design §4.3/§7, P7):

- **Stuck shutters** live in ``nirspec_stuck_shutters``, one row per
  ``(observation, exposure_root, physical shutter "q<quadrant>r<row>c<column>")`` with
  ``origin`` manual or web, and locally in the ``[<root>.shutters]`` tables of
  ``reference/nirspec/<obs>/stuck_closed_shutters.toml`` (read and written only through
  ``campfire_pipeline.nirspec.stuck_list``; precedence manual > web > auto, ``stuck =
  false`` vetoes an auto call). The pull makes each root's local ``web`` entries
  exactly the DB's web rows (``stuck_list.write_origin``: a shutter the file already has
  as manual is left alone, manual and auto entries are never touched, comments and layout
  are kept). The push sends the local manual entries (physical ones and legacy
  ``sid = [ordinals]  # hand`` ones mapped through the root's metafile) as ``manual``
  rows; auto calls stay local, the pipeline regenerates them.
- **Background overrides** (``nirspec_source_review.bkg_overrides``, ``{"3":[1]}``) ->
  ``reference/nirspec/<obs>/nodded_background_overrides.toml``, a local-first merge: the
  pull never removes or changes a local entry, it adds DB entries for (root, source)
  keys the file does not have and keeps (and reports) local entries that differ from
  the DB. Local entries reach the DB on deploy. A full overwrite here once wiped every
  hand-set override.

Writes are atomic (tmp + rename). These flags are DB-resident and never registered in
``storage_objects``.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

from campfire.deploy.nircam_masks import _utcnow_iso
from campfire.deploy.supabase import get_supabase_client

STUCK_TABLE = 'nirspec_stuck_shutters'
STUCK_CONFLICT = 'observation,exposure_root,shutter'
STUCK_FILE = 'stuck_closed_shutters.toml'


def _ref_dir(obs):
    """reference/nirspec/<obs>/ — equals observation.reference_dir, so the pull writes
    exactly where the pipeline reads."""
    from campfire_layout import Scope, reference_dir
    from campfire.deploy.nircam import _resolve_campfire_root
    return reference_dir('nirspec', Scope(obs=obs), root=_resolve_campfire_root())


def _obs_dir(obs):
    """products/nirspec/<obs>/ (where the rate files and metafiles are); may not exist."""
    from campfire_layout import Scope, dir_for
    from campfire.deploy.nircam import _resolve_campfire_root
    return dir_for('nirspec_spec', Scope(obs=obs), root=_resolve_campfire_root())


# ---------------------------------------------------------------------------
# Stuck shutters (physical keys, nirspec_stuck_shutters)
# ---------------------------------------------------------------------------

def _stuck_obs(obs, stuck_file=None, obs_dir=None):
    """The minimal observation ``stuck_list.load_obs`` / ``root_metafiles`` read: the
    TOML path, the rate files (each root's first one names its original metafile) and
    the directory a relative metafile name resolves against."""
    from campfire.deploy.discover import discover_rate_files
    obs_dir = Path(obs_dir) if obs_dir is not None else _obs_dir(obs)
    rates = [str(p) for p in discover_rate_files(obs_dir)] if obs_dir.is_dir() else []
    return SimpleNamespace(
        name=obs,
        stuck_closed_shutters_file=str(stuck_file or (_ref_dir(obs) / STUCK_FILE)),
        rate_files=rates, workspace_dir=str(obs_dir))


def fetch_stuck_rows(sb, obs, page=1000):
    """Every ``nirspec_stuck_shutters`` row of ``obs``, paged in the unique key's order
    (an observation holds a few hundred rows at most)."""
    rows, start = [], 0
    while True:
        r = (sb.table(STUCK_TABLE)
             .select('exposure_root, shutter, origin, stuck, note')
             .eq('observation', obs).order('exposure_root').order('shutter')
             .range(start, start + page - 1)
             .execute()).data or []
        rows += r
        if len(r) < page:
            return rows
        start += page


def db_entries(rows, origin):
    """``{root: {(q, r, c): Entry}}`` of the DB rows of one origin."""
    from campfire_pipeline.nirspec import stuck_list
    out = {}
    for row in rows:
        if row['origin'] != origin:
            continue
        out.setdefault(row['exposure_root'], {})[stuck_list.parse_key(row['shutter'])] = stuck_list.Entry(
            origin=origin, stuck=bool(row['stuck']), note=row.get('note') or '')
    return out


def plan_stuck_push(local_manual, db_rows):
    """Rows to upsert so the DB carries every local manual entry. Pure, for unit tests.

    ``local_manual`` is ``{root: {(q, r, c): Entry}}`` (manual entries only);
    ``db_rows`` the observation's current rows. A local entry is sent when the DB has
    no row for the shutter, a web row (manual outranks web), or a manual row with a
    different ``stuck``. Nothing is ever deleted. Returns ``(rows, stale)``:
    ``stale`` lists the DB manual rows no local entry has any more (reported, kept)."""
    from campfire_pipeline.nirspec import stuck_list
    db = {(r['exposure_root'], r['shutter']): r for r in db_rows}
    rows, sent = [], set()
    for root in sorted(local_manual):
        for key, e in sorted(local_manual[root].items()):
            skey = stuck_list.shutter_key(*key)
            sent.add((root, skey))
            cur = db.get((root, skey))
            want = {'exposure_root': root, 'shutter': skey, 'origin': 'manual',
                    'stuck': bool(e.stuck), 'note': e.note or None}
            if cur is not None and cur['origin'] == 'manual' and bool(cur['stuck']) == want['stuck']:
                continue  # a note-only difference is not worth a write
            rows.append(want)
    stale = sorted((r['exposure_root'], r['shutter']) for r in db_rows
                   if r['origin'] == 'manual' and (r['exposure_root'], r['shutter']) not in sent)
    return rows, stale


def local_manual_entries(obs, stuck_file=None, obs_dir=None):
    """The local manual entries ``{root: {(q, r, c): Entry}}``: physical manual entries
    plus legacy ``# hand`` (or untagged) entries mapped through each root's metafile
    (``stuck_list.load_obs``; a legacy entry whose metafile is missing is logged and
    left out)."""
    from campfire_pipeline.nirspec import stuck_list
    resolved = stuck_list.load_obs(_stuck_obs(obs, stuck_file, obs_dir))
    out = {}
    for root, entries in resolved.items():
        manual = {k: e for k, e in entries.items() if e.origin == 'manual'}
        if manual:
            out[root] = manual
    return out


def push_stuck_shutters(sb, obs, dry_run=False, stuck_file=None, obs_dir=None):
    """Push the local manual stuck-shutter entries of ``obs`` to ``nirspec_stuck_shutters``
    (upsert on the physical key, origin ``manual``). Web rows are the web's; auto calls
    stay local. Returns the number of rows sent."""
    stuck_file = Path(stuck_file or (_ref_dir(obs) / STUCK_FILE))
    if not stuck_file.exists():
        return 0
    local = local_manual_entries(obs, stuck_file, obs_dir)
    if not local:
        return 0
    rows, stale = plan_stuck_push(local, fetch_stuck_rows(sb, obs))
    for row in rows:
        row['observation'] = obs
    if rows and not dry_run:
        for i in range(0, len(rows), 500):
            sb.table(STUCK_TABLE).upsert(rows[i:i + 500], on_conflict=STUCK_CONFLICT).execute()
    n_local = sum(len(v) for v in local.values())
    print(f"  {'Would push' if dry_run else 'Pushed'} {len(rows)} manual stuck shutter"
          f"{'' if len(rows) == 1 else 's'} to {STUCK_TABLE} ({n_local - len(rows)} already in the DB)")
    if stale:
        print(f"  {len(stale)} manual row(s) in the DB have no local entry (kept): "
              + ', '.join(f'{r} {k}' for r, k in stale[:10]) + (' ...' if len(stale) > 10 else ''))
    return len(rows)


def apply_web_pull(path, obs, db_web, local_web_roots, *, allow_empty=False):
    """Make each root's local ``web`` entries the DB's (``stuck_list.write_origin``).

    ``db_web`` is ``{root: {(q, r, c): Entry}}``; ``local_web_roots`` the roots whose
    file already has physical web entries (so a root the web has cleared is cleared
    locally too). An empty ``db_web`` while the file has web entries changes nothing
    unless ``allow_empty``: a pull from an empty or wrong database must not wipe local
    config. Returns ``(changed_roots, skipped_empty)``."""
    from campfire_pipeline.nirspec import stuck_list
    if not db_web and local_web_roots and not allow_empty:
        return [], True
    changed = []
    for root in sorted(set(db_web) | set(local_web_roots)):
        if stuck_list.write_origin(str(path), root, db_web.get(root, {}), 'web', obs_name=obs):
            changed.append(root)
    return changed, False


def pull_stuck_shutters(obs, config, dry_run=False, stuck_file=None):
    """Mirror the DB's web stuck-shutter rows into the local TOML's ``web`` entries.

    Manual and auto entries are never touched, and a shutter the file has as manual
    keeps it (manual > web). DB manual rows missing from the file (pushed from another
    machine) are reported, not written: manual entries are edited by hand only.
    """
    from campfire.deploy import require_pipeline
    require_pipeline("Pulling stuck shutters (stuck_list writer)")
    from campfire_pipeline.nirspec import stuck_list

    stuck_file = Path(stuck_file or (_ref_dir(obs) / STUCK_FILE))
    rows = fetch_stuck_rows(get_supabase_client(config), obs)
    db_web = db_entries(rows, 'web')
    physical, legacy = stuck_list.read_file(str(stuck_file))
    local_web_roots = {root for root, ent in physical.items() if any(e.origin == 'web' for e in ent.values())}
    n_legacy_web = sum(1 for ent in legacy.values() for _o, origin in ent.values() if origin == 'web')
    resolved = (stuck_list.load_obs(_stuck_obs(obs, stuck_file)) if legacy
                else stuck_list.resolve(physical, {}, {}))
    missing_manual = [(r['exposure_root'], r['shutter']) for r in rows if r['origin'] == 'manual'
                      and getattr(resolved.get(r['exposure_root'], {}).get(stuck_list.parse_key(r['shutter'])),
                                  'origin', None) != 'manual']

    print(f"Observation: {obs}")
    print(f"DB stuck-shutter rows: {sum(len(v) for v in db_web.values())} web, "
          f"{len(rows) - sum(len(v) for v in db_web.values())} manual")

    target = stuck_file
    tmpdir = None
    if dry_run:
        tmpdir = tempfile.mkdtemp(prefix='stuck_pull_')
        target = Path(tmpdir) / STUCK_FILE
        if stuck_file.exists():
            shutil.copyfile(stuck_file, target)
    else:
        stuck_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        changed, skipped = apply_web_pull(target, obs, db_web, local_web_roots)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    if skipped:
        print(f"  kept the local web entries of {len(local_web_roots)} root(s): the DB has no web rows "
              f"for {obs} (a pull never wipes local config from an empty database)")
    elif changed:
        print(f"  {'Would update' if dry_run else 'Updated'} the web entries of {len(changed)} root(s) "
              f"in {stuck_file}")
    else:
        print(f"  {stuck_file.name}: web entries already match the DB")
    if n_legacy_web:
        print(f"  {n_legacy_web} legacy web entr{'y' if n_legacy_web == 1 else 'ies'} (sid = [ordinals]  # web) "
              f"still apply; remove them once the DB holds their physical rows")
    if missing_manual:
        print(f"  {len(missing_manual)} manual row(s) in the DB are not manual in the local file (not written): "
              + ', '.join(f'{r} {k}' for r, k in missing_manual[:10]) + (' ...' if len(missing_manual) > 10 else ''))


def stuck_snapshot(stuck_file, obs, obs_dir=None):
    """The deploy-time snapshot of the stuck list for ``deployments.stuck_shutters``:
    the resolved physical entries, in the TOML's own shape
    ``{root: {"shutters": {"q2r195c64": {"origin", "stuck", "a"?, "note"?}}}}``, plus,
    verbatim under ``{root: {sid: [ordinals]}}``, the legacy entries of roots whose
    metafile could not be found (so nothing in the file is lost)."""
    from campfire_pipeline.nirspec import stuck_list
    physical, legacy = stuck_list.read_file(str(stuck_file))
    sobs = _stuck_obs(obs, stuck_file, obs_dir)
    metafiles = stuck_list.root_metafiles(sobs, roots=set(legacy)) if legacy else {}
    resolved = stuck_list.resolve(physical, legacy, metafiles)
    out = {}
    for root in sorted(resolved):
        table = {}
        for key in sorted(resolved[root], key=lambda k: (k[2], k[1], k[0])):
            e = resolved[root][key]
            entry = {'origin': e.origin, 'stuck': e.stuck}
            if e.a is not None:
                entry['a'] = e.a
            if e.note:
                entry['note'] = e.note
            table[stuck_list.shutter_key(*key)] = entry
        if table:
            out.setdefault(root, {})[stuck_list.SHUTTERS] = table
    for root, entries in legacy.items():
        if root in metafiles:
            continue
        for sid, (ordinals, _origin) in entries.items():
            out.setdefault(root, {})[str(sid)] = list(ordinals)
    return out


def stuck_snapshot_to_toml(snapshot, obs_name):
    """Render a ``deployments.stuck_shutters`` snapshot as a stuck_closed_shutters.toml
    the pipeline reads: ``[<root>.shutters]`` inline tables for physical entries and
    ``sid = [ordinals]`` lines for legacy ones (pre-physical snapshots are all legacy;
    their provenance tags were not kept, so they read back as manual)."""
    import tomlkit
    from campfire_pipeline.nirspec.stuck_list import HEADER, SHUTTERS, _inline
    doc = tomlkit.parse(HEADER.format(obs=obs_name))
    for root in sorted(snapshot):
        table = snapshot[root] or {}
        rt = tomlkit.table(is_super_table=SHUTTERS in table and len(table) == 1)
        for key in sorted((k for k in table if k != SHUTTERS), key=lambda k: int(k)):
            rt[key] = [int(o) for o in table[key]]
        if SHUTTERS in table:
            st = tomlkit.table()
            for skey, entry in table[SHUTTERS].items():
                st[skey] = _inline(entry)
            rt.add(SHUTTERS, st)
        doc.add(root, rt)
    return tomlkit.dumps(doc)


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
# Push: local TOMLs -> DB (on deploy)
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
    """Push the local review config of ``obs`` on deploy: background overrides to
    ``nirspec_source_review`` (upsert on (observation, exposure_root, source_id),
    touching only that column, so web notes survive) and manual stuck shutters to
    ``nirspec_stuck_shutters`` (``push_stuck_shutters``). Auto-detected stuck shutters
    stay local: the pipeline regenerates them."""
    ref = _ref_dir(obs)
    bkg_local = {(root, sid): nodmap
                 for root, tab in read_bkg_overrides(ref / 'nodded_background_overrides.toml').items()
                 for sid, nodmap in tab.items()}
    n = 0
    if bkg_local:
        rows = plan_review_push(bkg_local, _fetch_review_rows(sb, obs, 'bkg_overrides'), 'bkg_overrides')
        for row in rows:
            row['observation'] = obs
        if rows and not dry_run:
            for i in range(0, len(rows), 500):
                (sb.table('nirspec_source_review')
                 .upsert(rows[i:i + 500], on_conflict='observation,exposure_root,source_id')
                 .execute())
        print(f"  {'Would push' if dry_run else 'Pushed'} {len(rows)} local bkg_overrides "
              f"entr{'y' if len(rows) == 1 else 'ies'} to nirspec_source_review "
              f"({len(bkg_local) - len(rows)} already in the DB)")
        n += len(rows)
    n += push_stuck_shutters(sb, obs, dry_run=dry_run)
    return n
