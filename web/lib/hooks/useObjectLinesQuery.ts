'use client';

import { useMemo } from 'react';
import { useQueries } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import type { ObjectLinesResponse } from '@/app/api/objects/lines/route';

export type { ObjectLineFit, ObjectLineRecord, ObjectLinesResponse } from '@/app/api/objects/lines/route';

// Fits change only on a deploy; a few minutes covers back-navigation.
const LINES_STALE_MS = 5 * 60 * 1000;

/** The route's per-request cap; an object with more member spectra is fetched in batches. */
export const LINES_REQUEST_BATCH = 50;

function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/**
 * The emission-line fits of a set of spectra (GET /api/objects/lines), in
 * batches of at most LINES_REQUEST_BATCH ids per request (the route's cap;
 * a cross-program object can carry more member spectra than that). Each
 * batch is its own cache entry keyed on its spectrum ids only — never on the
 * viewer (the QueryClient is cleared on sign-out) — and the batches are
 * merged here. `data` is undefined until every batch has resolved.
 */
export function useObjectLinesQuery(spectrumIds: number[], enabled = true) {
  const batches = useMemo(
    () => chunk([...new Set(spectrumIds)].sort((a, b) => a - b), LINES_REQUEST_BATCH).map((ids) => ids.join(',')),
    [spectrumIds],
  );
  const queries = useQueries({
    queries: batches.map((key) => ({
      queryKey: ['objectLines', key],
      queryFn: ({ signal }: { signal?: AbortSignal }) =>
        fetchJson<ObjectLinesResponse>(`/api/objects/lines?spectra=${key}`, { signal }),
      enabled: enabled && key.length > 0,
      staleTime: LINES_STALE_MS,
    })),
  });
  const signature = queries.map((q) => `${q.status}:${q.dataUpdatedAt}`).join('|');
  return useMemo(() => {
    const allDone = queries.length > 0 && queries.every((q) => q.isSuccess);
    const failed = queries.find((q) => q.isError);
    const data: ObjectLinesResponse | undefined = allDone
      ? { fits: queries.flatMap((q) => q.data?.fits ?? []) }
      : undefined;
    return {
      data,
      isPending: queries.length > 0 && queries.some((q) => q.isPending),
      error: failed ? (failed.error as Error) : null,
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature]);
}
