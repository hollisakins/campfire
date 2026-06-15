// Shared formatting helpers for the Updates feed. Safe to import from both
// server and client components (no Node APIs).

/** Format an ISO date (YYYY-MM-DD) as e.g. "Jun 10, 2026", anchored to UTC so
 *  date-only strings don't drift across timezones / hydration. */
export function formatUpdateDate(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(d);
}
