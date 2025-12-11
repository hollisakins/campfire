/**
 * SessionStorage-based navigation cache for instant prev/next lookup.
 * Stores visible object IDs when clicking from the table for quick navigation.
 */

const CACHE_KEY = 'campfire_nav_cache';
const CACHE_TTL = 30 * 60 * 1000; // 30 minutes

export interface NavCache {
  ids: string[];           // Ordered list of object IDs
  filters: string;         // Serialized filters for cache validation
  sort: string;            // Sort key (column_direction) for cache validation
  timestamp: number;       // When cache was created
  pageStart: number;       // Absolute index of first item in ids array
  total: number;           // Total count across all pages
}

export interface NavLookupResult {
  prev: string | null;
  next: string | null;
  index: number;           // 1-based absolute index
  total: number;
}

/**
 * Store navigation cache in sessionStorage
 */
export function setNavCache(cache: Omit<NavCache, 'timestamp'>): void {
  if (typeof window === 'undefined') return;

  try {
    const fullCache: NavCache = {
      ...cache,
      timestamp: Date.now(),
    };
    sessionStorage.setItem(CACHE_KEY, JSON.stringify(fullCache));
  } catch (error) {
    console.warn('Failed to set navigation cache:', error);
  }
}

/**
 * Get navigation cache from sessionStorage
 * Returns null if cache is missing, expired, or invalid
 */
export function getNavCache(): NavCache | null {
  if (typeof window === 'undefined') return null;

  try {
    const cached = sessionStorage.getItem(CACHE_KEY);
    if (!cached) return null;

    const cache: NavCache = JSON.parse(cached);

    // Check if cache is expired
    if (Date.now() - cache.timestamp > CACHE_TTL) {
      sessionStorage.removeItem(CACHE_KEY);
      return null;
    }

    return cache;
  } catch (error) {
    console.warn('Failed to get navigation cache:', error);
    return null;
  }
}

/**
 * Lookup prev/next object IDs from cache
 * Returns null if object is not found in cache
 */
export function lookupInCache(
  objectId: string,
  expectedFilters?: string,
  expectedSort?: string
): NavLookupResult | null {
  const cache = getNavCache();
  if (!cache) return null;

  // Optionally validate that filters/sort match
  if (expectedFilters !== undefined && cache.filters !== expectedFilters) {
    return null;
  }
  if (expectedSort !== undefined && cache.sort !== expectedSort) {
    return null;
  }

  // Find object in cached list
  const idx = cache.ids.indexOf(objectId);
  if (idx === -1) return null;

  return {
    prev: idx > 0 ? cache.ids[idx - 1] : null,
    next: idx < cache.ids.length - 1 ? cache.ids[idx + 1] : null,
    index: cache.pageStart + idx + 1, // 1-based absolute index
    total: cache.total,
  };
}

/**
 * Check if we're at a cache boundary (need server query for prev/next)
 */
export function isAtCacheBoundary(objectId: string): {
  atStart: boolean;
  atEnd: boolean;
} {
  const cache = getNavCache();
  if (!cache) return { atStart: false, atEnd: false };

  const idx = cache.ids.indexOf(objectId);
  if (idx === -1) return { atStart: false, atEnd: false };

  return {
    atStart: idx === 0 && cache.pageStart > 0,
    atEnd: idx === cache.ids.length - 1 && cache.pageStart + cache.ids.length < cache.total,
  };
}

/**
 * Clear navigation cache
 */
export function clearNavCache(): void {
  if (typeof window === 'undefined') return;

  try {
    sessionStorage.removeItem(CACHE_KEY);
  } catch (error) {
    console.warn('Failed to clear navigation cache:', error);
  }
}
