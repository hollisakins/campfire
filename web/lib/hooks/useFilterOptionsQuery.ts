'use client';

import { useQuery } from '@tanstack/react-query';
import { getFilterOptions } from '@/lib/actions/spectra';
import type { FilterOptionsResult } from '@/lib/actions/spectra';

export function useFilterOptionsQuery(enabled: boolean = true) {
  return useQuery<FilterOptionsResult>({
    queryKey: ['filterOptions'],
    queryFn: getFilterOptions,
    staleTime: 10 * 60 * 1000,  // 10 minutes - filter options rarely change
    enabled,
  });
}
