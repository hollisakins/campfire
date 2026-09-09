"""Inspected-redshift pull: portal objects → ``reference/nirspec/<obs>/redshifts.toml``.

The cloud→local half of the emission-line loop
(docs/design-emission-line-fitting.md §3). Inspection is recorded on the
portal at the *object* level (``objects.redshift`` — the generated
``COALESCE(redshift_inspected, redshift_auto)`` that is NULL for quality 1 —
and ``objects.redshift_quality``); the pipeline consumes it per *target*
(``<obs>_<source_id>``), so this pull walks ``targets`` for the observation,
joins each to its object, and writes one TOML table per target through the
pipeline's own writer (``redshift_reference.write_redshifts``), which is also
its reader, so the two sides cannot drift.

Any logged-in user with access to the program can pull (the query runs under
RLS on ``targets`` / ``objects``); it is not admin-only like the mask and flag
pulls, because reading a redshift needs no more than reading the catalog.
The file is regenerated in full on every pull (the DB is authoritative; edit
redshifts on the web) and is never registered in ``storage_objects``.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from campfire.deploy.nircam_masks import _utcnow_iso
from campfire.deploy.supabase import get_supabase_client

_BATCH = 200


def _ref_dir(obs) -> Path:
    from campfire_layout import Scope, reference_dir
    from campfire.deploy.nircam import _resolve_campfire_root
    return reference_dir('nirspec', Scope(obs=obs), root=_resolve_campfire_root())


def fetch_target_redshifts(client, obs: str) -> list[dict]:
    """``[{target_id, object_id, redshift, quality, version, inspected_at}]`` for one observation.

    Targets with no object row (never reconciled) are returned with
    ``quality = 0`` and no redshift so the pipeline reports them as
    uninspected rather than silently missing.
    """
    resp = (client.table('targets')
            .select('target_id, object_id')
            .eq('observation', obs)
            .execute())
    targets = resp.data or []
    object_ids = sorted({t['object_id'] for t in targets if t.get('object_id') is not None})

    objects: dict[int, dict] = {}
    for i in range(0, len(object_ids), _BATCH):
        chunk = object_ids[i:i + _BATCH]
        r = (client.table('objects')
             .select('id, object_id, redshift, redshift_quality, version, last_inspected_at')
             .in_('id', chunk)
             .execute())
        for row in r.data or []:
            objects[row['id']] = row

    out = []
    for t in targets:
        o = objects.get(t.get('object_id'))
        if o is None:
            out.append(dict(target_id=t['target_id'], object_id=None, redshift=None,
                            quality=0, version=None, inspected_at=None))
            continue
        z = o.get('redshift')
        out.append(dict(
            target_id=t['target_id'],
            object_id=o.get('object_id'),
            redshift=float(z) if z is not None else None,
            quality=int(o.get('redshift_quality') or 0),
            version=int(o['version']) if o.get('version') is not None else None,
            inspected_at=o.get('last_inspected_at'),
        ))
    return out


def summarize_redshifts(rows) -> dict:
    """Counts by quality label, for the pull's report and tests."""
    labels = {0: 'uninspected', 1: 'impossible', 2: 'tentative', 3: 'probable', 4: 'secure'}
    c = Counter(labels.get(r['quality'], 'other') for r in rows)
    c['total'] = len(rows)
    c['usable'] = sum(1 for r in rows if r['redshift'] is not None and r['quality'] >= 2)
    return dict(c)


def pull_redshifts(obs, config, dry_run=False, generated_by='campfire pull'):
    """Regenerate ``reference/nirspec/<obs>/redshifts.toml`` from the portal.

    Clean full-overwrite (the DB is authoritative), atomic write via the
    pipeline's ``write_redshifts``. Returns the summary counts.
    """
    from campfire.deploy import require_pipeline
    require_pipeline("Pulling inspected redshifts (pipeline TOML writer)")
    from campfire_pipeline.nirspec.redshift_reference import (
        REDSHIFTS_FILENAME, RedshiftEntry, write_redshifts,
    )

    out_file = _ref_dir(obs) / REDSHIFTS_FILENAME
    client = get_supabase_client(config)
    rows = fetch_target_redshifts(client, obs)
    stats = summarize_redshifts(rows)

    print(f"Observation: {obs}")
    print(f"Targets: {stats['total']} — "
          f"{stats.get('secure', 0)} secure, {stats.get('probable', 0)} probable, "
          f"{stats.get('tentative', 0)} tentative, {stats.get('impossible', 0)} impossible, "
          f"{stats.get('uninspected', 0)} uninspected")

    if not rows:
        print(f"No targets for observation={obs}; nothing to write.")
        return stats
    if dry_run:
        print(f"\nDry run — would write {out_file}")
        return stats

    entries = [RedshiftEntry(target_id=r['target_id'], redshift=r['redshift'], quality=r['quality'],
                             object_id=r['object_id'], version=r['version'],
                             inspected_at=r['inspected_at']) for r in rows]
    write_redshifts(entries, str(out_file), obs, generated_at=_utcnow_iso(), generated_by=generated_by)
    print(f"\nWrote {out_file}")
    return stats
