// Parity test: the TS cutout addressing (`plan.ts`) must reproduce `fitsgl-py`'s
// `plan_cutout` byte-for-byte on a shared golden fixture (epic #337, Phase 5).
// Regenerate the fixture with `__fixtures__/generate_plan_fixture.py`. Any drift
// between the browser/producer geometry and this server port fails here.

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, resolve } from 'path';
import { describe, it, expect } from 'vitest';
import { parseManifest, type LevelInfo } from './manifest';
import { planCutout, resolveSupertile, tilesForPixelBbox, type Rounding } from './plan';

const here = dirname(fileURLToPath(import.meta.url));
const manifest = parseManifest(
  JSON.parse(readFileSync(resolve(here, '__fixtures__/rj0911-f444w-manifest.json'), 'utf-8')),
);
const cases = JSON.parse(readFileSync(resolve(here, '__fixtures__/plan-cases.json'), 'utf-8')) as Array<{
  name: string;
  center: [number, number];
  fov: number | [number, number];
  outputSize: number | [number, number] | null;
  targetScaleArcsec: number | null;
  rounding: Rounding;
  expected: {
    levelIndex: number;
    bbox: { x0: number; y0: number; x1: number; y1: number };
    tiles: Array<{ tileX: number; tileY: number; supertileIndex: number; localX: number; localY: number }>;
    missing: Array<{ tileX: number; tileY: number }>;
  };
}>;

describe('planCutout — fitsgl-py golden parity', () => {
  for (const c of cases) {
    it(c.name, () => {
      const plan = planCutout(manifest, c.center, c.fov, {
        outputSize: c.outputSize ?? undefined,
        targetScaleArcsec: c.targetScaleArcsec ?? undefined,
        rounding: c.rounding,
      });
      expect(plan.levelIndex).toBe(c.expected.levelIndex);
      expect(plan.pixelBbox).toEqual(c.expected.bbox);
      expect(plan.tiles.map((t) => ({
        tileX: t.tile.tileX, tileY: t.tile.tileY,
        supertileIndex: t.supertileIndex, localX: t.localX, localY: t.localY,
      }))).toEqual(c.expected.tiles);
      expect(plan.missing.map((m) => ({ tileX: m.tileX, tileY: m.tileY }))).toEqual(c.expected.missing);
    });
  }
});

describe('resolveSupertile — multi-supertile gaps + local coords', () => {
  // A synthetic 2×2-supertile level (rj0911's real levels are single-file, so this
  // exercises the disjoint-supertile scan + gap the golden fixture cannot).
  const level: LevelInfo = {
    z: 0,
    filename: 'x',
    compression: 'RICE_1',
    lossless: false,
    shape: [1024, 1024], // [H, W] → 4×4 tiles at 256
    fpackTileCount: [4, 4], // [n_ty, n_tx]
    pixelScaleArcsec: 0.03,
    wcs: {},
    supertiles: [
      { filename: 'a', tileOrigin: [0, 0], tileCount: [2, 2] }, // top-left quadrant
      { filename: 'b', tileOrigin: [2, 0], tileCount: [2, 2] }, // top-right
      { filename: 'c', tileOrigin: [0, 2], tileCount: [2, 2] }, // bottom-left
      // bottom-right quadrant (tiles x∈[2,4), y∈[2,4)) is a DROPPED gap.
    ],
  };

  it('maps a global tile to its supertile with local coords', () => {
    const m = resolveSupertile(level, 3, 1); // top-right supertile 'b', local (1, 1)
    expect(m?.supertile.filename).toBe('b');
    expect(m?.supertileIndex).toBe(1);
    expect([m?.localX, m?.localY]).toEqual([1, 1]);
  });

  it('returns null for a dropped-supertile gap', () => {
    expect(resolveSupertile(level, 3, 3)).toBeNull(); // bottom-right, no supertile
  });

  it('planCutout-style coverage splits a box across supertiles + records the gap', () => {
    // A box spanning all four quadrants (whole level).
    const coords = tilesForPixelBbox(level, { x0: 0, y0: 0, x1: 1024, y1: 1024 }, 256);
    expect(coords).toHaveLength(16);
    const resolved = coords.map((c) => resolveSupertile(level, c.tileX, c.tileY));
    expect(resolved.filter((r) => r === null)).toHaveLength(4); // the dropped quadrant's 4 tiles
  });
});
