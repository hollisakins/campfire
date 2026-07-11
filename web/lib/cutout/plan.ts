// Read-side cutout addressing (epic #337, Phase 5) — a faithful TypeScript port of
// `fitsgl.tiles` + `fitsgl.cutout.plan_cutout` (the Python producer's read API).
// Given a band manifest + a sky center/FOV/output size it picks the pyramid level,
// projects the sky box to that level's pixel grid, and resolves the covering fpack
// tiles to their supertile files (+ supertile-local coords). Pure geometry: the
// only external dependency is `@fitsgl/core`'s astropy-validated TAN `skyToPix`.
//
// A golden parity test (`plan.test.ts`) pins every output against `fitsgl-py` so
// the port cannot silently drift from the browser/producer.

import { skyToPix } from '@fitsgl/core';
import { levelWcs, type LevelInfo, type Manifest, type SupertileInfo } from './manifest';

export type Rounding = 'nearest' | 'finer' | 'coarser';

export interface TileCoord {
  level: number;
  tileX: number;
  tileY: number;
}

/** Half-open pixel rectangle `[x0, x1) × [y0, y1)` in a level's grid. */
export interface PixelBBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface TileRef {
  tile: TileCoord;
  supertile: SupertileInfo;
  supertileIndex: number;
  /** Tile coords local to the supertile's own grid (what the `.fits.fz` addresses by). */
  localX: number;
  localY: number;
}

export interface CutoutPlan {
  levelIndex: number;
  level: LevelInfo;
  outputScaleArcsec: number;
  pixelBbox: PixelBBox;
  tiles: TileRef[];
  /** Covering tiles that fall in a dropped-supertile (all-NaN) gap → caller NaN-fills. */
  missing: TileCoord[];
}

export const bboxWidth = (b: PixelBBox): number => Math.max(0, b.x1 - b.x0);
export const bboxHeight = (b: PixelBBox): number => Math.max(0, b.y1 - b.y0);
export const bboxIsEmpty = (b: PixelBBox): boolean => b.x1 <= b.x0 || b.y1 <= b.y0;

/** `[n_tiles_x, n_tiles_y]` for a level (unpacks `fpackTileCount` = `[n_ty, n_tx]`). */
function levelTileGrid(level: LevelInfo): [number, number] {
  const [nTy, nTx] = level.fpackTileCount;
  return [nTx, nTy];
}

/** The supertile of `level` holding global tile `(tileX, tileY)`, or null in a gap. */
export function resolveSupertile(level: LevelInfo, tileX: number, tileY: number): TileRef | null {
  for (let index = 0; index < level.supertiles.length; index++) {
    const st = level.supertiles[index];
    const [tx0, ty0] = st.tileOrigin;
    const [snx, sny] = st.tileCount;
    if (tx0 <= tileX && tileX < tx0 + snx && ty0 <= tileY && tileY < ty0 + sny) {
      return {
        tile: { level: level.z, tileX, tileY },
        supertile: st,
        supertileIndex: index,
        localX: tileX - tx0,
        localY: tileY - ty0,
      };
    }
  }
  return null;
}

/** Every tile of `level` overlapping the half-open pixel box, row-major (y outer). */
export function tilesForPixelBbox(level: LevelInfo, bbox: PixelBBox, tileSize: number): TileCoord[] {
  const [nTx, nTy] = levelTileGrid(level);
  const h = level.shape[0];
  const w = level.shape[1];

  const x0 = Math.max(0, bbox.x0);
  const y0 = Math.max(0, bbox.y0);
  const x1 = Math.min(w, bbox.x1);
  const y1 = Math.min(h, bbox.y1);
  if (x1 <= x0 || y1 <= y0) return [];

  const tx0 = Math.floor(x0 / tileSize);
  const ty0 = Math.floor(y0 / tileSize);
  // x1/y1 are exclusive: the last covered pixel is x1-1, so a box ending exactly on
  // a tile boundary does not pull in the next, untouched column.
  const tx1 = Math.min(nTx - 1, Math.floor((x1 - 1) / tileSize));
  const ty1 = Math.min(nTy - 1, Math.floor((y1 - 1) / tileSize));

  const tiles: TileCoord[] = [];
  for (let ty = ty0; ty <= ty1; ty++) {
    for (let tx = tx0; tx <= tx1; tx++) {
      tiles.push({ level: level.z, tileX: tx, tileY: ty });
    }
  }
  return tiles;
}

