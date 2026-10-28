"""Archive tooling: collection of unreferenced objects (`campfire archive gc`)
and the delete-then-collect step `campfire dr purge` shares with it.

The content-addressed archive (``archive/<sha256>/<relpath>``, registry rows
with ``root = 'archive'``) holds exactly the files data releases reference
(docs/design-data-releases-impl-plan.md §9a, decisions D-P4-6 / D-P4-7). An
object is referenced by a release when its manifest names it, either as the
byte pass's copy (``dr_manifest.archive_key``) or as a copy the deploy gate
retained for a member (the object's ``archived_from_key`` / ``content_hash``
match a manifest row). An object no release references is collectable; there
is no time window and no per-type rule. A published release is never purged,
so its objects are referenced forever.

Collection is three phases, because nothing can hold a lock across the
bucket delete: ``dr_archive_collect_begin`` claims the rows (``archived ->
collecting``, owned by this run's token) under the freeze lock and the release
row lock, re-checking the references and the release state; per batch,
``dr_archive_collect_verify`` returns the keys this run still owns and the
bytes of exactly those are deleted from the bucket; then
``dr_archive_collect_finish`` deletes those collecting rows and logs one
``collect`` deploy event. Nothing builds on a collecting row (the byte pass
never offers it, the deploy gate's registration refuses it), so a copy that
would vanish under the pending delete is never recorded. A run interrupted
anywhere is finished by the next one: collecting rows are listed again, a
delete of a missing key succeeds, and both phases count what is already done
instead of refusing it. Every request a run makes is retried on a transport
error before it counts (``campfire.transient``, #666): the claim and the finish
are idempotent under the run's token, and a delete re-sent after a lost answer
finds the keys gone.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable, Optional

from . import transient
from .transient import retry_transient

#: A verify is trusted for this long before the delete (the claim it refreshed
#: expires after 10 minutes in SQL; a stall longer than this re-verifies).
VERIFY_TRUST_S = 60.0

#: archive_collectable / dr_collect_archive accept at most 2000 per call.
_PAGE = 1000
#: DeleteObjects takes at most 1000 keys per request.
_DELETE_BATCH = 1000
#: A delete whose connection dropped is re-sent this many times in all, each
#: time through the verify gate again.
_DELETE_ATTEMPTS = 3


def gc_candidates(client, *, page: int = _PAGE) -> list[dict]:
    """Every archive object no release references, by registry id.

    Pages ``archive_collectable`` on ``id`` so a large archive never needs one
    unbounded request. Always the full set: the report's totals must describe
    everything collection would take, so any display cap is applied by
    :func:`gc_report` (``max_rows``), never here.
    """
    out: list[dict] = []
    after = 0
    while True:
        resp = retry_transient(lambda: client.rpc('archive_collectable', {'p_after_id': after, 'p_limit': page}).execute(),
                               what='the collectable page')
        rows = resp.data or []
        out.extend(rows)
        if len(rows) < page:
            break
        after = int(rows[-1]['id'])
    return out


def at_rest_bytes(row: dict) -> int:
    """What the object costs in the bucket (stored size when compressed)."""
    v = row.get('stored_size_bytes')
    if v is None:
        v = row.get('size_bytes') or 0
    return int(v)


def gc_report(rows: list[dict], *, now: Optional[datetime] = None,
              max_rows: Optional[int] = None, dry_run: bool = True) -> dict:
    """The report payload: candidates plus totals, JSON-serialisable.

    ``candidates`` / ``bytes_at_rest`` / ``by_product_type`` always cover every
    row; *max_rows* caps only the per-object ``rows`` list (``rows_shown`` says
    how many made it), so a ``--limit`` never understates what collection
    would take.
    """
    now = now or datetime.now(timezone.utc)
    shown = rows if max_rows is None else rows[:max(0, max_rows)]
    by_type: dict[str, dict[str, int]] = {}
    total = 0
    for r in rows:
        b = at_rest_bytes(r)
        total += b
        t = by_type.setdefault(str(r.get('product_type') or '?'), {'objects': 0, 'bytes': 0})
        t['objects'] += 1
        t['bytes'] += b
    return {
        'generated_at': now.isoformat(),
        'dry_run': dry_run,
        'rule': 'archive objects no data release references',
        'candidates': len(rows),
        'bytes_at_rest': total,
        'by_product_type': by_type,
        'rows_shown': len(shown),
        'rows': [{
            'storage_key': r.get('storage_key'),
            'content_hash': r.get('content_hash'),
            'bytes_at_rest': at_rest_bytes(r),
            'product_type': r.get('product_type'),
            'field': r.get('field'),
            'observation': r.get('observation'),
            'archived_at': r.get('archived_at'),
            'archived_from_key': r.get('archived_from_key'),
        } for r in shown],
    }


def format_gc_report(report: dict, *, max_rows: int = 20) -> str:
    """Readable rendering of :func:`gc_report`."""
    from .sync import format_size

    head = 'Archive gc (dry run)' if report.get('dry_run') else 'Archive gc'
    lines = [
        f"{head}  rule: {report['rule']}",
        f"  Candidates: {report['candidates']:,} object(s), "
        f"{format_size(int(report['bytes_at_rest']))} at rest",
    ]
    if report['by_product_type']:
        lines.append("  By product type:")
        for kind, agg in sorted(report['by_product_type'].items(), key=lambda kv: -kv[1]['bytes']):
            lines.append(f"    {kind:<22} {agg['objects']:>7}  {format_size(int(agg['bytes'])):>10}")
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


class CollectError(RuntimeError):
    """The bucket refused to delete some objects; nothing was marked collected for them."""


def delete_archive_objects(s3, bucket: str, keys: list[str]) -> int:
    """Delete *keys* from *bucket* in DeleteObjects batches; returns how many were asked.

    A missing key is a success (S3 semantics), which is what makes a re-run
    after a crash heal. Any entry the bucket reports under ``Errors`` raises
    :class:`CollectError` before the rows are touched.
    """
    for k in keys:
        if not k.startswith('archive/'):
            raise CollectError(f"refusing to delete a non-archive key: {k}")
    errors: list[str] = []
    for i in range(0, len(keys), _DELETE_BATCH):
        batch = keys[i:i + _DELETE_BATCH]
        resp = s3.delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': k} for k in batch], 'Quiet': True})
        for e in (resp or {}).get('Errors') or []:
            errors.append(f"{e.get('Key')}: {e.get('Code')} {e.get('Message')}".strip())
    if errors:
        raise CollectError("the bucket refused to delete " + ', '.join(errors[:5])
                           + (f" and {len(errors) - 5} more" if len(errors) > 5 else ''))
    return len(keys)


def collect(client, s3, bucket: str, rows: list[dict], *, tag: Optional[str] = None,
            log: Optional[Callable[[str], None]] = None) -> dict:
    """Claim the objects of *rows*, delete them, then drop their registry rows.

    *rows* come from ``archive_collectable`` (``tag`` None) or
    ``dr_purge_archive_plan`` (``tag`` set, which lets the claim ignore that
    tag's own references). Every row must live on OSN (the only archive
    backend); anything else is refused before a byte moves. The claim is
    taken for every key before any byte is deleted, so a key another release
    came to reference since the plan stops the whole run with nothing gone.
    """
    import uuid

    log = log or (lambda _m: None)
    if not rows:
        return {'claimed': 0, 'deleted': 0, 'collected': 0, 'lost': 0, 'bytes': 0}
    foreign = [r['storage_key'] for r in rows if r.get('backend') not in (None, 'osn')]
    if foreign:
        raise CollectError(f"{len(foreign)} archive row(s) are not on OSN (first: {foreign[0]}); "
                           f"the archive has one backend")
    keys = [r['storage_key'] for r in rows]
    # one token for the run: begin stamps it on every claimed row, verify and
    # finish act only on rows that still carry it, so a run taken over after a
    # stall (10 minutes without a refresh) deletes nothing it no longer owns
    claim = str(uuid.uuid4())
    totals = {'claimed': 0, 'deleted': 0, 'collected': 0, 'lost': 0, 'bytes': 0}
    for i in range(0, len(keys), _PAGE):
        page = keys[i:i + _PAGE]
        resp = retry_transient(lambda: client.rpc('dr_archive_collect_begin',
                                                  {'p_keys': page, 'p_tag': tag, 'p_claim': claim}).execute(),
                               what='the collection claim', log=log)
        data = resp.data if isinstance(resp.data, dict) else {}
        totals['claimed'] += int(data.get('claimed') or 0)
    log(f"  claimed {totals['claimed']:,} archive object(s) for collection")
    for i in range(0, len(keys), _DELETE_BATCH):
        batch = keys[i:i + _DELETE_BATCH]
        # the verify is what fences the delete: it is only trusted while the
        # claim it refreshed cannot have expired (10 minutes in SQL), so a
        # process that stalled in between (a laptop asleep) verifies again
        # before it deletes anything. A delete whose connection dropped goes
        # back through the same gate rather than re-sending the stale key
        # list: by the time it is re-sent another run may own the keys.
        attempts = 0
        while True:
            verified_at = time.time()
            resp = retry_transient(lambda: client.rpc('dr_archive_collect_verify',
                                                      {'p_keys': batch, 'p_claim': claim}).execute(),
                                   what='the claim verify', log=log)
            held = list(resp.data or [])
            if time.time() - verified_at > VERIFY_TRUST_S:
                continue
            if not held:
                break
            try:
                deleted = delete_archive_objects(s3, bucket, held)
            except Exception as e:  # noqa: BLE001 - classified below
                attempts += 1
                if attempts >= _DELETE_ATTEMPTS or not transient.is_transient(e):
                    raise
                log(f"  retrying the bucket delete after a transient error "
                    f"({type(e).__name__}: {str(e)[:120]}; attempt {attempts} of {_DELETE_ATTEMPTS})")
                transient._sleep(2.0 ** (attempts - 1))
                continue                     # a missing key deletes fine; the gate decides which
            break
        totals['lost'] += len(batch) - len(held)
        if not held:
            continue
        totals['deleted'] += deleted
        resp = retry_transient(lambda: client.rpc('dr_archive_collect_finish',
                                                  {'p_keys': held, 'p_tag': tag, 'p_claim': claim}).execute(),
                               what='the collection finish', log=log)
        data = resp.data if isinstance(resp.data, dict) else {}
        totals['collected'] += int(data.get('collected') or 0)
        totals['bytes'] += int(data.get('bytes') or 0)
    log(f"  deleted {totals['deleted']:,} archive object(s)")
    log(f"  removed {totals['collected']:,} registry row(s)")
    if totals['lost']:
        log(f"  {totals['lost']:,} object(s) were taken over by another run and left to it")
    return totals
