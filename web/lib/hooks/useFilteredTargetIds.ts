'use client';

import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { getFilteredTargetIds } from '@/lib/actions/map';
import type { FilterOptions } from '@/lib/actions/filter-params';

export function useFilteredTargetIds(
  filters: FilterOptions,
  hasActiveFilters: boolean
) {
  return useQuery({
    queryKey: ['filteredTargetIds', filters],
    queryFn: () => getFilteredTargetIds(filters),
    enabled: hasActiveFilters,
    placeholderData: keepPreviousData,
    staleTime: 30 * 1000, // 30 seconds
  });
}
