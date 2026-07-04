import { describe, it, expect } from 'vitest';
import { encodeSource, decodeSource, buildNodGrid } from './nirspec-nods';
import type { SpectrumExposure } from './types';

function row(over: Partial<SpectrumExposure>): SpectrumExposure {
  return {
    id: 0, observation: 'ember_egs_p1', exposure_root: 'jw07076020001_04101',
    nod: '00001', detector: 'nrs1', source_id: 117757, exp_group: 0, grating: 'PRISM',
    filename: 'x.fits', storage_key: 'data/products/nirspec/o/x.fits',
    image_width: 40, image_height: 400, stage: 'cal', review_status: 'pending',
    masking: 'none', notes: null, created_at: '', updated_at: '', ...over,
  };
}

describe('encode/decode source', () => {
  it('round-trips, unambiguous on the last __', () => {
    expect(decodeSource(encodeSource('ember_egs_p1', 117757)))
      .toEqual({ observation: 'ember_egs_p1', sourceId: 117757 });
    // an observation name containing '__' still splits on the last one
    expect(decodeSource(encodeSource('weird__name', 42)))
      .toEqual({ observation: 'weird__name', sourceId: 42 });
  });
  it('rejects malformed', () => {
    expect(decodeSource('no-delimiter')).toBeNull();
    expect(decodeSource('obs__notanint')).toBeNull();
  });
});

describe('buildNodGrid', () => {
  it('groups by (exp_group, nod), columns by detector, ordered', () => {
    const rows = [
      row({ id: 1, exp_group: 1, nod: '00002', detector: 'nrs2' }),
      row({ id: 2, exp_group: 0, nod: '00001', detector: 'nrs1' }),
      row({ id: 3, exp_group: 0, nod: '00001', detector: 'nrs2' }),
      row({ id: 4, exp_group: 1, nod: '00002', detector: 'nrs1' }),
    ];
    const grid = buildNodGrid(rows);
    expect(grid.length).toBe(2);
    // ordered by (exp_group, nod)
    expect(grid[0].exp_group).toBe(0);
    expect(grid[0].cells.nrs1?.id).toBe(2);
    expect(grid[0].cells.nrs2?.id).toBe(3);
    expect(grid[1].exp_group).toBe(1);
    expect(grid[1].cells.nrs1?.id).toBe(4);
    expect(grid[1].cells.nrs2?.id).toBe(1);
    // >1 exp_group → dither-ordinal labels
    expect(grid[0].label).toBe('d1:00001');
    expect(grid[1].label).toBe('d2:00002');
  });

  it('single exp_group → bare nod labels, missing detector cell is null', () => {
    const grid = buildNodGrid([
      row({ id: 1, exp_group: 0, nod: '00001', detector: 'nrs1' }),
    ]);
    expect(grid.length).toBe(1);
    expect(grid[0].label).toBe('00001');
    expect(grid[0].cells.nrs1?.id).toBe(1);
    expect(grid[0].cells.nrs2).toBeNull();
  });
});
