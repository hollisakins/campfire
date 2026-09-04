// Per-field imaging asset versions (perf audit 2026-09, T1-1 / #497).
//
// Cutout URLs (`/api/tile-thumbnail`, `/api/og-image`) are keyed by object or
// target id, and their responses are cached for a week. Neither the browser
// cache nor the edge has any invalidation path when a field's tiles or FitsGL
// pyramid are re-deployed, so the URL itself must change: callers append
// `&v=<version>` from this module, exactly as `MapViewer` already does for
// leaflet tiles (`?v=${tile_version}`).
//
// The version is an opaque short hash over everything that can change what a
// cutout of that field renders: every `map_layers.tile_version` for the field
// and the default FitsGL field dataset's prefix + deploy time. Hashing (rather
// than exposing the prefix) means a draft-backed dataset visible only to
// admins leaks nothing but "something changed". Server-only: reads via the
// service client and memoizes in the Next data cache for five minutes, so a
// redeployed field's thumbnails change URL within that window.

import { createHash } from 'node:crypto';
import { unstable_cache } from 'next/cache';
import { createServiceClient } from '@/lib/supabase/server';

export interface AssetVersions {
  /** Short version token per field (lower-case field key as stored in the DB). */
  byField: Record<string, string>;
  /** Combined token over every field — for callers that don't know the field. */
  global: string;
}

function shortHash(input: string): string {
  return createHash('sha1').update(input).digest('hex').slice(0, 10);
}

async function computeAssetVersions(): Promise<AssetVersions> {
  const supabase = createServiceClient();
  const [{ data: layers }, { data: datasets }] = await Promise.all([
    supabase.from('map_layers').select('field, filter, tile_version'),
    supabase
      .from('fitsgl_datasets')
      .select('field, prefix, deployed_at, is_default')
      .eq('kind', 'field'),
  ]);

  // field → sorted list of version inputs
  const inputs = new Map<string, string[]>();
  const push = (field: string, s: string) => {
    const list = inputs.get(field);
    if (list) list.push(s);
    else inputs.set(field, [s]);
  };
  for (const l of layers ?? []) push(l.field, `layer:${l.filter}:${l.tile_version}`);
  // Mirror lib/cutout/source.ts: the default field dataset, else the first.
  const byField = new Map<string, typeof datasets>();
  for (const d of datasets ?? []) {
    const list = byField.get(d.field) ?? [];
    list.push(d);
    byField.set(d.field, list);
  }
  for (const [field, rows] of byField) {
    const ds = rows!.find((r) => r.is_default) ?? rows![0];
    push(field, `fitsgl:${ds.prefix}:${ds.deployed_at}`);
  }

  const versions: Record<string, string> = {};
  const fields = [...inputs.keys()].sort();
  for (const field of fields) {
    versions[field] = shortHash(inputs.get(field)!.sort().join('|'));
  }
  const global = shortHash(fields.map((f) => `${f}=${versions[f]}`).join('|'));
  return { byField: versions, global };
}

/**
 * Current imaging asset versions, memoized for five minutes. Never throws: a
 * lookup failure yields empty versions, and callers then omit `v=` (today's
 * behaviour) rather than failing a page render over a cache-key nicety.
 */
export async function getAssetVersions(): Promise<AssetVersions> {
  try {
    return await cachedAssetVersions();
  } catch (err) {
    console.error('asset versions unavailable:', err);
    return { byField: {}, global: '' };
  }
}

const cachedAssetVersions = unstable_cache(computeAssetVersions, ['asset-versions'], {
  revalidate: 300,
  tags: ['asset-versions'],
});

/** Version token for one field, falling back to the global token. */
export function assetVersionFor(versions: AssetVersions, field?: string | null): string {
  return (field && versions.byField[field]) || versions.global;
}
