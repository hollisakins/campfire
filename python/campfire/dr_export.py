"""
Data-release exports (P4b; format in docs/dr-format-v1.md).

``campfire dr export --tag T`` turns a frozen release into one gzip JSONL file
per (stream, scope) under ``dr/T/exports/`` on the private OSN data bucket: the
rows come from ``dr_export_rows`` (keyset pages of the frozen tables, scoped so
a program's file never reveals another program), one JSON object per line. The
``dr_exports`` ledger (seeded by ``dr_export_begin``) records each file, so a
run is resumable: it builds only what is not ``ready``.

Built locally, with the OSN credentials ``dr archive`` already needs. Safety:

* **Claims.** A file is claimed (``dr_export_mark`` building) before it is
  built; a fresh claim of another run is skipped, a claim older than ten
  minutes is taken over. The ready / failed marks carry the claim, so a run
  that lost its claim (a laptop that slept mid-build) never overwrites the
  newer state.
* **Per-build keys.** A file is uploaded to ``<scope>.<sha16>.<claim8>.jsonl.gz``
  (``dr_export_content_key``, which records the same key at the ready mark), so
  a stale run can only write its own key, never the file the ledger names.
* **Cleanup.** A run whose claim was lost deletes its own upload once the
  ledger confirms it does not name it. A run that finds every file ready
  deletes the other objects under the prefix the ledger does not name
  (superseded builds). Readers only see a release once it is published, and a
  published release's ledger never changes, so no reader can hold a url to a
  superseded file. Nothing else may be written under ``dr/<tag>/exports/``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Optional

from .transient import retry_transient

EXPORT_STREAMS = ('objects', 'spectra', 'line_fits', 'photometry', 'manifest')
PAGE = 2000


def content_export_key(base_key: str, sha256: str, claim: str) -> str:
    """The key a built file is stored under; equals ``dr_export_content_key``."""
    stem = base_key[:-len('.jsonl.gz')] if base_key.endswith('.jsonl.gz') else base_key
    sha16 = sha256[len('sha256:'):len('sha256:') + 16]
    return f"{stem}.{sha16}.{claim.replace('-', '')[:8]}.jsonl.gz"


@dataclass
class ExportReport:
    tag: str
    state: Optional[str] = None
    built: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    remaining: int = 0
    swept: int = 0

    def as_dict(self) -> dict:
        return {'tag': self.tag, 'state': self.state, 'built': self.built, 'failed': self.failed,
                'skipped': self.skipped, 'remaining': self.remaining, 'swept': self.swept}


def _rpc(client, name: str, params: dict):
    return client.rpc(name, params).execute().data


def _rpc_retrying(client, name: str, params: dict, *, what: str, log=None):
    """An idempotent RPC (a page fetch) retried on a transient transport error (#666)."""
    return retry_transient(lambda: _rpc(client, name, params), what=what, log=log)


def fetch_ledger(client, tag: str) -> list[dict]:
    resp = (client.table('dr_exports')
            .select('tag, stream, partition, storage_key, sha256, size_bytes, rows, state, built_at, error')
            .eq('tag', tag).order('stream').order('partition').execute())
    return list(resp.data or [])


def build_file(client, tag: str, stream: str, scope: str, out_path: str, *, page: int = PAGE,
               log=None) -> tuple[int, str, int]:
    """Page ``dr_export_rows`` into a gzip JSONL file at *out_path*.
    Returns (rows, sha256 of the gzip bytes, size in bytes). A page whose
    connection drops is fetched again (the cursor makes the fetch idempotent)."""
    rows = 0
    after = None
    with open(out_path, 'wb') as raw:
        # mtime=0 and no file name in the header: the same rows give the same bytes
        with gzip.GzipFile(filename='', fileobj=raw, mode='wb', compresslevel=6, mtime=0) as gz:
            while True:
                data = _rpc_retrying(client, 'dr_export_rows', {
                    'p_tag': tag, 'p_stream': stream, 'p_partition': scope,
                    'p_after': after, 'p_limit': page,
                }, what=f'{stream}/{scope} page', log=log) or []
                for r in data:
                    gz.write(json.dumps(r['row_json'], separators=(',', ':'), ensure_ascii=False).encode('utf-8'))
                    gz.write(b'\n')
                rows += len(data)
                if len(data) < page:
                    break
                after = data[-1]['next_cursor']
    h = hashlib.sha256()
    with open(out_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 << 20), b''):
            h.update(chunk)
    return rows, 'sha256:' + h.hexdigest(), os.path.getsize(out_path)


def _named(client, tag: str, key: str) -> bool:
    try:
        return any(r.get('storage_key') == key for r in fetch_ledger(client, tag))
    except Exception:  # noqa: BLE001 - unknown: keep the object
        return True


