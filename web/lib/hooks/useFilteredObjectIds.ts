'use client';

import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { getFilteredObjectIds } from '@/lib/actions/map';
import type { AdvancedFilterOptions } from '@/components/spectra/SpectraFilterBar';

export function useFilteredObjectIds(
  filters: AdvancedFilterOptions,
  hasActiveFilters: boolean
) {
  return useQuery({
    queryKey: ['filteredObjectIds', filters],
    queryFn: () => getFilteredObjectIds(filters),
    enabled: hasActiveFilters,
    placeholderData: keepPreviousData,
    staleTime: 30 * 1000, // 30 seconds
  });
}
