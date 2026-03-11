'use client';

import { useQuery } from '@tanstack/react-query';
import { getFieldMarkers } from '@/lib/actions/map';
import type { MapMarker } from '@/lib/actions/map';

export function useFieldMarkers(field: string | undefined) {
  return useQuery<MapMarker[]>({
    queryKey: ['fieldMarkers', field],
    queryFn: async () => {
      const result = await getFieldMarkers(field!);
      if (result.error) throw new Error(result.error);
      return result.markers;
    },
    enabled: !!field,
    staleTime: 10 * 60 * 1000, // 10 minutes — map markers rarely change
  });
}
