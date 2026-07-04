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

- **Background overrides — clean full-overwrite.** There is no auto-writer for the bkg
  TOML (it is hand-authored only), so the pull safely regenerates the whole file from
  the DB.
- **Stuck shutters — authority merge, hand > web > auto.** The stuck TOML is written by
  three sources (hand edits, this pull = "web", and the pipeline auto-detector). Because
  hand and web are both manual identification, the pull *merges* rather than overwrites,
  reading the ``# hand`` / ``# web`` / ``# auto`` provenance tags back out of the existing
  file (``stuck_shutters.load_stuck_shutters_tagged``) so a re-pull preserves hand entries,
  refreshes web entries from the DB (dropping ones the DB no longer flags), and lets auto
  fill gaps. The shared pipeline writer (``write_stuck_shutters_toml``) emits the tags, so
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
        Counts: ``hand``, ``auto``, ``web``, ``hand_protected``, ``web_dropped``.

    Rules (net realized ranking hand > web > auto):
      1. every hand entry is kept, never overwritten;
      2. auto entries are kept unless a hand entry holds the key;
      3. web (DB) overlays auto and prior web, but a hand entry protects its key;
      4. a prior web entry absent from the DB (and not hand-held) is dropped
         (web reversibility — the DB is authoritative for the web tier).
    """
    merged = {}       # (root, sid_str) -> [shutters]
    provenance = {}   # (root, sid_str) -> tag
    stats = {'hand': 0, 'auto': 0, 'web': 0, 'hand_protected': 0, 'web_dropped': 0}

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

    # 4. prior web entries the DB no longer flags — dropped (reversibility)
    for k, (_sh, tag) in existing_tagged.items():
        if tag == 'web' and k not in db_web and k not in hand_keys:
            stats['web_dropped'] += 1

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
          f" {stats['web_dropped']} stale web dropped")

    if dry_run:
        print(f"\nDry run — would write {stuck_file}")
        return

    stuck_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(stuck_file) + '.tmp'
    write_stuck_shutters_toml(data, tmp, obs, provenance=provenance)
    Path(tmp).replace(stuck_file)
    print(f"\nWrote {stuck_file}")


# ---------------------------------------------------------------------------
# Background overrides — clean full-overwrite
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
        '# — regenerated from the DB (nirspec_source_review) on each pull; edit via the web portal.',
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


def pull_bkg_overrides(obs, config, dry_run=False):
    """Regenerate nodded_background_overrides.toml from nirspec_source_review.bkg_overrides.

    Clean full-overwrite (no auto-writer to conflict with); atomic write.
    """
    bkg_file = _ref_dir(obs) / 'nodded_background_overrides.toml'

    client = get_supabase_client(config)
    resp = (client.table('nirspec_source_review')
            .select('exposure_root, source_id, bkg_overrides')
            .eq('observation', obs)
            .not_.is_('bkg_overrides', 'null')
            .execute())
    rows = resp.data or []

    data = {}
    n_sources = 0
    for row in rows:
        overrides = row.get('bkg_overrides') or {}
        if not overrides:
            continue
        root = row['exposure_root']
        sid_str = str(row['source_id'])
        data.setdefault(root, {})[sid_str] = {
            str(nod): list(nods) for nod, nods in overrides.items()
        }
        n_sources += 1

    content = _serialize_bkg_overrides(data, obs)

    print(f"Observation: {obs}")
    print(f"DB bkg-override sources: {n_sources}")

    if dry_run:
        print(f"\nDry run — would write {bkg_file}")
        return

    bkg_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = bkg_file.with_suffix(bkg_file.suffix + '.tmp')
    tmp.write_text(content)
    tmp.replace(bkg_file)
    print(f"\nWrote {bkg_file}")
