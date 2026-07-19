// Per-field cutout dispatch (epic #337, Phase 5): resolve a field's deployed
// FitsGL tile pyramid into the `BandSource[]` the cutout engine consumes. A field
// with a `kind='field'` row in `fitsgl_datasets` renders display cutouts from the
// FITS pyramid; a `null` return means "no (visible) pyramid" and the caller falls
// back to the legacy PNG-tile compositing (retired per-field in the follow-up PR).
//
// Kept free of `sharp`/route coupling so the science-FITS route can share it;
// PNG-flattening display helpers live in `./display`.

import {
  DEFAULT_TRILOGY_PARAMS,
  loadFitsglConfig,
  trilogyLevels,
  type FitsglConfig,
  type StretchMode,
  type TrilogyParams,
} from '@fitsgl/core';
import type { SupabaseClient } from '@supabase/supabase-js';
import { loadManifest } from './manifest';
import type { BandSource } from './index';
import type { Limits } from './render';

type FitsglBand = FitsglConfig['dataset']['bands'][number];

/** The producer's default stretch, resolved for the chosen display bands so a
 *  server cutout opens on the same transfer the map does. */
export interface DisplayStretchDefaults {
  stretch: StretchMode;
  /** Per-band display interval, aligned with the chosen bands (trilogy only:
   *  `[x0, x2]` from the precomputed stats + knobs). Absent ⇒ auto percentile. */
  limits?: Limits[];
  /** Per-band trilogy softening `k`, aligned with `limits` (trilogy only). */
  trilogyK?: number[];
}

export interface FieldCutoutSource {
  /** 1 band (single-band colormap) or 3 ordered `[R, G, B]` (composite). */
  bands: BandSource[];
  /** Band names matching `bands`, for labeling/provenance. */
  bandNames: string[];
  /** Native (finest-level) pixel scale in arcsec/px, for native-size defaults. */
  nativeScaleArcsec: number;
  /** Producer default stretch for `bands`, or `null` (engine default: asinh + auto). */
  display: DisplayStretchDefaults | null;
  /** Whether every backing mosaic is published (`fitsgl_dataset_is_public`).
   *  `false` ⇒ this render is admin-only: the response must NOT be
   *  shared-cacheable (`private, no-store`), or a CDN keyed only on the URL
   *  would replay draft imagery to non-admins. */
  isPublic: boolean;
}

/** `fetch` with Next data-cache revalidation, so a re-deployed dataset's
 *  `fitsgl.json`/`manifest.json` refresh within an hour (mirrors the legacy
 *  PNG tile fetcher's `revalidate: 3600`). */
const cachingFetch: typeof fetch = (input, init) =>
  fetch(input, { ...init, next: { revalidate: 3600 } });

/**
 * Pick the display bands from a dataset inventory.
 *
 * Producer default view first (`mode:'rgb'` with named channels); otherwise a
 * wavelength-ordered composite — reddest→R, middle→G, bluest→B — when the field
 * has ≥3 bands, else the default single band. Grid-group compatibility is NOT
 * required here (unlike the live viewer's composite): every band reprojects
 * independently onto the same North-up output grid.
 */
function chooseBands(config: FitsglConfig): FitsglBand[] {
  const { bands } = config.dataset;
  const dv = config.defaultView;

  if (dv.mode === 'rgb' && dv.r && dv.g && dv.b) {
    const byName = new Map(bands.map((b) => [b.name, b]));
    const rgb = [dv.r, dv.g, dv.b].map((n) => byName.get(n));
    if (rgb.every(Boolean)) return rgb as FitsglBand[];
  }

  if (bands.length >= 3) {
    // Blue→red by pivot wavelength; producers omit pivotUm ⇒ declaration order.
    const ordered = bands.every((b) => b.pivotUm != null)
      ? [...bands].sort((a, b) => a.pivotUm! - b.pivotUm!)
      : bands;
    const mid = Math.floor((ordered.length - 1) / 2);
    return [ordered[ordered.length - 1], ordered[mid], ordered[0]];
  }

  const single = (dv.band && bands.find((b) => b.name === dv.band)) || bands[0];
  return [single];
}

/**
 * Resolve the producer's default stretch for the chosen bands (exported for
 * tests). Trilogy needs precomputed `stats.trilogy` on EVERY chosen band —
 * levels derive server-side from those stats + the producer's knobs, exactly
 * as the map's `applyTrilogy` does, so cutout and map match by construction.
 * Any band missing stats ⇒ `null` (engine default: asinh + auto percentile),
 * matching the viewer's own fallback. Producer knobs (`defaultView.trilogy`)
 * arrive with @fitsgl/core ≥ 0.3.0 — an older validator strips the key and we
 * fall back to the library defaults.
 */
export function displayDefaults(
  config: FitsglConfig,
  chosen: FitsglBand[],
): DisplayStretchDefaults | null {
  const dv = config.defaultView;
  const mode = dv.stretch?.mode;
  if (mode === undefined) return null;
  if (mode !== 'trilogy') return { stretch: mode };
  const stats = chosen.map((b) => b.stats?.trilogy);
  if (stats.some((s) => s === undefined)) return null;
  const knobs = (dv as { trilogy?: Partial<TrilogyParams> }).trilogy;
  const params: TrilogyParams = { ...DEFAULT_TRILOGY_PARAMS, ...knobs };
  const levels = stats.map((s) => trilogyLevels(s!, params));
  return {
    stretch: 'trilogy',
    limits: levels.map((l) => ({ lo: l.x0, hi: l.x2 })),
    trilogyK: levels.map((l) => l.k),
  };
}

