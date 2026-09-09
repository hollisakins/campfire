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
  MAX_BANDS,
  loadFitsglConfig,
  trilogyLevels,
  type BandWeight,
  type FitsglConfig,
  type StretchMode,
  type TrilogyParams,
  type TrilogyStats,
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
  /** Dataset deployment stamp used to cache-bust descriptors and to prevent
   *  a render from being stored under a different asset-version snapshot. */
  datasetVersion: string;
}

/** Whether a resolved source belongs to the FitsGL snapshot folded into the
 * store key. A null source takes the legacy path, whose failure guard decides
 * whether that render is safe to persist. */
export function sourceMatchesDatasetVersion(
  source: Pick<FieldCutoutSource, 'datasetVersion'> | null,
  expected: string | undefined,
): boolean {
  return source === null || source.datasetVersion === expected;
}

/** Add the dataset deployment stamp to a descriptor URL. The public FitsGL
 * paths are overwritten in place, so URL-only Next caching would otherwise
 * serve the previous fitsgl.json/manifest.json for up to an hour after the
 * asset version already changed. */
export function versionedSourceUrl(raw: string, version: string): string {
  const url = new URL(raw);
  url.searchParams.set('__campfire_version', version);
  return url.toString();
}

function cachingFetchAtVersion(version: string): typeof fetch {
  return (input, init) => {
    const raw = typeof input === 'string'
      ? input
      : input instanceof URL
        ? input.toString()
        : input.url;
    const url = versionedSourceUrl(raw, version);
    const versionedInput = input instanceof Request ? new Request(url, input) : url;
    return fetch(versionedInput, { ...init, next: { revalidate: 3600 } });
  };
}

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
  const rgb = defaultRgbBands(config);
  if (rgb) return rgb;
  const { bands } = config.dataset;
  const dv = config.defaultView;
  const single = (dv.band && bands.find((b) => b.name === dv.band)) || bands[0];
  return [single];
}

/**
 * The dataset's default `[R, G, B]` triple: the producer's default view when it
 * names one, else reddest→R / middle→G / bluest→B by pivot wavelength (declaration
 * order when a producer omits pivots). `null` for a field with fewer than 3 bands.
 * Exported for the figure route's `rgb=auto`.
 */
export function defaultRgbBands(config: FitsglConfig): [FitsglBand, FitsglBand, FitsglBand] | null {
  const { bands } = config.dataset;
  const dv = config.defaultView;

  if (dv.mode === 'rgb' && dv.r && dv.g && dv.b) {
    const byName = new Map(bands.map((b) => [b.name, b]));
    const rgb = [dv.r, dv.g, dv.b].map((n) => byName.get(n));
    if (rgb.every(Boolean)) return rgb as [FitsglBand, FitsglBand, FitsglBand];
  }

  if (bands.length >= 3) {
    // Blue→red by pivot wavelength; producers omit pivotUm ⇒ declaration order.
    const ordered = bands.every((b) => b.pivotUm != null)
      ? [...bands].sort((a, b) => a.pivotUm! - b.pivotUm!)
      : bands;
    const mid = Math.floor((ordered.length - 1) / 2);
    return [ordered[ordered.length - 1], ordered[mid], ordered[0]];
  }
  return null;
}

/**
 * The producer's weighted composite — `defaultView.weights`, the full per-band
 * (R,G,B) contribution table the CAMPFIRE producer emits so the map opens on the
 * faithful multi-band trilogy rather than three representatives. Merged exactly
 * as the map's `trilogyComposite` (duplicates summed, declaration order kept),
 * names outside the inventory dropped, capped at the renderer's `MAX_BANDS`.
 * `null` when the producer declared none.
 */
export function defaultWeightedBands(
  config: FitsglConfig,
): Array<{ band: FitsglBand; weight: BandWeight }> | null {
  const weights = config.defaultView.weights;
  if (!weights || weights.length === 0) return null;
  const byName = new Map(config.dataset.bands.map((b) => [b.name, b]));
  const merged = new Map<string, [number, number, number]>();
  for (const { band, weight } of weights) {
    if (!byName.has(band)) continue;
    const cur = merged.get(band);
    if (cur === undefined) merged.set(band, [weight[0], weight[1], weight[2]]);
    else {
      cur[0] += weight[0];
      cur[1] += weight[1];
      cur[2] += weight[2];
    }
  }
  const entries = [...merged.entries()]
    .slice(0, MAX_BANDS)
    .map(([name, weight]) => ({ band: byName.get(name)!, weight: weight as BandWeight }));
  return entries.length > 0 ? entries : null;
}

