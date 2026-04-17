/**
 * Mock data for prototype development
 * Provides diverse test data without requiring database access
 */

import type { SpectrumTarget, Spectrum, Program } from '@/lib/types';

// Mock Programs
export const MOCK_PROGRAMS: Program[] = [
  { slug: 'jades', program_name: 'JADES', pi_name: 'N. Lützgendorf', description: 'JWST Advanced Deep Extragalactic Survey', is_public: true, cycle: 1, created_at: '2024-01-01T00:00:00Z' },
  { slug: 'ceers', program_name: 'CEERS', pi_name: 'S. Finkelstein', description: 'Cosmic Evolution Early Release Science', is_public: true, cycle: 1, created_at: '2024-01-01T00:00:00Z' },
  { slug: 'capers', program_name: 'CAPERS', pi_name: 'M. Dickinson', description: 'Cycle 2 Deep Fields', is_public: false, cycle: 2, created_at: '2024-01-01T00:00:00Z' },
  { slug: 'ember', program_name: 'EMBER', pi_name: 'P. Oesch', description: 'Emission line Mapping of the Bright Epoch of Reionization', is_public: false, cycle: 4, created_at: '2024-01-01T00:00:00Z' },
];

export const MOCK_FIELDS = ['COSMOS', 'UDS', 'EGS', 'GOODS-N', 'GOODS-S'];
export const MOCK_OBSERVATIONS = [
  'jades_cosmos_p1',
  'jades_cosmos_p2',
  'ceers_egs_p1',
  'capers_cosmos_p1',
  'ember_uds_p4',
  'ember_goodsn_p1',
];

const GRATINGS = ['PRISM', 'G140M', 'G235M', 'G395M'] as const;

// Helper to generate random spectra for an object
function generateSpectra(objectId: string, gratingSet: string[], baseSNR: number): Spectrum[] {
  return gratingSet.map((grating) => ({
    id: Math.floor(Math.random() * 100000),
    spectrum_id: `${objectId}_${grating.toLowerCase()}`,
    target_id: objectId,
    grating,
    fits_path: `s3://campfire-data/${objectId}/${grating.toLowerCase()}_spec.fits`,
    reduction_version: 'v0.3',
    signal_to_noise: baseSNR + Math.random() * 10 - 5,
    exposure_time: grating === 'PRISM' ? 2500 + Math.random() * 1000 : 5000 + Math.random() * 2000,
    created_at: '2024-06-15T12:00:00Z',
  }));
}

