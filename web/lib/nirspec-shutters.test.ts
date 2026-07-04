import { describe, it, expect } from 'vitest';
import { computeShutterRegions, shutterOverlayRegions } from './nirspec-shutters';

// Oracle values computed directly from the pipeline's _compute_shutter_regions
// (stuck_shutters.py) — the web port must match the *_nods.pdf geometry exactly.
describe('computeShutterRegions', () => {
  it('matches the pipeline oracle for representative (nShutters, nRows)', () => {
    expect(computeShutterRegions(3, 400).map((r) => [r.rowStart, r.rowEnd]))
      .toEqual([[46, 147], [149, 252], [254, 355]]);
    expect(computeShutterRegions(5, 400).map((r) => [r.rowStart, r.rowEnd]))
      .toEqual([[31, 97], [99, 165], [167, 234], [236, 302], [304, 370]]);
    expect(computeShutterRegions(1, 400).map((r) => [r.rowStart, r.rowEnd]))
      .toEqual([[96, 305]]);
    expect(computeShutterRegions(3, 100).map((r) => [r.rowStart, r.rowEnd]))
      .toEqual([[12, 37], [39, 62], [64, 89]]);
  });

  it('returns one region per shutter, ordered bottom→top', () => {
    const regions = computeShutterRegions(5, 400);
    expect(regions).toHaveLength(5);
    // region 0 is the bottom (lowest rows), region N-1 the top (highest rows)
    for (let i = 1; i < regions.length; i++) {
      expect(regions[i].rowStart).toBeGreaterThan(regions[i - 1].rowStart);
    }
  });
});

describe('shutterOverlayRegions', () => {
  it('pre-reprocessing: ordinals N..1, no stuck flags without a stuck list', () => {
    const regions = shutterOverlayRegions({ shutsta: 'xxx', stuckList: [], stkshtrs: 'N/A', nRows: 400 });
    expect(regions.map((r) => r.ordinal)).toEqual([3, 2, 1]); // bottom→top
    expect(regions.every((r) => !r.stuck)).toBe(true);
  });

  it('pre-reprocessing: marks the flagged ordinal stuck', () => {
    const regions = shutterOverlayRegions({ shutsta: 'xxx', stuckList: [2], stkshtrs: 'N/A', nRows: 400 });
    expect(regions.find((r) => r.ordinal === 2)?.stuck).toBe(true);
    expect(regions.find((r) => r.ordinal === 1)?.stuck).toBe(false);
  });

  it('post-reprocessing: removed stuck shutter shrinks the window', () => {
    // 2 shutters remain in SHUTSTA, ordinal 3 was removed from the metafile.
    const regions = shutterOverlayRegions({ shutsta: 'xx', stuckList: [3], stkshtrs: 'OPEN', nRows: 400 });
    // nOriginal=3, remaining=[1,2], window spans ordinals 2..1
    expect(regions.map((r) => r.ordinal)).toEqual([2, 1]);
    expect(regions).toHaveLength(2);
  });

  it('empty SHUTSTA yields no regions', () => {
    expect(shutterOverlayRegions({ shutsta: '', stuckList: [], stkshtrs: 'N/A', nRows: 400 })).toEqual([]);
    expect(shutterOverlayRegions({ shutsta: null, stuckList: [], stkshtrs: 'N/A', nRows: 400 })).toEqual([]);
  });
});
