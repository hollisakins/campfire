// Decode + assemble the covering supertiles of a cutout plan into one native-pixel
// float array (epic #337, Phase 5). Reuses `@fitsgl/core`'s `FpackFile` — the same
// RICE/GZIP2 fpack decoder the browser viewer runs — but server-side, with the
// default HTTP range fetcher reading the public tiles bucket. Tiles are placed by
// their global level-pixel origin; dropped-supertile gaps (`plan.missing`) and
// out-of-array borders stay NaN so the caller can mask them.

import { FpackFile } from '@fitsgl/core/internal';
import type { CutoutPlan, TileRef } from './plan';

export interface AssembledRegion {
  /** Row-major native pixels of the plan's `pixelBbox`, NaN where no data. */
  data: Float32Array;
  width: number;
  height: number;
  /** The level-pixel origin (== `plan.pixelBbox.x0/y0`) the array is offset from. */
  x0: number;
  y0: number;
}

/** Group a plan's tiles by their supertile file so each `.fits.fz` opens once. */
function bySupertile(plan: CutoutPlan): Map<string, TileRef[]> {
  const groups = new Map<string, TileRef[]>();
  for (const t of plan.tiles) {
    const key = t.supertile.filename;
    const g = groups.get(key);
    if (g) g.push(t);
    else groups.set(key, [t]);
  }
  return groups;
}

/**
 * Fetch, decode, and assemble the plan's covering region.
 * `baseUrl` is the band's tile directory (where `manifest.json` and the `.fits.fz`
 * supertiles live), e.g. `https://tiles.example/fitsgl/<field>/composite/<band>/`.
 */
export async function assembleRegion(plan: CutoutPlan, baseUrl: string): Promise<AssembledRegion> {
  const { pixelBbox } = plan;
  const width = Math.max(0, pixelBbox.x1 - pixelBbox.x0);
  const height = Math.max(0, pixelBbox.y1 - pixelBbox.y0);
  const data = new Float32Array(width * height).fill(NaN);
  if (width === 0 || height === 0) return { data, width, height, x0: pixelBbox.x0, y0: pixelBbox.y0 };

  const base = baseUrl.endsWith('/') ? baseUrl : baseUrl + '/';

  for (const [filename, tiles] of bySupertile(plan)) {
    const file = await FpackFile.open(base + filename, undefined, undefined);
    for (const t of tiles) {
      const px = await file.getTile(t.localX, t.localY);
      const dims = file.tileDims(t.localX, t.localY);
      // The tile's origin in level pixels (global tile index × the fpack tile edge).
      // ztile1/ztile2 is the supertile's fpack tile size; a v1 level tiles at that.
      const originX = t.tile.tileX * file.ztile1;
      const originY = t.tile.tileY * file.ztile2;
      blit(px, dims.width, dims.height, originX - pixelBbox.x0, originY - pixelBbox.y0, data, width, height);
    }
  }
  return { data, width, height, x0: pixelBbox.x0, y0: pixelBbox.y0 };
}

/** Copy a `sw×sh` source tile into `dst` at `(dx, dy)`, clipping to the dst box. */
function blit(
  src: Float32Array,
  sw: number,
  sh: number,
  dx: number,
  dy: number,
  dst: Float32Array,
  dw: number,
  dh: number,
): void {
  const cx0 = Math.max(0, dx);
  const cy0 = Math.max(0, dy);
  const cx1 = Math.min(dw, dx + sw);
  const cy1 = Math.min(dh, dy + sh);
  for (let y = cy0; y < cy1; y++) {
    const sy = y - dy;
    let s = sy * sw + (cx0 - dx);
    let d = y * dw + cx0;
    for (let x = cx0; x < cx1; x++) dst[d++] = src[s++];
  }
}
