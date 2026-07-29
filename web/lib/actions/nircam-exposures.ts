'use server';

import { createClient } from '@/lib/supabase/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { getS3ClientForBackend, getBucketNameForBackend, type DataBackend } from '@/lib/storage';
import type { NircamExposure, MaskRegionsPayload } from '@/lib/types';

export interface ExposurePngUrls {
  preview: string | null;
  full: string | null;
}

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

// Fast-path guard for the triage hot loop (save + prev/next + prefetch).
// requireAdmin() spends two sequential network round trips before the real
// query — auth.getUser() re-validates the JWT against Supabase Auth, then
// user_profiles is read — purely to produce a clean error message: every table
// and RPC the hot-path functions touch is already admin-gated at the database
// (nircam_exposures RLS policies; the get_admin_exposure_* RPCs' explicit
// is_admin() checks), so a non-admin session gets zero rows or an RPC error,
// never data. Here we only confirm a session exists (a cookie read, no
// network) and let the database be the authority. Keep requireAdmin for
// anything not fully RLS/RPC-gated (e.g. the nircam_reduction_progress view).
async function requireSession() {
  const supabase = await createClient();
  const { data: { session } } = await supabase.auth.getSession();
  if (!session) throw new Error('Not authenticated');
  return supabase;
}

// ---------------------------------------------------------------------------
// Read
// ---------------------------------------------------------------------------

export interface ExposuresResult {
  exposures: NircamExposure[];
  total: number;
  error?: string;
}

export interface ExposureFilters {
  field?: string;
  filter?: string;
  detector?: string;
  reviewStatus?: string;
  stage?: string;
  correction?: string;
}

export interface ExposureSort {
  sortColumn?: string;
  sortDirection?: 'asc' | 'desc';
}

// Sort-key whitelist (accepted by get_admin_exposures /
// get_admin_exposure_neighbors) lives in @/lib/admin/sort-keys — a 'use server'
// module may only export async functions.

function rpcExposureParams(params?: ExposureFilters & ExposureSort) {
  return {
    p_field: params?.field ?? null,
    p_filter: params?.filter ?? null,
    p_detector: params?.detector ?? null,
    p_review_status: params?.reviewStatus ?? null,
    p_stage: params?.stage ?? null,
    p_correction: params?.correction ?? null,
    p_sort_column: params?.sortColumn ?? 'filename',
    p_sort_direction: params?.sortDirection ?? 'asc',
  };
}

// Backed by get_admin_exposures: whitelisted server-side sort + windowed total
// in one scan (no count:'exact' second query).
export async function getNircamExposures(params?: ExposureFilters & ExposureSort & {
  page?: number;       // 1-based
  pageSize?: number;   // default 50
}): Promise<ExposuresResult> {
  try {
    const supabase = await requireSession();

    const { data, error } = await supabase.rpc('get_admin_exposures', {
      ...rpcExposureParams(params),
      p_page: params?.page ?? 1,
      p_page_size: params?.pageSize ?? 50,
    });

    if (error) {
      return { exposures: [], total: 0, error: error.message };
    }

    const rows = (data ?? []) as (NircamExposure & { total_count: number })[];
    const total = rows[0]?.total_count ?? 0;
    return {
      exposures: rows.map(({ total_count: _t, ...row }) => row as NircamExposure),
      total,
    };
  } catch (err) {
    return {
      exposures: [],
      total: 0,
      error: err instanceof Error ? err.message : 'Failed to fetch exposures',
    };
  }
}

export interface ExposureNeighbors {
  /** id of the previous/next exposure in the filtered, ordered set. */
  prevId: number | null;
  nextId: number | null;
  /** 1-based position of the current exposure, and total matches. */
  position: number | null;
  total: number;
  /** The ±window ids in order — feeds the PNG prefetch. */
  windowIds: number[];
  error?: string;
}

