/**
 * SessionStorage-backed nav cache for the /admin/nircam exposure detail page.
 *
 * The list page is single-fetch (no pagination), so this is much simpler
 * than the generic [[navigation-cache]]: store the visible filtered ID
 * order on row click, look up neighbors on the detail page.
 *
 * Cache lives ~30 min and is invalidated when the user changes filters
 * (the list page rewrites it on every fetch + click).
 */

const KEY = 'campfire_nircam_nav';
const TTL_MS = 30 * 60 * 1000;

interface Cache {
  ids: number[];
  ts: number;
}

export function setNircamNav(ids: number[]): void {
  if (typeof window === 'undefined') return;
  try {
    sessionStorage.setItem(KEY, JSON.stringify({ ids, ts: Date.now() } as Cache));
  } catch {
    /* storage full or disabled — silent: nav still works, just no prev/next */
  }
}

export function getNircamNav(): Cache | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Cache;
    if (!parsed?.ids?.length) return null;
    if (Date.now() - parsed.ts > TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

export interface NircamNavLookup {
  prev: number | null;
  next: number | null;
  index: number;  // 1-based
  total: number;
}

export function lookupNircamNav(id: number): NircamNavLookup | null {
  const cache = getNircamNav();
  if (!cache) return null;
  const idx = cache.ids.indexOf(id);
  if (idx === -1) return null;
  return {
    prev: idx > 0 ? cache.ids[idx - 1] : null,
    next: idx < cache.ids.length - 1 ? cache.ids[idx + 1] : null,
    index: idx + 1,
    total: cache.ids.length,
  };
}