// Generate 75 mock objects with realistic distributions
function generateMockSpectra(): SpectrumTarget[] {
  const objects: SpectrumTarget[] = [];
  let id = 1;

  // High-z objects (z > 7) with various configurations
  const highZConfigs = [
    { field: 'COSMOS', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 8.5, quality: 4, snr: 25, gratings: ['PRISM', 'G395M'], features: 34, dqFlags: 0 },
    { field: 'COSMOS', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 9.2, quality: 3, snr: 15, gratings: ['PRISM'], features: 2, dqFlags: 32 },
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 10.1, quality: 4, snr: 35, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 34, dqFlags: 0 },
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 7.8, quality: 2, snr: 8, gratings: ['PRISM'], features: 2, dqFlags: 48 },
    { field: 'GOODS-N', obs: 'ember_goodsn_p1', program: MOCK_PROGRAMS[3], z: 11.5, quality: 3, snr: 12, gratings: ['PRISM', 'G395M'], features: 3, dqFlags: 0 },
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 8.9, quality: 4, snr: 42, gratings: ['PRISM', 'G235M', 'G395M'], features: 34, dqFlags: 0 },
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 7.3, quality: 4, snr: 55, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 35, dqFlags: 0 },
    { field: 'COSMOS', obs: 'capers_cosmos_p1', program: MOCK_PROGRAMS[2], z: 9.8, quality: 2, snr: 6, gratings: ['PRISM'], features: 2, dqFlags: 32 },
  ];

  // Mid-z objects (3 < z < 7)
  const midZConfigs = [
    { field: 'COSMOS', obs: 'jades_cosmos_p2', program: MOCK_PROGRAMS[0], z: 5.2, quality: 4, snr: 45, gratings: ['PRISM', 'G235M', 'G395M'], features: 32, dqFlags: 0 },
    { field: 'COSMOS', obs: 'jades_cosmos_p2', program: MOCK_PROGRAMS[0], z: 4.8, quality: 4, snr: 38, gratings: ['PRISM', 'G395M'], features: 36, dqFlags: 0 },
    { field: 'COSMOS', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 6.1, quality: 3, snr: 22, gratings: ['PRISM', 'G395M'], features: 34, dqFlags: 0 },
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 5.5, quality: 4, snr: 52, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 33, dqFlags: 0 },
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 4.2, quality: 4, snr: 68, gratings: ['PRISM', 'G395M'], features: 32, dqFlags: 0 },
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 3.8, quality: 4, snr: 75, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 40, dqFlags: 0 },
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 6.5, quality: 3, snr: 18, gratings: ['PRISM', 'G235M'], features: 34, dqFlags: 2 },
    { field: 'GOODS-S', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 4.0, quality: 4, snr: 48, gratings: ['PRISM', 'G395M'], features: 36, dqFlags: 0 },
    { field: 'GOODS-N', obs: 'ember_goodsn_p1', program: MOCK_PROGRAMS[3], z: 5.8, quality: 2, snr: 11, gratings: ['PRISM'], features: 16, dqFlags: 32 },
    { field: 'COSMOS', obs: 'capers_cosmos_p1', program: MOCK_PROGRAMS[2], z: 3.5, quality: 4, snr: 82, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 41, dqFlags: 0 },
  ];

  // Low-z objects (z < 3)
  const lowZConfigs = [
    { field: 'COSMOS', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 2.1, quality: 4, snr: 95, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 44, dqFlags: 0 },
    { field: 'COSMOS', obs: 'jades_cosmos_p2', program: MOCK_PROGRAMS[0], z: 1.5, quality: 4, snr: 88, gratings: ['PRISM', 'G395M'], features: 12, dqFlags: 0 },
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 0.8, quality: 4, snr: 120, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 12, dqFlags: 0 },
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 2.3, quality: 4, snr: 65, gratings: ['PRISM', 'G235M', 'G395M'], features: 40, dqFlags: 0 },
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 1.2, quality: 4, snr: 105, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 44, dqFlags: 0 },
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 2.8, quality: 3, snr: 32, gratings: ['PRISM', 'G395M'], features: 36, dqFlags: 0 },
    { field: 'GOODS-S', obs: 'jades_cosmos_p2', program: MOCK_PROGRAMS[0], z: 0.5, quality: 4, snr: 150, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 12, dqFlags: 0 },
    { field: 'COSMOS', obs: 'capers_cosmos_p1', program: MOCK_PROGRAMS[2], z: 1.8, quality: 4, snr: 72, gratings: ['PRISM', 'G235M', 'G395M'], features: 40, dqFlags: 0 },
  ];

  // Special cases: LRDs, broad-line AGN, problematic data
  const specialConfigs = [
    { field: 'COSMOS', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 5.5, quality: 4, snr: 28, gratings: ['PRISM', 'G395M'], features: 48, dqFlags: 0 }, // LRD + broad line
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 4.2, quality: 4, snr: 35, gratings: ['PRISM', 'G235M', 'G395M'], features: 48, dqFlags: 0 }, // LRD
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 3.1, quality: 4, snr: 62, gratings: ['PRISM', 'G140M', 'G235M', 'G395M'], features: 32, dqFlags: 0 }, // Broad line AGN
    { field: 'COSMOS', obs: 'jades_cosmos_p2', program: MOCK_PROGRAMS[0], z: null, quality: 1, snr: 3, gratings: ['PRISM'], features: 0, dqFlags: 16 }, // No detection
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: null, quality: 1, snr: 5, gratings: ['PRISM', 'G395M'], features: 0, dqFlags: 48 }, // Low SNR + no detection
    { field: 'GOODS-N', obs: 'ember_goodsn_p1', program: MOCK_PROGRAMS[3], z: 6.8, quality: 2, snr: 14, gratings: ['PRISM'], features: 2, dqFlags: 3 }, // Chip gap + contamination
    { field: 'COSMOS', obs: 'capers_cosmos_p1', program: MOCK_PROGRAMS[2], z: 2.5, quality: 3, snr: 25, gratings: ['PRISM', 'G395M'], features: 32, dqFlags: 8 }, // Multiple sources
  ];

  // Not inspected objects
  const notInspectedConfigs = [
    { field: 'COSMOS', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 4.5, quality: 0, snr: 18, gratings: ['PRISM'], features: 0, dqFlags: 0 },
    { field: 'COSMOS', obs: 'jades_cosmos_p2', program: MOCK_PROGRAMS[0], z: 3.2, quality: 0, snr: 22, gratings: ['PRISM', 'G395M'], features: 0, dqFlags: 0 },
    { field: 'UDS', obs: 'ember_uds_p4', program: MOCK_PROGRAMS[3], z: 5.8, quality: 0, snr: 12, gratings: ['PRISM'], features: 0, dqFlags: 0 },
    { field: 'EGS', obs: 'ceers_egs_p1', program: MOCK_PROGRAMS[1], z: 2.1, quality: 0, snr: 45, gratings: ['PRISM', 'G235M'], features: 0, dqFlags: 0 },
    { field: 'GOODS-S', obs: 'jades_cosmos_p1', program: MOCK_PROGRAMS[0], z: 7.2, quality: 0, snr: 8, gratings: ['PRISM'], features: 0, dqFlags: 0 },
  ];

  // Combine all configs
  const allConfigs = [...highZConfigs, ...midZConfigs, ...lowZConfigs, ...specialConfigs, ...notInspectedConfigs];

  // Generate additional random objects to reach ~75 total
  while (allConfigs.length < 75) {
    const field = MOCK_FIELDS[Math.floor(Math.random() * MOCK_FIELDS.length)];
    const program = MOCK_PROGRAMS[Math.floor(Math.random() * MOCK_PROGRAMS.length)];
    const obs = MOCK_OBSERVATIONS[Math.floor(Math.random() * MOCK_OBSERVATIONS.length)];
    const numGratings = Math.random() < 0.3 ? 1 : Math.random() < 0.6 ? 2 : Math.random() < 0.85 ? 3 : 4;
    const gratings = GRATINGS.slice(0, numGratings);
    const z = Math.random() < 0.1 ? null : Math.random() * 12;
    const quality = z === null ? 1 : Math.floor(Math.random() * 5);
    const snr = 5 + Math.random() * 95;

    allConfigs.push({
      field,
      obs,
      program,
      z,
      quality,
      snr,
      gratings: [...gratings],
      features: Math.floor(Math.random() * 64),
      dqFlags: Math.random() < 0.2 ? Math.floor(Math.random() * 512) : 0,
    });
  }

  // Generate objects from configs
  for (const config of allConfigs) {
    const sourceId = 100000 + Math.floor(Math.random() * 900000);
    const objectId = `${config.obs}_${sourceId}`;
    const spectra = generateSpectra(objectId, config.gratings, config.snr);
    const maxSnr = Math.max(...spectra.map(s => s.signal_to_noise || 0));
    const totalExptime = spectra.reduce((sum, s) => sum + ((s as Spectrum & { exptime?: number }).exptime || 0), 0);

    objects.push({
      id: id++,
      target_id: objectId,
      program_slug: config.program.slug,
      program_name: config.program.program_name || undefined,
      field: config.field,
      observation: config.obs,
      ra: 34 + Math.random() * 2 + (config.field === 'EGS' ? 180 : 0),
      dec: -5 + Math.random() * 2 + (config.field === 'GOODS-N' ? 67 : 0),
      redshift: config.z,
      redshift_auto: config.z,
      redshift_inspected: config.quality > 0 ? config.z : null,
      redshift_quality: config.quality,
      spectral_features: config.features,
      dq_flags: config.dqFlags,
      last_inspected_at: config.quality > 0 ? '2024-10-15T14:30:00Z' : null,
      last_inspected_by: config.quality > 0 ? 'user-123' : null,
      created_at: '2024-06-15T12:00:00Z',
      updated_at: '2024-10-15T14:30:00Z',
      spectra,
      max_snr: maxSnr,
      num_gratings: config.gratings.length,
      // Extended field for future column testing
      total_exptime: totalExptime,
    } as SpectrumTarget & { total_exptime: number });
  }

  return objects;
}