// Bounded prev/next nav for the detail page (get_admin_exposure_neighbors):
// the ±window neighbor ids and absolute position of the current exposure
// within the SAME filtered+ordered set the list page shows. Replaces the
// sessionStorage nav cache that fetched every matching id and broke on
// refresh/direct entry.
export async function getExposureNeighbors(
  currentId: number,
  params?: ExposureFilters & ExposureSort & { window?: number },
): Promise<ExposureNeighbors> {
  const empty: ExposureNeighbors = {
    prevId: null, nextId: null, position: null, total: 0, windowIds: [],
  };
  try {
    const supabase = await requireSession();

    const { data, error } = await supabase.rpc('get_admin_exposure_neighbors', {
      p_current_id: currentId,
      ...rpcExposureParams(params),
      p_window: params?.window ?? 3,
    });

    if (error) return { ...empty, error: error.message };
    const rows = (data ?? []) as { id: number; nav_position: number; total_count: number }[];
    if (rows.length === 0) return empty;  // currentId not in the filtered set

    const idx = rows.findIndex((r) => r.id === currentId);
    const current = idx >= 0 ? rows[idx] : null;
    return {
      prevId: idx > 0 ? rows[idx - 1].id : null,
      nextId: idx >= 0 && idx < rows.length - 1 ? rows[idx + 1].id : null,
      position: current?.nav_position ?? null,
      total: rows[0]?.total_count ?? 0,
      windowIds: rows.map((r) => r.id),
    };
  } catch (err) {
    return {
      ...empty,
      error: err instanceof Error ? err.message : 'Failed to fetch neighbors',
    };
  }
}

export async function getNircamExposureById(id: number): Promise<{
  exposure: NircamExposure | null;
  error?: string;
}> {
  try {
    const supabase = await requireSession();

    const { data, error } = await supabase
      .from('nircam_exposures')
      .select('*')
      .eq('id', id)
      .single();

    if (error) {
      return { exposure: null, error: error.message };
    }

    return { exposure: data };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error ? err.message : 'Failed to fetch exposure',
    };
  }
}

const PNG_PRESIGN_TTL_SECONDS = 3600;

/**
 * Presigned GET URLs for a batch of exposures' preview + full PNGs, keyed by
 * exposure id (epic #261, N5). The admin UI puts these straight in `<img src>`
 * — display needs no CORS, so we skip the `/api/nircam-preview` proxy hop and
 * let the browser fetch object storage directly.
 *
 * Keys are re-derived server-side from `nircam_exposures` under the admin's RLS
 * session, never trusted from the client, so this can't presign arbitrary
 * objects. Each object's home backend is read from the registry (defaulting
 * OSN, where canonical PNGs live — like the FITS route; deliberately NOT the
 * `OSN_READ_ENABLED`-gated dual-read helper, which would divert to R2 when the
 * flag is off, and legacy R2 rows still resolve via their `r2` registry entry).
 *
 * ALWAYS returns an entry for every requested id (null urls when a PNG is
 * absent or a single sign fails) so the caller can distinguish "no PNG" from
 * "still presigning" and never wedges on a spinner. URLs live ~1h (covers a
 * viewing session + the sibling prefetch window).
 */
export async function presignExposurePngs(
  ids: number[],
): Promise<Record<number, ExposurePngUrls>> {
  const out: Record<number, ExposurePngUrls> = Object.fromEntries(
    ids.map((i) => [i, { preview: null, full: null } as ExposurePngUrls]),
  );
  if (ids.length === 0) return out;
  try {
    const supabase = await requireSession();
    const { data, error } = await supabase
      .from('nircam_exposures')
      .select('id, png_path, full_png_path')
      .in('id', ids);
    if (error || !data) return out;

    const keys = [...new Set(
      data.flatMap((r) => [r.png_path, r.full_png_path].filter(Boolean) as string[]),
    )];
    if (keys.length === 0) return out;

    // Resolve each object's home backend from the registry (admin RLS sees all
    // rows); default OSN for anything unregistered — canonical PNGs are OSN.
    const { data: soRows } = await supabase
      .from('storage_objects')
      .select('storage_key, backend')
      .eq('status', 'active')
      .in('storage_key', keys);
    const backendByKey = new Map(
      (soRows ?? []).map((r) => [r.storage_key as string, r.backend as DataBackend]),
    );

    // Presign per key, resilient: one unsignable key doesn't sink the batch.
    const urlByKey = new Map<string, string>();
    await Promise.all(keys.map(async (k) => {
      try {
        const backend: DataBackend = backendByKey.get(k) === 'r2' ? 'r2' : 'osn';
        const url = await getSignedUrl(
          getS3ClientForBackend(backend),
          new GetObjectCommand({ Bucket: getBucketNameForBackend(backend), Key: k }),
          { expiresIn: PNG_PRESIGN_TTL_SECONDS },
        );
        urlByKey.set(k, url);
      } catch (err) {
        console.error(`presignExposurePngs: failed to sign ${k}:`, err);
      }
    }));

    for (const r of data) {
      out[r.id] = {
        preview: r.png_path ? urlByKey.get(r.png_path) ?? null : null,
        full: r.full_png_path ? urlByKey.get(r.full_png_path) ?? null : null,
      };
    }
    return out;
  } catch {
    return out;
  }
}

