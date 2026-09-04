'use client';

import { useCallback, useEffect } from 'react';
import { useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { getSpectra } from '@/lib/actions/spectra';
import type { FilterOptions, PaginatedSpectraResult } from '@/lib/actions/spectra';
import { DEFAULT_FILTERS } from '@/lib/actions/filter-params';
import type { SortColumn, SortDirection, ViewMode } from '@/lib/actions/spectra-types';

/** True when any filter differs from its default (arrays non-empty, scalars set). */
function hasActiveFilters(filters: Partial<FilterOptions>): boolean {
  return (Object.keys(filters) as (keyof FilterOptions)[]).some((key) => {
    const value = filters[key];
    const fallback = DEFAULT_FILTERS[key];
    if (Array.isArray(value)) return value.length > 0;
    if (value === null || value === undefined) return false;
    return value !== fallback;
  });
}

export interface UseSpectraQueryParams {
  filters: Partial<FilterOptions>;
  page: number;
  pageSize: number;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  viewMode?: ViewMode;
  enabled?: boolean;
}

// Helper to schedule work during browser idle time
function scheduleIdleWork(callback: () => void): () => void {
  if (typeof requestIdleCallback !== 'undefined') {
    const id = requestIdleCallback(callback, { timeout: 2000 });
    return () => cancelIdleCallback(id);
  } else {
    // Fallback for Safari: use setTimeout with delay
    const id = setTimeout(callback, 100);
    return () => clearTimeout(id);
  }
}

export function useSpectraQuery(params: UseSpectraQueryParams) {
  const { filters, page, pageSize, sortColumn, sortDirection, viewMode = 'objects', enabled = true } = params;
  const queryClient = useQueryClient();

  // The exact total is a second full pass over the filter inside the RPC
  // (#501), and it only depends on the filters + view, not on page or sort.
  // Ask for it once per filter set, remember it in the query cache, and fill
  // it back into later pages / re-sorts (which then run without the count).
  const fetchPage = useCallback(
    async (p: number): Promise<PaginatedSpectraResult> => {
      const countKey = ['spectra-count', { filters, viewMode }] as const;
      const known = queryClient.getQueryData<number>(countKey);
      const result = await getSpectra(filters, p, pageSize, sortColumn, sortDirection, viewMode, {
        includeCount: known === undefined,
      });
      // Only a successful, authenticated response is an authoritative count:
      // the action resolves with total 0 (not a throw) on RPC errors and on
      // a lapsed session, and caching that would pin a "0 results" pager.
      if (result.total >= 0 && !result.error && result.isAuthenticated) {
        queryClient.setQueryData(countKey, result.total);
        return result;
      }
      if (result.error || !result.isAuthenticated) return result;
      if (known !== undefined) {
        return {
          ...result,
          total: known,
          totalPages: Math.ceil(known / pageSize),
          isComplete: known <= pageSize,
        };
      }
      return result;
    },
    [filters, pageSize, sortColumn, sortDirection, viewMode, queryClient],
  );

  const query = useQuery<PaginatedSpectraResult>({
    queryKey: ['spectra', { filters, page, pageSize, sortColumn, sortDirection, viewMode }],
    queryFn: () => fetchPage(page),
    enabled,
    placeholderData: keepPreviousData,
  });

  // Prefetch adjacent pages in the background after main content loads.
  // Only for the unfiltered default listing: Next serializes server actions
  // per client, so a speculative page±1 RPC queues AHEAD of the user's next
  // filter/sort/page action — while filters are being adjusted that is dead
  // time on every click, and the prefetched keys are rarely reused (#499).
  const filtersActive = hasActiveFilters(filters);
  useEffect(() => {
    if (!query.data || !enabled || query.isFetching || filtersActive) return;

    const totalPages = query.data.totalPages;
    const isComplete = query.data.isComplete;

    // Only prefetch when in server-side pagination mode (not when we have full dataset)
    if (isComplete) return;

    // Schedule prefetching during browser idle time
    const cancelIdle = scheduleIdleWork(() => {
      // Prefetch next page
      if (page < totalPages) {
        queryClient.prefetchQuery({
          queryKey: ['spectra', { filters, page: page + 1, pageSize, sortColumn, sortDirection, viewMode }],
          queryFn: () => fetchPage(page + 1),
          staleTime: 30 * 1000, // Consider fresh for 30 seconds
        });
      }

      // Prefetch previous page
      if (page > 1) {
        queryClient.prefetchQuery({
          queryKey: ['spectra', { filters, page: page - 1, pageSize, sortColumn, sortDirection, viewMode }],
          queryFn: () => fetchPage(page - 1),
          staleTime: 30 * 1000,
        });
      }
    });

    return cancelIdle;
  }, [query.data, query.isFetching, page, pageSize, filters, filtersActive, sortColumn, sortDirection, viewMode, enabled, queryClient, fetchPage]);

  return query;
}
