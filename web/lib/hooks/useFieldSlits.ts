'use client';

import { useQuery } from '@tanstack/react-query';
import { getFieldShutters, getFieldSlits } from '@/lib/actions/map';
import type { SlitRegion, Shutter } from '@/lib/actions/map';

export function useFieldSlits(field: string | undefined) {
  return useQuery<(SlitRegion | Shutter)[]>({
    queryKey: ['fieldSlits', field],
    queryFn: async () => {
      // Try shutters table first, fall back to legacy slit_regions
      const shuttersResult = await getFieldShutters(field!);
      if (shuttersResult.error) throw new Error(shuttersResult.error);
      if (shuttersResult.shutters.length > 0) return shuttersResult.shutters;

      const slitsResult = await getFieldSlits(field!);
      if (slitsResult.error) throw new Error(slitsResult.error);
      return slitsResult.slits;
    },
    enabled: !!field,
    staleTime: 10 * 60 * 1000, // 10 minutes — shutter data rarely changes
  });
}
