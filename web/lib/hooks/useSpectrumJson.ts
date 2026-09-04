'use client';

// The one client cache for spectrum sidecars (#500). Three components used to
// keep their own copies of the same JSON — a `useRef` Map in
// MultiSpectrumViewer, a module LRU in useSpectrumDataCache, none in
// RedshiftFitSummary — so an object page fetched each spectrum 2–3×. These
// TanStack queries are keyed on the FITS path, which survives the later
// GET-route / content-addressed changes to how the bytes are served.

import { useEffect } from 'react';
import { useQuery, useQueries, useQueryClient, type QueryClient } from '@tanstack/react-query';
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

/** Trim the cache whenever `trigger` changes (a fetch landed). */
export function useSpectrumCacheTrim(trigger: unknown): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    trimSpectrumCache(queryClient);
  }, [queryClient, trigger]);
}

export function useSpectrumJson(fitsPath: string, enabled = true) {
  const query = useQuery({ ...spectrumJsonQueryOptions(fitsPath), enabled });
  useSpectrumCacheTrim(query.data);
  return query;
}

export function useRedshiftFit(fitsPath: string, enabled = true) {
  return useQuery({ ...redshiftFitQueryOptions(fitsPath), enabled });
}

/** One fit query per path, in order. */
export function useRedshiftFits(fitsPaths: string[]) {
  return useQueries({ queries: fitsPaths.map((p) => redshiftFitQueryOptions(p)) });
}

/** Cap on cached spectrum payloads (the 2-D S/N array dominates; ~0.5 MB
 *  each). Browsing objects and the inspection prefetch (every grating of
 *  the current, next and previous object) would otherwise hold every
 *  spectrum visited for gcTime. Same bound the module LRU this replaced had;
 *  enforced from every path that fills the cache (the hooks below and the
 *  inspection prefetch). */
const MAX_CACHED_SPECTRA = 24;

/**
 * Drop the oldest unobserved, settled spectrum entries beyond
 * MAX_CACHED_SPECTRA (their fit siblings go with them). Entries a mounted
 * component is reading, and fetches still in flight (a prefetch has no
 * observer and dataUpdatedAt 0 until it lands), are never evicted.
 */
export function trimSpectrumCache(queryClient: QueryClient): void {
  const cache = queryClient.getQueryCache();
  const idle = cache
    .findAll({ queryKey: ['spectrum-json'] })
    .filter((q) => q.getObserversCount() === 0 && q.state.fetchStatus === 'idle')
    .sort((a, b) => b.state.dataUpdatedAt - a.state.dataUpdatedAt);
  for (const q of idle.slice(MAX_CACHED_SPECTRA)) {
    const fitsPath = q.queryKey[1] as string;
    queryClient.removeQueries({ queryKey: spectrumJsonKey(fitsPath), exact: true });
    queryClient.removeQueries({ queryKey: redshiftFitKey(fitsPath), exact: true });
  }
}

/**
 * Warm the cache for every spectrum of an object (inspection mode: instant
 * tab switching and prev/next). Already-fresh entries are skipped; the cache
 * is trimmed to MAX_CACHED_SPECTRA idle entries afterwards.
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
  ).then(() => trimSpectrumCache(queryClient));
}
