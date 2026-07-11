// FitsGL per-band pyramid manifest (`<band>/manifest.json`) — the shared
// contract between the pipeline producer (`fitsgl-py`), the browser viewer
// (`@fitsgl/core`), and this server-side cutout engine (epic #337, Phase 5).
//
// Field names mirror `fitsgl.manifest` (Python) so the TS addressing in `plan.ts`
// is a faithful port; a golden parity test (`plan.test.ts`) pins them to
// `fitsgl-py`'s `plan_cutout` so the two never drift.

import { parseWcs, type TanWcs } from '@fitsgl/core';

/** A standalone `.fits.fz` rectangle within a level, in the level's tile grid. */
export interface SupertileInfo {
  filename: string;
  /** Tile-grid origin `[tile_x0, tile_y0]` of this supertile within the level. */
  tileOrigin: readonly [number, number];
  /** Tile-grid extent `[n_tiles_x, n_tiles_y]` of this supertile. */
  tileCount: readonly [number, number];
}

export interface LevelInfo {
  /** Pyramid level index (0 = finest / native). */
  z: number;
  /** The single-file `.fits.fz` for a v1 level (== `supertiles[0].filename`). */
  filename: string;
  compression: string;
  lossless: boolean;
  /** Level pixel dimensions `[H, W]` (row, col). */
  shape: readonly [number, number];
  /** `[n_tiles_y, n_tiles_x]` — note the y-major order (matches `fitsgl-py`). */
  fpackTileCount: readonly [number, number];
  pixelScaleArcsec: number;
  /** Flat FITS WCS header dict for this level; feed to `parseWcs`. */
  wcs: Record<string, unknown>;
  supertiles: SupertileInfo[];
}

export interface Manifest {
  version: number;
  sourceFile?: string;
  /** Native (z=0) dimensions `[H, W]`. */
  nativeShape: readonly [number, number];
  /** The fpack tile edge (px) every level is tiled at; the unit of tile grids. */
  fpackTileSize: number;
  levels: LevelInfo[];
}

function pair(v: unknown, what: string): readonly [number, number] {
  if (!Array.isArray(v) || v.length < 2) throw new Error(`manifest: ${what} is not a [a, b] pair`);
  return [Number(v[0]), Number(v[1])];
}

/** Parse a raw `manifest.json` object into a typed {@link Manifest}. */
export function parseManifest(json: unknown): Manifest {
  const m = json as Record<string, unknown>;
  const rawLevels = m.levels;
  if (!Array.isArray(rawLevels) || rawLevels.length === 0) throw new Error('manifest has no levels');
  const levels: LevelInfo[] = rawLevels.map((lv) => {
    const l = lv as Record<string, unknown>;
    const supertiles = (l.supertiles as unknown[]).map((st) => {
      const s = st as Record<string, unknown>;
      return {
        filename: String(s.filename),
        tileOrigin: pair(s.tile_origin, 'supertile.tile_origin'),
        tileCount: pair(s.tile_count, 'supertile.tile_count'),
      } satisfies SupertileInfo;
    });
    return {
      z: Number(l.z),
      filename: String(l.filename),
      compression: String(l.compression),
      lossless: Boolean(l.lossless),
      shape: pair(l.shape, 'level.shape'),
      fpackTileCount: pair(l.fpack_tile_count, 'level.fpack_tile_count'),
      pixelScaleArcsec: Number(l.pixel_scale_arcsec),
      wcs: (l.wcs ?? {}) as Record<string, unknown>,
      supertiles,
    } satisfies LevelInfo;
  });
  return {
    version: Number(m.version ?? 0),
    sourceFile: m.source_file ? String(m.source_file) : undefined,
    nativeShape: pair(m.native_shape, 'native_shape'),
    fpackTileSize: Number(m.fpack_tile_size),
    levels,
  };
}

/** Fetch + parse a band `manifest.json` from a URL. */
export async function loadManifest(
  url: string,
  signal?: AbortSignal,
  fetchImpl: typeof fetch = fetch,
): Promise<Manifest> {
  const res = await fetchImpl(url, { signal });
  if (!res.ok) throw new Error(`manifest fetch failed (${res.status}) for ${url}`);
  return parseManifest(await res.json());
}

/** Parse a level's flat WCS header dict into a `@fitsgl/core` `TanWcs`. */
export function levelWcs(level: LevelInfo): TanWcs {
  const wcs = parseWcs(level.wcs);
  if (!wcs) throw new Error(`level z=${level.z} has no usable TAN/ICRS WCS`);
  return wcs;
}
