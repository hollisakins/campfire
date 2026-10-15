/**
 * The data-release browsing mode (P6c-1, docs/design-data-releases-impl-plan.md §8).
 *
 * A release is selected by `?dr=<tag>` on the page URL. The parameter is
 * parsed in exactly ONE place — middleware (`drRequestDecision`) — which
 * stamps the tag onto the request as the `x-campfire-dr` header. Everything
 * downstream (server pages, the Supabase clients, the client-side provider)
 * reads that header; nothing re-derives the mode from the URL. A `dr`
 * parameter that is not one valid tag is a 404 while the pages flag is on,
 * and ignored entirely while it is off. Tags travel verbatim (case-sensitive).
 * No directive: both sides import this.
 */

/** Same shape the database accepts (dr_request_context). */
export const DR_TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$/;

export const DR_PARAM = 'dr';
export const DR_HEADER = 'x-campfire-dr';

/** Paths that never browse a release (the FitsGL map is live-only by decision). */
const LIVE_ONLY_PATHS = ['/map'];

/** A well-formed tag, or null for anything else (never lowercased). */
export function parseDrTag(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  return DR_TAG_RE.test(value) ? value : null;
}

export type DrRequestDecision =
  | { kind: 'live' }
  | { kind: 'release'; tag: string }
  | { kind: 'not-found' }
  | { kind: 'strip'; location: string };

/**
 * What middleware does with a request URL:
 *   - flag off, or no `dr` parameter → live;
 *   - a live-only path with `dr` → redirect to the same URL without it;
 *   - exactly one well-formed `dr` → that release;
 *   - anything else (repeated, empty, malformed) → 404.
 */
export function drRequestDecision(url: URL | string, mode: 'off' | 'admin' | 'all'): DrRequestDecision {
  const u = typeof url === 'string' ? new URL(url, 'http://local') : url;
  const all = u.searchParams.getAll(DR_PARAM);
  if (mode === 'off' || all.length === 0) return { kind: 'live' };
  if (LIVE_ONLY_PATHS.some((p) => u.pathname === p || u.pathname.startsWith(`${p}/`))) {
    const stripped = new URL(u.toString());
    stripped.searchParams.delete(DR_PARAM);
    return { kind: 'strip', location: `${stripped.pathname}${stripped.search}${stripped.hash}` };
  }
  if (all.length !== 1) return { kind: 'not-found' };
  const tag = parseDrTag(all[0]);
  return tag ? { kind: 'release', tag } : { kind: 'not-found' };
}

/**
 * `href` with `?dr=<tag>` carried along (kept when the href already names a
 * release; untouched when no release is selected). Relative and absolute
 * hrefs alike; the hash is preserved.
 */
export function withDr(href: string, tag: string | null | undefined): string {
  if (!tag) return href;
  const hashAt = href.indexOf('#');
  const hash = hashAt >= 0 ? href.slice(hashAt) : '';
  const base = hashAt >= 0 ? href.slice(0, hashAt) : href;
  const qAt = base.indexOf('?');
  const path = qAt >= 0 ? base.slice(0, qAt) : base;
  const params = new URLSearchParams(qAt >= 0 ? base.slice(qAt + 1) : '');
  if (!params.has(DR_PARAM)) params.set(DR_PARAM, tag);
  const qs = params.toString();
  return `${path}${qs ? `?${qs}` : ''}${hash}`;
}

/** `href` without any `?dr=` (the "Live catalog" link, the map links). */
export function withoutDr(href: string): string {
  const hashAt = href.indexOf('#');
  const hash = hashAt >= 0 ? href.slice(hashAt) : '';
  const base = hashAt >= 0 ? href.slice(0, hashAt) : href;
  const qAt = base.indexOf('?');
  if (qAt < 0) return href;
  const path = base.slice(0, qAt);
  const params = new URLSearchParams(base.slice(qAt + 1));
  params.delete(DR_PARAM);
  const qs = params.toString();
  return `${path}${qs ? `?${qs}` : ''}${hash}`;
}
