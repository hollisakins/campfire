"""
Retain-on-supersede for the direct (boto3) upload path.

The presign route performs this step for login-mode deploys
(``web/lib/server/retain.ts``). Service-role and ``--local`` deploys upload
straight to the bucket with boto3 and never pass through that route, so the
same gate runs here, client side, right before ``upload_files_direct``. The
semantics are the route's, kept deliberately in step:

* only the six retained NIRSpec finals/sidecars (decision D-P2-1);
* skip new keys, true no-op re-uploads and provisional (etag) registry hashes;
* **drift** when the live registry hash moved past the plan, the live object's
  size no longer matches its row, the object is missing, or the conditional
  copy fails its ETag precondition;
* the retained copy is a same-bucket ``CopyObject`` to
  ``archive/<sha256>/<relpath>`` guarded by ``CopySourceIfMatch`` (OSN answers
  412 on a mismatch, measured 2026-10-11), followed by an archive registry row
  (duplicate = someone already registered the same content) and an ``archive``
  deploy event.

Modes (env ``CAMPFIRE_RETAIN_MODE``, default ``log``): ``off`` does nothing,
``log`` prints what would be archived and lets the upload proceed (the shadow
run), ``enforce`` archives first and refuses the upload set on any drift or
copy failure — the one unrecoverable event in the model is a live object
overwritten before its copy exists.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from campfire_layout import LayoutError, archive_key, key_for_parsed, parse_key

RETAINED_PRODUCT_TYPES = frozenset({
    'nirspec_spec', 'spectrum_json', 'spectrum_1d_json',
    'zfit', 'nirspec_lines', 'nirspec_lines_json',
})

_ROW_COLUMNS = (
    'id, storage_key, content_hash, size_bytes, stored_size_bytes, content_type, '
    'product_type, instrument, observation, spectrum_id, program_slug, '
    'deployment_id, cfpipe_version, updated_at'
)
_LOOKUP_CHUNK = 50


class RetainError(RuntimeError):
    """Raised in ``enforce`` mode when a retained copy could not be guaranteed."""


def retain_mode(env: Optional[dict] = None) -> str:
    v = (env if env is not None else os.environ).get('CAMPFIRE_RETAIN_MODE', 'log')
    v = (v or 'log').strip().lower()
    return v if v in ('off', 'enforce') else 'log'


@dataclass(frozen=True)
class RetainDecision:
    key: str
    action: str                       # skip-new | skip-type | skip-unchanged | skip-provisional-hash | drift | retain
    row: Optional[dict] = None
    archive_key: Optional[str] = None
    reason: Optional[str] = None


def _fetch_live_rows(client, canonicals: list[str]) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for i in range(0, len(canonicals), _LOOKUP_CHUNK):
        chunk = canonicals[i:i + _LOOKUP_CHUNK]
        resp = (client.table('storage_objects').select(_ROW_COLUMNS)
                .in_('storage_key', chunk).eq('status', 'active').eq('root', 'live')
                .execute())
        for r in resp.data or []:
            rows[r['storage_key']] = r
    return rows


def plan_retain(tasks: Iterable, client) -> list[RetainDecision]:
    """Decide per upload task whether a retained copy must exist before the PUT.

    ``tasks`` are ``UploadTask``-shaped (``r2_key``; optional
    ``expected_old_hash`` / ``new_hash`` as stamped by ``plan_push``).
    """
    tasks = list(tasks)
    # The key names its product type, so non-retained kinds (exposures, rate
    # files, mosaics: the bulk of a big push) are decided without a lookup.
    canonical: dict[str, Optional[str]] = {}
    kind: dict[str, Optional[str]] = {}
    for t in tasks:
        try:
            pk = parse_key(t.r2_key)
            canonical[t.r2_key] = key_for_parsed(pk)
            kind[t.r2_key] = pk.product_type
        except (LayoutError, ValueError):
            canonical[t.r2_key] = kind[t.r2_key] = None
    wanted = sorted({canonical[t.r2_key] for t in tasks
                     if canonical[t.r2_key] and kind[t.r2_key] in RETAINED_PRODUCT_TYPES})
    rows = _fetch_live_rows(client, wanted) if wanted else {}

    out: list[RetainDecision] = []
    for t in tasks:
        key = t.r2_key
        row = rows.get(canonical.get(key) or '')
        old = getattr(t, 'expected_old_hash', None)
        new = getattr(t, 'new_hash', None)
        if kind.get(key) is not None and kind[key] not in RETAINED_PRODUCT_TYPES:
            out.append(RetainDecision(key, 'skip-type'))
        elif row is None:
            out.append(RetainDecision(key, 'skip-new'))
        elif row['product_type'] not in RETAINED_PRODUCT_TYPES:
            out.append(RetainDecision(key, 'skip-type', row))
        elif new and new == row['content_hash']:
            out.append(RetainDecision(key, 'skip-unchanged', row))
        elif not str(row['content_hash'] or '').startswith('sha256:'):
            out.append(RetainDecision(key, 'skip-provisional-hash', row))
        elif old and old != row['content_hash']:
            out.append(RetainDecision(key, 'drift', row,
                                      reason=f"registry hash {row['content_hash']} != planned {old}"))
        else:
            pk = parse_key(row['storage_key'])
            ak = archive_key(pk.product_type, pk.scope, pk.filename, content_hash=row['content_hash'])
            out.append(RetainDecision(key, 'retain', row, archive_key=ak))
    return out


def summarize(decisions: list[RetainDecision]) -> dict[str, int]:
    c: dict[str, int] = {}
    for d in decisions:
        c[d.action] = c.get(d.action, 0) + 1
    return c


def _status_code(exc: Exception) -> Optional[int]:
    resp = getattr(exc, 'response', None) or {}
    meta = resp.get('ResponseMetadata', {}) if isinstance(resp, dict) else {}
    code = meta.get('HTTPStatusCode')
    if code is None and isinstance(resp, dict):
        err = resp.get('Error', {}).get('Code')
        code = {'404': 404, 'NotFound': 404, '412': 412, 'PreconditionFailed': 412}.get(err)
    return code


@dataclass(frozen=True)
class RetainOutcome:
    key: str
    status: str                       # archived | drift | error
    archive_key: Optional[str] = None
    copied: bool = False
    reason: Optional[str] = None


def execute_retain(decision: RetainDecision, *, s3, bucket: str, client,
                   actor: Optional[str] = None, now: Optional[datetime] = None) -> RetainOutcome:
    """Make one retained copy exist (HEAD live → HEAD archive → conditional copy → row → event)."""
    from campfire.deploy.supabase import log_deploy_event

    row, ak, key = decision.row, decision.archive_key, decision.key
    expected_size = row.get('stored_size_bytes') or row.get('size_bytes')

    try:
        head = s3.head_object(Bucket=bucket, Key=row['storage_key'])
    except Exception as e:  # noqa: BLE001 - botocore ClientError and friends
        if _status_code(e) == 404:
            return RetainOutcome(key, 'drift', reason='live object missing in the bucket (registry row dangling)')
        return RetainOutcome(key, 'error', reason=f'HEAD failed: {e}')
    if head.get('ContentLength') is not None and int(head['ContentLength']) != int(expected_size):
        return RetainOutcome(key, 'drift',
                             reason=f"live object is {head['ContentLength']} bytes, registry says {expected_size}")
    etag = (head.get('ETag') or '').strip('"')

    copied = False
    try:
        s3.head_object(Bucket=bucket, Key=ak)
        exists = True
    except Exception as e:  # noqa: BLE001
        if _status_code(e) != 404:
            return RetainOutcome(key, 'error', reason=f'HEAD archive failed: {e}')
        exists = False
    if not exists:
        try:
            s3.copy_object(Bucket=bucket, Key=ak,
                           CopySource={'Bucket': bucket, 'Key': row['storage_key']},
                           CopySourceIfMatch=etag, MetadataDirective='COPY')
            copied = True
        except Exception as e:  # noqa: BLE001
            if _status_code(e) == 412:
                return RetainOutcome(key, 'drift', reason='live object changed during retain (copy precondition failed)')
            return RetainOutcome(key, 'error', reason=f'copy failed: {e}')

    ts = (now or datetime.now(timezone.utc)).isoformat()
    archive_row = {
        'backend': 'osn', 'bucket': 'data', 'storage_key': ak,
        'root': 'archive', 'status': 'archived',
        'content_hash': row['content_hash'],
        'size_bytes': row.get('size_bytes'), 'stored_size_bytes': row.get('stored_size_bytes'),
        'content_type': row.get('content_type') or 'application/octet-stream',
        'product_type': row['product_type'], 'instrument': row.get('instrument'),
        'observation': row.get('observation'), 'spectrum_id': row.get('spectrum_id'),
        'program_slug': row.get('program_slug'), 'deployment_id': row.get('deployment_id'),
        'cfpipe_version': row.get('cfpipe_version'),
        'archived_from_key': row['storage_key'], 'archived_at': ts,
        'archived_by_deployment': None, 'uploaded_by': actor, 'updated_at': ts,
    }
    try:
        (client.table('storage_objects')
         .upsert(archive_row, on_conflict='backend,bucket,storage_key', ignore_duplicates=True)
         .execute())
    except Exception as e:  # noqa: BLE001
        return RetainOutcome(key, 'error', reason=f'registry insert failed: {e}')

    log_deploy_event(client, action='archive', actor=actor, observation=row.get('observation'),
                     affected_count=1,
                     metadata={'live_key': row['storage_key'], 'archive_key': ak,
                               'content_hash': row['content_hash'], 'copied': copied})
    return RetainOutcome(key, 'archived', archive_key=ak, copied=copied)


def retain_before_direct_upload(tasks: list, *, client, s3, bucket: str,
                                actor: Optional[str] = None,
                                mode: Optional[str] = None,
                                log=print) -> list[RetainOutcome]:
    """The gate the direct upload path runs before overwriting live objects.

    Returns the outcomes (empty in ``off`` / ``log`` modes). Raises
    :class:`RetainError` in ``enforce`` mode when any candidate drifted or
    failed to copy — nothing has been uploaded at that point.
    """
    mode = mode or retain_mode()
    if mode == 'off' or not tasks:
        return []
    decisions = plan_retain(tasks, client)
    to_retain = [d for d in decisions if d.action == 'retain']
    drifted = [d for d in decisions if d.action == 'drift']
    counts = summarize(decisions)
    if mode == 'log':
        for d in to_retain:
            log(f"  [retain:log] would archive {d.row['storage_key']} ({d.row['content_hash']}) -> {d.archive_key}")
        for d in drifted:
            log(f"  [retain:log] drift on {d.key}: {d.reason}")
        if to_retain or drifted:
            log(f"  [retain:log] {counts}")
        return []
    if drifted:
        raise RetainError("retain gate: registry drift on " +
                          ", ".join(f"{d.key} ({d.reason})" for d in drifted[:5]) +
                          (f" and {len(drifted) - 5} more" if len(drifted) > 5 else ""))
    outcomes = [execute_retain(d, s3=s3, bucket=bucket, client=client, actor=actor) for d in to_retain]
    bad = [o for o in outcomes if o.status != 'archived']
    if bad:
        raise RetainError("retain gate: could not archive " +
                          ", ".join(f"{o.key} ({o.status}: {o.reason})" for o in bad[:5]) +
                          (f" and {len(bad) - 5} more" if len(bad) > 5 else ""))
    archived = sum(1 for o in outcomes if o.copied)
    if outcomes:
        log(f"  Retained {len(outcomes)} superseded object(s) in the archive ({archived} copied, "
            f"{len(outcomes) - archived} already present)")
    return outcomes
