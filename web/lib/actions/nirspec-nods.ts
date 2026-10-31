'use server';

import { requireAdmin as requireAdminIdentity } from '@/lib/auth/identity';
import type { SpectrumExposure, NirspecSourceReview, NirspecStuckShutter } from '@/lib/types';
import { SHUTTER_KEY_RE } from '@/lib/nirspec-shutters';

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

/**
 * Upsert a source's background overrides by the row's natural key. Only the
 * bkg_overrides column is sent, so the upsert leaves the row's other columns (the
 * legacy stuck_shutters list, notes) as they are. An empty override map is stored
 * as SQL NULL (normalizeBkgOverrides). Stuck shutters live in nirspec_stuck_shutters
 * (setWebStuckShutters).
 */
export async function saveBkgOverrides(
  observation: string,
  exposureRoot: string,
  sourceId: number,
  bkgOverrides: Record<string, number[]> | null,
): Promise<{ review?: NirspecSourceReview; error?: string }> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('nirspec_source_review')
      .upsert(
        {
          observation,
          exposure_root: exposureRoot,
          source_id: sourceId,
          bkg_overrides: bkgOverrides,
          updated_at: new Date().toISOString(),
        },
        { onConflict: 'observation,exposure_root,source_id' },
      )
      .select()
      .single();
    if (error) return { error: error.message };
    return { review: data as NirspecSourceReview };
  } catch (err) {
    return { error: err instanceof Error ? err.message : 'Failed to save background overrides' };
  }
}

// ---------------------------------------------------------------------------
// Physical stuck shutters (nirspec_stuck_shutters): one row per (observation,
// exposure_root, "q<quadrant>r<row>c<column>"). The web writes origin 'web' only;
// a manual row (pushed from the local TOML) is never changed from here.
// Reads go through GET /api/admin/nirspec/stuck-shutters.
// ---------------------------------------------------------------------------

const ROOT_RE = /^jw\d{11}_\d{5}$/;

/**
 * Set the web mark of physical shutters of one exposure root: `stuck` true (stuck
 * closed) or false (not stuck: vetoes the pipeline's own call) upserts origin
 * 'web' rows; null deletes the web rows. Refuses when any of the shutters has a
 * manual row. Returns the shutters' rows after the write.
 */
export async function setWebStuckShutters(
  observation: string,
  exposureRoot: string,
  shutters: string[],
  stuck: boolean | null,
): Promise<{ rows?: NirspecStuckShutter[]; error?: string }> {
  try {
    if (!observation || !ROOT_RE.test(exposureRoot)) return { error: 'Invalid exposure root' };
    const keys = [...new Set(shutters)];
    if (keys.length === 0 || keys.length > 64 || keys.some((k) => !SHUTTER_KEY_RE.test(k))) {
      return { error: 'Invalid shutter keys' };
    }
    const supabase = await requireAdmin();
    const { data: current, error: readError } = await supabase
      .from('nirspec_stuck_shutters')
      .select('shutter, origin')
      .eq('observation', observation)
      .eq('exposure_root', exposureRoot)
      .in('shutter', keys);
    if (readError) return { error: readError.message };
    const manual = (current ?? []).filter((r) => r.origin === 'manual').map((r) => r.shutter);
    if (manual.length) return { error: `${manual.join(', ')} set locally (manual)` };

    if (stuck === null) {
      const { error } = await supabase
        .from('nirspec_stuck_shutters')
        .delete()
        .eq('observation', observation)
        .eq('exposure_root', exposureRoot)
        .eq('origin', 'web')
        .in('shutter', keys);
      if (error) return { error: error.message };
      return { rows: [] };
    }
    const { data, error } = await supabase
      .from('nirspec_stuck_shutters')
      .upsert(
        keys.map((shutter) => ({
          observation, exposure_root: exposureRoot, shutter, origin: 'web', stuck,
        })),
        { onConflict: 'observation,exposure_root,shutter' },
      )
      .select();
    if (error) return { error: error.message };
    return { rows: (data ?? []) as NirspecStuckShutter[] };
  } catch (err) {
    return { error: err instanceof Error ? err.message : 'Failed to save stuck shutters' };
  }
}
