'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import type { FilterOptionsResult } from '@/app/api/filter-options/route';

export type { FilterOptionsResult } from '@/app/api/filter-options/route';

/**
 * Programs / fields / observations the viewer may filter on
 * (GET /api/filter-options). Keyed without the viewer: the QueryClient is
 * cleared on sign-out, and the route answers 401 to anonymous callers, so
 * pass `enabled` only once auth has resolved to a user.
 */
export function useFilterOptionsQuery(enabled: boolean = true) {
  return useQuery<FilterOptionsResult>({
    queryKey: ['filterOptions'],
    queryFn: ({ signal }) => fetchJson<FilterOptionsResult>('/api/filter-options', { signal }),
    staleTime: 10 * 60 * 1000,  // 10 minutes - filter options rarely change
    enabled,
  });
}
