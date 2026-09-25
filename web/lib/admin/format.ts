// ---------------------------------------------------------------------------
// Shared admin formatters (2026-08 dashboard redesign).
//
// The admin panel had eight independently copy-pasted byte/date formatters,
// four of which never normalized Postgres timestamp strings ('2026-08-30
// 12:00:00+00', or naive strings with no zone at all) and therefore rendered
// local-shifted dates. This is the one home for all of them; new admin code
// imports from here instead of re-declaring.
// ---------------------------------------------------------------------------

/**
 * Parse a Postgres timestamp string defensively. Handles:
 *   - '2026-08-30T12:00:00+00:00' (ISO, fine as-is)
 *   - '2026-08-30 12:00:00+00'    (space separator, truncated offset)
 *   - '2026-08-30T12:00:00'       (naive — the schema's `timestamp without
 *     time zone` columns store UTC, so treat as UTC, never local)
 */
export function parseTimestamp(ts: string): Date {
  let iso = ts;
  if (iso.includes(' ') && !iso.includes('T')) iso = iso.replace(' ', 'T');
  if (iso.endsWith('+00')) iso = iso + ':00';
  else if (!iso.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(iso)) iso = iso + 'Z';
  return new Date(iso);
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null || Number.isNaN(Number(n))) return '—';
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB', 'PB']) {
    if (Math.abs(v) < 1024 || u === 'PB') return u === 'B' ? `${v} B` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v} B`;
}

/** Absolute timestamp, always including the year: 'Mar 4, 2026, 14:02'. */
export function fmtWhen(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = parseTimestamp(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}

/**
 * Compact relative age: '4m', '3h', '6d', '2mo', '1y'. Pair with the absolute
 * form in a `title` attribute so hover always resolves the ambiguity.
 */
export function fmtAgo(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = parseTimestamp(ts);
  if (isNaN(d.getTime())) return ts;
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  if (s < 86400 * 60) return `${Math.floor(s / 86400)}d`;
  if (s < 86400 * 365) return `${Math.floor(s / (86400 * 30))}mo`;
  return `${Math.floor(s / (86400 * 365))}y`;
}

/** Age in whole days, for threshold checks ('draft older than 3 days'). */
export function ageDays(ts: string | null | undefined): number {
  if (!ts) return 0;
  const d = parseTimestamp(ts);
  if (isNaN(d.getTime())) return 0;
  return Math.floor((Date.now() - d.getTime()) / 86400000);
}

/** Locale-grouped count: 12,481. */
export function fmtCount(n: number | null | undefined): string {
  if (n == null || Number.isNaN(Number(n))) return '—';
  return Number(n).toLocaleString();
}
