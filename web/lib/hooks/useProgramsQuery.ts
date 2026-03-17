'use client';

import { useQuery } from '@tanstack/react-query';
import { getProgramsOverview, getProgramDetail } from '@/lib/actions/programs';
import type { ProgramsOverviewResult, ProgramDetailResult } from '@/lib/actions/programs';

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
