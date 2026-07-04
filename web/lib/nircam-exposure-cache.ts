/**
 * In-memory exposure cache for the /admin/nircam triage flow.
 *
 * Lives at module scope (per browser-tab session). Keyed by exposure id so
 * the detail page can render instantly when the user steps to a sibling
 * via prev/next, while a background re-fetch revalidates against the DB.
 *
 * Intentionally simple: no LRU eviction, no TTL — admin sessions are short
 * and a few hundred row rows of cached state is well under any browser memory
 * budget. The cache is dropped on full reload (refresh, navigate-away).
 */

import type { NircamExposure } from '@/lib/types';

const cache = new Map<number, NircamExposure>();

export function getCachedExposure(id: number): NircamExposure | null {
  return cache.get(id) ?? null;
}

export function setCachedExposure(exp: NircamExposure): void {
  cache.set(exp.id, exp);
}

export function clearExposureCache(): void {
  cache.clear();
}

/**
 * Retained decoded exposure PNGs, keyed by URL. This is the "local cache" that
 * makes prev/next switching paint-instant.
 *
 * The exposure PNGs are served straight into <img> from OSN via presigned URLs
 * (epic #261 N5) — deliberately bypassing the Next proxy other images use. That
 * proxy is also where those images pick up a `Cache-Control` header; the OSN
 * objects themselves carry none (the deploy upload sets only content-type). So
 * the browser will NOT reliably keep a fetched PNG in its HTTP cache, and a
 * throwaway `new Image()` prefetch is garbage-collected the moment it returns —
 * both of which mean the eventual <img> just refetches ~5.7 MB from OSN (~1 s).
 *
 * Holding a live HTMLImageElement keeps the browser's in-memory image cache hot
 * for that exact URL, so a later <img src=sameUrl> reuses the decoded bitmap
 * with no network round-trip — independent of any Cache-Control header. Bounded
 * LRU because full-res PNGs are large; the window we actively step through
 * (a few ahead + one back + current) stays resident while older ones evict.
 */
const RETAINED_IMAGE_LIMIT = 12;
const retainedImages = new Map<string, HTMLImageElement>();

/**
 * Warm + retain a PNG (full-res mask surface or preview) so the next render of
 * an <img src=...> is paint-instant. Returns immediately; the fetch/decode
 * continues in the background and the element is held so its bytes stay in the
 * browser's image cache. Idempotent per URL (refreshes LRU recency), so it's
 * safe to call on every navigation.
 */
export function prefetchPng(url: string | null): void {
  if (!url || typeof window === 'undefined') return;
  const existing = retainedImages.get(url);
  if (existing) {
    // Already warmed — bump recency so the window in view isn't evicted.
    retainedImages.delete(url);
    retainedImages.set(url, existing);
    return;
  }
  const img = new Image();
  img.decoding = 'async';
  img.src = url;
  // Retain a reference so the fetched+decoded bytes stay resident; a later
  // <img src=url> reuses them with no refetch. (A plain fetch() could land in
  // a different cache bucket by credentials/mode and miss the <img>, so we use
  // an Image element — the same request the <img> will make.)
  retainedImages.set(url, img);
  while (retainedImages.size > RETAINED_IMAGE_LIMIT) {
    const oldest = retainedImages.keys().next().value;
    if (oldest === undefined) break;
    retainedImages.delete(oldest);
  }
}
