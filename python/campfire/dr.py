"""Data-release tooling: the readiness report (`campfire dr status`).

P1 of the data-release plan (docs/design-data-releases-impl-plan.md §3). The
numbers come from one SECURITY DEFINER RPC, ``dr_readiness()`` (admin or
service role); this module fetches it and renders it as a readable report.
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
