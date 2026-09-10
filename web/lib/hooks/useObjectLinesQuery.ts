'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import type { ObjectLinesResponse } from '@/app/api/objects/lines/route';

export type { ObjectLineFit, ObjectLineRecord, ObjectLinesResponse } from '@/app/api/objects/lines/route';

// Fits change only on a deploy; a few minutes covers back-navigation.
const LINES_STALE_MS = 5 * 60 * 1000;

/**
 * The emission-line fits of a set of spectra (GET /api/objects/lines).
 * Keyed on the spectrum ids only — never on the viewer (the QueryClient is
 * cleared on sign-out).
 */
export function useObjectLinesQuery(spectrumIds: number[], enabled = true) {
  const key = [...spectrumIds].sort((a, b) => a - b).join(',');
  return useQuery<ObjectLinesResponse>({
    queryKey: ['objectLines', key],
    queryFn: ({ signal }) => fetchJson<ObjectLinesResponse>(`/api/objects/lines?spectra=${key}`, { signal }),
    enabled: enabled && spectrumIds.length > 0,
    staleTime: LINES_STALE_MS,
  });
}
