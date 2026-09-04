'use client';

// The one client cache for spectrum sidecars (#500). Three components used to
// keep their own copies of the same JSON — a `useRef` Map in
// MultiSpectrumViewer, a module LRU in useSpectrumDataCache, none in
// RedshiftFitSummary — so an object page fetched each spectrum 2–3×. These
// TanStack queries are keyed on the FITS path.
//
// Where the bytes come from (perf T2-D2, #508): one `/api/spectrum/sidecars`
// call per spectrum resolves every sidecar to a delivery-front url (one
// access check per spectrum per page), and the payloads are fetched from the
// Worker directly — CORS-readable, edge-cached per content hash. The 1-D
// sidecar is a separate query from the full JSON so the primary trace paints
// before the 2-D S/N array lands. When the front is not configured the same
// queries fall back to /api/spectrum and /api/redshift-fit, which stream.

import { useEffect } from 'react';
import { useQuery, useQueries, useQueryClient, type QueryClient } from '@tanstack/react-query';
import type { SpectrumData, SpectrumData1D } from '@/app/api/spectrum/route';
import type { RedshiftFitData } from '@/app/api/redshift-fit/route';
import type { SpectrumSidecarUrls } from '@/app/api/spectrum/sidecars/route';

// Sidecars only change on a re-deploy, and the routes already let the browser
// keep them for a day; 30 min is long enough for a whole inspection session.
const SIDECAR_STALE_MS = 30 * 60 * 1000;
const SIDECAR_GC_MS = 30 * 60 * 1000;
// A front url is valid for at least one 6 h presign window.
const URLS_STALE_MS = 60 * 60 * 1000;

export const spectrumSidecarsKey = (fitsPath: string) => ['spectrum-sidecars', fitsPath] as const;
export const spectrumJsonKey = (fitsPath: string) => ['spectrum-json', fitsPath] as const;
export const spectrum1dKey = (fitsPath: string) => ['spectrum-1d', fitsPath] as const;
export const redshiftFitKey = (fitsPath: string) => ['redshift-fit', fitsPath] as const;

async function errorMessage(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json();
    if (body?.error) return body.error;
  } catch { /* non-JSON error body */ }
  return fallback;
}

export async function fetchSidecarUrls(fitsPath: string): Promise<SpectrumSidecarUrls> {
  const res = await fetch(`/api/spectrum/sidecars?path=${encodeURIComponent(fitsPath)}`);
  if (!res.ok) throw new Error(await errorMessage(res, 'Failed to resolve spectrum'));
  return res.json();
}

export function spectrumSidecarsQueryOptions(fitsPath: string) {
  return {
    queryKey: spectrumSidecarsKey(fitsPath),
    queryFn: () => fetchSidecarUrls(fitsPath),
    staleTime: URLS_STALE_MS,
    gcTime: URLS_STALE_MS,
  };
}

/** The resolved sidecar urls for a path, from the cache or one resolve call
 * shared by every sidecar query of the same path. */
function sidecarUrls(client: QueryClient, fitsPath: string): Promise<SpectrumSidecarUrls> {
  return client.fetchQuery(spectrumSidecarsQueryOptions(fitsPath));
}

async function fetchJsonFrom(url: string, fallback: string): Promise<Response> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(await errorMessage(res, fallback));
  return res;
}

export async function fetchSpectrumJson(client: QueryClient, fitsPath: string): Promise<SpectrumData> {
  const urls = await sidecarUrls(client, fitsPath);
  if (urls.front) {
    if (!urls.spectrum) throw new Error('Spectrum data not available');
    return (await fetchJsonFrom(urls.spectrum, 'Failed to load spectrum')).json();
  }
  return (await fetchJsonFrom(`/api/spectrum?path=${encodeURIComponent(fitsPath)}`, 'Failed to load spectrum')).json();
}

/** The 1-D payload. A spectrum deployed before the sidecar existed answers
 * with its full JSON (a superset), so this never fails where the full one
 * would succeed. */
export async function fetchSpectrum1d(client: QueryClient, fitsPath: string): Promise<SpectrumData1D> {
  const urls = await sidecarUrls(client, fitsPath);
  if (urls.front) {
    const url = urls.spectrum_1d ?? urls.spectrum;
    if (!url) throw new Error('Spectrum data not available');
    return (await fetchJsonFrom(url, 'Failed to load spectrum')).json();
  }
  return (await fetchJsonFrom(`/api/spectrum?path=${encodeURIComponent(fitsPath)}&include=1d`, 'Failed to load spectrum')).json();
}

