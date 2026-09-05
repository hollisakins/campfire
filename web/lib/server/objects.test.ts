import { describe, it, expect, vi } from 'vitest';

vi.mock('server-only', () => ({}));
vi.mock('@/lib/auth/identity', () => ({ getRequestIdentity: async () => ({ user: null, supabase: null }) }));
vi.mock('@/lib/auth/access-context', () => ({ getAccessContext: async () => ({ accessibleSlugs: [] }) }));
vi.mock('@/lib/supabase/service', () => ({ createServiceClient: () => null }));

import { scopeObjectAggregates } from './objects';
import type { ObjectMemberTarget, Spectrum } from '@/lib/types';

const spec = (over: Partial<Spectrum>): Spectrum => ({
  id: 1, spectrum_id: 's', target_id: 't', grating: 'PRISM', fits_path: 'p', ...over,
} as Spectrum);
const member = (over: Partial<ObjectMemberTarget>): ObjectMemberTarget => ({
  id: 1, target_id: 't', program_slug: 'prog', program_name: 'Prog', observation: 'o',
  ra: 0, dec: 0, redshift_auto: null, max_snr: null, max_exposure_time: null, spectra: [], ...over,
});

// The stored objects.* aggregates span every member program; the page shows
// only what the viewer's accessible members contribute (mirrors the SQL
// helper object_scoped_aggregates()).
describe('scopeObjectAggregates', () => {
  it('recomputes counts, programs, gratings and maxima from the scoped members', () => {
    const out = scopeObjectAggregates([
      member({ program_slug: 'b', spectra: [
        spec({ id: 1, grating: 'PRISM', signal_to_noise: 5, exposure_time: 100 }),
        spec({ id: 2, grating: 'G140M', signal_to_noise: null, exposure_time: 300 }),
      ] }),
      member({ id: 2, target_id: 't2', program_slug: 'a', spectra: [
        spec({ id: 3, grating: 'PRISM', signal_to_noise: 12, exposure_time: null }),
      ] }),
    ]);
    expect(out).toEqual({
      n_targets: 2,
      n_spectra: 3,
      programs: ['a', 'b'],
      gratings: ['G140M', 'PRISM'],
      max_snr: 12,
      max_exposure_time: 300,
    });
  });

  it('answers nulls and empties for a member list with no spectra', () => {
    expect(scopeObjectAggregates([member({ spectra: [] })])).toEqual({
      n_targets: 1, n_spectra: 0, programs: ['prog'], gratings: [], max_snr: null, max_exposure_time: null,
    });
    expect(scopeObjectAggregates([])).toEqual({
      n_targets: 0, n_spectra: 0, programs: [], gratings: [], max_snr: null, max_exposure_time: null,
    });
  });
});