def _build_one(client, s3, bucket: str, tag: str, entry: dict, report: ExportReport, tmpdir: str,
               *, log=None) -> None:
    stream, scope = entry['stream'], entry['partition']
    try:
        claimed = _rpc(client, 'dr_export_mark', {
            'p_tag': tag, 'p_stream': stream, 'p_partition': scope, 'p_state': 'building'}) or {}
    except Exception as e:  # noqa: BLE001 - another run holds it, or it became ready
        report.skipped.append({'stream': stream, 'scope': scope, 'reason': getattr(e, 'message', None) or str(e)})
        return
    claim = claimed.get('claim')
    if not claim:
        report.skipped.append({'stream': stream, 'scope': scope, 'reason': 'the building mark returned no claim'})
        return
    base_key = claimed.get('base_key') or entry.get('storage_key') or ''
    uploaded = None
    path = os.path.join(tmpdir, 'part.jsonl.gz')
    try:
        rows, sha, size = build_file(client, tag, stream, scope, path, log=log)
        key = content_export_key(base_key, sha, claim)
        def _put():
            # a fresh handle per attempt: a dropped upload leaves the first one half-read
            with open(path, 'rb') as body:
                s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType='application/gzip')
        retry_transient(_put, what=f'{stream}/{scope} upload', log=log)
        uploaded = key
        marked = _rpc(client, 'dr_export_mark', {
            'p_tag': tag, 'p_stream': stream, 'p_partition': scope, 'p_state': 'ready',
            'p_sha256': sha, 'p_size_bytes': size, 'p_rows': rows, 'p_claim': claim}) or {}
        report.state = marked.get('state', report.state)
        report.built.append({'stream': stream, 'scope': scope, 'rows': rows, 'size': size})
    except Exception as e:  # noqa: BLE001 - one file never stops the run
        message = getattr(e, 'message', None) or str(e)
        fail_ok = True
        try:
            fail = _rpc(client, 'dr_export_mark', {
                'p_tag': tag, 'p_stream': stream, 'p_partition': scope, 'p_state': 'failed',
                'p_error': message[:2000], 'p_claim': claim}) or {}
        except Exception:  # noqa: BLE001
            fail, fail_ok = {}, False
        # The ledger never names this build's key (keys are per build, a claim
        # marks ready at most once) -- unless the ready mark committed and only
        # its answer was lost, so the ledger is re-read before deleting.
        if uploaded and fail_ok and not _named(client, tag, uploaded):
            try:
                s3.delete_object(Bucket=bucket, Key=uploaded)
            except Exception:  # noqa: BLE001 - the end-of-run sweep gets it
                pass
        if fail.get('stale_claim'):
            report.skipped.append({'stream': stream, 'scope': scope, 'reason': f'claim lost while building: {message}'})
        else:
            report.failed.append({'stream': stream, 'scope': scope, 'error': message})
    finally:
        if os.path.exists(path):
            os.remove(path)


def sweep_superseded(client, s3, bucket: str, tag: str) -> int:
    """Delete the objects under ``dr/<tag>/exports/`` the ledger does not name.

    Only while every ledger row is ready: an upload whose ready mark is still
    pending always has its row in ``building``, so a complete ledger read after
    the listing proves no run is between its upload and its mark."""
    prefix = f"dr/{tag}/exports/"
    keys = []
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(o['Key'] for o in page.get('Contents', []) or [])
    ledger = fetch_ledger(client, tag)          # AFTER the listing
    if not ledger or any(r.get('state') != 'ready' for r in ledger):
        return 0
    named = {r.get('storage_key') for r in ledger}
    stale = [k for k in keys if k not in named]
    for i in range(0, len(stale), 1000):
        s3.delete_objects(Bucket=bucket, Delete={'Objects': [{'Key': k} for k in stale[i:i + 1000]]})
    return len(stale)


def run_export(client, s3, bucket: str, tag: str, *, log: Callable[[str], None] = print) -> ExportReport:
    """Seed the ledger, build every file that is not ready, report what is left."""
    report = ExportReport(tag=tag)
    begun = _rpc(client, 'dr_export_begin', {'p_tag': tag}) or {}
    report.state = begun.get('state')
    todo = [r for r in fetch_ledger(client, tag) if r.get('state') != 'ready']
    log(f"  {len(todo)} file(s) to build")
    with tempfile.TemporaryDirectory(prefix=f'campfire-dr-{tag}-') as tmpdir:
        for i, entry in enumerate(todo, 1):
            _build_one(client, s3, bucket, tag, entry, report, tmpdir, log=log)
            if i % 10 == 0 or i == len(todo):
                log(f"  {i}/{len(todo)}: {len(report.built)} built, {len(report.failed)} failed, "
                    f"{len(report.skipped)} skipped")
    # a failure another run repaired since is not a failure of the release; the
    # status is read after this, so it reflects the same repairs (Codex on #600)
    if report.failed:
        try:
            ready = {(r['stream'], r['partition']) for r in fetch_ledger(client, tag) if r.get('state') == 'ready'}
        except Exception:  # noqa: BLE001
            ready = set()
        for f in list(report.failed):
            if (f['stream'], f['scope']) in ready:
                report.failed.remove(f)
                report.skipped.append({'stream': f['stream'], 'scope': f['scope'],
                                       'reason': f"built by another run after: {f['error']}"})
    status = _rpc(client, 'dr_export_status', {'p_tag': tag}) or {}
    report.state = status.get('state', report.state)
    report.remaining = int(status.get('remaining') or 0)
    if report.remaining == 0:
        report.swept = sweep_superseded(client, s3, bucket, tag)
    return report


def format_export_report(report: ExportReport) -> str:
    lines = [f"{report.tag}: {report.state} · built {len(report.built)} file(s) · "
             f"{len(report.failed)} failed · {len(report.skipped)} skipped · {report.remaining} remaining"
             + (f" · {report.swept} superseded file(s) deleted" if report.swept else '')]
    for f in report.failed[:20]:
        lines.append(f"  ✗ {f['stream']}/{f['scope']}: {f['error']}")
    for s in report.skipped[:20]:
        lines.append(f"  - {s['stream']}/{s['scope']}: {s['reason']}")
    return '\n'.join(lines)