/** The producer's trilogy knobs over the library defaults (see `displayDefaults`). */
export function producerTrilogyParams(config: FitsglConfig): TrilogyParams {
  const knobs = (config.defaultView as { trilogy?: Partial<TrilogyParams> }).trilogy;
  return { ...DEFAULT_TRILOGY_PARAMS, ...knobs };
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
  const params = producerTrilogyParams(config);
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
): Promise<{
  prefix: string;
  config: FitsglConfig;
  isPublic: boolean;
  sourceVersion: string;
} | null> {
  const { data: rows, error } = await supabase
    .from('fitsgl_datasets')
    .select('prefix, field, kind, tiles, bands, pixel_scale, fitsgl_json_url, is_default, deployed_at')
    .eq('field', field)
    .eq('kind', 'field');
  if (error) throw new Error(`fitsgl_datasets query failed for field ${field}: ${error.message}`);
  if (!rows || rows.length === 0) return null;
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
    throw new Error(`fitsgl_dataset_is_public failed for field ${field}: ${pubErr.message}`);
  }
  if (opts.requirePublic && !isPublic) return null;

  const sourceVersion = String(ds.deployed_at);
  const config = await loadFitsglConfig(ds.fitsgl_json_url, cachingFetchAtVersion(sourceVersion));
  return { prefix: ds.prefix, config, isPublic: Boolean(isPublic), sourceVersion };
}

