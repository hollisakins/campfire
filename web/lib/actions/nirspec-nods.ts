'use server';

import { createClient } from '@/lib/supabase/server';
import type { SpectrumExposure } from '@/lib/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function requireAdmin() {
  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) throw new Error('Not authenticated');

  const { data: profile } = await supabase
    .from('user_profiles')
    .select('is_admin')
    .eq('user_id', user.id)
    .single();

  if (!profile?.is_admin) throw new Error('Admin access required');
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