function levelsByZ(manifest: Manifest): LevelInfo[] {
  if (manifest.levels.length === 0) throw new Error('manifest has no levels');
  const levels = [...manifest.levels].sort((a, b) => a.z - b.z);
  for (const lvl of levels) {
    const s = lvl.pixelScaleArcsec;
    if (!(Number.isFinite(s) && s > 0)) {
      throw new Error(`level z=${lvl.z} has a non-positive/non-finite pixel_scale_arcsec (${s})`);
    }
  }
  return levels;
}

/** Pick the pyramid level (its `z`) whose pixel scale best serves `targetScaleArcsec`. */
export function selectLevelIndex(
  manifest: Manifest,
  targetScaleArcsec: number,
  rounding: Rounding = 'nearest',
): number {
  if (!(targetScaleArcsec > 0)) throw new Error(`targetScaleArcsec must be positive (got ${targetScaleArcsec})`);
  const levels = levelsByZ(manifest);
  const scales = levels.map((l) => l.pixelScaleArcsec);

  if (rounding === 'nearest') {
    // Closest in log space — one level pixel ≈ one output pixel (the viewer's rule).
    let bestI = 0;
    let bestD = Infinity;
    for (let i = 0; i < levels.length; i++) {
      const d = Math.abs(Math.log2(scales[i] / targetScaleArcsec));
      if (d < bestD) {
        bestD = d;
        bestI = i;
      }
    }
    return levels[bestI].z;
  }
  if (rounding === 'finer') {
    // Coarsest level still at least as fine as the request (scale <= target).
    let chosen = levels[0];
    for (let i = 0; i < levels.length; i++) {
      if (scales[i] <= targetScaleArcsec) chosen = levels[i];
    }
    return chosen.z;
  }
  // 'coarser': finest level not finer than the request (scale >= target).
  for (let i = 0; i < levels.length; i++) {
    if (scales[i] >= targetScaleArcsec) return levels[i].z;
  }
  return levels[levels.length - 1].z;
}

function asPair(v: number | [number, number], what: string): [number, number] {
  const p: [number, number] = typeof v === 'number' ? [v, v] : [v[0], v[1]];
  if (!(p[0] > 0 && p[1] > 0)) throw new Error(`${what} must be positive (got ${JSON.stringify(v)})`);
  return p;
}

/** RA/Dec (deg) samples along the perimeter of the requested North/East-aligned box. */
function skyBoxPerimeter(
  center: [number, number],
  fovArcsec: [number, number],
  samples: number,
): Array<[number, number]> {
  const [raC, decC] = center;
  const halfWDeg = fovArcsec[0] / 2 / 3600;
  const halfHDeg = fovArcsec[1] / 2 / 3600;
  const n = Math.max(2, samples);
  const t: number[] = [];
  for (let i = 0; i < n; i++) t.push(-1 + (2 * i) / (n - 1));

  const out: Array<[number, number]> = [];
  const push = (dx: number, dy: number) => {
    const dec = decC + dy;
    let cosDec = Math.cos((dec * Math.PI) / 180);
    if (Math.abs(cosDec) <= 1e-9) cosDec = 1e-9;
    out.push([raC + dx / cosDec, dec]);
  };
  for (const sy of [-1, 1]) for (const tv of t) push(tv * halfWDeg, sy * halfHDeg); // bottom & top
  for (const sx of [-1, 1]) for (const tv of t) push(sx * halfWDeg, tv * halfHDeg); // left & right
  return out;
}

