"""
NIRCam per-exposure exclusion round-trip — Supabase ``review_status`` ↔ combine.

The web admin UI flags exposures as ``review_status='excluded'``; a reducer's
``fields.toml [<field>].skip`` globs are a second, hand-maintained exclusion
source. This module bridges both to the pipeline ``combine`` step (epic #261, N6).

``pull`` (via :func:`pull_exclusions`)
    Materializes every ``review_status='excluded'`` row for a field into
    ``$CAMPFIRE_ROOT/reference/nircam/<field>/exposures.json``. The pipeline
    ``Field.setup_workspace`` reads that file and drops the listed rootnames from
    ``get_exposure_files`` / ``get_uncal_files`` — so they leave both combine and
    outlier detection. The list is rewritten in full every pull, so un-excluding
    an exposure in the portal re-includes it on the next combine (reversible).

``import-skip`` (via :func:`import_skip`)
    Seeds the DB from a reducer's existing ``fields.toml [<field>].skip`` globs:
    every DB exposure whose rootname matches a skip pattern is set
    ``review_status='excluded'``. Additive only — never un-excludes, so the
    portal stays the authority for reversal.
"""

import fnmatch
import json
from datetime import datetime, timezone

EXPOSURES_JSON_VERSION = 1


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def pull_exclusions(field, config, dry_run=False):
    """Materialize ``review_status='excluded'`` rows → ``exposures.json``.

    Always writes (even with zero exclusions) so un-excluding the last exposure
    yields an empty list rather than a stale file that combine keeps honoring.
    Full overwrite, no merge → total reversibility.
    """
    from campfire.deploy.nircam import _resolve_nircam_dirs
    from campfire.deploy.supabase import get_supabase_client

    dirs = _resolve_nircam_dirs(field)
    out_path = dirs['reference'] / 'exposures.json'

    client = get_supabase_client(config)
    resp = (client.table('nircam_exposures')
            .select('filter,filename')
            .eq('field', field)
            .eq('review_status', 'excluded')
            .execute())
    rows = resp.data or []

    by_filter = {}
    for row in rows:
        by_filter.setdefault(row['filter'], set()).add(row['filename'])
    excluded = {f: sorted(v) for f, v in sorted(by_filter.items())}
    n = sum(len(v) for v in excluded.values())

    doc = {
        'version': EXPOSURES_JSON_VERSION,
        'field': field,
        'generated_at': _utcnow_iso(),
        'generated_by': 'campfire deploy nircam pull',
        'excluded': excluded,
    }

    print(f"Field: {field}")
    print(f"Excluded exposures: {n} across {len(excluded)} filter(s)")
    if dry_run:
        print(f"  would write {out_path}")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix('.json.tmp')
    tmp_path.write_text(json.dumps(doc, indent=2) + '\n')
    tmp_path.replace(out_path)
    print(f"  wrote {out_path}")


def import_skip(field, config, dry_run=False):
    """Seed DB ``review_status='excluded'`` from ``fields.toml [<field>].skip``.

    Reuses the pipeline's ``Field`` loader so the glob / brace-expansion
    semantics are identical to what ``combine`` excludes. Matching mirrors
    ``Field.get_exposure_files`` exactly: combine globs ``<pattern>*.fits``, so a
    rootname is excluded iff ``fnmatch(rootname, pattern + '*')``. Additive only
    — never clears an exposure the portal (or a prior run) already un-excluded.
    """
    from campfire.deploy.supabase import get_supabase_client

    # Lazy import: reuse the exact, already-brace-expanded/validated skip globs
    # combine itself consumes (both packages are installed cluster-side).
    from campfire_pipeline.nircam.field import Field
    skip_patterns = list(Field.load(field).skip)
    if not skip_patterns:
        print(f"No skip patterns in fields.toml for field={field}")
        return

    client = get_supabase_client(config)
    resp = (client.table('nircam_exposures')
            .select('filter,filename,review_status')
            .eq('field', field)
            .execute())
    rows = resp.data or []

    to_exclude = []
    for row in rows:
        if row.get('review_status') == 'excluded':
            continue  # additive — already excluded
        name = row['filename']
        if any(fnmatch.fnmatch(name, pat + '*') for pat in skip_patterns):
            to_exclude.append(row)

    print(f"Field: {field}")
    print(f"Skip patterns: {len(skip_patterns)}")
    print(f"Newly matched exposures to exclude: {len(to_exclude)}")
    for row in to_exclude:
        print(f"  {row['filter']}/{row['filename']}")
    if dry_run:
        print("\nDry run — no changes made.")
        return
    if not to_exclude:
        return

    now = _utcnow_iso()
    updates = [{
        'field': field,
        'filter': row['filter'],
        'filename': row['filename'],
        'review_status': 'excluded',
        'updated_at': now,
    } for row in to_exclude]
    for i in range(0, len(updates), 500):
        batch = updates[i:i + 500]
        client.table('nircam_exposures').upsert(
            batch, on_conflict='field,filter,filename').execute()
    print(f"\nExcluded {len(updates)} exposure(s)")
