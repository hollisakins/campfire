'use server';

import { createClient } from '@/lib/supabase/server';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { getS3ClientForBackend, getBucketNameForBackend, type DataBackend } from '@/lib/storage';
import { EXPOSURE_SORT_KEYS } from '@/lib/admin/sort-keys';
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

export interface ExposurePngManifest {
  /**
   * One entry per exposure in the filtered set that HAS a display PNG (the
   * full-res mask surface when deployed, else the preview — the byte
   * /api/nircam-png serves), in the same order the list shows. Exposures with
   * no PNG at all are omitted: they can't be downloaded, so they must not
   * count toward the pre-download button or keep it enabled. `bytes` is the
   * object's exact registry size, or null for a key the registry doesn't
   * know (the size hint then reads as a lower bound).
   */
  entries: { id: number; bytes: number | null }[];
  error?: string;
}

/**
 * The PNG pre-download manifest for a filtered set: which exposures have a
 * downloadable display PNG, in inspection order, each with its exact size
 * from the storage_objects registry. Feeds both halves of the list page's
 * pre-cache control — the warm's id list (lib/nircam-png-store.ts) and the
 * bytes-needed hint, which is real registry data rather than a per-image
 * heuristic (full-res PNG sizes are content-dependent and vary ~2× between
 * fields). Paged internally (PostgREST caps a single response at 1000 rows)
 * with a hard cap as a runaway guard.
 */
