"""Data-release tooling: readiness (`campfire dr status`), freeze and purge.

P1 of the data-release plan (docs/design-data-releases-impl-plan.md §3) for the
readiness report -- one SECURITY DEFINER RPC, ``dr_readiness()`` (admin or
service role), fetched and rendered here -- and P4a (§6) for the freeze:
``dr_enqueue_freeze`` starts it (pg_cron on production, inline where pg_cron is
absent), the CLI polls ``dr_releases`` until it leaves ``freezing``, and
``dr_purge_release`` deletes an unpublished tag.
Nothing here depends on the deploy extra — the CLI resolves the Supabase client
the same way ``campfire pull`` does and hands it in.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def fetch_readiness(client) -> dict:
    """Call ``dr_readiness()`` and return the report dict.

    Raises whatever the client raises (a non-admin gets the RPC's
    ``Access denied`` error; an older database without the function gets a
    PostgREST 404) — the CLI turns both into a one-line message.
    """
    resp = client.rpc('dr_readiness', {}).execute()
    data = resp.data
    if not isinstance(data, dict):
        raise RuntimeError(f"dr_readiness returned an unexpected payload: {data!r}")
    return data


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def _n(value: Any, absent: str = 'n/a') -> str:
    """Format a count; ``None`` (a column that does not exist yet) → *absent*."""
    if value is None:
        return absent
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _when(value: Any) -> str:
    """Short UTC timestamp for a jsonb timestamptz string."""
    if not value:
        return '-'
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return str(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime('%Y-%m-%d %H:%M UTC')


def _table(rows: list[list[str]], header: list[str], *, right: set[int] = frozenset()) -> list[str]:
    """Plain fixed-width table; columns in *right* are right-aligned."""
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list[str]) -> str:
        return '  '.join(
            (c.rjust(widths[i]) if i in right else c.ljust(widths[i]))
            for i, c in enumerate(cells)
        ).rstrip()

    return ['  ' + fmt(header), '  ' + fmt(['-' * w for w in widths])] + ['  ' + fmt(r) for r in rows]


def versions_sorted(report: dict) -> list[tuple[str, int, int]]:
    """(cfpipe_version, spectra, observations) sorted by spectra desc, then name."""
    out = []
    for version, counts in (report.get('spectra_by_version') or {}).items():
        counts = counts or {}
        out.append((str(version), int(counts.get('spectra') or 0), int(counts.get('observations') or 0)))
    out.sort(key=lambda t: (-t[1], t[0]))
    return out


def format_readiness(report: dict) -> str:
    """Render the ``dr_readiness()`` payload as a readable report."""
    lines: list[str] = []
    lines.append(f"Data-release readiness  (generated {_when(report.get('generated_at'))})")
    lines.append('')

    pub = report.get('published') or {}
    lines.append(
        f"Published: {_n(pub.get('spectra'))} spectra · {_n(pub.get('observations'))} observations · "
        f"{_n(pub.get('programs'))} programs · {_n(pub.get('objects'))} objects")

    versions = versions_sorted(report)
    lines.append('')
    lines.append(f"Published spectra by cfpipe_version ({len(versions)} version string(s)):")
    if versions:
        lines.extend(_table(
            [[v, f"{s:,}", f"{o:,}"] for v, s, o in versions],
            ['cfpipe_version', 'spectra', 'observations'], right={1, 2}))
    else:
        lines.append('  (none)')

    drafts = report.get('drafts') or {}
    revoked = report.get('revoked') or {}
    lines.append('')
    lines.append(
        f"Drafts:  {_n(drafts.get('spectra'))} spectra · {_n(drafts.get('nircam_images'))} nircam_images · "
        f"{_n(drafts.get('deployments'))} deployments")
    lines.append(
        f"Revoked: {_n(revoked.get('spectra'))} spectra · {_n(revoked.get('deployments'))} deployments")

    leases = report.get('leases') or []
    lines.append('')
    lines.append(f"Deploy leases: {len(leases)}" + ('' if leases else '  (no deploy in progress)'))
    if leases:
        rows = []
        for lease in leases:
            who = lease.get('in_progress_by_username') or lease.get('in_progress_by') or 'service-role'
            rows.append([
                f"{lease.get('scope_type')}:{lease.get('scope_key')}",
                str(who), str(lease.get('in_progress_host') or '-'),
                _when(lease.get('in_progress_since')), _when(lease.get('heartbeat_at')),
                'STALE' if lease.get('stale') else 'live',
            ])
        lines.extend(_table(rows, ['scope', 'by', 'host', 'since', 'heartbeat', 'state']))

    lines.extend(format_releases(report))

    redrafted = report.get('redrafted_nircam_fields') or []
    lines.append('')
    lines.append(f"Re-drafted NIRCam fields: {len(redrafted)}" + ('' if redrafted else '  (none)'))
    if redrafted:
        lines.extend(_table(
            [[str(f.get('field')), _n(f.get('draft_images')), _n(f.get('published_images')),
              _n(f.get('published_deployments'))] for f in redrafted],
            ['field', 'draft mosaics', 'published mosaics', 'published deployments'], right={1, 2, 3}))

    dangling = report.get('dangling_registry_rows') or {}
    by_obs = dangling.get('by_observation') or {}
    lines.append('')
    lines.append(f"Dangling nirspec_spec registry rows (no spectra row): {_n(dangling.get('count'))}")
    if by_obs:
        top = sorted(by_obs.items(), key=lambda kv: (-int(kv[1] or 0), kv[0]))
        lines.extend(_table([[k, _n(v)] for k, v in top[:15]], ['observation', 'rows'], right={1}))
        if len(top) > 15:
            lines.append(f"  ... and {len(top) - 15} more observation(s)")

    etag = report.get('etag_hash_rows') or {}
    total_etag = sum(int(v or 0) for v in etag.values())
    lines.append('')
    lines.append(f"Provisional etag: hashes (active rows): {total_etag:,}")
    if etag:
        lines.extend(_table(
            [[k, _n(v)] for k, v in sorted(etag.items(), key=lambda kv: (-int(kv[1] or 0), kv[0]))],
            ['product_type', 'rows'], right={1}))

    lf = report.get('line_fits') or {}
    lines.append('')
    lines.append(
        f"Line fits: {_n(lf.get('total'))} total · {_n(lf.get('stale_redshift'))} stale redshift · "
        f"{_n(lf.get('stale_spectrum'))} stale spectrum")
    lines.append(
        f"Objects: {_n(report.get('needs_review'))} need review · "
        f"{_n(report.get('implicit_pins'))} implicit pins (inspected_used_auto)")

    sh = report.get('science_hash_missing') or {}
    absent = 'n/a (column not deployed yet)'
    lines.append(
        f"Science hash missing: spectra {_n(sh.get('spectra'), absent)} · "
        f"storage_objects {_n(sh.get('storage_objects'), absent)}")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# freeze / purge (P4a)
# ---------------------------------------------------------------------------

FREEZE_TERMINAL_STATES = frozenset({'frozen', 'failed', 'exporting', 'exported', 'copying',
                                    'ready', 'published', 'withdrawn'})


def components_from_options(*, nirspec: bool, fields: tuple[str, ...] | list[str] = (),
                            photometry: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Build the components array ``dr_enqueue_freeze`` validates.

    ``fields`` are NIRCam field names (``nircam/<field>``); ``photometry``
    entries are ``<field>/<catalog_name>`` (``photometry/<field>/<catalog>``).
    Order and duplicates are normalised so the same selection always produces
    the same array.
    """
    out: list[str] = []
    if nirspec:
        out.append('nirspec')
    for f in sorted(set(fields)):
        out.append(f'nircam/{f}')
    for p in sorted(set(photometry)):
        if '/' not in p:
            raise ValueError(f"--photometry expects <field>/<catalog_name>, got {p!r}")
        out.append(f'photometry/{p}')
    if not out:
        raise ValueError('nothing to freeze: pass --nirspec, --field and/or --photometry')
    return out