/** `null` when no fit exists for the spectrum; throws on other failures. */
export async function fetchRedshiftFit(client: QueryClient, fitsPath: string): Promise<RedshiftFitData | null> {
  const urls = await sidecarUrls(client, fitsPath);
  if (urls.front) {
    if (!urls.zfit) return null;
    return (await fetchJsonFrom(urls.zfit, 'Failed to load redshift fit')).json();
  }
  const res = await fetch(`/api/redshift-fit?path=${encodeURIComponent(fitsPath)}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`Failed to load redshift fit (${res.status})`);
  return res.json();
}

export function spectrumJsonQueryOptions(client: QueryClient, fitsPath: string) {
  return {
    queryKey: spectrumJsonKey(fitsPath),
    queryFn: () => fetchSpectrumJson(client, fitsPath),
    staleTime: SIDECAR_STALE_MS,
    gcTime: SIDECAR_GC_MS,
  };
}

export function spectrum1dQueryOptions(client: QueryClient, fitsPath: string) {
  return {
    queryKey: spectrum1dKey(fitsPath),
    queryFn: () => fetchSpectrum1d(client, fitsPath),
    staleTime: SIDECAR_STALE_MS,
    gcTime: SIDECAR_GC_MS,
  };
}

export function redshiftFitQueryOptions(client: QueryClient, fitsPath: string) {
  return {
    queryKey: redshiftFitKey(fitsPath),
    queryFn: () => fetchRedshiftFit(client, fitsPath),
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

/** The full spectrum JSON (1-D + 2-D S/N). */
export function useSpectrumJson(fitsPath: string, enabled = true) {
  const client = useQueryClient();
  const query = useQuery({ ...spectrumJsonQueryOptions(client, fitsPath), enabled });
  useSpectrumCacheTrim(query.data);
  return query;
}

/** The 1-D sidecar — what paints first. */
export function useSpectrum1d(fitsPath: string, enabled = true) {
  const client = useQueryClient();
  return useQuery({ ...spectrum1dQueryOptions(client, fitsPath), enabled });
}

export function useRedshiftFit(fitsPath: string, enabled = true) {
  const client = useQueryClient();
  return useQuery({ ...redshiftFitQueryOptions(client, fitsPath), enabled });
}

/** One fit query per path, in order; `enabled[i]` false skips a fetch (the
 * row already carries the scalars the caller needs). */
export function useRedshiftFits(fitsPaths: string[], enabled?: boolean[]) {
  const client = useQueryClient();
  return useQueries({
    queries: fitsPaths.map((p, i) => ({
      ...redshiftFitQueryOptions(client, p),
      enabled: enabled ? enabled[i] : true,
    })),
  });
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
 * MAX_CACHED_SPECTRA (their 1-D, fit and url siblings go with them). Entries
 * a mounted component is reading, and fetches still in flight (a prefetch
 * has no observer and dataUpdatedAt 0 until it lands), are never evicted.
 */
export function trimSpectrumCache(queryClient: QueryClient): void {
  const cache = queryClient.getQueryCache();
  const idle = cache
    .findAll({ queryKey: ['spectrum-json'] })
    .filter((q) => q.getObserversCount() === 0 && q.state.fetchStatus === 'idle')
    .sort((a, b) => b.state.dataUpdatedAt - a.state.dataUpdatedAt);
  for (const q of idle.slice(MAX_CACHED_SPECTRA)) {
    const fitsPath = q.queryKey[1] as string;
    for (const key of [spectrumJsonKey(fitsPath), spectrum1dKey(fitsPath), redshiftFitKey(fitsPath), spectrumSidecarsKey(fitsPath)]) {
      queryClient.removeQueries({ queryKey: key, exact: true });
    }
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
      queryClient.prefetchQuery(spectrum1dQueryOptions(queryClient, p)),
      queryClient.prefetchQuery(spectrumJsonQueryOptions(queryClient, p)),
      queryClient.prefetchQuery(redshiftFitQueryOptions(queryClient, p)),
    ]),
  ).then(() => trimSpectrumCache(queryClient));
}
