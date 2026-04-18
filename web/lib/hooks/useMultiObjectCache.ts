'use client';

import { useCallback } from 'react';
import type { ObjectDetail } from '@/lib/types';
import type { ObjectNavigationData } from './useObjectNavigation';

interface CachedObjectData {
  object: ObjectDetail;
  timestamp: number;
}

// Module-level cache (persists across renders, shared within session).
// Keyed by IAU object_id. LRU eviction: on read/write we reinsert so
// Map.keys() iteration order reflects recency — oldest key is evicted first.
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
    // Touch for LRU: re-insert so it moves to most-recent position.
    objectCache.delete(objectId);
    objectCache.set(objectId, cached);
    return cached;
  }, []);

  const setCached = useCallback((objectId: string, data: ObjectNavigationData): void => {
    // If present, delete first so the reinserted entry lands at most-recent.
    if (objectCache.has(objectId)) {
      objectCache.delete(objectId);
    } else if (objectCache.size >= MAX_CACHE_SIZE) {
      const oldestKey = objectCache.keys().next().value;
      if (oldestKey) objectCache.delete(oldestKey);
    }
    objectCache.set(objectId, {
      object: data.object,
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
