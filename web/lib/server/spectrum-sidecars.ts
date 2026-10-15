import 'server-only';

import type { SupabaseClient } from '@supabase/supabase-js';
import { QueryClient, dehydrate, type DehydratedState } from '@tanstack/react-query';
import { deriveSibling } from '@/lib/layout';
import { cdnFrontBase, frontUrlsForResolved } from '@/lib/server/cdn-front';
import { resolveObjectBackends, type ResolvedObject } from '@/lib/r2';
import { releaseFrontUrls, resolveReleaseMembers } from '@/lib/server/dr-files';
import { spectrumSidecarsKey, type SpectrumSidecarUrls } from '@/lib/spectrum-sidecars';

type SidecarKeys = [json: string, json1d: string, zfit: string, lines: string];

/** The release a page is browsing: its manifest is read with the viewer's client. */
export interface SidecarRelease {
  supabase: SupabaseClient;
  tag: string;
}

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
export async function resolveSpectrumSidecars(
  fitsPaths: string[],
  release?: SidecarRelease | null,
): Promise<Map<string, SpectrumSidecarUrls>> {
  const front = cdnFrontBase() !== null;
  const unknown = (): SpectrumSidecarUrls => ({
    front, spectrum: null, spectrum_1d: null, zfit: null, lines: null, has_1d: null, has_zfit: null, has_lines: null,
  });
  const out = new Map<string, SpectrumSidecarUrls>();

  const keysByPath = new Map<string, SidecarKeys>();
  for (const p of new Set(fitsPaths)) {
    try {
      keysByPath.set(p, [
        deriveSibling(p, 'spectrum_json'),
        deriveSibling(p, 'spectrum_1d_json'),
        deriveSibling(p, 'zfit'),
        deriveSibling(p, 'nirspec_lines_json'),
      ]);
    } catch (err) {
      console.warn(`spectrum sidecars: cannot derive sidecar keys for ${p}:`, err);
      out.set(p, unknown());
    }
  }
  if (keysByPath.size === 0) return out;

  if (release) {
    // Under a release (P6c-2) a sidecar is the release's ARCHIVED copy of the
    // member, or nothing: a pending member is definitively unavailable
    // (`has_*: false`, no url), never the live file. The streaming routes
    // make the same decision for the no-front fallback.
    return resolveReleaseSidecars(keysByPath, release, out, front);
  }

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
  for (const [p, [jsonKey, json1dKey, zfitKey, linesKey]] of keysByPath) {
    const [json, json1d, zfit, lines] = resolved.slice(i, i + 4);
    i += 4;
    // The resolver fails open with no content identity for ANY key, so "no
    // row" is only believed when the full JSON — which every deployed
    // spectrum registers — did resolve.
    const presence = (o: ResolvedObject | undefined) => (o?.contentHash ? true : json?.contentHash ? false : null);
    out.set(p, {
      front,
      spectrum: urls.get(jsonKey) ?? null,
      spectrum_1d: urls.get(json1dKey) ?? null,
      zfit: urls.get(zfitKey) ?? null,
      lines: urls.get(linesKey) ?? null,
      has_spectrum: json?.contentHash ? true : null,
      has_1d: presence(json1d),
      has_zfit: presence(zfit),
      has_lines: presence(lines),
    });
  }
  return out;
}

async function resolveReleaseSidecars(
  keysByPath: Map<string, SidecarKeys>,
  release: SidecarRelease,
  out: Map<string, SpectrumSidecarUrls>,
  front: boolean,
): Promise<Map<string, SpectrumSidecarUrls>> {
  const keys = [...keysByPath.values()].flat();
  try {
    const members = await resolveReleaseMembers(release.supabase, release.tag, keys);
    const urls = await releaseFrontUrls(members);
    for (const [p, [jsonKey, json1dKey, zfitKey, linesKey]] of keysByPath) {
      const available = (k: string) => members.get(k) != null;
      out.set(p, {
        front,
        // `front: true` with a null url for an available member means the
        // front is off or the mint failed: the client streams the archive
        // copy through the app route (which resolves the manifest again).
        spectrum: available(jsonKey) ? urls.get(jsonKey) ?? null : null,
        spectrum_1d: available(json1dKey) ? urls.get(json1dKey) ?? null : null,
        zfit: available(zfitKey) ? urls.get(zfitKey) ?? null : null,
        lines: available(linesKey) ? urls.get(linesKey) ?? null : null,
        has_spectrum: available(jsonKey),
        has_1d: available(json1dKey),
        has_zfit: available(zfitKey),
        has_lines: available(linesKey),
      });
    }
  } catch (err) {
    console.error('spectrum sidecars: release manifest resolve failed', err);
    // Nothing is handed out: a release never falls back to live files.
    for (const p of keysByPath.keys()) {
      out.set(p, { front, spectrum: null, spectrum_1d: null, zfit: null, lines: null, has_spectrum: false, has_1d: false, has_zfit: false, has_lines: false });
    }
  }
  return out;
}

/** Whether the release holds the spectrum's own JSON (the plot's payload);
 * the page omits the plot of a member whose copy is not archived yet. */
export function releaseSpectrumAvailable(urls: SpectrumSidecarUrls | undefined): boolean {
  if (!urls) return false;
  // The archived full JSON is what plots; with the front off its url is null,
  // so the presence flag decides (an archived member without a 1-D sidecar
  // still plots through the streaming route).
  return urls.has_spectrum === true || urls.spectrum !== null || urls.spectrum_1d !== null || urls.has_1d === true;
}

/**
 * The resolved urls as TanStack hydration state. The object page renders it
 * through a <HydrationBoundary> around the client tree, so every sidecar
 * query of those paths (MultiSpectrumViewer, SpectrumPlot, RedshiftFitSummary,
 * the inspection prefetch) finds `['spectrum-sidecars', path]` already
 * settled and fetches the payload straight away. Under a release the entries
 * carry the tag (lib/spectrum-sidecars keys).
 */
export function dehydrateSidecarUrls(sidecars: Map<string, SpectrumSidecarUrls>, dr?: string | null): DehydratedState {
  const client = new QueryClient();
  for (const [fitsPath, urls] of sidecars) client.setQueryData(spectrumSidecarsKey(fitsPath, dr), urls);
  const state = dehydrate(client);
  client.clear();
  return state;
}
