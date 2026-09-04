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
// and the default FitsGL field dataset's prefix, deploy time, backing mosaic
// hashes and publish state. The global
// token additionally folds in the latest NIRSpec deployment time, because
// `/api/shutters` (the cutout's shutter overlay, browser-cached for a day
// under the same token — #506) changes only when an observation is deployed.
// Server-only: reads via the service client and memoizes in the Next data
// cache for five minutes, so a redeployed field's thumbnails change URL
// within that window.
//
// Exposure: the per-field map (`byField`) is keyed by field name and covers
// every field in the DB, draft-backed datasets included — it must stay on the
// server (og-image metadata uses it). Client components only ever receive the
// `global` token (`clientAssetVersion`), which says "something changed" and
// nothing else; any redeploy therefore refreshes every thumbnail URL, which
// is the right trade for a browser-private cache.

import { createHash } from 'node:crypto';
import { unstable_cache } from 'next/cache';
import { createServiceClient } from '@/lib/supabase/server';

export interface AssetVersions {
  /** Short version token per field (lower-case field key as stored in the DB).
   *  SERVER-ONLY: names every field, including admin-only draft datasets. */
  byField: Record<string, string>;
  /** Combined token over every field — the only part safe to send to clients. */
  global: string;
}

/** The client-safe projection: just the global token. */
export interface ClientAssetVersion {
  global: string;
}

export function clientAssetVersion(versions: AssetVersions): ClientAssetVersion {
  return { global: versions.global };
}

function shortHash(input: string): string {
  return createHash('sha1').update(input).digest('hex').slice(0, 10);
}

async function computeAssetVersions(): Promise<AssetVersions> {
  const supabase = createServiceClient();
  const [{ data: layers }, { data: datasets }, { data: latestDeploy }] = await Promise.all([
    supabase.from('map_layers').select('field, filter, tile_version'),
    supabase
      .from('fitsgl_datasets')
      .select('field, prefix, deployed_at, source_hashes, is_default, tiles, bands, pixel_scale')
      .eq('kind', 'field'),
    // Shutter geometry only changes with a deployment (see header).
    supabase
      .from('deployments')
      .select('deployed_at')
      .order('deployed_at', { ascending: false })
      .limit(1)
      .maybeSingle(),
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
    // source_hashes (the backing mosaics' content hashes) tracks the
    // pyramid's pixels; deployed_at is stamped by every FitsGL deploy (#509)
    // so a rebuild with the same mosaics but a changed fitsgl.json (band
    // selection, stretch, trilogy knobs) still moves the version. The
    // publish state is part of it too: a publish or unpublish of the backing
    // mosaics changes what a render of this field may show, and the cutout
    // store keys on this version (#509).
    const { data: isPublic, error: pubErr } = await supabase.rpc('fitsgl_dataset_is_public', {
      p_field: ds.field,
      p_tiles: ds.tiles,
      p_bands: ds.bands,
      p_pixel_scale: ds.pixel_scale,
    });
    if (pubErr) console.error(`fitsgl_dataset_is_public failed for field ${field}:`, pubErr);
    const publicity = pubErr ? 'unknown' : String(Boolean(isPublic));
    push(field, `fitsgl:${ds.prefix}:${ds.deployed_at}:${JSON.stringify(ds.source_hashes ?? null)}:public=${publicity}`);
  }

  const versions: Record<string, string> = {};
  const fields = [...inputs.keys()].sort();
  for (const field of fields) {
    versions[field] = shortHash(inputs.get(field)!.sort().join('|'));
  }
  const global = shortHash(
    fields.map((f) => `${f}=${versions[f]}`).join('|') + `|deploy=${latestDeploy?.deployed_at ?? ''}`,
  );
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
