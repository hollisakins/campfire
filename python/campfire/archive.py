"""Archive tooling: the mosaic retention dry run (`campfire archive gc --dry-run`).

P2 of the data-release plan (docs/design-data-releases-impl-plan.md §4, "gc").
The content-addressed archive (`archive/<sha256>/<relpath>`, registry rows with
``root = 'archive'``) is write-once; the only thing that ever leaves it is a
NIRCam mosaic past its retention window. NIRSpec finals and sidecars are never
collected (design §4.2), so this module refuses to even list them.

Collection is a status flip, not a delete: a collected row keeps its hash, size
and date (``status = 'collected'``, ``collected_at``) so a manifest resolves to
"existed, collected on <date>". The real collection lands with P4, because it
must skip every object a published DR references (``dr_members``); until then
this module is a dry run only -- it lists what the window would select and how
many bytes at rest that is, and changes nothing.

The window has no agreed number yet (the design says "time-boxed", amending
NIRCam decision D3); ``DEFAULT_RETENTION_DAYS`` is a placeholder the CLI
exposes as ``--older-than`` so the dry run can be read at several windows
before one is written down.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

#: Placeholder until the retention window is decided (plan §12, open).
DEFAULT_RETENTION_DAYS = 365

#: The only product types retention may ever collect (design §4.2).
COLLECTABLE_PRODUCT_TYPES: tuple[str, ...] = ('nircam_mosaic',)

_COLUMNS = ('id, storage_key, content_hash, size_bytes, stored_size_bytes, product_type, '
            'field, filter, observation, archived_from_key, archived_at, archived_by_deployment')
_PAGE = 1000


def gc_candidates(client, *, older_than_days: int = DEFAULT_RETENTION_DAYS,
                  product_types: Iterable[str] = COLLECTABLE_PRODUCT_TYPES,
                  now: Optional[datetime] = None) -> list[dict]:
    """Archive rows the retention window would collect, oldest first.

    ``archived`` mosaic rows (never ``collected``, never NIRSpec) whose
    ``archived_at`` is older than *older_than_days*. Paged on ``id`` so a large
    archive never needs one unbounded request. Always the full set: the
    report's totals must describe everything the window selects, so any
    display cap is applied by :func:`gc_report` (``max_rows``), never here.
    DR membership is NOT checked here (``dr_members`` arrives with P4); the
    report says so.
    """
    types = tuple(product_types)
    bad = [t for t in types if t not in COLLECTABLE_PRODUCT_TYPES]
    if bad:
        raise ValueError(f"retention never collects {', '.join(bad)}; "
                         f"collectable: {', '.join(COLLECTABLE_PRODUCT_TYPES)}")
    if older_than_days < 0:
        raise ValueError('older_than_days must be >= 0')
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=older_than_days)).isoformat()

    out: list[dict] = []
    last_id = 0
    while True:
        q = (client.table('storage_objects').select(_COLUMNS)
             .eq('root', 'archive').eq('status', 'archived')
             .in_('product_type', list(types))
             .lt('archived_at', cutoff)
             .gt('id', last_id)
             .order('id').limit(_PAGE))
        rows = q.execute().data or []
        out.extend(rows)
        if len(rows) < _PAGE:
            break
        last_id = int(rows[-1]['id'])
    out.sort(key=lambda r: (str(r.get('archived_at') or ''), int(r.get('id') or 0)))
    return out


def at_rest_bytes(row: dict) -> int:
    """What the object costs in the bucket (stored size when compressed)."""
    v = row.get('stored_size_bytes')
    if v is None:
        v = row.get('size_bytes') or 0
    return int(v)


def gc_report(rows: list[dict], *, older_than_days: int, now: Optional[datetime] = None,
              max_rows: Optional[int] = None) -> dict:
    """The dry-run payload: candidates plus totals, JSON-serialisable.

    ``candidates`` / ``bytes_at_rest`` / ``by_field`` always cover every row;
    *max_rows* caps only the per-object ``rows`` list (``rows_shown`` says how
    many made it), so a ``--limit`` never understates what the window selects.
    """
    now = now or datetime.now(timezone.utc)
    shown = rows if max_rows is None else rows[:max(0, max_rows)]
    by_field: dict[str, dict[str, int]] = {}
    total = 0
    for r in rows:
        b = at_rest_bytes(r)
        total += b
        f = by_field.setdefault(str(r.get('field') or '?'), {'objects': 0, 'bytes': 0})
        f['objects'] += 1
        f['bytes'] += b
    return {
        'generated_at': now.isoformat(),
        'dry_run': True,
        'older_than_days': older_than_days,
        'product_types': list(COLLECTABLE_PRODUCT_TYPES),
        'dr_membership_checked': False,
        'candidates': len(rows),
        'bytes_at_rest': total,
        'by_field': by_field,
        'rows_shown': len(shown),
        'rows': [{
            'storage_key': r.get('storage_key'),
            'content_hash': r.get('content_hash'),
            'bytes_at_rest': at_rest_bytes(r),
            'field': r.get('field'),
            'filter': r.get('filter'),
            'archived_at': r.get('archived_at'),
            'archived_from_key': r.get('archived_from_key'),
        } for r in shown],
    }


def format_gc_report(report: dict, *, max_rows: int = 20) -> str:
    """Readable rendering of :func:`gc_report`."""
    from .sync import format_size

    lines = [
        f"Archive gc (dry run)  window: mosaics archived more than {report['older_than_days']} day(s) ago",
        "  Nothing is collected by this command. DR membership is not checked yet "
        "(dr_members lands with P4); NIRSpec products are never collected.",
        f"  Candidates: {report['candidates']:,} object(s), "
        f"{format_size(int(report['bytes_at_rest']))} at rest",
    ]
    if report['by_field']:
        lines.append("  By field:")
        for field, agg in sorted(report['by_field'].items(), key=lambda kv: -kv[1]['bytes']):
            lines.append(f"    {field:<20} {agg['objects']:>6}  {format_size(int(agg['bytes'])):>10}")
    rows = report.get('rows') or []
    if rows:
        lines.append("  Oldest first:")
        for r in rows[:max_rows]:
            when = str(r.get('archived_at') or '')[:10]
            lines.append(f"    {when}  {format_size(int(r['bytes_at_rest'])):>10}  {r['storage_key']}")
        hidden = int(report['candidates']) - min(len(rows), max_rows)
        if hidden > 0:
            lines.append(f"    … {hidden} more (totals above include them; --limit N / --json to list more)")
    return '\n'.join(lines)
