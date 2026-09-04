'use server';

import { getRequestIdentity, requireAdmin as requireAdminIdentity } from '@/lib/auth/identity';
import { GetObjectCommand } from '@aws-sdk/client-s3';
import { getSignedUrl } from '@aws-sdk/s3-request-presigner';
import { getS3ClientForBackend, getBucketNameForBackend, type DataBackend } from '@/lib/storage';
import { filenameSearchPattern } from '@/lib/admin/exposure-search';
import type { NircamExposure, MaskRegionsPayload } from '@/lib/types';

export interface ExposurePngUrls {
  preview: string | null;
  full: string | null;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function requireAdmin() {
  const { supabase } = await requireAdminIdentity();
  return supabase;
}

// Fast-path guard for the triage hot loop (save + prev/next + prefetch).
// requireAdmin() resolves the memoized access context purely to produce a
// clean error message: every table and RPC the hot-path functions touch is
// already admin-gated at the database (nircam_exposures RLS policies; the
// get_admin_exposure_* RPCs' explicit is_admin() checks), so a non-admin
// session gets zero rows or an RPC error, never data. Here we only confirm a
// verified session exists (a cookie read + local signature check, no network)
// and let the database be the authority. Keep requireAdmin for anything not
// fully RLS/RPC-gated (e.g. the nircam_reduction_progress view).
async function requireSession() {
  const { user, supabase } = await getRequestIdentity();
  if (!user) throw new Error('Not authenticated');
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
  /** Raw wildcard filename search ("jw01727001*"); glob → ILIKE happens at the RPC boundary. */
  search?: string;
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
    p_search: filenameSearchPattern(params?.search),
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

// ---------------------------------------------------------------------------
// Related exposures (same visit / simultaneous SW+LW)
// ---------------------------------------------------------------------------

export interface RelatedExposure {
  id: number;
  filename: string;
  filter: string;
  detector: string;
  review_status: NircamExposure['review_status'];
  masked: boolean;
}

export interface RelatedExposuresResult {
  /**
   * Exposures read out at the same moment as this one through the other
   * channel of the same module: NIRCam's dichroic feeds a module's SW
   * (nrc[ab]1-4) and LW (nrc[ab]long) detectors from the same sky
   * simultaneously, so an artifact here (satellite trail, scattered light,
   * a bright transient) usually shows up in these too.
   */
  simultaneous: RelatedExposure[];
  /** Everything else sharing this exposure's visit (self + simultaneous excluded). */
  sameVisit: RelatedExposure[];
  error?: string;
}

// JWST filename: {visit}_{visitgroup+seq+activity}_{exposure}_{detector}[.fits]
function parseExposureName(filename: string): { act: string; exp: string } | null {
  const parts = filename.replace(/\.fits$/, '').split('_');
  return parts.length >= 4 ? { act: parts[1], exp: parts[2] } : null;
}

// Mirrors the pipeline's module_of/channel_of (campfire_pipeline/nircam/
// association.py): the deployed `detector` column holds the FITS DETECTOR
// header value verbatim, which is uppercase (NRCALONG) — and nrca5/nrcb5 are
// aliases for the LW detectors — so normalize before classifying.
const detectorModule = (detector: string) => detector.toLowerCase().slice(0, 4); // 'nrca' | 'nrcb'
function detectorChannel(detector: string): 'sw' | 'lw' {
  const d = detector.toLowerCase();
  return d.endsWith('long') || d.endsWith('5') ? 'lw' : 'sw';
}

/**
 * The other exposures a triage decision here might implicate: everything in
 * the same visit (split out: the simultaneously-read other-channel exposures
 * of the same module). Feeds the detail page's "Related exposures" panel —
 * quick jumps to siblings that likely share the same problem.
 */
export async function getRelatedExposures(id: number): Promise<RelatedExposuresResult> {
  const empty: RelatedExposuresResult = { simultaneous: [], sameVisit: [] };
  try {
    const supabase = await requireSession();

    const { data: cur, error } = await supabase
      .from('nircam_exposures')
      .select('id, field, visit, filename, detector')
      .eq('id', id)
      .single();
    if (error) return { ...empty, error: error.message };
    // No visit recorded (pre-backfill row or unparseable filename) — nothing
    // to relate on.
    if (!cur?.visit) return empty;

    const { data, error: listErr } = await supabase
      .from('nircam_exposures')
      .select('id, filename, filter, detector, review_status, mask_regions')
      .eq('field', cur.field)
      .eq('visit', cur.visit)
      .neq('id', id)
      .order('filename');
    if (listErr) return { ...empty, error: listErr.message };

    const curName = parseExposureName(cur.filename);
    const curModule = detectorModule(cur.detector);
    const curChannel = detectorChannel(cur.detector);

    const out = empty;
    for (const r of data ?? []) {
      const rel: RelatedExposure = {
        id: r.id,
        filename: r.filename,
        filter: r.filter,
        detector: r.detector,
        review_status: r.review_status,
        masked: ((r.mask_regions as MaskRegionsPayload | null)?.polygons?.length ?? 0) > 0,
      };
      const name = parseExposureName(r.filename);
      const simultaneous = !!curName && !!name &&
        name.act === curName.act && name.exp === curName.exp &&
        detectorModule(r.detector) === curModule &&
        detectorChannel(r.detector) !== curChannel;
      (simultaneous ? out.simultaneous : out.sameVisit).push(rel);
    }
    return out;
  } catch (err) {
    return {
      ...empty,
      error: err instanceof Error ? err.message : 'Failed to fetch related exposures',
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