export async function getNircamExposurePngManifest(
  params?: ExposureFilters & ExposureSort,
): Promise<ExposurePngManifest> {
  const CHUNK = 1000;
  const CAP = 20000;
  // storage_objects lookups go out as GET query strings; ~100 keys per
  // request keeps the URL comfortably under proxy limits.
  const SIZE_CHUNK = 100;
  try {
    const supabase = await requireSession();
    const sortCol =
      params?.sortColumn && (EXPOSURE_SORT_KEYS as readonly string[]).includes(params.sortColumn)
        ? params.sortColumn
        : 'filename';
    const asc = (params?.sortDirection ?? 'asc') === 'asc';

    const rows: { id: number; key: string }[] = [];
    for (let off = 0; off < CAP; off += CHUNK) {
      let q = supabase.from('nircam_exposures').select('id, png_path, full_png_path');
      if (params?.field) q = q.eq('field', params.field);
      if (params?.filter) q = q.eq('filter', params.filter);
      if (params?.detector) q = q.eq('detector', params.detector);
      if (params?.reviewStatus) q = q.eq('review_status', params.reviewStatus);
      if (params?.stage) q = q.eq('stage', params.stage);
      if (params?.correction) q = q.eq('correction', params.correction);
      // Mirror get_admin_exposures' ORDER BY: 'filename' is the compound
      // (field, filter, filename) list order; everything gets the id tiebreak.
      if (sortCol === 'filename') {
        q = q
          .order('field', { ascending: asc })
          .order('filter', { ascending: asc })
          .order('filename', { ascending: asc });
      } else {
        q = q.order(sortCol, { ascending: asc, nullsFirst: false });
      }
      q = q.order('id', { ascending: true }).range(off, off + CHUNK - 1);

      const { data, error } = await q;
      if (error) return { entries: [], error: error.message };
      for (const r of data ?? []) {
        // Same display-byte rule as /api/nircam-png: full-res wins.
        const key = (r.full_png_path ?? r.png_path) as string | null;
        if (key) rows.push({ id: r.id as number, key });
      }
      if (!data || data.length < CHUNK) break;
    }

    const keys = [...new Set(rows.map((r) => r.key))];
    const sizeByKey = new Map<string, number>();
    const chunks: string[][] = [];
    for (let i = 0; i < keys.length; i += SIZE_CHUNK) {
      chunks.push(keys.slice(i, i + SIZE_CHUNK));
    }
    // Best-effort: a failed size lookup leaves those entries at bytes:null
    // (hint degrades to a lower bound) rather than failing the manifest.
    await Promise.all(chunks.map(async (chunk) => {
      const { data } = await supabase
        .from('storage_objects')
        .select('storage_key, size_bytes')
        .eq('status', 'active')
        .in('storage_key', chunk);
      for (const r of data ?? []) {
        if (typeof r.size_bytes === 'number') sizeByKey.set(r.storage_key, r.size_bytes);
      }
    }));

    return { entries: rows.map((r) => ({ id: r.id, bytes: sizeByKey.get(r.key) ?? null })) };
  } catch (err) {
    return {
      entries: [],
      error: err instanceof Error ? err.message : 'Failed to fetch PNG manifest',
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

export interface PresignExposurePngsResult {
  /**
   * Presigned urls per requested id. An id maps to null urls when the
   * exposure genuinely has no PNG (cacheable by the client), and is ABSENT
   * when its urls could not be produced this call (retryable) — the
   * distinction matters because the client caches these for ~50 min, and a
   * transient failure cached as "no PNG" used to blank the viewer for the
   * rest of the session.
   */
  urls: Record<number, ExposurePngUrls>;
  /** Set when any requested id could not be signed (those ids are omitted). */
  error?: string;
}

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
 * URLs live ~1h (covers a viewing session + the sibling prefetch window).
 */
export async function presignExposurePngs(
  ids: number[],
): Promise<PresignExposurePngsResult> {
  if (ids.length === 0) return { urls: {} };
  try {
    const supabase = await requireSession();
    const { data, error } = await supabase
      .from('nircam_exposures')
      .select('id, png_path, full_png_path')
      .in('id', ids);
    if (error || !data) {
      return { urls: {}, error: error?.message ?? 'Exposure lookup failed' };
    }

    // An id the select didn't return doesn't exist (or isn't visible) — no
    // PNG will ever materialize for it, so a null-urls entry is cacheable.
    const out: Record<number, ExposurePngUrls> = Object.fromEntries(
      ids.map((i) => [i, { preview: null, full: null } as ExposurePngUrls]),
    );

    const keys = [...new Set(
      data.flatMap((r) => [r.png_path, r.full_png_path].filter(Boolean) as string[]),
    )];
    if (keys.length === 0) return { urls: out };

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
    const failedKeys = new Set<string>();
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
        failedKeys.add(k);
      }
    }));

    let anyFailed = false;
    for (const r of data) {
      const rowFailed =
        (r.png_path && failedKeys.has(r.png_path)) ||
        (r.full_png_path && failedKeys.has(r.full_png_path));
      if (rowFailed) anyFailed = true;
      const preview = r.png_path ? urlByKey.get(r.png_path) ?? null : null;
      const full = r.full_png_path ? urlByKey.get(r.full_png_path) ?? null : null;
      if (rowFailed && preview === null && full === null) {
        // Nothing signed for this row — retryable, not "no PNG": omit the id
        // entirely so the client doesn't cache the failure.
        delete out[r.id];
        continue;
      }
      // Per-key, not per-row: one unsignable key (e.g. a mixed-backend row
      // whose other backend is misconfigured) must not sink a URL that DID
      // sign — the viewer falls back full↔preview on its own.
      out[r.id] = { preview, full };
    }
    return anyFailed
      ? { urls: out, error: 'Failed to sign some exposure image URLs' }
      : { urls: out };
  } catch (err) {
    return {
      urls: {},
      error: err instanceof Error ? err.message : 'Presign failed',
    };
  }
}

// ---------------------------------------------------------------------------
// Update
// ---------------------------------------------------------------------------

// Triage review updates (review_status / correction / notes) intentionally
// have no server action: they go through POST /api/admin/nircam/review via
// the durable outbox (lib/nircam-review-outbox.ts), which needs a transport
// with timeout, retry, and keepalive — none of which server actions expose.
// The route carries the review_decided_at last-writer-wins guard that makes
// its at-least-once delivery safe.

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
