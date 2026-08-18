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
import type { ExposurePngUrls } from '@/lib/actions/nircam-exposures';

const cache = new Map<number, NircamExposure>();

export function getCachedExposure(id: number): NircamExposure | null {
  return cache.get(id) ?? null;
}

export function setCachedExposure(exp: NircamExposure): void {
  cache.set(exp.id, exp);
}

/**
 * Drop a row whose cached value can no longer be trusted — e.g. an optimistic
 * write whose save failed AND whose revert refetch also failed. Deleting beats
 * leaving the never-persisted row in place: the next visit misses the cache
 * and refetches the server's truth instead of painting the attempted value as
 * if it had stuck.
 */
export function deleteCachedExposure(id: number): void {
  cache.delete(id);
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

/**
 * True when a PNG is already fully loaded in the retained cache — i.e. it can be
 * painted with no network wait. Callers use this to decide between a seamless
 * swap (warm) and blanking to a loading state (cold), so the viewer never shows
 * a stale exposure's pixels beside the next exposure's metadata.
 */
export function isPngCached(url: string | null): boolean {
  if (!url) return false;
  const img = retainedImages.get(url);
  return !!img && img.complete && img.naturalWidth > 0;
}

/**
 * Presigned OSN GET URLs per exposure id.
 *
 * MODULE scope on purpose: the /admin/nircam/[id] page REMOUNTS on every
 * prev/next (the App Router re-instantiates the dynamic-segment page), so
 * holding these in React state — as the page originally did — wiped them on
 * each step. That forced a fresh presign (a new signature) on arrival, which
 * both flashed the presign spinner AND missed the retained-image cache keyed by
 * that URL, so every step refetched the PNG from OSN. Cached here alongside the
 * row + image caches, a prefetched sibling's URL survives the remount: the next
 * exposure paints from cache with no spinner and no network round-trip.
 *
 * Entries carry a signed-at timestamp and expire before the presigned URL's own
 * ~1 h TTL (PNG_PRESIGN_TTL_SECONDS in lib/actions/nircam-exposures.ts). Once
 * stale, getCachedPngUrls treats the id as absent so the caller re-presigns a
 * fresh URL — otherwise a long session (or an exposure whose retained image has
 * since been LRU-evicted) could serve an expired URL straight into <img> and
 * render a broken image with no in-session recovery. Unbounded like the row
 * cache (a couple of small strings per id; sessions are short).
 */
const PNG_URL_FRESH_MS = 50 * 60 * 1000; // < the 3600 s presign TTL, with buffer
const pngUrlCache = new Map<number, { urls: ExposurePngUrls; signedAt: number }>();

export function getCachedPngUrls(id: number): ExposurePngUrls | undefined {
  const entry = pngUrlCache.get(id);
  if (!entry) return undefined;
  if (Date.now() - entry.signedAt > PNG_URL_FRESH_MS) {
    pngUrlCache.delete(id);
    return undefined;
  }
  return entry.urls;
}

export function setCachedPngUrls(id: number, urls: ExposurePngUrls): void {
  pngUrlCache.set(id, { urls, signedAt: Date.now() });
}

/**
 * In-flight MASK saves + the most recent save failure.
 *
 * Triage review decisions no longer register here — they ride the durable
 * outbox (lib/nircam-review-outbox.ts), whose queued/acked state is what
 * readers consult (via overlayReviewDecisions) instead of a suppression
 * marker. Mask saves still use the plain server-action transport, so their
 * in-flight ids are tracked here at module scope (surviving the page remount)
 * for two consumers:
 *
 *  - the background revalidation and sibling prefetch, which must not
 *    overwrite the cache with a row snapshotted before an in-flight mask
 *    write commits (mask_regions is the one field family the review overlay
 *    cannot reconstruct); and
 *  - the beforeunload warning, since an in-flight mask save dies with the
 *    tab.
 *
 * The save-failure banner is shared: mask saves and the outbox both raise it.
 *
 * Subscribed via useSyncExternalStore: the snapshot is a monotonic version
 * counter; consumers read the actual values after the version changes.
 */

export interface ExposureSaveError {
  id: number;
  filename: string;
  message: string;
  /**
   * How the failure should read to the operator. 'retrying' — the outbox
   * still holds the decision and keeps retrying (transient transport/server
   * trouble); the banner is informational and clears itself when a retry
   * lands. 'permanent' (default when absent) — the write was rejected and
   * dropped; the operator must re-apply the decision.
   */
  kind?: 'retrying' | 'permanent';
}

const pendingSaveIds = new Map<number, number>(); // id → in-flight save count
let saveError: ExposureSaveError | null = null;
let saveStateVersion = 0;
const saveStateListeners = new Set<() => void>();

function emitSaveState(): void {
  saveStateVersion++;
  for (const listener of saveStateListeners) listener();
}

export function subscribeSaveState(listener: () => void): () => void {
  saveStateListeners.add(listener);
  return () => saveStateListeners.delete(listener);
}

export function getSaveStateVersion(): number {
  return saveStateVersion;
}

export function hasPendingSave(id: number): boolean {
  return pendingSaveIds.has(id);
}

/** Any save still in flight, for the beforeunload "decisions unsaved" guard. */
export function hasAnyPendingSave(): boolean {
  return pendingSaveIds.size > 0;
}

export function beginPendingSave(id: number): void {
  pendingSaveIds.set(id, (pendingSaveIds.get(id) ?? 0) + 1);
  emitSaveState();
}

export function endPendingSave(id: number): void {
  const n = pendingSaveIds.get(id) ?? 0;
  if (n > 1) pendingSaveIds.set(id, n - 1);
  else pendingSaveIds.delete(id);
  emitSaveState();
}

export function getSaveError(): ExposureSaveError | null {
  return saveError;
}

export function setSaveError(err: ExposureSaveError | null): void {
  saveError = err;
  emitSaveState();
}

/**
 * Per-exposure save queue for MASK saves: saves for the same row run strictly
 * in dispatch order. Fire-and-forget flushes mean two mask writes for one
 * exposure can be in flight together (toolbar Save racing the editor's
 * auto-flush on navigation); if the transport reordered them, the older
 * polygons would win in the database and the cache. Chaining per id makes
 * dispatch order the commit order regardless of transport behavior. Saves
 * for different exposures stay independent. (Triage review writes don't
 * queue here — the outbox's review_decided_at last-writer-wins guard handles
 * their ordering server-side.)
 */
const saveQueues = new Map<number, Promise<unknown>>();

export function enqueueSave<T>(id: number, task: () => Promise<T>): Promise<T> {
  const prev = saveQueues.get(id) ?? Promise.resolve();
  const run = prev.then(task, task);
  const tail = run.then(() => undefined, () => undefined);
  saveQueues.set(id, tail);
  tail.then(() => {
    if (saveQueues.get(id) === tail) saveQueues.delete(id);
  });
  return run;
}
