'use client';

import { useQuery, keepPreviousData } from '@tanstack/react-query';
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

  return useQuery<PaginatedSpectraResult>({
    queryKey: ['spectra', { filters, page, pageSize, sortColumn, sortDirection }],
    queryFn: () => getSpectra(filters, page, pageSize, sortColumn, sortDirection),
    enabled,
    placeholderData: keepPreviousData,
  });
}