/** Load a chosen band's manifest into an engine `BandSource`. */
async function toBandSource(band: FitsglBand, sourceVersion: string): Promise<BandSource> {
  const manifestUrl = band.tiles[0]; // absolute after loadFitsglConfig
  return {
    manifest: await loadManifest(manifestUrl, undefined, cachingFetchAtVersion(sourceVersion)),
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
  return (await resolveFieldCutoutSourceResult(supabase, field, opts)).source;
}

export interface FieldCutoutSourceResult {
  source: FieldCutoutSource | null;
  /** True when `source` is null because resolution FAILED (query, RPC,
   *  manifest fetch/parse), not because the field has no visible pyramid.
   *  The legacy fallback render is still right to serve, but not to store
   *  under the key a FitsGL render will later want (#509). */
  failed: boolean;
}

/** `resolveFieldCutoutSource`, keeping "no pyramid" and "could not tell"
 *  apart for callers that persist what they render. */
export async function resolveFieldCutoutSourceResult(
  supabase: SupabaseClient,
  field: string,
  opts: { requirePublic?: boolean } = {},
): Promise<FieldCutoutSourceResult> {
  try {
    const ds = await fetchFieldDataset(supabase, field, opts);
    if (!ds) return { source: null, failed: false };
    const chosen = chooseBands(ds.config);
    const bands = await Promise.all(chosen.map((band) => toBandSource(band, ds.sourceVersion)));
    return {
      source: {
        bands,
        bandNames: chosen.map((b) => b.name),
        nativeScaleArcsec: bands[0].manifest.levels[0].pixelScaleArcsec,
        display: displayDefaults(ds.config, chosen),
        isPublic: ds.isPublic,
        datasetVersion: ds.sourceVersion,
      },
      failed: false,
    };
  } catch (err) {
    console.error(`FitsGL cutout source unavailable for field ${field}:`, err);
    return { source: null, failed: true };
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

/** One band of a science source: engine input + inventory metadata. */
export interface ScienceBand extends BandSource {
  name: string;
  label?: string;
  pivotUm?: number;
  /** Precomputed trilogy stats (absent on older datasets ⇒ no trilogy composite). */
  trilogy?: TrilogyStats;
}

/** The composite a figure was asked for, resolved against the inventory. */
export interface CompositeSource {
  /** The `[R, G, B]` channel bands: an explicit triple, or the dataset's
   *  default (producer view / wavelength-ordered). Drives the simple
   *  shared-range composite, and the trilogy one when `weighted` is absent. */
  triple: [ScienceBand, ScienceBand, ScienceBand];
  /** The producer's full weighted band table (`rgb=auto` only): the faithful
   *  multi-band trilogy the map opens on. Each band stretched on its own
   *  levels, channels as weighted averages (`weightedTrilogyPixel`). */
  weighted?: { bands: ScienceBand[]; weights: BandWeight[] };
}

export interface FieldScienceSource {
  /** One entry per requested band, in request (or inventory) order. */
  bands: ScienceBand[];
  /** The composite when one was requested and the field can serve it. */
  rgb?: CompositeSource;
  /** Producer trilogy knobs over the library defaults — the composite's
   *  baseline, which a request may override knob by knob. */
  trilogyParams: TrilogyParams;
  /** Dataset prefix, for provenance headers. */
  datasetPrefix: string;
  /** Dataset deployment stamp used to cache-bust its descriptors. */
  datasetVersion: string;
}

/**
 * Resolve a field's *science* cutout source: every dataset band, or the
 * requested subset (case-insensitive names; `[]` ⇒ no single-band panels),
 * each with its manifest loaded. `rgb` asks for a composite triple as well —
 * three names, or `'auto'` for the dataset default (`rgb` stays undefined on
 * the result when the field has fewer than 3 bands). `null` ⇒ no (visible)
 * pyramid for the field; throws {@link UnknownBandError} for names not in the
 * inventory (a client error, not a fallback case).
 */
export async function resolveFieldScienceSource(
  supabase: SupabaseClient,
  field: string,
  opts: { requirePublic?: boolean; bands?: string[]; rgb?: 'auto' | string[] } = {},
): Promise<FieldScienceSource | null> {
  const ds = await fetchFieldDataset(supabase, field, opts).catch((err) => {
    console.error(`FitsGL science source unavailable for field ${field}:`, err);
    return null;
  });
  if (!ds) return null;

  const inventory = ds.config.dataset.bands;
  const byName = new Map(inventory.map((b) => [b.name.toLowerCase(), b]));
  const lookup = (names: string[]): FitsglBand[] => {
    const unknown = names.filter((n) => !byName.has(n.toLowerCase()));
    if (unknown.length > 0) throw new UnknownBandError(unknown, inventory.map((b) => b.name));
    return names.map((n) => byName.get(n.toLowerCase())!);
  };

  const chosen = opts.bands === undefined ? inventory : lookup(opts.bands);
  const rgbChosen =
    opts.rgb === undefined
      ? null
      : opts.rgb === 'auto'
        ? defaultRgbBands(ds.config)
        : (lookup(opts.rgb) as [FitsglBand, FitsglBand, FitsglBand]);

  // Each band's manifest loads once even when it serves both a panel and a channel.
  const loaded = new Map<string, Promise<ScienceBand>>();
  const load = (b: FitsglBand): Promise<ScienceBand> => {
    let p = loaded.get(b.name);
    if (!p) {
      p = toBandSource(b, ds.sourceVersion).then((src) => ({
        ...src,
        name: b.name,
        label: b.label,
        pivotUm: b.pivotUm,
        trilogy: b.stats?.trilogy,
      }));
      loaded.set(b.name, p);
    }
    return p;
  };

  const weightedChosen = opts.rgb === 'auto' && rgbChosen ? defaultWeightedBands(ds.config) : null;

  const [bands, triple, weightedBands] = await Promise.all([
    Promise.all(chosen.map(load)),
    rgbChosen ? Promise.all(rgbChosen.map(load)) : Promise.resolve(undefined),
    weightedChosen ? Promise.all(weightedChosen.map((e) => load(e.band))) : Promise.resolve(undefined),
  ]);
  let rgb: CompositeSource | undefined;
  if (triple) {
    rgb = { triple: triple as CompositeSource['triple'] };
    if (weightedBands && weightedChosen) {
      rgb.weighted = { bands: weightedBands, weights: weightedChosen.map((e) => e.weight) };
    }
  }
  return {
    bands,
    rgb,
    trilogyParams: producerTrilogyParams(ds.config),
    datasetPrefix: ds.prefix,
    datasetVersion: ds.sourceVersion,
  };
}
