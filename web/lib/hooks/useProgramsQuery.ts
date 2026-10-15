'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import { useDrTag } from '@/lib/dr/client';
import type { ProgramsOverviewResponse } from '@/app/api/metadata/programs/route';
import type { ProgramDetailResponse } from '@/app/api/metadata/programs/[slug]/route';
import type { ObservationsOverviewResponse } from '@/app/api/metadata/observations/route';
import type { DatabaseOverviewResponse } from '@/app/api/metadata/overview/route';

// The metadata page fires three of these on mount. As server actions they
// ran one after the other (Next serializes action POSTs per client); as GET
// routes they run in parallel and abort with the query (perf T2-C, #506).
// Keys carry no viewer identity — the QueryClient is cleared on sign-out.

export function useProgramsOverviewQuery(enabled: boolean = true) {
  const dr = useDrTag();
  return useQuery<ProgramsOverviewResponse>({
    queryKey: ['programsOverview', dr ?? 'live'],
    queryFn: ({ signal }) => fetchJson<ProgramsOverviewResponse>('/api/metadata/programs', { signal, dr }),
    staleTime: 10 * 60 * 1000, // 10 minutes - program stats rarely change
    enabled,
  });
}

export function useProgramDetailQuery(programSlug: string, enabled: boolean = true) {
  const dr = useDrTag();
  return useQuery<ProgramDetailResponse>({
    queryKey: ['programDetail', programSlug, dr ?? 'live'],
    queryFn: ({ signal }) =>
      fetchJson<ProgramDetailResponse>(`/api/metadata/programs/${encodeURIComponent(programSlug)}`, { signal, dr }),
    staleTime: 10 * 60 * 1000,
    enabled: enabled && !!programSlug,
  });
}

export function useObservationsOverviewQuery(enabled: boolean = true) {
  const dr = useDrTag();
  return useQuery<ObservationsOverviewResponse>({
    queryKey: ['observationsOverview', dr ?? 'live'],
    queryFn: ({ signal }) => fetchJson<ObservationsOverviewResponse>('/api/metadata/observations', { signal, dr }),
    staleTime: 10 * 60 * 1000,
    enabled,
  });
}

export function useDatabaseOverviewQuery(enabled: boolean = true) {
  const dr = useDrTag();
  return useQuery<DatabaseOverviewResponse>({
    queryKey: ['databaseOverview', dr ?? 'live'],
    queryFn: ({ signal }) => fetchJson<DatabaseOverviewResponse>('/api/metadata/overview', { signal, dr }),
    staleTime: 10 * 60 * 1000,
    enabled,
  });
}
