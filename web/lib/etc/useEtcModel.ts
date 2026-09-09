'use client';

import { useQuery } from '@tanstack/react-query';
import { fetchJson } from '@/lib/fetch-json';
import { ETC_MODELS_BASE, type ModelManifest, type NoiseModel } from './model';

/**
 * Load the latest bundled campfire-etc noise model from the static copies in
 * `public/etc/models/` (manifest → model file). Model files are immutable
 * versioned data, so the query never goes stale within a session.
 */
export function useEtcModel(version?: string) {
  return useQuery({
    queryKey: ['etcModel', version ?? 'latest'],
    queryFn: async ({ signal }) => {
      const manifest = await fetchJson<ModelManifest>(`${ETC_MODELS_BASE}/manifest.json`, { signal });
      const want = version ?? manifest.latest;
      const entry = manifest.versions.find((v) => v.version === want);
      if (!entry) throw new Error(`No bundled noise model version ${want}`);
      return fetchJson<NoiseModel>(`${ETC_MODELS_BASE}/${entry.file}`, { signal });
    },
    staleTime: Infinity,
    gcTime: Infinity,
  });
}
