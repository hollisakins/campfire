/** Shared request guards for both Worker endpoints (`/proxy`, `/o/`). */

/**
 * Validate that a signed target URL is safe to fetch: https, no embedded
 * credentials, and a hostname that exactly matches (or is a subdomain of) one
 * of the allowlisted object-store hosts. Exported for unit testing.
 */
export function isAllowedFetchUrl(
  rawUrl: string,
  allowedHostsCsv: string
): { ok: true } | { ok: false; reason: string } {
  let parsed: URL;
  try {
    parsed = new URL(rawUrl);
  } catch {
    return { ok: false, reason: 'unparseable url' };
  }
  if (parsed.protocol !== 'https:') {
    return { ok: false, reason: 'non-https scheme' };
  }
  if (parsed.username || parsed.password) {
    return { ok: false, reason: 'embedded credentials' };
  }
  const allowed = allowedHostsCsv
    .split(',')
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
  const host = parsed.hostname.toLowerCase();
  const hostOk = allowed.some((h) => host === h || host.endsWith('.' + h));
  if (!hostOk) {
    return { ok: false, reason: 'host not in allowlist' };
  }
  return { ok: true };
}
