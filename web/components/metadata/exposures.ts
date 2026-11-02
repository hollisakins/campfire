import type { ProgramExposure } from '@/app/api/metadata/programs/[slug]/exposures/route';

/** One exposure of an observation: its detectors' rows folded together. */
export interface ExposureRow {
  exposureRoot: string;
  date: string | null;
  grating: string | null;
  filter: string | null;
  readoutPattern: string | null;
  nGroups: number | null;
  nIntegrations: number | null;
  exposureTime: number | null;
  ditherPosition: number | null;
  nDitherPositions: number | null;
  /** detector -> which master background it uses (null: none) */
  mbkg: { detector: string; source: 'combined' | 'exposure' | null }[];
}

const first = <T,>(rows: ProgramExposure[], pick: (r: ProgramExposure) => T | null): T | null => {
  for (const r of rows) {
    const v = pick(r);
    if (v !== null && v !== undefined) return v;
  }
  return null;
};

/** Fold the exposure x detector rows into one row per exposure, in exposure order.
 * The observing columns are the exposure's (both detectors read the same header
 * values); the master background stays per detector. */
export function groupExposures(rows: ProgramExposure[]): ExposureRow[] {
  const byRoot = new Map<string, ProgramExposure[]>();
  for (const r of rows) {
    const list = byRoot.get(r.exposure_root);
    if (list) list.push(r);
    else byRoot.set(r.exposure_root, [r]);
  }
  return [...byRoot.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([exposureRoot, rs]) => ({
      exposureRoot,
      date: first(rs, (r) => r.date_obs),
      grating: first(rs, (r) => r.grating),
      filter: first(rs, (r) => r.filter),
      readoutPattern: first(rs, (r) => r.readout_pattern),
      nGroups: first(rs, (r) => r.n_groups),
      nIntegrations: first(rs, (r) => r.n_integrations),
      exposureTime: first(rs, (r) => r.exposure_time),
      ditherPosition: first(rs, (r) => r.dither_position),
      nDitherPositions: first(rs, (r) => r.n_dither_positions),
      mbkg: [...rs]
        .sort((a, b) => a.detector.localeCompare(b.detector))
        .map((r) => ({ detector: r.detector, source: r.mbkg_source })),
    }));
}

/** The master-background cell: one word when every detector uses the same one,
 * else each detector's ("exposure (NRS1), combined (NRS2)"); '—' when none has one. */
export function mbkgLabel(mbkg: ExposureRow['mbkg']): string {
  const known = mbkg.filter((m) => m.source !== null);
  if (known.length === 0) return '—';
  const sources = new Set(mbkg.map((m) => m.source));
  if (sources.size === 1) return known[0].source!;
  return mbkg.map((m) => `${m.source ?? '—'} (${m.detector.toUpperCase()})`).join(', ');
}
