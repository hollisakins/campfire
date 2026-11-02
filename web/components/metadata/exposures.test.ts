import { describe, expect, it } from 'vitest';
import type { ProgramExposure } from '@/app/api/metadata/programs/[slug]/exposures/route';
import { groupExposures, mbkgLabel } from './exposures';

const row = (over: Partial<ProgramExposure>): ProgramExposure => ({
  observation: 'spurs_a2744', exposure_root: 'jw09214009001_03105_00001', detector: 'nrs1',
  grating: 'G140M', filter: 'F100LP', date_obs: '2025-11-06', exposure_time: 8753.334,
  readout_pattern: 'NRSIRS2', n_groups: 20, n_integrations: 6, dither_position: 1,
  n_dither_positions: 3, mbkg_source: 'combined', ...over,
});

describe('groupExposures', () => {
  it('folds the detectors of an exposure into one row, in exposure order', () => {
    const rows = groupExposures([
      row({ exposure_root: 'jw09214009001_03105_00002', dither_position: 2 }),
      row({ detector: 'nrs2' }),
      row({ detector: 'nrs1', mbkg_source: 'exposure' }),
    ]);
    expect(rows.map((r) => r.exposureRoot)).toEqual(['jw09214009001_03105_00001', 'jw09214009001_03105_00002']);
    expect(rows[0].mbkg).toEqual([
      { detector: 'nrs1', source: 'exposure' },
      { detector: 'nrs2', source: 'combined' },
    ]);
    expect(rows[0]).toMatchObject({ grating: 'G140M', filter: 'F100LP', nGroups: 20, nDitherPositions: 3 });
    expect(rows[1].ditherPosition).toBe(2);
  });

  it('takes a value from whichever detector carries it', () => {
    const [r] = groupExposures([row({ filter: null }), row({ detector: 'nrs2', filter: 'F100LP' })]);
    expect(r.filter).toBe('F100LP');
  });
});

describe('mbkgLabel', () => {
  it('is one word when the detectors agree, per detector when they do not', () => {
    expect(mbkgLabel([{ detector: 'nrs1', source: 'combined' }, { detector: 'nrs2', source: 'combined' }]))
      .toBe('combined');
    expect(mbkgLabel([{ detector: 'nrs1', source: 'exposure' }, { detector: 'nrs2', source: 'combined' }]))
      .toBe('exposure (NRS1), combined (NRS2)');
    expect(mbkgLabel([{ detector: 'nrs1', source: null }, { detector: 'nrs2', source: 'combined' }]))
      .toBe('— (NRS1), combined (NRS2)');
    expect(mbkgLabel([{ detector: 'nrs1', source: null }])).toBe('—');
  });
});