def enqueue_freeze(client, tag: str, components: list[str], *, notes: str | None = None,
                   inline: bool = False) -> dict:
    """Call ``dr_enqueue_freeze``; returns the row summary it answers with."""
    resp = client.rpc('dr_enqueue_freeze', {
        'p_tag': tag, 'p_components': components, 'p_notes': notes, 'p_inline': inline,
    }).execute()
    data = resp.data
    if not isinstance(data, dict):
        raise RuntimeError(f"dr_enqueue_freeze returned an unexpected payload: {data!r}")
    return data


def fetch_release(client, tag: str) -> dict | None:
    """The ``dr_releases`` row for *tag* (admins see every state), or None."""
    resp = (client.table('dr_releases')
            .select('tag,state,components,counts,error,freeze_started_at,frozen_at,'
                    'freeze_duration_ms,published_at,withdrawn_at,notes')
            .eq('tag', tag).limit(1).execute())
    rows = resp.data or []
    return rows[0] if rows else None


def wait_for_freeze(client, tag: str, *, poll_seconds: float = 5.0, timeout_seconds: float = 3600.0,
                    sleep=None, echo=None, clock=None) -> dict:
    """Poll ``dr_releases`` until the freeze leaves ``freezing``.

    Returns the final row. Raises ``TimeoutError`` after *timeout_seconds*;
    the freeze keeps running server-side (pg_cron), so the caller can simply
    poll again later with ``campfire dr status``.
    """
    import time as _time
    sleep = sleep or _time.sleep
    clock = clock or _time.monotonic
    t0 = clock()
    last_state = None
    while True:
        row = fetch_release(client, tag)
        if row is None:
            raise RuntimeError(f"release {tag} disappeared while waiting")
        state = row.get('state')
        if state != 'freezing' and state in FREEZE_TERMINAL_STATES or state == 'draft':
            return row
        if echo is not None and state != last_state:
            echo(f"  {tag}: {state} …")
            last_state = state
        if clock() - t0 > timeout_seconds:
            raise TimeoutError(f"freeze of {tag} still running after {int(timeout_seconds)} s")
        sleep(poll_seconds)


