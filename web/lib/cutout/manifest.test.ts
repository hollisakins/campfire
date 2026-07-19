// parseManifest compatibility tests (epic #337, Phase 5): a legacy v1 level has
// no `supertiles[]`; the parser must synthesize the one full-grid supertile the
// way the reference `fitsgl.manifest.LevelInfo.from_dict` does (v1 shim), so
// cutouts keep working against pre-supertile pyramid deployments.

import { describe, it, expect } from 'vitest';
import { parseManifest } from './manifest';

const V1_LEVEL = {
  z: 0,
  filename: 'f444w_z0.fits.fz',
  compression: 'RICE_1',
  lossless: false,
  shape: [1024, 2048], // [H, W]
  fpack_tile_count: [2, 4], // [n_ty, n_tx]
  pixel_scale_arcsec: 0.03,
  wcs: { CTYPE1: 'RA---TAN', CTYPE2: 'DEC--TAN' },
};

describe('parseManifest v1 shim', () => {
  it('synthesizes one full-grid supertile when `supertiles` is absent', () => {
    const m = parseManifest({
      version: 1,
      native_shape: [1024, 2048],
      fpack_tile_size: 512,
      levels: [V1_LEVEL],
    });
    expect(m.levels[0].supertiles).toEqual([
      // mirrors fitsgl-py: [n_ty, n_tx] → tile_count [n_tx, n_ty], origin [0, 0]
      { filename: 'f444w_z0.fits.fz', tileOrigin: [0, 0], tileCount: [4, 2] },
    ]);
  });

  it('still parses explicit v2 supertiles verbatim', () => {
    const m = parseManifest({
      version: 2,
      native_shape: [1024, 2048],
      fpack_tile_size: 512,
      levels: [
        {
          ...V1_LEVEL,
          supertiles: [
            { filename: 'a.fits.fz', tile_origin: [0, 0], tile_count: [2, 2] },
            { filename: 'b.fits.fz', tile_origin: [2, 0], tile_count: [2, 2] },
          ],
        },
      ],
    });
    expect(m.levels[0].supertiles).toHaveLength(2);
    expect(m.levels[0].supertiles[1]).toEqual({
      filename: 'b.fits.fz',
      tileOrigin: [2, 0],
      tileCount: [2, 2],
    });
  });
});
