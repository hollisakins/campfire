/**
 * The data-release browsing mode (P6c-1, docs/design-data-releases-impl-plan.md §8).
 *
 * A release is selected by `?dr=<tag>` on the page URL. The tag travels
 * verbatim (tags are case-sensitive): middleware copies it onto the request
 * as the `x-campfire-dr` header, the server Supabase clients forward that
 * header to PostgREST, and the database's pre-request hook selects the
 * frozen rows for the transaction. No directive: both sides import this.
 */

/** Same shape the database accepts (dr_request_context). */
export const DR_TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$/;

export const DR_PARAM = 'dr';
export const DR_HEADER = 'x-campfire-dr';

/** A well-formed tag, or null for anything else (never lowercased). */
export function parseDrTag(value: string | null | undefined): string | null {
  if (typeof value !== 'string') return null;
  return DR_TAG_RE.test(value) ? value : null;
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

/** `href` without any `?dr=` (the "Live catalog" link). */
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

/** The tag a request URL selects, if any (what middleware stamps). */
export function drTagFromUrl(url: URL | string): string | null {
  const u = typeof url === 'string' ? new URL(url, 'http://local') : url;
  return parseDrTag(u.searchParams.get(DR_PARAM));
}

/**
 * The tag from a page's `searchParams` (Next hands a record whose values may
 * be arrays; a repeated `dr` is treated as none).
 */
export function drTagFromSearchParams(
  searchParams: Record<string, string | string[] | undefined> | URLSearchParams | null | undefined,
): string | null {
  if (!searchParams) return null;
  if (searchParams instanceof URLSearchParams) {
    const all = searchParams.getAll(DR_PARAM);
    return all.length === 1 ? parseDrTag(all[0]) : null;
  }
  const v = searchParams[DR_PARAM];
  return typeof v === 'string' ? parseDrTag(v) : null;
}

/**
 * The header value middleware stamps for a request URL: the tag when the
 * pages flag is on (`admin` / `all`), null when the flag is off (the
 * parameter is then ignored everywhere) or no valid tag is present.
 */
export function drHeaderFor(url: URL | string, mode: 'off' | 'admin' | 'all'): string | null {
  if (mode === 'off') return null;
  return drTagFromUrl(url);
}
