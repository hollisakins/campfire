'use client';

import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { getPinnedObjectsMetadata } from '@/lib/actions/spectra';
import type { PinnedObject, PinnedObjectMetadata } from '@/lib/types';

/**
 * Resolve display metadata for pinned objects under the viewer's own RLS
 * scope. Pins in preferences are bare references (see PinnedObject); this
 * hook fetches field/redshift/quality for the bucket cards. Pins the viewer
 * can't access are simply absent from the returned map.
 */
export function usePinnedMetadataQuery(pins: PinnedObject[], enabled = true) {
  const key = pins.map(p => `${p.route}:${p.target_id}`).sort().join(',');

  return useQuery<{ metadata: Record<string, PinnedObjectMetadata>; isAuthenticated: boolean }>({
    queryKey: ['pinned-metadata', key],
    queryFn: () => getPinnedObjectsMetadata(pins.map(({ target_id, route }) => ({ target_id, route }))),
    enabled: enabled && pins.length > 0,
    staleTime: 5 * 60 * 1000,
    placeholderData: keepPreviousData,
  });
}
