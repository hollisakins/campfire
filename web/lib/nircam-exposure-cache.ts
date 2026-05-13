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
 * Warm browser HTTP cache for a preview PNG so the next render of an
 * <img src=...> is paint-instant. Returns immediately; the fetch continues
 * in the background and lands in the same cache the eventual <img> will use.
 */
export function prefetchPreviewPng(url: string | null): void {
  if (!url || typeof window === 'undefined') return;
  // new Image() triggers a normal HTTP fetch from the renderer with the
  // browser's image cache; safer than fetch() (which can land in a different
  // bucket depending on credentials/mode and miss when the <img> later
  // tries to use it).
  const img = new Image();
  img.decoding = 'async';
  img.src = url;
}
