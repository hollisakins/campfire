'use client';

// The one client cache for spectrum sidecars (#500). Three components used to
// keep their own copies of the same JSON — a `useRef` Map in
// MultiSpectrumViewer, a module LRU in useSpectrumDataCache, none in
// RedshiftFitSummary — so an object page fetched each spectrum 2–3×. These
// TanStack queries are keyed on the FITS path, which survives the later
// GET-route / content-addressed changes to how the bytes are served.

import { useQuery, useQueries, type QueryClient } from '@tanstack/react-query';
import type { SpectrumData } from '@/app/api/spectrum/route';
import type { RedshiftFitData } from '@/app/api/redshift-fit/route';

// Sidecars only change on a re-deploy, and the routes already let the browser
// keep them for a day; 30 min is long enough for a whole inspection session.
const SIDECAR_STALE_MS = 30 * 60 * 1000;
const SIDECAR_GC_MS = 30 * 60 * 1000;

export const spectrumJsonKey = (fitsPath: string) => ['spectrum-json', fitsPath] as const;
export const redshiftFitKey = (fitsPath: string) => ['redshift-fit', fitsPath] as const;

export async function fetchSpectrumJson(fitsPath: string): Promise<SpectrumData> {
  const res = await fetch(`/api/spectrum?path=${encodeURIComponent(fitsPath)}`);
  if (!res.ok) {
    let message = 'Failed to load spectrum';
    try {
      const body = await res.json();
      if (body?.error) message = body.error;
    } catch { /* non-JSON error body */ }
    throw new Error(message);
  }
  return res.json();
}

/** `null` when no fit exists for the spectrum (404); throws on other failures. */
export async function fetchRedshiftFit(fitsPath: string): Promise<RedshiftFitData | null> {
  const res = await fetch(`/api/redshift-fit?path=${encodeURIComponent(fitsPath)}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Failed to load redshift fit (${res.status})`);
  return res.json();
}

export function spectrumJsonQueryOptions(fitsPath: string) {
  return {
    queryKey: spectrumJsonKey(fitsPath),
    queryFn: () => fetchSpectrumJson(fitsPath),
    staleTime: SIDECAR_STALE_MS,
    gcTime: SIDECAR_GC_MS,
  };
}

export function redshiftFitQueryOptions(fitsPath: string) {
  return {
    queryKey: redshiftFitKey(fitsPath),
    queryFn: () => fetchRedshiftFit(fitsPath),
    staleTime: SIDECAR_STALE_MS,
    gcTime: SIDECAR_GC_MS,
  };
}

export function useSpectrumJson(fitsPath: string, enabled = true) {
  return useQuery({ ...spectrumJsonQueryOptions(fitsPath), enabled });
}

export function useRedshiftFit(fitsPath: string, enabled = true) {
  return useQuery({ ...redshiftFitQueryOptions(fitsPath), enabled });
}

/** One fit query per path, in order. */
export function useRedshiftFits(fitsPaths: string[]) {
  return useQueries({ queries: fitsPaths.map((p) => redshiftFitQueryOptions(p)) });
}

/**
 * Warm the cache for every spectrum of an object (inspection mode: instant
 * tab switching and prev/next). Already-fresh entries are skipped.
 */
export function prefetchSpectrumSidecars(
  queryClient: QueryClient,
  fitsPaths: string[],
): Promise<void> {
  return Promise.all(
    fitsPaths.flatMap((p) => [
      queryClient.prefetchQuery(spectrumJsonQueryOptions(p)),
      queryClient.prefetchQuery(redshiftFitQueryOptions(p)),
    ]),
  ).then(() => undefined);
}
