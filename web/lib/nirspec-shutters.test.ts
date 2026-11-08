import { describe, it, expect } from 'vitest';
import {
  computeShutterRegions, shutterOverlayRegions,
  parseShutterKey, slitletOrdinals, stuckOrdinals, nextWebMark, cutoutClickMark, EXPOSURE_ROOT_RE,
  type StuckShutterMark,
} from './nirspec-shutters';

describe('physical shutter keys', () => {
  it('parses the stuck_list key and rejects anything else', () => {
    expect(parseShutterKey('q2r195c64')).toEqual({ q: 2, r: 195, c: 64 });
    for (const bad of ['q5r1c1', 'q0r1c1', 'Q2r195c64', 'q2r195', 'q2r195c64 ', '']) {
      expect(parseShutterKey(bad)).toBeNull();
    }
  });

  it('numbers the slitlet by ascending column (the legacy ordinal frame)', () => {
    const m = slitletOrdinals(['q2r195c65', 'q2r195c63', 'q2r195c64']);
    expect([...m.entries()]).toEqual([[1, ['q2r195c63']], [2, ['q2r195c64']], [3, ['q2r195c65']]]);
    // numeric, not lexicographic: c9 before c10
    expect(slitletOrdinals(['q1r5c10', 'q1r5c9']).get(1)).toEqual(['q1r5c9']);
  });

  it('maps marks onto overlay ordinals; a not-stuck veto is not stuck', () => {
    const keys = ['q2r195c63', 'q2r195c64', 'q2r195c65'];
    const marks = new Map<string, StuckShutterMark>([
      ['q2r195c63', { origin: 'web', stuck: true }],
      ['q2r195c64', { origin: 'web', stuck: false }],
      ['q2r195c65', { origin: 'manual', stuck: true }],
      ['q3r1c1', { origin: 'web', stuck: true }],   // another source's shutter
    ]);
    expect(stuckOrdinals(keys, marks)).toEqual([1, 3]);
  });

  it('cycles web marks none → stuck → not stuck → none; manual is read-only', () => {
    expect(nextWebMark(undefined)).toBe(true);
    expect(nextWebMark({ origin: 'web', stuck: true })).toBe(false);
    expect(nextWebMark({ origin: 'web', stuck: false })).toBeNull();
    expect(nextWebMark({ origin: 'manual', stuck: true })).toBeUndefined();
  });

  it('a cutout click clears what is drawn stuck, by a veto when no web row made it stuck', () => {
    const stuck: StuckShutterMark = { origin: 'web', stuck: true };
    const veto: StuckShutterMark = { origin: 'web', stuck: false };
    expect(cutoutClickMark(false, [undefined])).toBe(true);        // drawn open: mark stuck
    expect(cutoutClickMark(true, [stuck, stuck])).toBeNull();      // web rows made it stuck: delete
    expect(cutoutClickMark(true, [undefined])).toBe(false);        // only the cell's STKSHTRS: veto
    expect(cutoutClickMark(true, [veto])).toBeNull();              // already vetoed: delete the veto
    expect(cutoutClickMark(false, [veto])).toBe(true);
  });

  it('accepts exposure roots whose activity is base 36', () => {
    expect(EXPOSURE_ROOT_RE.test('jw07076001001_03101')).toBe(true);
    expect(EXPOSURE_ROOT_RE.test('jw12497004001_0410f')).toBe(true);
    for (const bad of ['jw12497004001_0410F', 'jw1249700400_04101', 'jw12497004001_04101_00001']) {
      expect(EXPOSURE_ROOT_RE.test(bad)).toBe(false);
    }
  });
});

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

  it("post-reprocessing: the cell's STKSHTRS list sets the geometry, automatic calls included", () => {
    // ordinal 1 of 3 removed by the pipeline's own call: no mark in the database
    const regions = shutterOverlayRegions({ shutsta: 'xx', stuckList: [], stkshtrs: '[1]', nRows: 400 });
    expect(regions.map((r) => r.ordinal)).toEqual([3, 2]);
    // an interior removed shutter stays in the window and shows stuck
    const inner = shutterOverlayRegions({ shutsta: 'xx', stuckList: [], stkshtrs: '[2]', nRows: 400 });
    expect(inner.map((r) => [r.ordinal, r.stuck])).toEqual([[3, false], [2, true], [1, false]]);
  });

  it('empty SHUTSTA yields no regions', () => {
    expect(shutterOverlayRegions({ shutsta: '', stuckList: [], stkshtrs: 'N/A', nRows: 400 })).toEqual([]);
    expect(shutterOverlayRegions({ shutsta: null, stuckList: [], stkshtrs: 'N/A', nRows: 400 })).toEqual([]);
  });
});
