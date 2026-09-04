'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import type { NearbyShuttersResponse } from '@/app/api/shutters/route';

// Geometry only changes on a deployment, which also changes `version`.
const SHUTTERS_STALE_MS = 24 * 60 * 60 * 1000;

/**
 * Shutters within `fov` arcsec of a point (GET /api/shutters), for the
 * cutout overlay. Keyed on the coordinates + fov + asset version, never on
 * the viewer: the QueryClient is cleared on sign-out (AuthContext), and the
 * route's `private` cache header keeps the bytes out of shared caches.
 * Disabled until all coordinates are known.
 */
export function useNearbyShuttersQuery(params: {
  field?: string;
  ra?: number;
  dec?: number;
  fov: number;
  /** Asset version token (useAssetVersion) — part of the URL and the key. */
  version?: string;
  enabled?: boolean;
}) {
  const { field, ra, dec, fov, version, enabled = true } = params;
  const ready = field !== undefined && ra !== undefined && dec !== undefined;
  return useQuery<NearbyShuttersResponse>({
    queryKey: ['nearbyShutters', field, ra, dec, fov, version ?? ''],
    queryFn: ({ signal }) =>
      fetchJson<NearbyShuttersResponse>(
        `/api/shutters?ra=${ra}&dec=${dec}&field=${encodeURIComponent(field!)}&fov=${fov}`
          + (version ? `&v=${encodeURIComponent(version)}` : ''),
        { signal },
      ),
    enabled: enabled && ready,
    staleTime: SHUTTERS_STALE_MS,
  });
}
