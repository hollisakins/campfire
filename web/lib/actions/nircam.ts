'use server';

import { createClient } from '@/lib/supabase/server';
import { paginateQuery } from '@/lib/supabase/paginate';
import { generateDownloadUrls } from '@/lib/r2';
import type {
  NircamImage, NircamExpmap, NircamFieldCard, NircamFieldSummary,
} from '@/lib/types';

// Presigned-GET lifetime for <img> sources (layout / expmap plots / thumbnails).
// Raw presigned URLs go straight into <img> tags — no proxy hop, no CORS needed
// (same pattern as the admin exposure previews) — so this just needs to outlive
// a browsing session.
const IMG_PRESIGN_TTL_SECONDS = 21600;

export interface NircamImagesResult {
  images: NircamImage[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamExpmapsResult {
  expmaps: NircamExpmap[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamFieldsResult {
  fields: NircamFieldCard[];
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamFieldSummaryResult {
  summary: NircamFieldSummary | null;
  error?: string;
  isAuthenticated: boolean;
}

export interface NircamFieldImagesResult {
  /** Presigned GET for the field's <field>_layout.png, if deployed. */
  layoutUrl: string | null;
  /** filter -> presigned GET for its dark expmap plot PNG. */
  expmapPlots: Record<string, string>;
  /** mosaic base key (thumb key minus `_thumb.png`) -> presigned GET. */
  thumbnails: Record<string, string>;
  error?: string;
}

/**
 * Fetch NIRCam images from the database, optionally scoped to one field.
 * Requires authentication but no program-based access control.
 * Returns all matching images for client-side filtering/sorting.
 */
export async function getNircamImages(field?: string): Promise<NircamImagesResult> {
  const supabase = await createClient();

  // Check if user is authenticated
  const { data: { user } } = await supabase.auth.getUser();

  if (!user) {
    return {
      images: [],
      isAuthenticated: false,
    };
  }

  try {
    const { data, error } = await paginateQuery<NircamImage>(
      () => {
        let q = supabase
          .from('nircam_images')
          .select('*');
        if (field) q = q.eq('field', field);
        return q
          .order('field', { ascending: true })
          .order('filter', { ascending: true })
          .order('tile', { ascending: true })
          .order('id', { ascending: true });
      },
    );

    if (error) {
      console.error('Error fetching NIRCam images:', error);
      return {
        images: [],
        error: error.message,
        isAuthenticated: true,
      };
    }

    return {
      images: data,
      isAuthenticated: true,
    };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam images:', err);
    return {
      images: [],
      error: 'An unexpected error occurred',
      isAuthenticated: true,
    };
  }
}

/**
 * Fetch the per-(field, filter) exposure-coverage maps a user may see.
 *
 * Expmaps are registered in `storage_objects` (product_type `nircam_expmap`) and
 * their visibility rides the owning field deployment via RLS: a published field
 * deployment is public to everyone, drafts stay admin-only. We simply select
 * active rows and let `select_storage_objects_by_access` do the gating — no
 * bespoke access logic here, mirroring how mosaics rely on `nircam_images` RLS.
 */
export async function getNircamExpmaps(field?: string): Promise<NircamExpmapsResult> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { expmaps: [], isAuthenticated: false };
  }

  try {
    const { data, error } = await paginateQuery<{
      field: string; filter: string | null; storage_key: string;
      size_bytes: number | null;
    }>(
      () => {
        let q = supabase
          .from('storage_objects')
          .select('field, filter, storage_key, size_bytes')
          .eq('product_type', 'nircam_expmap')
          .eq('status', 'active');
        if (field) q = q.eq('field', field);
        return q
          .order('field', { ascending: true })
          .order('filter', { ascending: true });
      },
    );

    if (error) {
      console.error('Error fetching NIRCam expmaps:', error);
      return { expmaps: [], error: error.message, isAuthenticated: true };
    }

    const expmaps: NircamExpmap[] = data
      .filter((r) => r.filter)  // per-filter product; skip any unscoped row
      .map((r) => ({
        field: r.field,
        filter: r.filter as string,
        storage_key: r.storage_key,
        file_size: r.size_bytes ?? undefined,
      }));

    return { expmaps, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam expmaps:', err);
    return { expmaps: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * The /nircam landing grid: one card per field the caller can see any mosaic
 * of (get_nircam_fields RPC — SECURITY INVOKER, so nircam_images RLS scopes
 * non-admins to published data). Each row's layout key is presigned into a
 * ready-to-render <img> URL here so the client never handles storage keys.
 */
export async function getNircamFields(): Promise<NircamFieldsResult> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { fields: [], isAuthenticated: false };
  }

  try {
    const { data, error } = await supabase.rpc('get_nircam_fields');
    if (error) {
      console.error('Error fetching NIRCam fields:', error);
      return { fields: [], error: error.message, isAuthenticated: true };
    }

    const rows = (data ?? []) as (Omit<NircamFieldCard, 'layout_url'> & {
      layout_key: string | null;
    })[];

    // Presign the layout plots in one dual-read batch; a failed presign just
    // leaves that card's preview null (the card renders a placeholder).
    const keys = rows.map((r) => r.layout_key).filter(Boolean) as string[];
    const urlByKey = new Map<string, string>();
    if (keys.length > 0) {
      try {
        const urls = await generateDownloadUrls(keys, IMG_PRESIGN_TTL_SECONDS);
        keys.forEach((k, i) => urlByKey.set(k, urls[i]));
      } catch (err) {
        console.error('Error presigning NIRCam layout plots:', err);
      }
    }

    const fields: NircamFieldCard[] = rows.map(({ layout_key, ...r }) => ({
      ...r,
      layout_url: layout_key ? urlByKey.get(layout_key) ?? null : null,
    }));

    return { fields, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam fields:', err);
    return { fields: [], error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * The /nircam/[field] overview (get_nircam_field_summary RPC). Returns null
 * for a field the caller can't see any mosaic of — the page treats unknown
 * and unauthorized fields identically.
 */
export async function getNircamFieldSummary(field: string): Promise<NircamFieldSummaryResult> {
  const supabase = await createClient();

  const { data: { user } } = await supabase.auth.getUser();
  if (!user) {
    return { summary: null, isAuthenticated: false };
  }

  try {
    const { data, error } = await supabase.rpc('get_nircam_field_summary', {
      p_field: field,
    });
    if (error) {
      console.error('Error fetching NIRCam field summary:', error);
      return { summary: null, error: error.message, isAuthenticated: true };
    }

    const row = ((data ?? []) as (NircamFieldSummary & { layout_key: string | null })[])[0];
    if (!row) return { summary: null, isAuthenticated: true };

    // layout_key is presigned separately by getNircamFieldImages; strip it.
    const { layout_key: _lk, ...summary } = row as NircamFieldSummary & { layout_key: string | null };
    void _lk;
    return { summary, isAuthenticated: true };
  } catch (err) {
    console.error('Unexpected error fetching NIRCam field summary:', err);
    return { summary: null, error: 'An unexpected error occurred', isAuthenticated: true };
  }
}

/**
 * Presigned <img> URLs for one field's plot products: the field layout PNG,
 * the per-filter dark expmap plots, and the per-mosaic thumbnails.
 *
 * The key set is derived entirely server-side from `storage_objects` under the
 * caller's RLS session (published deployments visible to all authed users,
 * drafts admin-only), so no client-supplied key is ever presigned. Raw
 * presigned GET URLs go straight into <img> tags — browsers don't apply CORS
 * to image rendering, so no proxy hop is needed (the same pattern as the
 * admin exposure previews).
 */
export async function getNircamFieldImages(field: string): Promise<NircamFieldImagesResult> {
  const empty: NircamFieldImagesResult = { layoutUrl: null, expmapPlots: {}, thumbnails: {} };

  const supabase = await createClient();
  const { data: { user } } = await supabase.auth.getUser();
  if (!user) return empty;

  try {
    const { data, error } = await paginateQuery<{
      storage_key: string; product_type: string; filter: string | null;
    }>(
      () => supabase
        .from('storage_objects')
        .select('storage_key, product_type, filter')
        .eq('field', field)
        .eq('status', 'active')
        .in('product_type', ['nircam_layout', 'nircam_expmap_plot', 'nircam_mosaic_thumbnail'])
        .order('storage_key'),
    );

    if (error) {
      console.error('Error fetching NIRCam field images:', error);
      return { ...empty, error: error.message };
    }
    if (data.length === 0) return empty;

    const keys = data.map((r) => r.storage_key);
    const urls = await generateDownloadUrls(keys, IMG_PRESIGN_TTL_SECONDS);
    const urlByKey = new Map(keys.map((k, i) => [k, urls[i]]));

    const result: NircamFieldImagesResult = { layoutUrl: null, expmapPlots: {}, thumbnails: {} };
    for (const row of data) {
      const url = urlByKey.get(row.storage_key);
      if (!url) continue;
      if (row.product_type === 'nircam_layout') {
        result.layoutUrl = url;
      } else if (row.product_type === 'nircam_expmap_plot' && row.filter) {
        result.expmapPlots[row.filter] = url;
      } else if (row.product_type === 'nircam_mosaic_thumbnail') {
        // Key thumbnails by mosaic base so the table can match a mosaic row
        // via its own file_path minus `_<ext>.fits`.
        result.thumbnails[row.storage_key.replace(/_thumb\.png$/, '')] = url;
      }
    }

    return result;
  } catch (err) {
    console.error('Unexpected error fetching NIRCam field images:', err);
    return { ...empty, error: 'An unexpected error occurred' };
  }
}
