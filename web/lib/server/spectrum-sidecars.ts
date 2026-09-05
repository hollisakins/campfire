import 'server-only';

import { QueryClient, dehydrate, type DehydratedState } from '@tanstack/react-query';
import { deriveSibling } from '@/lib/layout';
import { cdnFrontBase, frontUrlsForResolved } from '@/lib/server/cdn-front';
import { resolveObjectBackends, type ResolvedObject } from '@/lib/r2';
import { spectrumSidecarsKey, type SpectrumSidecarUrls } from '@/lib/spectrum-sidecars';

type SidecarKeys = [json: string, json1d: string, zfit: string];

/**
 * Resolve every sidecar of a batch of spectra to delivery-front urls in ONE
 * registry resolution (perf T2-D2 #508, batched for T2-E #510): the object
 * page calls this once for all member spectra during the server render, so
 * the browser's first spectrum request is the 1-D payload itself — no
 * per-spectrum /api/spectrum/sidecars round trip after hydration. The route
 * calls it for one path.
 *
 * Authorization is the CALLER's: pass only paths the viewer may read (the
 * page's member spectra come from an RLS-filtered query; the route checks the
 * `spectra` row first). Never throws: a path the layout cannot derive
 * siblings for, or a registry failure, answers with the front flag and no
 * urls (`has_*: null`), and the client's streaming fallbacks decide.
 */
export async function resolveSpectrumSidecars(fitsPaths: string[]): Promise<Map<string, SpectrumSidecarUrls>> {
  const front = cdnFrontBase() !== null;
  const unknown = (): SpectrumSidecarUrls => ({ front, spectrum: null, spectrum_1d: null, zfit: null, has_1d: null, has_zfit: null });
  const out = new Map<string, SpectrumSidecarUrls>();

  const keysByPath = new Map<string, SidecarKeys>();
  for (const p of new Set(fitsPaths)) {
    try {
      keysByPath.set(p, [
        deriveSibling(p, 'spectrum_json'),
        deriveSibling(p, 'spectrum_1d_json'),
        deriveSibling(p, 'zfit'),
      ]);
    } catch (err) {
      console.warn(`spectrum sidecars: cannot derive sidecar keys for ${p}:`, err);
      out.set(p, unknown());
    }
  }
  if (keysByPath.size === 0) return out;

  const keys = [...keysByPath.values()].flat();
  let resolved: ResolvedObject[];
  let urls: Map<string, string | null>;
  try {
    // One registry resolution (memoized, chunked) serves both the front urls
    // and the presence flags.
    resolved = await resolveObjectBackends(keys);
    urls = await frontUrlsForResolved(keys, resolved);
  } catch (err) {
    console.error('spectrum sidecars: resolve failed; the client falls back to the streaming routes', err);
    for (const p of keysByPath.keys()) out.set(p, unknown());
    return out;
  }

  let i = 0;
  for (const [p, [jsonKey, json1dKey, zfitKey]] of keysByPath) {
    const [json, json1d, zfit] = resolved.slice(i, i + 3);
    i += 3;
    // The resolver fails open with no content identity for ANY key, so "no
    // row" is only believed when the full JSON — which every deployed
    // spectrum registers — did resolve.
    const presence = (o: ResolvedObject | undefined) => (o?.contentHash ? true : json?.contentHash ? false : null);
    out.set(p, {
      front,
      spectrum: urls.get(jsonKey) ?? null,
      spectrum_1d: urls.get(json1dKey) ?? null,
      zfit: urls.get(zfitKey) ?? null,
      has_1d: presence(json1d),
      has_zfit: presence(zfit),
    });
  }
  return out;
}

/**
 * The resolved urls as TanStack hydration state. The object page renders it
 * through a <HydrationBoundary> around the client tree, so every sidecar
 * query of those paths (MultiSpectrumViewer, SpectrumPlot, RedshiftFitSummary,
 * the inspection prefetch) finds `['spectrum-sidecars', path]` already
 * settled and fetches the payload straight away.
 */
export function dehydrateSidecarUrls(sidecars: Map<string, SpectrumSidecarUrls>): DehydratedState {
  const client = new QueryClient();
  for (const [fitsPath, urls] of sidecars) client.setQueryData(spectrumSidecarsKey(fitsPath), urls);
  const state = dehydrate(client);
  client.clear();
  return state;
}
