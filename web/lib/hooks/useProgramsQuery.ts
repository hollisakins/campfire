'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import type {
  ProgramsOverviewResult,
  ProgramDetailResult,
  ObservationsOverviewResult,
  DatabaseOverviewResult,
} from '@/lib/server/programs';

// The metadata page fires three of these on mount. As server actions they
// ran one after the other (Next serializes action POSTs per client); as GET
// routes they run in parallel and abort with the query (perf T2-C, #506).
// Keys carry no viewer identity — the QueryClient is cleared on sign-out.

export function useProgramsOverviewQuery(enabled: boolean = true) {
  return useQuery<ProgramsOverviewResult>({
    queryKey: ['programsOverview'],
    queryFn: ({ signal }) => fetchJson<ProgramsOverviewResult>('/api/metadata/programs', { signal }),
    staleTime: 10 * 60 * 1000, // 10 minutes - program stats rarely change
    enabled,
  });
}

export function useProgramDetailQuery(programSlug: string, enabled: boolean = true) {
  return useQuery<ProgramDetailResult>({
    queryKey: ['programDetail', programSlug],
    queryFn: ({ signal }) =>
      fetchJson<ProgramDetailResult>(`/api/metadata/programs/${encodeURIComponent(programSlug)}`, { signal }),
    staleTime: 10 * 60 * 1000,
    enabled: enabled && !!programSlug,
  });
}

export function useObservationsOverviewQuery(enabled: boolean = true) {
  return useQuery<ObservationsOverviewResult>({
    queryKey: ['observationsOverview'],
    queryFn: ({ signal }) => fetchJson<ObservationsOverviewResult>('/api/metadata/observations', { signal }),
    staleTime: 10 * 60 * 1000,
    enabled,
  });
}

export function useDatabaseOverviewQuery(enabled: boolean = true) {
  return useQuery<DatabaseOverviewResult>({
    queryKey: ['databaseOverview'],
    queryFn: ({ signal }) => fetchJson<DatabaseOverviewResult>('/api/metadata/overview', { signal }),
    staleTime: 10 * 60 * 1000,
    enabled,
  });
}
