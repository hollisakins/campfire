/**
 * In-memory rate-exposure cache for the /admin/nirspec/rate triage flow.
 *
 * Module-scope (per browser-tab session), keyed by row id, so the detail page can
 * render instantly when stepping to a sibling via prev/next while a background
 * re-fetch revalidates against the DB. Mirrors nircam-exposure-cache.ts, minus the
 * PNG prefetch (rate review is FITS-only). Dropped on full reload.
 */

import type { NirspecRateExposure } from '@/lib/types';

const cache = new Map<number, NirspecRateExposure>();

export function getCachedRate(id: number): NirspecRateExposure | null {
  return cache.get(id) ?? null;
}

export function setCachedRate(exp: NirspecRateExposure): void {
  cache.set(exp.id, exp);
}

export function clearRateCache(): void {
  cache.clear();
}
