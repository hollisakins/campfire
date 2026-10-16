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

/**
 * Where a release can be browsed (P6c-1). Everything else is live-only — the
 * FitsGL map, tags/lists, inspection, profile, admin, auth, home, docs, the
 * cutout tool — and a `?dr=` there is redirected away. `/api/v1` is not
 * listed: the bearer routes own their own gate (P6c-2) and middleware never
 * stamps them.
 */
const RELEASE_PATHS = [
  '/nirspec', '/nircam', '/releases',
  '/api/objects', '/api/spectrum', '/api/filter-options', '/api/metadata', '/api/download',
  '/api/line-fit', '/api/redshift-fit', '/api/shutters', '/api/photometry-pz', '/api/dr',
];
const LIVE_ONLY_UNDER_RELEASE_PATHS = ['/nirspec/tags', '/nircam/*/cutouts'];

function matches(pathname: string, prefix: string): boolean {
  if (prefix.includes('*')) {
    const re = new RegExp('^' + prefix.split('*').map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('[^/]+') + '(/|$)');
    return re.test(pathname);
  }
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

/** Can this path browse a release? */
export function isReleasePath(pathname: string): boolean {
  if (LIVE_ONLY_UNDER_RELEASE_PATHS.some((p) => matches(pathname, p))) return false;
  return RELEASE_PATHS.some((p) => matches(pathname, p));
}

const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/**
 * Under a release, an `/api/*` request that is not a read is refused before
 * its handler (P6c-1: reads are GET route handlers per AGENTS.md; writes are
 * everything else). Server actions post to page URLs, not `/api`, and keep
 * their own `assertLiveRequest()` guards.
 */
export function isRefusedWriteUnderRelease(pathname: string, method: string): boolean {
  return pathname.startsWith('/api/') && !SAFE_METHODS.has(method.toUpperCase());
}

/** A well-formed tag, or null for anything else (never lowercased). */
export function parseDrTag(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  return DR_TAG_RE.test(value) ? value : null;
}

export type DrRequestDecision =
  | { kind: 'live' }
  | { kind: 'release'; tag: string }
  | { kind: 'not-found' }
  | { kind: 'strip'; location: string }
  | { kind: 'read-only' };

/**
 * What middleware does with a request:
 *   - anything under `/api/v1` → live, untouched (the bearer gate owns `dr`);
 *   - flag off, or no `dr` parameter → live;
 *   - a write (`/api/*`, non-GET) with any `dr` → 409 read-only;
 *   - a path that cannot browse a release → redirect to the same URL without it;
 *   - a repeated, empty or malformed `dr` → 404;
 *   - else → that release.
 */
export function drRequestDecision(url: URL | string, mode: 'off' | 'admin' | 'all', method = 'GET'): DrRequestDecision {
  const u = typeof url === 'string' ? new URL(url, 'http://local') : url;
  // The bearer API owns its own gate and answers JSON errors itself:
  // middleware neither stamps nor rewrites anything under /api/v1 (P6c-2).
  if (u.pathname === '/api/v1' || u.pathname.startsWith('/api/v1/')) return { kind: 'live' };
  const all = u.searchParams.getAll(DR_PARAM);
  if (mode === 'off' || all.length === 0) return { kind: 'live' };
  // A write with any `dr` is refused outright: a 307 would be re-issued
  // with its method and body against the live catalog.
  if (isRefusedWriteUnderRelease(u.pathname, method)) return { kind: 'read-only' };
  if (!isReleasePath(u.pathname)) {
    const stripped = new URL(u.toString());
    stripped.searchParams.delete(DR_PARAM);
    return { kind: 'strip', location: `${stripped.pathname}${stripped.search}${stripped.hash}` };
  }
  if (all.length !== 1) return { kind: 'not-found' };
  const tag = parseDrTag(all[0]);
  if (!tag) return { kind: 'not-found' };
  return { kind: 'release', tag };
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
