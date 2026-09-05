// displayDefaults (epic #337 / map-RGB-defaults): the producer's default stretch,
// resolved server-side so cutouts open on the same transfer the map does. Trilogy
// levels derive from precomputed per-band stats + the producer's knobs via
// @fitsgl/core's own trilogyLevels — the same math the viewer's applyTrilogy runs.

import { describe, it, expect } from 'vitest';
import { DEFAULT_TRILOGY_PARAMS, trilogyLevels, type FitsglConfig } from '@fitsgl/core';
import { displayDefaults, sourceMatchesDatasetVersion, versionedSourceUrl } from './source';

describe('versionedSourceUrl', () => {
  it('cache-busts overwritten descriptors while preserving existing query parameters', () => {
    expect(versionedSourceUrl('https://cdn.example/fitsgl.json?download=1', '2026-09-04T12:00:00+00:00'))
      .toBe('https://cdn.example/fitsgl.json?download=1&__campfire_version=2026-09-04T12%3A00%3A00%2B00%3A00');
  });
});

describe('sourceMatchesDatasetVersion', () => {
  it('rejects a render source from a different asset-version snapshot', () => {
    expect(sourceMatchesDatasetVersion({ datasetVersion: 'new' }, 'new')).toBe(true);
    expect(sourceMatchesDatasetVersion({ datasetVersion: 'old' }, 'new')).toBe(false);
    expect(sourceMatchesDatasetVersion(null, 'new')).toBe(true);
  });
});

const TRI_STATS = {
  mean: 0.001,
  sigma: 0.005,
  tail: { p99: 0.08, p99_9: 0.76, p99_99: 4.0, p99_999: 15.0 },
};

function config(overrides: {
  stretch?: { mode?: 'linear' | 'log' | 'asinh' | 'trilogy' | 'sqrt' };
  trilogy?: Record<string, number>;
  dropStatsOn?: string;
}): FitsglConfig {
  const bands = ['r', 'g', 'b'].map((name) => ({
    name,
    tiles: [`https://cdn/${name}/manifest.json`],
    grid: { group: 0 },
    ...(overrides.dropStatsOn === name
      ? {}
      : { stats: { histogram: { counts: [1], lo: 0, hi: 1 }, trilogy: TRI_STATS } }),
  }));
  return {
    schemaVersion: 1,
    dataset: { name: 'x', bands },
    defaultView: {
      mode: 'rgb',
      r: 'r',
      g: 'g',
      b: 'b',
      ...(overrides.stretch !== undefined && { stretch: overrides.stretch }),
      // `trilogy` knobs are a @fitsgl/core ≥ 0.3.0 contract field; simulate a
      // parsed config carrying them (0.2.0's validator strips the key).
      ...(overrides.trilogy !== undefined && { trilogy: overrides.trilogy }),
    } as FitsglConfig['defaultView'],
  };
}

const chosen = (c: FitsglConfig) => c.dataset.bands;

describe('displayDefaults', () => {
  it('returns null when the producer declares no stretch (engine default)', () => {
    expect(displayDefaults(config({}), chosen(config({})))).toBeNull();
  });

  it('passes a non-trilogy stretch through without limits (auto percentile)', () => {
    const c = config({ stretch: { mode: 'log' } });
    expect(displayDefaults(c, chosen(c))).toEqual({ stretch: 'log' });
  });

  it('solves per-band trilogy levels from stats with default knobs', () => {
    const c = config({ stretch: { mode: 'trilogy' } });
    const d = displayDefaults(c, chosen(c));
    const expected = trilogyLevels(TRI_STATS, DEFAULT_TRILOGY_PARAMS);
    expect(d?.stretch).toBe('trilogy');
    expect(d?.limits).toHaveLength(3);
    expect(d?.limits?.[0]).toEqual({ lo: expected.x0, hi: expected.x2 });
    expect(d?.trilogyK?.[0]).toBeCloseTo(expected.k, 10);
  });

  it('honors producer knobs when the config carries them (core ≥ 0.3.0)', () => {
    const knobs = { noiselum: 0.12, satpercent: 0.01 };
    const c = config({ stretch: { mode: 'trilogy' }, trilogy: knobs });
    const d = displayDefaults(c, chosen(c));
    const expected = trilogyLevels(TRI_STATS, { ...DEFAULT_TRILOGY_PARAMS, ...knobs });
    expect(d?.trilogyK?.[0]).toBeCloseTo(expected.k, 10);
    expect(d?.limits?.[0].hi).toBeCloseTo(expected.x2, 10);
  });

  it('falls back to null when any chosen band lacks trilogy stats', () => {
    const c = config({ stretch: { mode: 'trilogy' }, dropStatsOn: 'g' });
    expect(displayDefaults(c, chosen(c))).toBeNull();
  });
});
