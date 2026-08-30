"""
NIRCam per-exposure exclusion round-trip — Supabase ``review_status`` ↔ combine.

The web admin UI flags exposures as ``review_status='excluded'``; this module
bridges that state to the pipeline ``combine`` step (epic #261, N6).

``pull`` (via :func:`pull_exclusions`)
    Materializes every ``review_status='excluded'`` row for a field into
    ``$CAMPFIRE_ROOT/reference/nircam/<field>/exposures.json``. The pipeline
    ``Field.setup_workspace`` reads that file and drops the listed rootnames from
    ``get_exposure_files`` / ``get_uncal_files`` — so they leave both combine and
    outlier detection. The list is rewritten in full every pull, so un-excluding
    an exposure in the portal re-includes it on the next combine (reversible).
"""

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
