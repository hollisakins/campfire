'use server';

import { requireAdmin as requireAdminIdentity } from '@/lib/auth/identity';
import type { SpectrumExposure, NirspecSourceReview } from '@/lib/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function requireAdmin() {
  const { supabase } = await requireAdminIdentity();
  return supabase;
}

export interface NodSource {
  observation: string;
  source_id: number;
  grating: string | null;
  cellCount: number;         // rows in the grid (all nod×detector cells)
  exposureRootCount: number; // distinct exposures
  detectors: string[];       // which detectors present
}

// spectrum_exposures is admin-RLS + small, so a direct query + JS aggregation is
// correct (no RPC), mirroring the P3 nirspec-rate actions.
export async function listNirspecNodSources(): Promise<{
  sources: NodSource[];
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('spectrum_exposures')
      .select('observation, source_id, grating, detector, exposure_root');
    if (error) return { sources: [], error: error.message };

    const rows = (data ?? []) as {
      observation: string; source_id: number; grating: string | null;
      detector: string; exposure_root: string;
    }[];
    const byKey = new Map<string, {
      observation: string; source_id: number; grating: string | null;
      cellCount: number; roots: Set<string>; detectors: Set<string>;
    }>();
    for (const r of rows) {
      const key = `${r.observation}::${r.source_id}`;
      let s = byKey.get(key);
      if (!s) {
        s = { observation: r.observation, source_id: r.source_id, grating: r.grating,
              cellCount: 0, roots: new Set(), detectors: new Set() };
        byKey.set(key, s);
      }
      s.cellCount += 1;
      s.roots.add(r.exposure_root);
      if (r.detector) s.detectors.add(r.detector);
    }
    const sources = [...byKey.values()].map((s) => ({
      observation: s.observation,
      source_id: s.source_id,
      grating: s.grating,
      cellCount: s.cellCount,
      exposureRootCount: s.roots.size,
      detectors: [...s.detectors].sort(),
    })).sort((a, b) =>
      a.observation.localeCompare(b.observation) || a.source_id - b.source_id);

    return { sources };
  } catch (err) {
    return {
      sources: [],
      error: err instanceof Error ? err.message : 'Failed to list nod sources',
    };
  }
}

export async function getNirspecNodGrid(
  observation: string,
  sourceId: number,
): Promise<{ rows: SpectrumExposure[]; error?: string }> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('spectrum_exposures')
      .select('*')
      .eq('observation', observation)
      .eq('source_id', sourceId)
      .order('exp_group', { ascending: true, nullsFirst: true })
      .order('nod', { ascending: true })
      .order('detector', { ascending: true });
    if (error) return { rows: [], error: error.message };
    return { rows: (data ?? []) as SpectrumExposure[] };
  } catch (err) {
    return {
      rows: [],
      error: err instanceof Error ? err.message : 'Failed to fetch nod grid',
    };
  }
}

// ---------------------------------------------------------------------------
// Source-scoped flags (P6): nirspec_source_review, one row per
// (observation, exposure_root, source_id). A nods grid spans several exposure
// roots, so the page loads all reviews for the source and indexes by root.
// ---------------------------------------------------------------------------

export async function getNirspecSourceReviews(
  observation: string,
  sourceId: number,
): Promise<{ reviews: NirspecSourceReview[]; error?: string }> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('nirspec_source_review')
      .select('*')
      .eq('observation', observation)
      .eq('source_id', sourceId);
    if (error) return { reviews: [], error: error.message };
    return { reviews: (data ?? []) as NirspecSourceReview[] };
  } catch (err) {
    return {
      reviews: [],
      error: err instanceof Error ? err.message : 'Failed to fetch source reviews',
    };
  }
}

// The row holds two independent entries (stuck shutters, background overrides), each
// synced on its own by `campfire config push / pull`. A save writes ONE of them, through
// the same guarded write the CLI uses (nirspec_review_apply): the caller passes the
// entry as the page last saw it, and the change lands only if the row still holds
// that. A page left open across a CLI push (or another reviewer's edit) therefore gets
// `conflict` back instead of overwriting what it never saw.
export type StuckExpect = { value: number[]; source: NirspecSourceReview['stuck_source'] } | null;
export type BkgExpect = { value: Record<string, number[]> } | null;
export interface SaveReviewResult {
  ok: boolean;
  conflict?: boolean;   // the entry changed since the page read it; nothing was written
  error?: string;
}

async function applyReviewChange(
  observation: string,
  change: {
    channel: 'stuck' | 'bkg';
    exposure_root: string;
    source_id: number;
    expect: StuckExpect | BkgExpect;
    set: { value: number[]; source: 'web' } | { value: Record<string, number[]> } | null;
  },
): Promise<SaveReviewResult> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase.rpc('nirspec_review_apply', {
      p_observation: observation,
      p_changes: [change],
    });
    if (error) return { ok: false, error: error.message };
    const result = data as { applied?: number; unchanged?: number; refused?: unknown[] } | null;
    if (result?.refused?.length) return { ok: false, conflict: true };
    if ((result?.applied ?? 0) + (result?.unchanged ?? 0) !== 1) {
      return { ok: false, error: 'Save was not confirmed' };
    }
    return { ok: true };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : 'Failed to save source review' };
  }
}

/**
 * Save a source's stuck-shutter entry, tagged `web`. An empty list is stored as
 * `[]`, not NULL: it records "reviewed, none stuck", which the pipeline's
 * auto-detector respects (it never re-adds a source that has an entry), so
 * un-flagging a detected shutter here is durable. `expect` is the entry as the
 * page last saw it (null = none).
 */
export async function saveStuckShutters(
  observation: string,
  exposureRoot: string,
  sourceId: number,
  stuckShutters: number[],
  expect: StuckExpect,
): Promise<SaveReviewResult> {
  return applyReviewChange(observation, {
    channel: 'stuck',
    exposure_root: exposureRoot,
    source_id: sourceId,
    expect,
    set: { value: stuckShutters, source: 'web' },
  });
}

/**
 * Save a source's background-override entry (`null` = no overrides; see
 * normalizeBkgOverrides for the empty-map / empty-list conventions). `expect` is
 * the entry as the page last saw it (null = none).
 */
export async function saveBkgOverrides(
  observation: string,
  exposureRoot: string,
  sourceId: number,
  bkgOverrides: Record<string, number[]> | null,
  expect: BkgExpect,
): Promise<SaveReviewResult> {
  return applyReviewChange(observation, {
    channel: 'bkg',
    exposure_root: exposureRoot,
    source_id: sourceId,
    expect,
    set: bkgOverrides ? { value: bkgOverrides } : null,
  });
}
