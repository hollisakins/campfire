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
import { fetchJson } from '@/lib/fetch-json';

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

export function fetchSidecarUrls(fitsPath: string): Promise<SpectrumSidecarUrls> {
  return fetchJson<SpectrumSidecarUrls>(`/api/spectrum/sidecars?path=${encodeURIComponent(fitsPath)}`);
}

export function spectrumSidecarsQueryOptions(fitsPath: string) {
  return {
    queryKey: spectrumSidecarsKey(fitsPath),
    queryFn: () => fetchSidecarUrls(fitsPath),
    staleTime: URLS_STALE_MS,
    gcTime: URLS_STALE_MS,
    // A failed resolve degrades to the streaming routes (sidecarUrls), which
    // run their own access check; the default three retries would hold every
    // sidecar of the spectrum for ~7 s before that fallback starts.
    retry: 1,
  };
}

const NO_FRONT: SpectrumSidecarUrls = { front: false, spectrum: null, spectrum_1d: null, zfit: null, has_1d: null, has_zfit: null };

/** The resolved sidecar urls for a path, from the cache or one resolve call
 * shared by every sidecar query of the same path. A failed resolve is not a
 * failed spectrum: it answers as "front off", and the streaming routes (which
 * run their own access check) decide. */
async function sidecarUrls(client: QueryClient, fitsPath: string): Promise<SpectrumSidecarUrls> {
  try {
    return await client.fetchQuery(spectrumSidecarsQueryOptions(fitsPath));
  } catch (err) {
    console.warn('sidecar resolve failed; falling back to the streaming routes', err);
    return NO_FRONT;
  }
}

async function fetchJsonFrom(url: string, fallback: string): Promise<Response> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(await errorMessage(res, fallback));
  return res;
}

/**
 * Fetch a sidecar from its front url when there is one and it answers, else
 * from the app route (which streams the same bytes). A front url is null for
 * reasons other than "no such product" — front off, registry row not active
 * yet, presign failed (frontUrlsFor's contract) — so null is never read as
 * absence here; only the route's own answer decides that.
 */
async function fetchSidecar(frontUrl: string | null, routeUrl: string, fallback: string): Promise<Response> {
  if (frontUrl) {
    try {
      const res = await fetch(frontUrl);
      if (res.ok) return res;
      console.warn(`sidecar front answered ${res.status}; falling back to ${routeUrl}`);
    } catch (err) {
      console.warn(`sidecar front fetch failed; falling back to ${routeUrl}`, err);
    }
  }
  return fetchJsonFrom(routeUrl, fallback);
}

export async function fetchSpectrumJson(client: QueryClient, fitsPath: string): Promise<SpectrumData> {
  const urls = await sidecarUrls(client, fitsPath);
  const res = await fetchSidecar(
    urls.front ? urls.spectrum : null,
    `/api/spectrum?path=${encodeURIComponent(fitsPath)}`,
    'Failed to load spectrum',
  );
  return res.json();
}

/** The 1-D payload. A spectrum deployed before the sidecar existed answers
 * with its full JSON (a superset), so this never fails where the full one
 * would succeed. */
export async function fetchSpectrum1d(client: QueryClient, fitsPath: string): Promise<SpectrumData1D> {
  const urls = await sidecarUrls(client, fitsPath);
  const res = await fetchSidecar(
    urls.front ? (urls.spectrum_1d ?? urls.spectrum) : null,
    `/api/spectrum?path=${encodeURIComponent(fitsPath)}&include=1d`,
    'Failed to load spectrum',
  );
  return res.json();
}

/** `null` when no fit exists for the spectrum — the resolve's definitive
 * `has_zfit: false` (no round trip), else the route's 404; throws on other
 * failures. A null front url alone is never read as absence. */
export async function fetchRedshiftFit(client: QueryClient, fitsPath: string): Promise<RedshiftFitData | null> {
  const urls = await sidecarUrls(client, fitsPath);
  if (urls.has_zfit === false) return null;
  if (urls.front && urls.zfit) {
    try {
      const res = await fetch(urls.zfit);
      if (res.ok) return res.json();
      console.warn(`zfit front answered ${res.status}; falling back to /api/redshift-fit`);
    } catch (err) {
      console.warn('zfit front fetch failed; falling back to /api/redshift-fit', err);
    }
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

/** The 1-D sidecar — what paints first. Trims too: for a spectrum whose
 * 1-D answer is the full payload the full query never runs, so this is the
 * only landing that would bound the cache. */
export function useSpectrum1d(fitsPath: string, enabled = true) {
  const client = useQueryClient();
  const query = useQuery({ ...spectrum1dQueryOptions(client, fitsPath), enabled });
  useSpectrumCacheTrim(query.data);
  return query;
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
 * Drop the oldest unobserved, settled spectrum paths beyond
 * MAX_CACHED_SPECTRA (full JSON, 1-D, fit and url entries go together).
 * Paths a mounted component is reading, and fetches still in flight (a
 * prefetch has no observer and dataUpdatedAt 0 until it lands), are never
 * evicted. Counts 1-D-only paths too: MultiSpectrumViewer never creates a
 * full-JSON entry.
 */
export function trimSpectrumCache(queryClient: QueryClient): void {
  const cache = queryClient.getQueryCache();
  // A path is a candidate only when none of its payload entries (full or 1-D)
  // is observed or in flight; its recency is its latest landed payload.
  const latest = new Map<string, number>();
  const busy = new Set<string>();
  for (const prefix of ['spectrum-json', 'spectrum-1d']) {
    for (const q of cache.findAll({ queryKey: [prefix] })) {
      const fitsPath = q.queryKey[1] as string;
      if (q.getObserversCount() > 0 || q.state.fetchStatus !== 'idle') {
        busy.add(fitsPath);
        continue;
      }
      latest.set(fitsPath, Math.max(latest.get(fitsPath) ?? 0, q.state.dataUpdatedAt));
    }
  }
  const idle = [...latest.entries()]
    .filter(([p]) => !busy.has(p))
    .sort((a, b) => b[1] - a[1]);
  for (const [fitsPath] of idle.slice(MAX_CACHED_SPECTRA)) {
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
    fitsPaths.map(async (p) => {
      const urls = await queryClient.fetchQuery(spectrumSidecarsQueryOptions(p)).catch(() => null);
      await Promise.all([
        queryClient.prefetchQuery(spectrum1dQueryOptions(queryClient, p)),
        // The full payload is a separate download only when the 1-D sidecar
        // is a distinct object; otherwise the 1-D query already carries it.
        fullPayloadIsSeparate(urls)
          ? queryClient.prefetchQuery(spectrumJsonQueryOptions(queryClient, p))
          : Promise.resolve(),
        queryClient.prefetchQuery(redshiftFitQueryOptions(queryClient, p)),
      ]);
    }),
  ).then(() => trimSpectrumCache(queryClient));
}

/**
 * Whether fetching the full spectrum JSON would download anything the 1-D
 * query does not already deliver. A spectrum deployed before the 1-D sidecar
 * existed (`has_1d` false) answers the 1-D query with its full payload, so a
 * second download of the same bytes is pure waste — front on or off. When
 * the resolve failed or the registry did not answer (`has_1d` null) the
 * client cannot tell, and the full query runs.
 */
export function fullPayloadIsSeparate(urls: SpectrumSidecarUrls | null | undefined): boolean {
  return urls?.has_1d !== false;
}

/** The resolved sidecar urls as a query (shared with every sidecar fetch). */
export function useSpectrumSidecarUrls(fitsPath: string, enabled = true) {
  return useQuery({ ...spectrumSidecarsQueryOptions(fitsPath), enabled });
}