/**
 * Fetch the field's default `kind='field'` dataset row + its resolved
 * `fitsgl.json`, or `null` when the field has no (visible) pyramid.
 *
 * `requirePublic` is for service-role callers, which bypass RLS: it mirrors the
 * `authenticated_select_fitsgl_datasets` policy via the same SECURITY DEFINER
 * `fitsgl_dataset_is_public()` check, so an unpublished-backed dataset never
 * serves public/API cutouts. User-scoped clients rely on RLS instead (admins
 * intentionally see draft-backed datasets, matching the map).
 */
async function fetchFieldDataset(
  supabase: SupabaseClient,
  field: string,
  opts: { requirePublic?: boolean },
): Promise<{ prefix: string; config: FitsglConfig; isPublic: boolean } | null> {
  const { data: rows, error } = await supabase
    .from('fitsgl_datasets')
    .select('prefix, field, kind, tiles, bands, pixel_scale, fitsgl_json_url, is_default')
    .eq('field', field)
    .eq('kind', 'field');
  if (error || !rows || rows.length === 0) return null;
  const ds = rows.find((r) => r.is_default) ?? rows[0];

  // Publicity is always evaluated (not only under requirePublic): callers that
  // may render draft-backed pyramids (admin RLS / admin API) need it to pick a
  // non-shared cache policy for the response.
  const { data: isPublic, error: pubErr } = await supabase.rpc('fitsgl_dataset_is_public', {
    p_field: ds.field,
    p_tiles: ds.tiles,
    p_bands: ds.bands,
    p_pixel_scale: ds.pixel_scale,
  });
  if (pubErr) {
    console.error(`fitsgl_dataset_is_public failed for field ${field}:`, pubErr);
    return null;
  }
  if (opts.requirePublic && !isPublic) return null;

  const config = await loadFitsglConfig(ds.fitsgl_json_url, cachingFetch);
  return { prefix: ds.prefix, config, isPublic: Boolean(isPublic) };
}

/** Load a chosen band's manifest into an engine `BandSource`. */
async function toBandSource(band: FitsglBand): Promise<BandSource> {
  const manifestUrl = band.tiles[0]; // absolute after loadFitsglConfig
  return {
    manifest: await loadManifest(manifestUrl, undefined, cachingFetch),
    baseUrl: new URL('.', manifestUrl).toString(),
  };
}

/**
 * Resolve a field's *display* cutout source (the PR-B PNG routes), or `null`
 * when the field has no (visible) pyramid — including on any fetch/parse
 * failure, so callers can fall back to the legacy path during the transition.
 */
export async function resolveFieldCutoutSource(
  supabase: SupabaseClient,
  field: string,
  opts: { requirePublic?: boolean } = {},
): Promise<FieldCutoutSource | null> {
  try {
    const ds = await fetchFieldDataset(supabase, field, opts);
    if (!ds) return null;
    const chosen = chooseBands(ds.config);
    const bands = await Promise.all(chosen.map(toBandSource));
    return {
      bands,
      bandNames: chosen.map((b) => b.name),
      nativeScaleArcsec: bands[0].manifest.levels[0].pixelScaleArcsec,
      display: displayDefaults(ds.config, chosen),
      isPublic: ds.isPublic,
    };
  } catch (err) {
    console.error(`FitsGL cutout source unavailable for field ${field}:`, err);
    return null;
  }
}

/** Requested band(s) not in the dataset — the science routes turn this into a 400. */
export class UnknownBandError extends Error {
  constructor(
    public readonly unknown: string[],
    public readonly available: string[],
  ) {
    super(`unknown band(s) ${unknown.join(', ')}; available: ${available.join(', ')}`);
    this.name = 'UnknownBandError';
  }
}

export interface FieldScienceSource {
  /** One entry per requested band, in request (or inventory) order. */
  bands: Array<BandSource & { name: string; label?: string; pivotUm?: number }>;
  /** Dataset prefix, for provenance headers. */
  datasetPrefix: string;
}

/**
 * Resolve a field's *science* cutout source: every dataset band, or the
 * requested subset (case-insensitive names), each with its manifest loaded.
 * `null` ⇒ no (visible) pyramid for the field; throws {@link UnknownBandError}
 * for names not in the inventory (a client error, not a fallback case).
 */
export async function resolveFieldScienceSource(
  supabase: SupabaseClient,
  field: string,
  opts: { requirePublic?: boolean; bands?: string[] } = {},
): Promise<FieldScienceSource | null> {
  const ds = await fetchFieldDataset(supabase, field, opts).catch((err) => {
    console.error(`FitsGL science source unavailable for field ${field}:`, err);
    return null;
  });
  if (!ds) return null;

  const inventory = ds.config.dataset.bands;
  let chosen = inventory;
  if (opts.bands && opts.bands.length > 0) {
    const byName = new Map(inventory.map((b) => [b.name.toLowerCase(), b]));
    const unknown = opts.bands.filter((n) => !byName.has(n.toLowerCase()));
    if (unknown.length > 0) throw new UnknownBandError(unknown, inventory.map((b) => b.name));
    chosen = opts.bands.map((n) => byName.get(n.toLowerCase())!);
  }

  const bands = await Promise.all(
    chosen.map(async (b) => ({
      ...(await toBandSource(b)),
      name: b.name,
      label: b.label,
      pivotUm: b.pivotUm,
    })),
  );
  return { bands, datasetPrefix: ds.prefix };
}
