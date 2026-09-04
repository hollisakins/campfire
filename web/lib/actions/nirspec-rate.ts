'use server';

import { requireAdmin as requireAdminIdentity } from '@/lib/auth/identity';
import { NIRSPEC_RATE_SORT_KEYS } from '@/lib/admin/sort-keys';
import type { NirspecRateExposure, MaskRegionsPayload } from '@/lib/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function requireAdmin() {
  const { supabase } = await requireAdminIdentity();
  return supabase;
}

export interface RateFilters {
  observation?: string;
  detector?: string;
  reviewStatus?: string;
  grating?: string;
}

export interface RateSort {
  sortColumn?: string;
  sortDirection?: 'asc' | 'desc';
}

export interface RateExposuresResult {
  rows: NirspecRateExposure[];
  total: number;
  error?: string;
}

export interface RateNeighbors {
  prevId: number | null;
  nextId: number | null;
  position: number | null;   // 1-based
  total: number;
  windowIds: number[];
  error?: string;
}

// Sort-key whitelist lives in @/lib/admin/sort-keys (a 'use server' module may
// only export async functions). Validate here as defense-in-depth even though the
// client already whitelists — never pass an unvetted column into .order().
function safeSortColumn(col?: string): string {
  return col && (NIRSPEC_RATE_SORT_KEYS as readonly string[]).includes(col) ? col : 'filename';
}

// nirspec_rate_exposures is admin-only via RLS, so a direct authenticated query is
// safe and correct — no RPC needed (unlike NIRCam, whose large table used a
// windowed-count RPC). Applies the categorical filters as equality matches.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function applyRateFilters(query: any, f: RateFilters) {
  if (f.observation) query = query.eq('observation', f.observation);
  if (f.detector) query = query.eq('detector', f.detector);
  if (f.reviewStatus) query = query.eq('review_status', f.reviewStatus);
  if (f.grating) query = query.eq('grating', f.grating);
  return query;
}

// ---------------------------------------------------------------------------
// Read
// ---------------------------------------------------------------------------

export async function getNirspecRateExposures(
  params?: RateFilters & RateSort & { page?: number; pageSize?: number },
): Promise<RateExposuresResult> {
  try {
    const supabase = await requireAdmin();
    const page = params?.page ?? 1;
    const pageSize = params?.pageSize ?? 50;
    const from = (page - 1) * pageSize;
    const to = from + pageSize - 1;

    let query = supabase
      .from('nirspec_rate_exposures')
      .select('*', { count: 'exact' });
    query = applyRateFilters(query, params ?? {});
    query = query
      .order(safeSortColumn(params?.sortColumn), {
        ascending: params?.sortDirection !== 'desc',
      })
      .range(from, to);

    const { data, count, error } = await query;
    if (error) return { rows: [], total: 0, error: error.message };
    return { rows: (data ?? []) as NirspecRateExposure[], total: count ?? 0 };
  } catch (err) {
    return {
      rows: [], total: 0,
      error: err instanceof Error ? err.message : 'Failed to fetch rate exposures',
    };
  }
}

export async function getNirspecRateExposureById(id: number): Promise<{
  exposure: NirspecRateExposure | null;
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('nirspec_rate_exposures')
      .select('*')
      .eq('id', id)
      .single();
    if (error) return { exposure: null, error: error.message };
    return { exposure: data as NirspecRateExposure };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error ? err.message : 'Failed to fetch rate exposure',
    };
  }
}

// Bounded prev/next nav for the detail page, over the SAME filtered+ordered set
// the list page shows. The table is tiny (2 detectors × exposures/obs), so we
// fetch the full ordered id list and compute neighbors in JS — no RPC needed.
export async function getNirspecRateNeighbors(
  currentId: number,
  params?: RateFilters & RateSort & { window?: number },
): Promise<RateNeighbors> {
  const empty: RateNeighbors = {
    prevId: null, nextId: null, position: null, total: 0, windowIds: [],
  };
  try {
    const supabase = await requireAdmin();
    let query = supabase.from('nirspec_rate_exposures').select('id');
    query = applyRateFilters(query, params ?? {});
    query = query.order(safeSortColumn(params?.sortColumn), {
      ascending: params?.sortDirection !== 'desc',
    });
    const { data, error } = await query;
    if (error) return { ...empty, error: error.message };

    const ids = (data ?? []).map((r) => r.id as number);
    const idx = ids.indexOf(currentId);
    if (idx < 0) return empty;  // currentId not in the filtered set (direct entry)

    const win = params?.window ?? 3;
    return {
      prevId: idx > 0 ? ids[idx - 1] : null,
      nextId: idx < ids.length - 1 ? ids[idx + 1] : null,
      position: idx + 1,
      total: ids.length,
      windowIds: ids.slice(Math.max(0, idx - win), idx + win + 1),
    };
  } catch (err) {
    return {
      ...empty,
      error: err instanceof Error ? err.message : 'Failed to fetch neighbors',
    };
  }
}

export async function getNirspecRateFilterOptions(): Promise<{
  observations: string[];
  gratings: string[];
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('nirspec_rate_exposures')
      .select('observation, grating');
    if (error) return { observations: [], gratings: [], error: error.message };
    const rows = (data ?? []) as { observation: string; grating: string | null }[];
    const uniq = (xs: (string | null)[]) =>
      [...new Set(xs.filter((x): x is string => !!x))].sort();
    return {
      observations: uniq(rows.map((r) => r.observation)),
      gratings: uniq(rows.map((r) => r.grating)),
    };
  } catch (err) {
    return {
      observations: [], gratings: [],
      error: err instanceof Error ? err.message : 'Failed to fetch filter options',
    };
  }
}

// ---------------------------------------------------------------------------
// Update
// ---------------------------------------------------------------------------

export async function updateNirspecRateReview(
  id: number,
  updates: {
    review_status?: 'pending' | 'approved' | 'excluded';
    notes?: string;
  },
): Promise<{ exposure: NirspecRateExposure | null; error?: string }> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase
      .from('nirspec_rate_exposures')
      .update({ ...updates, updated_at: new Date().toISOString() })
      .eq('id', id)
      .select()
      .single();
    if (error) return { exposure: null, error: error.message };
    return { exposure: data as NirspecRateExposure };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error ? err.message : 'Failed to update rate exposure',
    };
  }
}

/**
 * Persist the polygon list for a single rate exposure.
 *
 * Vertices are stored as DS9 ``image`` 1-indexed coords so the payload round-trips
 * through ``campfire deploy nirspec pull-rate-masks`` (P3b) and ``apply_mask_dq``
 * without further transform. "Masked" is derived state: a non-empty
 * ``mask_regions`` is the sole signal; clearing all polygons nulls it.
 */
export async function saveRateMaskRegions(
  id: number,
  regions: MaskRegionsPayload,
): Promise<{ exposure: NirspecRateExposure | null; error?: string }> {
  try {
    const supabase = await requireAdmin();
    const hasPolygons = (regions?.polygons?.length ?? 0) > 0;
    const { data, error } = await supabase
      .from('nirspec_rate_exposures')
      .update({
        mask_regions: hasPolygons ? regions : null,
        updated_at: new Date().toISOString(),
      })
      .eq('id', id)
      .select()
      .single();
    if (error) return { exposure: null, error: error.message };
    return { exposure: data as NirspecRateExposure };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error ? err.message : 'Failed to save mask regions',
    };
  }
}
