'use client';

import { useQuery } from '@tanstack/react-query';
import { getFieldObjectMarkers } from '@/lib/actions/map';
import type { MapObjectMarker } from '@/lib/actions/map';

export function useFieldObjectMarkers(field: string | undefined) {
  return useQuery<MapObjectMarker[]>({
    queryKey: ['fieldObjectMarkers', field],
    queryFn: async () => {
      const result = await getFieldObjectMarkers(field!);
      if (result.error) throw new Error(result.error);
      return result.markers;
    },
    enabled: !!field,
    staleTime: 10 * 60 * 1000, // 10 minutes — map markers rarely change
  });
}
