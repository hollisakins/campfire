'use client';

import { useQuery } from '@tanstack/react-query';
import {
  getProgramsOverview,
  getProgramDetail,
  getObservationsOverview,
  getDatabaseOverview,
} from '@/lib/actions/programs';
import type {
  ProgramsOverviewResult,
  ProgramDetailResult,
  ObservationsOverviewResult,
  DatabaseOverviewResult,
} from '@/lib/actions/programs';

export function useProgramsOverviewQuery(enabled: boolean = true) {
  return useQuery<ProgramsOverviewResult>({
    queryKey: ['programsOverview'],
    queryFn: getProgramsOverview,
    staleTime: 10 * 60 * 1000, // 10 minutes - program stats rarely change
    enabled,
  });
}

export function useProgramDetailQuery(programSlug: string, enabled: boolean = true) {
  return useQuery<ProgramDetailResult>({
    queryKey: ['programDetail', programSlug],
    queryFn: () => getProgramDetail(programSlug),
    staleTime: 10 * 60 * 1000,
    enabled: enabled && !!programSlug,
  });
}

export function useObservationsOverviewQuery(enabled: boolean = true) {
  return useQuery<ObservationsOverviewResult>({
    queryKey: ['observationsOverview'],
    queryFn: getObservationsOverview,
    staleTime: 10 * 60 * 1000,
    enabled,
  });
}

export function useDatabaseOverviewQuery(enabled: boolean = true) {
  return useQuery<DatabaseOverviewResult>({
    queryKey: ['databaseOverview'],
    queryFn: getDatabaseOverview,
    staleTime: 10 * 60 * 1000,
    enabled,
  });
}