export interface PlanOptions {
  /** Intended output size in px (scalar or `[w, h]`); sets the level via `fov / size`. */
  outputSize?: number | [number, number];
  /** Output pixel scale (arcsec/px) to select the level by, overriding `outputSize`. */
  targetScaleArcsec?: number;
  rounding?: Rounding;
  perimeterSamples?: number;
}

/**
 * Plan a cutout: pick the level and resolve the covering tiles/supertiles. Mirrors
 * `fitsgl.cutout.plan_cutout`. `center` is ICRS `[ra, dec]` deg; `fov` is arcsec
 * (scalar = square, or `[width, height]` along RA×Dec). An out-of-field request
 * returns an empty plan (`plan.tiles.length === 0`).
 */
export function planCutout(
  manifest: Manifest,
  center: [number, number],
  fov: number | [number, number],
  opts: PlanOptions = {},
): CutoutPlan {
  if (manifest.levels.length === 0) throw new Error('manifest has no levels');
  const fovPair = asPair(fov, 'fov');
  const { outputSize, targetScaleArcsec, rounding = 'nearest', perimeterSamples = 16 } = opts;

  let scale: number;
  if (targetScaleArcsec !== undefined) {
    if (!(targetScaleArcsec > 0)) throw new Error(`targetScaleArcsec must be positive (got ${targetScaleArcsec})`);
    scale = targetScaleArcsec;
  } else if (outputSize !== undefined) {
    const out = asPair(outputSize, 'outputSize');
    // The *finer* of the two per-axis scales — never serve an anisotropic request
    // from a level coarser than its finer axis needs.
    scale = Math.min(fovPair[0] / out[0], fovPair[1] / out[1]);
  } else {
    scale = Math.min(...manifest.levels.map((l) => l.pixelScaleArcsec));
  }

  const tileSize = manifest.fpackTileSize;
  const levelIndex = selectLevelIndex(manifest, scale, rounding);
  const level = manifest.levels.find((l) => l.z === levelIndex)!;

  const wcs = levelWcs(level);
  const h = level.shape[0];
  const w = level.shape[1];

  let minX = Infinity;
  let maxX = -Infinity;
  let minY = Infinity;
  let maxY = -Infinity;
  let anyFinite = false;
  for (const [ra, dec] of skyBoxPerimeter(center, fovPair, perimeterSamples)) {
    const p = skyToPix(wcs, ra, dec);
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
    anyFinite = true;
    if (p.x < minX) minX = p.x;
    if (p.x > maxX) maxX = p.x;
    if (p.y < minY) minY = p.y;
    if (p.y > maxY) maxY = p.y;
  }

  if (!anyFinite) {
    return { levelIndex, level, outputScaleArcsec: scale, pixelBbox: { x0: 0, y0: 0, x1: 0, y1: 0 }, tiles: [], missing: [] };
  }

  // `skyToPix` returns world (0-based, half-pixel-centred) coords: the array index
  // holding world `w` is `floor(w)`. (Python's `floor(astropy_p + 0.5)` is the same,
  // since fitsgl world = astropy 0-based + 0.5.)
  const col0 = Math.floor(minX);
  const col1 = Math.floor(maxX);
  const row0 = Math.floor(minY);
  const row1 = Math.floor(maxY);
  const bbox: PixelBBox = {
    x0: Math.max(0, col0),
    y0: Math.max(0, row0),
    x1: Math.min(w, col1 + 1),
    y1: Math.min(h, row1 + 1),
  };

  const tiles: TileRef[] = [];
  const missing: TileCoord[] = [];
  for (const coord of tilesForPixelBbox(level, bbox, tileSize)) {
    const match = resolveSupertile(level, coord.tileX, coord.tileY);
    if (match === null) missing.push(coord);
    else tiles.push(match);
  }

  return { levelIndex, level, outputScaleArcsec: scale, pixelBbox: bbox, tiles, missing };
}
