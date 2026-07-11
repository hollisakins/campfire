// Row-order tests for the RGBA renderers (epic #337, Phase 5): input float
// arrays are FITS bottom-up (row 0 = south), output RGBA is raster top-down
// (row 0 = top), so the northernmost input row must land in the FIRST RGBA row.
// Pins the North-up fix from the #363 review (claude + Codex both caught the
// missing flip).

import { describe, it, expect } from 'vitest';
import { renderSingleBand, renderRGB } from './render';

const px = (rgba: Uint8ClampedArray, x: number, y: number, w: number) => {
  const o = (y * w + x) * 4;
  return [rgba[o], rgba[o + 1], rgba[o + 2], rgba[o + 3]];
};

describe('renderSingleBand', () => {
  it('flips FITS bottom-up rows to raster top-down (north ends up on top)', () => {
    // 2×2: bottom row (south) = 0 (dark), top row (north) = 1 (bright)
    const data = new Float32Array([0, 0, 1, 1]);
    const rgba = renderSingleBand(data, 2, 2, {
      limits: { lo: 0, hi: 1 },
      stretch: 'linear',
      colormap: 'gray',
    });
    expect(px(rgba, 0, 0, 2)).toEqual([255, 255, 255, 255]); // raster top = north = bright
    expect(px(rgba, 0, 1, 2)).toEqual([0, 0, 0, 255]); // raster bottom = south = dark
  });

  it('keeps NaN transparency at the flipped position', () => {
    // NaN at south-right (index 1) → transparent at raster bottom-right
    const data = new Float32Array([0, NaN, 1, 1]);
    const rgba = renderSingleBand(data, 2, 2, {
      limits: { lo: 0, hi: 1 },
      stretch: 'linear',
      colormap: 'gray',
    });
    expect(px(rgba, 1, 1, 2)[3]).toBe(0); // bottom-right transparent
    expect(px(rgba, 1, 0, 2)[3]).toBe(255); // top-right opaque
  });
});

describe('renderRGB', () => {
  it('flips rows and maps channels R/G/B in the flipped order', () => {
    // 1×2 column: south px has r=0, north px has r=1 (g/b constant 0)
    const r = new Float32Array([0, 1]);
    const g = new Float32Array([0, 0]);
    const b = new Float32Array([0, 0]);
    const rgba = renderRGB([r, g, b], 1, 2, {
      limits: [
        { lo: 0, hi: 1 },
        { lo: 0, hi: 1 },
        { lo: 0, hi: 1 },
      ],
      stretch: 'linear',
    });
    expect(px(rgba, 0, 0, 1)).toEqual([255, 0, 0, 255]); // top = north = bright red
    expect(px(rgba, 0, 1, 1)).toEqual([0, 0, 0, 255]); // bottom = south = dark
  });
});
