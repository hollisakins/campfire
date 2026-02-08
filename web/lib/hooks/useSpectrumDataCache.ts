'use client';

import { useCallback } from 'react';
import type { SpectrumData } from '@/app/api/spectrum/route';
import type { RedshiftFitData } from '@/app/api/redshift-fit/route';
import type { Spectrum } from '@/lib/types';

interface CachedSpectrumData {
  spectrum: SpectrumData;
  fitData: RedshiftFitData | null;
  timestamp: number;
}

// Cache configuration
const MAX_CACHE_SIZE = 20; // ~10MB for 20 gratings
const CACHE_TTL = 30 * 60 * 1000; // 30 minutes

// Module-level cache (persists across renders, shared within session)
const dataCache = new Map<string, CachedSpectrumData>();

export function useSpectrumDataCache() {
  /**
   * Evict oldest cache entry when cache is full (LRU policy)
   */
  const evictOldest = useCallback(() => {
    let oldestKey: string | null = null;
    let oldestTime = Infinity;

    for (const [key, entry] of dataCache.entries()) {
      if (entry.timestamp < oldestTime) {
        oldestTime = entry.timestamp;
        oldestKey = key;
      }
    }

    if (oldestKey) {
      dataCache.delete(oldestKey);
      console.log(`[Cache] Evicted oldest entry: ${oldestKey.split('/').pop()}`);
    }
  }, []);

  /**
   * Prefetch FITS data for all gratings in parallel
   * Called once on InspectionModeOverlay mount
   */
  const prefetchGratings = useCallback(async (spectra: Spectrum[]) => {
    console.log('[Cache] Starting prefetch for', spectra.length, 'gratings');

    const promises = spectra.map(async (spec) => {
      // Skip if already cached
      if (dataCache.has(spec.fits_path)) {
        console.log(`[Cache] Already cached: ${spec.grating}`);
        return;
      }

      console.log(`[Cache] Fetching ${spec.grating}...`);

      try {
        // Fetch spectrum and fit data in parallel
        const [specRes, fitRes] = await Promise.all([
          fetch(`/api/spectrum?path=${encodeURIComponent(spec.fits_path)}`),
          fetch(`/api/redshift-fit?path=${encodeURIComponent(spec.fits_path)}`)
            .catch(() => null), // Graceful fallback if fit doesn't exist
        ]);

        if (!specRes.ok) {
          console.warn(`[Cache] Failed to prefetch ${spec.grating}:`, specRes.status);
          return;
        }

        const specData: SpectrumData = await specRes.json();
        const fitData: RedshiftFitData | null =
          fitRes?.ok ? await fitRes.json() : null;

        // Evict oldest entry if cache is full
        if (dataCache.size >= MAX_CACHE_SIZE) {
          evictOldest();
        }

        // Store in module-level cache with timestamp
        dataCache.set(spec.fits_path, {
          spectrum: specData,
          fitData,
          timestamp: Date.now(),
        });

        console.log(`[Cache] ✓ Prefetched ${spec.grating}`);
      } catch (error) {
        console.warn(`[Cache] Failed to prefetch ${spec.grating}:`, error);
      }
    });

    // Wait for all gratings to finish (parallel fetch)
    await Promise.all(promises);
    console.log('[Cache] Prefetch complete. Cache size:', dataCache.size);
  }, [evictOldest]);

  /**
   * Get cached data for a FITS path
   * Returns undefined if not in cache or expired
   */
  const getCached = useCallback((fitsPath: string): CachedSpectrumData | undefined => {
    const cached = dataCache.get(fitsPath);

    if (!cached) {
      console.log(`[Cache] Lookup ${fitsPath.split('/').pop()}: MISS`);
      return undefined;
    }

    // Check if entry has expired
    const age = Date.now() - cached.timestamp;
    if (age > CACHE_TTL) {
      console.log(`[Cache] Lookup ${fitsPath.split('/').pop()}: EXPIRED (${Math.round(age / 60000)}min old)`);
      dataCache.delete(fitsPath);
      return undefined;
    }

    console.log(`[Cache] Lookup ${fitsPath.split('/').pop()}: HIT`);
    return cached;
  }, []);

  /**
   * Clear the entire cache (call when leaving inspection mode)
   */
  const clearCache = useCallback(() => {
    console.log('[Cache] Clearing cache');
    dataCache.clear();
  }, []);

  return { prefetchGratings, getCached, clearCache };
}
