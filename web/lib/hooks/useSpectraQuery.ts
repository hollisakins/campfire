'use client';

import { useEffect } from 'react';
import { useQuery, useQueryClient, keepPreviousData } from '@tanstack/react-query';
import { getSpectra } from '@/lib/actions/spectra';
import type { FilterOptions, PaginatedSpectraResult } from '@/lib/actions/spectra';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';

export interface UseSpectraQueryParams {
  filters: Partial<FilterOptions>;
  page: number;
  pageSize: number;
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  enabled?: boolean;
}

export function useSpectraQuery(params: UseSpectraQueryParams) {
  const { filters, page, pageSize, sortColumn, sortDirection, enabled = true } = params;
  const queryClient = useQueryClient();

  const query = useQuery<PaginatedSpectraResult>({
    queryKey: ['spectra', { filters, page, pageSize, sortColumn, sortDirection }],
    queryFn: () => getSpectra(filters, page, pageSize, sortColumn, sortDirection),
    enabled,
    placeholderData: keepPreviousData,
  });

  // Prefetch adjacent pages for instant navigation
  useEffect(() => {
    if (!query.data || !enabled) return;

    const totalPages = query.data.totalPages;
    const isComplete = query.data.isComplete;

    // Only prefetch when in server-side pagination mode (not when we have full dataset)
    if (isComplete) return;

    // Prefetch next page
    if (page < totalPages) {
      queryClient.prefetchQuery({
        queryKey: ['spectra', { filters, page: page + 1, pageSize, sortColumn, sortDirection }],
        queryFn: () => getSpectra(filters, page + 1, pageSize, sortColumn, sortDirection),
        staleTime: 30 * 1000, // Consider fresh for 30 seconds
      });
    }

    // Prefetch previous page
    if (page > 1) {
      queryClient.prefetchQuery({
        queryKey: ['spectra', { filters, page: page - 1, pageSize, sortColumn, sortDirection }],
        queryFn: () => getSpectra(filters, page - 1, pageSize, sortColumn, sortDirection),
        staleTime: 30 * 1000,
      });
    }
  }, [query.data, page, pageSize, filters, sortColumn, sortDirection, enabled, queryClient]);

  return query;
}
