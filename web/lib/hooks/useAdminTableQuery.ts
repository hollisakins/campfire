'use client';

import { useEffect } from 'react';
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query';
import type { SortState } from './useTableUrlState';

// ---------------------------------------------------------------------------
// TanStack Query wrapper for admin lists: keepPreviousData for skeleton-free
// page flips + idle prefetch of the next page (generalizing useSpectraQuery's
// UX for the standardized {rows, total} admin page shape).
// ---------------------------------------------------------------------------

export interface AdminPageResult<TRow> {
  rows: TRow[];
  total: number;
  error?: string;
}

interface AdminTableQueryOptions<TRow> {
  /** Cache scope, e.g. 'admin-deployments'. */
  scope: string;
  /** Debounced filters object (part of the query key). */
  filters: unknown;
  sort: SortState;
  page: number;
  pageSize: number;
  fetchPage: (page: number) => Promise<AdminPageResult<TRow>>;
  enabled?: boolean;
}

const STALE_MS = 30_000;

export function useAdminTableQuery<TRow>({
  scope,
  filters,
  sort,
  page,
  pageSize,
  fetchPage,
  enabled = true,
}: AdminTableQueryOptions<TRow>) {
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: [scope, filters, sort, pageSize, page],
    queryFn: () => fetchPage(page),
    placeholderData: keepPreviousData,
    staleTime: STALE_MS,
    enabled,
  });

  // Prefetch the next page once the current one lands, so paging forward is
  // instant. Bounded: only the single adjacent page.
  const total = query.data?.total;
  useEffect(() => {
    if (!enabled || total === undefined) return;
    if (page * pageSize >= total) return;
    queryClient.prefetchQuery({
      queryKey: [scope, filters, sort, pageSize, page + 1],
      queryFn: () => fetchPage(page + 1),
      staleTime: STALE_MS,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, total, page, pageSize, scope, queryClient]);

  return {
    rows: query.data?.rows ?? [],
    total: total ?? 0,
    error: query.error
      ? 'Failed to load data'
      : query.data?.error ?? null,
    /** True only when there is no data at all yet (initial load → skeletons). */
    isInitialLoading: query.isLoading,
    /** True during any fetch (spinner in the pagination bar). */
    isFetching: query.isFetching,
    refetch: query.refetch,
  };
}
