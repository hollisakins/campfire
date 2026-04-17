'use client';

import { useCallback } from 'react';
import type { ObjectDetail } from '@/lib/types';
import type { ObjectNavigationData } from './useObjectNavigation';

interface CachedObjectData {
  object: ObjectDetail;
  rgbImageUrl: string | null;
  timestamp: number;
}

// Module-level cache (persists across renders, shared within session).
// Keyed by IAU object_id.
const objectCache = new Map<string, CachedObjectData>();
const CACHE_TTL = 30 * 60 * 1000; // 30 minutes
const MAX_CACHE_SIZE = 10;

export function useMultiObjectCache() {
  const getCached = useCallback((objectId: string): CachedObjectData | undefined => {
    const cached = objectCache.get(objectId);
    if (!cached) return undefined;
    if (Date.now() - cached.timestamp > CACHE_TTL) {
      objectCache.delete(objectId);
      return undefined;
    }
    return cached;
  }, []);

  const setCached = useCallback((objectId: string, data: ObjectNavigationData): void => {
    if (objectCache.size >= MAX_CACHE_SIZE) {
      const oldestKey = objectCache.keys().next().value;
      if (oldestKey) objectCache.delete(oldestKey);
    }
    objectCache.set(objectId, {
      object: data.object,
      rgbImageUrl: data.rgbImageUrl,
      timestamp: Date.now(),
    });
  }, []);

  const deleteCached = useCallback((objectId: string): void => {
    objectCache.delete(objectId);
  }, []);

  const clearCache = useCallback((): void => {
    objectCache.clear();
  }, []);

  return { getCached, setCached, deleteCached, clearCache };
}