export const MOCK_SPECTRA: (SpectrumTarget & { total_exptime?: number })[] = generateMockSpectra();

// Helper function to apply filters to mock data (client-side filtering for prototypes)
export type FilterMode = 'any' | 'all' | 'none';

export interface MockFilterOptions {
  programs?: string[];
  fields?: string[];
  gratings?: string[];
  gratings_mode?: FilterMode;
  observations?: string[];
  redshift_quality?: number[];
  redshift_min?: number | null;
  redshift_max?: number | null;
  max_snr_min?: number | null;
  max_snr_max?: number | null;
  max_exposure_time_min?: number | null;
  max_exposure_time_max?: number | null;
  spectral_features?: number[];
  spectral_features_mode?: FilterMode;
  dq_flags?: number[];
  dq_flags_mode?: FilterMode;
  inspected_only?: boolean | null;
  search?: string;
}

export function applyFiltersToMockData(
  data: SpectrumTarget[],
  filters: MockFilterOptions
): SpectrumTarget[] {
  return data.filter(obj => {
    // Program filter
    if (filters.programs && filters.programs.length > 0) {
      if (!filters.programs.includes(obj.program_slug)) return false;
    }

    // Field filter
    if (filters.fields && filters.fields.length > 0) {
      if (!filters.fields.includes(obj.field)) return false;
    }

    // Grating filter with mode support
    if (filters.gratings && filters.gratings.length > 0) {
      const objGratings = obj.spectra.map(s => s.grating);
      const mode = filters.gratings_mode || 'any';

      if (mode === 'any') {
        // Object has ANY of the selected gratings
        if (!filters.gratings.some(g => objGratings.includes(g))) return false;
      } else if (mode === 'all') {
        // Object has ALL of the selected gratings
        if (!filters.gratings.every(g => objGratings.includes(g))) return false;
      } else if (mode === 'none') {
        // Object has NONE of the selected gratings
        if (filters.gratings.some(g => objGratings.includes(g))) return false;
      }
    }

    // Observation filter
    if (filters.observations && filters.observations.length > 0) {
      if (!obj.observation || !filters.observations.includes(obj.observation)) return false;
    }

    // Redshift quality filter
    if (filters.redshift_quality && filters.redshift_quality.length > 0) {
      if (!filters.redshift_quality.includes(obj.redshift_quality)) return false;
    }

    // Redshift range
    if (filters.redshift_min !== undefined && filters.redshift_min !== null) {
      if (obj.redshift === null || obj.redshift < filters.redshift_min) return false;
    }
    if (filters.redshift_max !== undefined && filters.redshift_max !== null) {
      if (obj.redshift === null || obj.redshift > filters.redshift_max) return false;
    }

    // Max S/N range
    if (filters.max_snr_min !== undefined && filters.max_snr_min !== null) {
      if (!obj.max_snr || obj.max_snr < filters.max_snr_min) return false;
    }
    if (filters.max_snr_max !== undefined && filters.max_snr_max !== null) {
      if (!obj.max_snr || obj.max_snr > filters.max_snr_max) return false;
    }

    // Max exposure time range
    if (filters.max_exposure_time_min !== undefined && filters.max_exposure_time_min !== null) {
      if (!obj.max_exposure_time || obj.max_exposure_time < filters.max_exposure_time_min) return false;
    }
    if (filters.max_exposure_time_max !== undefined && filters.max_exposure_time_max !== null) {
      if (!obj.max_exposure_time || obj.max_exposure_time > filters.max_exposure_time_max) return false;
    }

    // Spectral features bitmask with mode
    if (filters.spectral_features && filters.spectral_features.length > 0) {
      const mask = filters.spectral_features.reduce((acc, v) => acc | v, 0);
      const mode = filters.spectral_features_mode || 'any';

      if (mode === 'any') {
        if (((obj.spectral_features ?? 0) & mask) === 0) return false;
      } else if (mode === 'all') {
        if (((obj.spectral_features ?? 0) & mask) !== mask) return false;
      } else if (mode === 'none') {
        if (((obj.spectral_features ?? 0) & mask) !== 0) return false;
      }
    }

    // DQ flags bitmask with mode
    if (filters.dq_flags && filters.dq_flags.length > 0) {
      const mask = filters.dq_flags.reduce((acc, v) => acc | v, 0);
      const mode = filters.dq_flags_mode || 'any';

      if (mode === 'any') {
        if (((obj.dq_flags ?? 0) & mask) === 0) return false;
      } else if (mode === 'all') {
        if (((obj.dq_flags ?? 0) & mask) !== mask) return false;
      } else if (mode === 'none') {
        if (((obj.dq_flags ?? 0) & mask) !== 0) return false;
      }
    }

    // Inspected only
    if (filters.inspected_only === true) {
      if (obj.redshift_quality === 0) return false;
    } else if (filters.inspected_only === false) {
      if (obj.redshift_quality !== 0) return false;
    }

    // Text search
    if (filters.search && filters.search.length > 0) {
      if (!obj.target_id.toLowerCase().includes(filters.search.toLowerCase())) return false;
    }

    return true;
  });
}

// Get unique values for filter dropdowns
export function getUniqueFields(): string[] {
  return [...new Set(MOCK_SPECTRA.map(s => s.field))].sort();
}

export function getUniqueObservations(): string[] {
  return [...new Set(MOCK_SPECTRA.map(s => s.observation).filter(Boolean) as string[])].sort();
}

export function getUniqueGratings(): string[] {
  const gratings = new Set<string>();
  MOCK_SPECTRA.forEach(s => s.spectra.forEach(sp => gratings.add(sp.grating)));
  return [...gratings].sort();
}
