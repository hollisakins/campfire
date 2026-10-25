import { describe, expect, it } from 'vitest';
import type { StorageBudget } from '@/lib/actions/storage-registry';
import {
  budgetPercent,
  budgetSegments,
  deploymentItems,
  isReleaseVersion,
  releaseInProgress,
} from './dashboard';

describe('isReleaseVersion', () => {
  it('accepts X.Y.Z only', () => {
    expect(isReleaseVersion('0.9.3')).toBe(true);
    expect(isReleaseVersion('1.12.0')).toBe(true);
    expect(isReleaseVersion('0.9.4.dev6+ga1b2c3d')).toBe(false);
    expect(isReleaseVersion('0.9.3+dirty')).toBe(false);
    expect(isReleaseVersion('experimental')).toBe(false);
    expect(isReleaseVersion(null)).toBe(false);
    expect(isReleaseVersion('')).toBe(false);
  });
});

describe('deploymentItems', () => {
  it('prefers spectra, then exposures for a field, then targets', () => {
    expect(deploymentItems({ n_spectra: 12, n_targets: 5, field: null })).toEqual({ count: 12, unit: 'spectra' });
    expect(deploymentItems({ n_spectra: 1, n_targets: null, field: null })).toEqual({ count: 1, unit: 'spectrum' });
    expect(deploymentItems({ n_spectra: null, n_targets: 96, field: 'cosmos' })).toEqual({ count: 96, unit: 'exposures' });
    expect(deploymentItems({ n_spectra: null, n_targets: 1, field: 'cosmos' })).toEqual({ count: 1, unit: 'exposure' });
    expect(deploymentItems({ n_spectra: null, n_targets: 7, field: null })).toEqual({ count: 7, unit: 'targets' });
    expect(deploymentItems({ n_spectra: null, n_targets: null, field: null })).toBeNull();
  });
});

describe('releaseInProgress', () => {
  it('names the states with a running job', () => {
    for (const s of ['freezing', 'exporting', 'copying']) expect(releaseInProgress(s)).toBe(true);
    for (const s of ['draft', 'frozen', 'exported', 'ready', 'published', 'withdrawn', 'failed']) {
      expect(releaseInProgress(s)).toBe(false);
    }
  });
});

const budget = (over: Partial<StorageBudget> = {}): StorageBudget => ({
  total_bytes: 700,
  cap_bytes: 1000,
  pct_used: 70,
  registry_bytes: 600,
  tile_bytes: 100,
  by_product_type: null,
  by_backend: null,
  by_class: {
    live: { count: 10, bytes: 400 },
    archive: { count: 3, bytes: 150 },
    gc_pending: { count: 2, bytes: 50 },
    collected: { count: 4, bytes: 0, logical_bytes: 900 },
  },
  ...over,
});

describe('budgetSegments', () => {
  it('orders live, archive, awaiting collection, tiles and sums to the total', () => {
    const segs = budgetSegments(budget());
    expect(segs.map((s) => s.key)).toEqual(['live', 'archive', 'gc_pending', 'tiles']);
    expect(segs.reduce((n, s) => n + s.bytes, 0)).toBe(700);
    expect(segs[2].count).toBe(2);
    expect(segs[3].count).toBeNull();
  });

  it('falls back to one live segment without the class breakdown', () => {
    const segs = budgetSegments(budget({ by_class: null }));
    expect(segs[0].bytes).toBe(600);
    expect(segs[1].bytes).toBe(0);
    expect(segs[3].bytes).toBe(100);
  });
});

describe('budgetPercent', () => {
  it('clamps to the meter', () => {
    expect(budgetPercent(budget())).toBe(70);
    expect(budgetPercent(budget({ total_bytes: 5000 }))).toBe(100);
    expect(budgetPercent(budget({ cap_bytes: 0 }))).toBe(0);
  });
});
