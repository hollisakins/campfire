// ---------------------------------------------------------------------------
// Pure helpers for the admin dashboard (no React, no data access), so the
// derivations the panels render can be unit-tested.
// ---------------------------------------------------------------------------

import type { StorageBudget } from '@/lib/actions/storage-registry';

/** A cfpipe release string: X.Y.Z. Anything else (.dev, +dirty, an override) is unreleased. */
export const RELEASE_VERSION_RE = /^\d+\.\d+\.\d+$/;

export function isReleaseVersion(version: string | null | undefined): boolean {
  return !!version && RELEASE_VERSION_RE.test(version);
}

/** What a deployment row counts: spectra for NIRSpec, exposures for NIRCam fields. */
export function deploymentItems(d: {
  n_spectra: number | null;
  n_targets: number | null;
  field: string | null;
}): { count: number; unit: string } | null {
  if (d.n_spectra != null) return { count: d.n_spectra, unit: d.n_spectra === 1 ? 'spectrum' : 'spectra' };
  if (d.n_targets != null) {
    const unit = d.field ? (d.n_targets === 1 ? 'exposure' : 'exposures') : (d.n_targets === 1 ? 'target' : 'targets');
    return { count: d.n_targets, unit };
  }
  return null;
}

/** Release states with a job running on a machine somewhere. */
export const RELEASE_STATES_IN_PROGRESS = new Set(['freezing', 'exporting', 'copying']);

export function releaseInProgress(state: string): boolean {
  return RELEASE_STATES_IN_PROGRESS.has(state);
}

export interface BudgetSegment {
  key: 'live' | 'archive' | 'gc_pending' | 'tiles';
  label: string;
  bytes: number;
  count: number | null;
  className: string;
}

/**
 * The bucket's bytes at rest by class, in meter order. live + archive +
 * gc_pending is the registry; tiles are the one R2 exception. Collected rows
 * occupy nothing and are not a segment.
 */
export function budgetSegments(b: StorageBudget): BudgetSegment[] {
  const cls = b.by_class;
  const segments: BudgetSegment[] = [
    { key: 'live', label: 'Live', bytes: cls?.live.bytes ?? 0, count: cls?.live.count ?? null, className: 'bg-primary' },
    { key: 'archive', label: 'Archive', bytes: cls?.archive.bytes ?? 0, count: cls?.archive.count ?? null, className: 'bg-success' },
    { key: 'gc_pending', label: 'Awaiting collection', bytes: cls?.gc_pending?.bytes ?? 0, count: cls?.gc_pending?.count ?? null, className: 'bg-warning' },
    { key: 'tiles', label: 'Map tiles', bytes: b.tile_bytes ?? 0, count: null, className: 'bg-text-tertiary' },
  ];
  // Without the class breakdown the whole total is "live".
  if (!cls) segments[0].bytes = Math.max(0, b.total_bytes - (b.tile_bytes ?? 0));
  return segments;
}

/** Percent of the cap, clamped to [0, 100] for the meter width. */
export function budgetPercent(b: StorageBudget): number {
  if (!b.cap_bytes) return 0;
  return Math.min(100, Math.max(0, (b.total_bytes / b.cap_bytes) * 100));
}