// ---------------------------------------------------------------------------
// Update
// ---------------------------------------------------------------------------

export async function updateExposureReview(
  id: number,
  updates: {
    review_status?: 'pending' | 'approved' | 'excluded';
    correction?: 'none' | 'needed' | 'done';
    notes?: string;
  },
): Promise<{ exposure: NircamExposure | null; error?: string }> {
  try {
    const supabase = await requireSession();

    const { data, error } = await supabase
      .from('nircam_exposures')
      .update({
        ...updates,
        updated_at: new Date().toISOString(),
      })
      .eq('id', id)
      .select()
      .single();

    if (error) {
      return { exposure: null, error: error.message };
    }

    return { exposure: data };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error ? err.message : 'Failed to update exposure',
    };
  }
}

// ---------------------------------------------------------------------------
// Mask polygons
// ---------------------------------------------------------------------------

/**
 * Persist the polygon list for a single exposure.
 *
 * Vertices are stored as DS9 ``image`` 1-indexed coords so the same payload
 * round-trips through ``campfire deploy pull-masks`` and ``apply_masks_step``
 * without any further transform. "Masked" is derived state: a non-empty
 * ``mask_regions`` is the sole signal; clearing all polygons nulls it.
 */
export async function saveExposureMaskRegions(
  id: number,
  regions: MaskRegionsPayload,
): Promise<{ exposure: NircamExposure | null; error?: string }> {
  try {
    const supabase = await requireSession();
    const hasPolygons = (regions?.polygons?.length ?? 0) > 0;

    const { data, error } = await supabase
      .from('nircam_exposures')
      .update({
        mask_regions: hasPolygons ? regions : null,
        updated_at: new Date().toISOString(),
      })
      .eq('id', id)
      .select()
      .single();

    if (error) {
      return { exposure: null, error: error.message };
    }
    return { exposure: data };
  } catch (err) {
    return {
      exposure: null,
      error: err instanceof Error
        ? err.message
        : 'Failed to save mask regions',
    };
  }
}

// ---------------------------------------------------------------------------
// Reduction progress (aggregated view)
// ---------------------------------------------------------------------------

// One row per (field, filter, detector) — matches the columns of the
// nircam_reduction_progress view. Callers aggregate back up to filter/field
// grain; detector grain exists to power per-detector pending quick-filters.
export interface ReductionProgress {
  field: string;
  filter: string;
  detector: string;
  total: number;
  pending_review: number;
  approved: number;
  excluded: number;
  masked: number;
  needs_correction: number;
}

export async function getReductionProgress(): Promise<{
  progress: ReductionProgress[];
  error?: string;
}> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase
      .from('nircam_reduction_progress')
      .select('*')
      .order('field')
      .order('filter')
      .order('detector');

    if (error) {
      return { progress: [], error: error.message };
    }

    return { progress: data || [] };
  } catch (err) {
    return {
      progress: [],
      error: err instanceof Error ? err.message : 'Failed to fetch progress',
    };
  }
}

// The former getExcludedExposures action (copy-paste source for fields.toml
// skip=[]) is gone: exclusions reach the pipeline automatically via
// `campfire pull` → reference/nircam/<field>/exposures.json
// (campfire.deploy.nircam_exclusions.pull_exclusions), which
// Field._load_excluded_exposures merges into the effective skip list.

// ---------------------------------------------------------------------------
// Filter options (for dropdowns)
// ---------------------------------------------------------------------------

// Backed by get_admin_exposure_facets: distinct values via grouped scans
// server-side (replaces fetching every row and deduping in JS).
export async function getExposureFilterOptions(): Promise<{
  fields: string[];
  filters: string[];
  detectors: string[];
  stages: string[];
  error?: string;
}> {
  try {
    const supabase = await requireSession();

    const { data, error } = await supabase.rpc('get_admin_exposure_facets');

    if (error) {
      return { fields: [], filters: [], detectors: [], stages: [], error: error.message };
    }

    const rows = (data ?? []) as { kind: string; value: string }[];
    const pick = (kind: string) => rows.filter((r) => r.kind === kind).map((r) => r.value);
    return {
      fields: pick('field'),
      filters: pick('filter'),
      detectors: pick('detector'),
      stages: pick('stage'),
    };
  } catch (err) {
    return {
      fields: [],
      filters: [],
      detectors: [],
      stages: [],
      error: err instanceof Error ? err.message : 'Failed to fetch filter options',
    };
  }
}
