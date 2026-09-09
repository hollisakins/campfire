import { describe, it, expect } from 'vitest';
import { trilogyLevelsForBands, DEFAULT_TRILOGY_PARAMS, type FitsglConfig, type TrilogyStats } from '@fitsgl/core';
import { renderRGB, renderWeightedTrilogy } from './render';
import { defaultRgbBands, defaultWeightedBands } from './source';

const stats = (mean: number, sigma: number): TrilogyStats => ({
  mean,
  sigma,
  tail: { p99: mean + 20 * sigma, p99_9: mean + 60 * sigma, p99_99: mean + 200 * sigma, p99_999: mean + 800 * sigma },
});

describe('renderWeightedTrilogy', () => {
  it('reduces to the per-channel RGB composite on unit weights', () => {
    const w = 4;
    const h = 3;
    const mk = (seed: number) =>
      Float32Array.from({ length: w * h }, (_, i) => Math.sin(i * 0.7 + seed) * 5 + seed);
    const bands = [mk(1), mk(2), mk(3)];
    bands[1][5] = NaN; // one no-data pixel in the green band
    bands[0][7] = NaN; bands[1][7] = NaN; bands[2][7] = NaN; // all-NaN pixel
    const levels = trilogyLevelsForBands([stats(1, 0.5), stats(2, 0.5), stats(3, 0.5)], DEFAULT_TRILOGY_PARAMS);
    const weighted = renderWeightedTrilogy(bands, w, h, {
      levels,
      weights: [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
    });
    const rgb = renderRGB([bands[0], bands[1], bands[2]], w, h, {
      limits: [
        { lo: levels[0].x0, hi: levels[0].x2 },
        { lo: levels[1].x0, hi: levels[1].x2 },
        { lo: levels[2].x0, hi: levels[2].x2 },
      ],
      stretch: 'trilogy',
      trilogyK: levels.map((l) => l.k),
    });
    expect(Array.from(weighted)).toEqual(Array.from(rgb));
    // The all-NaN pixel stays transparent (row-flipped: FITS row 1 → raster row 1 of 3).
    const o = ((h - 1 - 1) * w + 3) * 4;
    expect(weighted[o + 3]).toBe(0);
  });

  it('mixes a band into several channels by its weights', () => {
    const w = 1;
    const h = 1;
    const bright = Float32Array.of(100);
    const levels = trilogyLevelsForBands([stats(0, 1)], DEFAULT_TRILOGY_PARAMS);
    const out = renderWeightedTrilogy([bright], w, h, { levels, weights: [[1, 0.5, 0]] });
    expect(out[0]).toBeGreaterThan(0);
    expect(out[1]).toBe(out[0]); // a weighted average of one band is that band, whatever its weight
    expect(out[2]).toBe(0);
    expect(out[3]).toBe(255);
  });

  it('rejects mismatched parallel arrays', () => {
    expect(() => renderWeightedTrilogy([Float32Array.of(1)], 1, 1, { levels: [], weights: [] })).toThrow();
  });
});

function config(over: Partial<FitsglConfig['defaultView']>, names = ['f115w', 'f277w', 'f444w']): FitsglConfig {
  return {
    schemaVersion: 1,
    dataset: {
      name: 'x',
      bands: names.map((name, i) => ({ name, tiles: [`${name}/manifest.json`], grid: { group: 0 }, pivotUm: 1 + i })),
    },
    defaultView: { mode: 'rgb', r: 'f444w', g: 'f277w', b: 'f115w', ...over },
  };
}

describe('defaultWeightedBands', () => {
  it('is null without producer weights', () => {
    expect(defaultWeightedBands(config({}))).toBeNull();
    expect(defaultWeightedBands(config({ weights: [] }))).toBeNull();
  });

  it('keeps the producer table in order, merges duplicates and drops unknown bands', () => {
    const out = defaultWeightedBands(
      config({
        weights: [
          { band: 'f115w', weight: [0, 0, 1] },
          { band: 'f277w', weight: [0, 1, 0] },
          { band: 'f444w', weight: [1, 0, 0] },
          { band: 'f277w', weight: [0.5, 0, 0] },
          { band: 'nope', weight: [1, 1, 1] },
        ],
      }),
    )!;
    expect(out.map((e) => e.band.name)).toEqual(['f115w', 'f277w', 'f444w']);
    expect(out[1].weight).toEqual([0.5, 1, 0]);
  });

  it('the triple still comes from the producer view', () => {
    expect(defaultRgbBands(config({}))!.map((b) => b.name)).toEqual(['f444w', 'f277w', 'f115w']);
  });
});

describe('rainbowBands', () => {
  it('orders by wavelength, spreads hues blue→red and picks the simple triple', async () => {
    const { rainbowBands } = await import('./source');
    const cfg = config({}, ['f444w', 'f115w', 'f277w']); // pivots follow declaration order here
    // Re-pivot so declaration order differs from wavelength order.
    cfg.dataset.bands[0].pivotUm = 4.4;
    cfg.dataset.bands[1].pivotUm = 1.15;
    cfg.dataset.bands[2].pivotUm = 2.77;
    const out = rainbowBands(cfg.dataset.bands);
    expect(out.weighted.map((e) => e.band.name)).toEqual(['f115w', 'f277w', 'f444w']);
    expect(out.weighted[0].weight).toEqual([0, 0, 1]); // bluest → blue
    expect(out.weighted[2].weight).toEqual([1, 0, 0]); // reddest → red
    expect(out.triple.map((b) => b.name)).toEqual(['f444w', 'f277w', 'f115w']);
  });

  it('caps a long band list at MAX_BANDS keeping both ends', async () => {
    const { rainbowBands } = await import('./source');
    const { MAX_BANDS } = await import('@fitsgl/core');
    const names = Array.from({ length: MAX_BANDS + 5 }, (_, i) => `b${String(i).padStart(2, '0')}`);
    const out = rainbowBands(config({}, names).dataset.bands);
    expect(out.weighted).toHaveLength(MAX_BANDS);
    expect(out.weighted[0].band.name).toBe(names[0]);
    expect(out.weighted[MAX_BANDS - 1].band.name).toBe(names[names.length - 1]);
  });
});
