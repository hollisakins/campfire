'use client';

import { useCallback } from 'react';
import type { SpectrumObject, Spectrum } from '@/lib/types';

export interface NavigationData {
  spectrum: SpectrumObject;
  rgbImageUrl: string | null;
  nav: {
    prev: string | null;
    next: string | null;
    index: number;
    total: number;
  };
}

interface CachedObjectData {
  spectrum: SpectrumObject;
  rgbImageUrl: string | null;
  nav: {
    prev: string | null;
    next: string | null;
    index: number;
    total: number;
  };
  timestamp: number;
}

// Module-level cache (persists across renders, shared within session)
const objectCache = new Map<string, CachedObjectData>();
const CACHE_TTL = 30 * 60 * 1000; // 30 minutes
const MAX_CACHE_SIZE = 10; // Limit to 10 objects (~5MB)

export function useMultiObjectCache() {
  /**
   * Get cached object data with TTL check
   * Returns undefined if not in cache or expired
   */
  const getCached = useCallback((objectId: string): CachedObjectData | undefined => {
    const cached = objectCache.get(objectId);

    if (!cached) {
      console.log(`[ObjectCache] MISS: ${objectId}`);
      return undefined;
    }

    // Check if expired
    if (Date.now() - cached.timestamp > CACHE_TTL) {
      console.log(`[ObjectCache] EXPIRED: ${objectId}`);
      objectCache.delete(objectId);
      return undefined;
    }

    console.log(`[ObjectCache] HIT: ${objectId}`);
    return cached;
  }, []);

  /**
   * Store object data in cache with LRU eviction
   */
  const setCached = useCallback((objectId: string, data: NavigationData): void => {
    // LRU eviction: remove oldest entry if at max size
    if (objectCache.size >= MAX_CACHE_SIZE) {
      const oldestKey = objectCache.keys().next().value;
      if (oldestKey) {
        console.log(`[ObjectCache] Evicting oldest: ${oldestKey}`);
        objectCache.delete(oldestKey);
      }
    }

    objectCache.set(objectId, {
      spectrum: data.spectrum,
      rgbImageUrl: data.rgbImageUrl,
      nav: data.nav,
      timestamp: Date.now(),
    });

    console.log(`[ObjectCache] SET: ${objectId} (cache size: ${objectCache.size})`);
  }, []);

  /**
   * Prefetch adjacent objects in parallel
   * Prioritizes 'next' over 'prev' (users navigate forward more)
   * Optionally prefetches FITS data for each object
   */
  const prefetchAdjacent = useCallback(async (
    prevId: string | null,
    nextId: string | null,
    fetchFn: (id: string) => Promise<NavigationData | null>,
    prefetchFitsFn?: (spectra: Spectrum[]) => Promise<void>
  ): Promise<void> => {
    const promises: Promise<void>[] = [];

    // Prioritize next (fetch first)
    if (nextId && !getCached(nextId)) {
      console.log(`[ObjectCache] Prefetching next: ${nextId}`);
      promises.push(
        fetchFn(nextId).then(async data => {
          if (data) {
            setCached(nextId, data);
            // Also prefetch FITS data if function provided
            if (prefetchFitsFn) {
              console.log(`[ObjectCache] Prefetching FITS for: ${nextId}`);
              await prefetchFitsFn(data.spectrum.spectra);
            }
          }
        }).catch(err => {
          console.warn(`[ObjectCache] Failed to prefetch ${nextId}:`, err);
        })
      );
    }

    // Prefetch prev (lower priority)
    if (prevId && !getCached(prevId)) {
      console.log(`[ObjectCache] Prefetching prev: ${prevId}`);
      promises.push(
        fetchFn(prevId).then(async data => {
          if (data) {
            setCached(prevId, data);
            // Also prefetch FITS data if function provided
            if (prefetchFitsFn) {
              console.log(`[ObjectCache] Prefetching FITS for: ${prevId}`);
              await prefetchFitsFn(data.spectrum.spectra);
            }
          }
        }).catch(err => {
          console.warn(`[ObjectCache] Failed to prefetch ${prevId}:`, err);
        })
      );
    }

    // Wait for all prefetches to complete
    await Promise.all(promises);
  }, [getCached, setCached]);

  /**
   * Clear the entire cache
   */
  const clearCache = useCallback((): void => {
    console.log(`[ObjectCache] Clearing cache (${objectCache.size} entries)`);
    objectCache.clear();
  }, []);

  return {
    getCached,
    setCached,
    prefetchAdjacent,
    clearCache,
  };
}
