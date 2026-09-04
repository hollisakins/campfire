'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import type { AdjacentObjectsResponse } from '@/app/api/objects/adjacent/route';

/**
 * Prev/next ids and position for the object page's navigation arrows
 * (GET /api/objects/adjacent). `filterStr` is the list page's own URL
 * parameter string (filters + sort), forwarded verbatim, so the key is the
 * exact sequence being walked. Callers enable it only on a navigation-cache
 * miss or at a page boundary (ObjectNavigation); the request is aborted if
 * the user moves on before it lands.
 */
export function useAdjacentObjectsQuery(objectId: string, filterStr: string, enabled = true) {
  return useQuery<AdjacentObjectsResponse>({
    queryKey: ['adjacentObjects', objectId, filterStr],
    queryFn: ({ signal }) =>
      fetchJson<AdjacentObjectsResponse>(
        `/api/objects/adjacent?id=${encodeURIComponent(objectId)}${filterStr ? `&${filterStr}` : ''}`,
        { signal },
      ),
    enabled,
    staleTime: 60 * 1000,
  });
}