def purge_release(client, tag: str) -> dict:
    """Call ``dr_purge_release``; returns the deleted-row summary."""
    resp = client.rpc('dr_purge_release', {'p_tag': tag}).execute()
    data = resp.data
    if not isinstance(data, dict):
        raise RuntimeError(f"dr_purge_release returned an unexpected payload: {data!r}")
    return data


def format_release(row: dict) -> str:
    """One readable block for a ``dr_releases`` row / enqueue summary."""
    counts = row.get('counts') or {}
    lines = [f"{row.get('tag')}: {row.get('state')}"]
    comps = row.get('components') or []
    if comps:
        lines.append(f"  components: {', '.join(str(c) for c in comps)}")
    if counts:
        label = {'manifest': 'manifest files'}
        parts = [f"{_n(counts.get(k))} {label.get(k, k)}" for k in ('spectra', 'objects', 'line_fits', 'photometry', 'manifest')
                 if counts.get(k) is not None]
        lines.append('  frozen: ' + ' · '.join(parts))
        by_type = counts.get('manifest_by_type') or {}
        if by_type:
            lines.append('  manifest: ' + ' · '.join(f"{_n(v)} {k}" for k, v in sorted(by_type.items())))
    if row.get('state') == 'freezing' and row.get('freeze_started_at'):
        # a freeze that never lands (the pg_cron job did not run) is visible as such
        lines.append(f"  freezing since {_when(row.get('freeze_started_at'))}")
    if row.get('frozen_at'):
        dur = row.get('freeze_duration_ms')
        dur_s = f" in {int(dur) / 1000:.1f} s" if dur is not None else ''
        lines.append(f"  frozen at {_when(row.get('frozen_at'))}{dur_s}")
    if row.get('published_at'):
        lines.append(f"  published at {_when(row.get('published_at'))}")
    if row.get('withdrawn_at'):
        lines.append(f"  withdrawn at {_when(row.get('withdrawn_at'))}")
    if row.get('error'):
        lines.append(f"  error: {row.get('error')}")
    return '\n'.join(lines)


def format_releases(report: dict) -> list[str]:
    """The releases section of ``format_readiness``."""
    releases = report.get('releases')
    if releases is None:
        return []
    lines = ['', f"Data releases: {len(releases)}" + ('' if releases else '  (none frozen yet)')]
    if releases:
        rows = []
        for r in releases:
            counts = r.get('counts') or {}
            rows.append([str(r.get('tag')), str(r.get('state')),
                         _n(counts.get('spectra'), '-'), _n(counts.get('objects'), '-'),
                         _n(counts.get('manifest'), '-'),
                         _when(r.get('frozen_at')), _when(r.get('published_at'))])
        lines.extend(_table(rows, ['tag', 'state', 'spectra', 'objects', 'files', 'frozen', 'published'],
                            right={2, 3, 4}))
        for r in releases:
            if r.get('state') == 'freezing' and r.get('freeze_started_at'):
                lines.append(f"  {r.get('tag')}: freezing since {_when(r.get('freeze_started_at'))}")
            if r.get('error'):
                lines.append(f"  {r.get('tag')}: {r.get('error')}")
    return lines


# ---------------------------------------------------------------------------
# exports (P4b): readers
# ---------------------------------------------------------------------------

def fetch_files(session, tag: str) -> dict:
    """``GET /api/v1/dr/<tag>/files`` for the caller (presigned urls of the
    export files they may read; building them is ``campfire.dr_export``)."""
    resp = session.get(f'/dr/{tag}/files', timeout=60)
    if resp.status_code == 404:
        raise RuntimeError(f"release {tag} is not available to you (unknown, or not published)")
    resp.raise_for_status()
    return resp.json()
