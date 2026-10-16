'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import { useDrTag } from '@/lib/dr/client';
import type { NearbyObjectsResponse } from '@/app/api/objects/near/route';

export type { NearbyObject } from '@/app/api/objects/near/route';

// Redshift and quality change as neighbours are inspected; a minute is long
// enough to cover back-navigation within one object without going stale.
const NEARBY_STALE_MS = 60 * 1000;

/**
 * The nearest visible objects to a point (GET /api/objects/near), closest
 * first. Keyed on the query geometry only — never on the viewer (the
 * QueryClient is cleared on sign-out).
 */
export function useNearbyObjectsQuery(params: {
  ra: number;
  dec: number;
  radiusArcsec: number;
  limit: number;
  /** object_id to leave out of the result (the object being viewed). */
  exclude?: string;
  enabled?: boolean;
}) {
  const { ra, dec, radiusArcsec, limit, exclude, enabled = true } = params;
  const dr = useDrTag();
  return useQuery<NearbyObjectsResponse>({
    queryKey: ['nearbyObjects', ra, dec, radiusArcsec, limit, exclude ?? '', dr ?? 'live'],
    queryFn: ({ signal }) =>
      fetchJson<NearbyObjectsResponse>(
        `/api/objects/near?ra=${ra}&dec=${dec}&radius=${radiusArcsec}&limit=${limit}`
          + (exclude ? `&exclude=${encodeURIComponent(exclude)}` : ''),
        { signal, dr },
      ),
    enabled,
    staleTime: NEARBY_STALE_MS,
  });
}
