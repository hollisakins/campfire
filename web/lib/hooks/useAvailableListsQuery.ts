'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import { useDrTag } from '@/lib/dr/client';
import type { ListsAvailableResponse } from '@/app/api/lists/available/route';

/**
 * The lists the viewer can see (GET /api/lists/available), for the Tags
 * chip. Keyed without the viewer: the QueryClient is cleared on sign-out,
 * and the route answers 401 to anonymous callers, so pass `enabled` only
 * once auth has resolved to a user.
 */
export function useAvailableListsQuery(enabled: boolean = true) {
  const dr = useDrTag();
  return useQuery<ListsAvailableResponse>({
    queryKey: ['availableLists', dr ?? 'live'],
    queryFn: ({ signal }) => fetchJson<ListsAvailableResponse>('/api/lists/available', { signal, dr }),
    staleTime: 60 * 1000,
    enabled,
  });
}
