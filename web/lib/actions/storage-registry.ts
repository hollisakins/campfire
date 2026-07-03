'use server';

import { createClient } from '@/lib/supabase/server';

// ---------------------------------------------------------------------------
// Storage-registry (Axis A) admin actions — epic #210, B5.
//
// Read-only browser over `storage_objects`, the shadow index of every object in
// cloud storage. This is the "what's in the cloud" view (orthogonal to the
// draft/publish lifecycle, which lives on the Deployments page). Populated by
// every deploy — including the canonical spectrum-exposure intermediates.
// `storage_objects` RLS is admin-only; requireAdmin is defense-in-depth.
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

export interface StorageObjectRow {
  id: number;
  storage_key: string;
  product_type: string;
  instrument: string | null;
  observation: string | null;
  field: string | null;
  exposure_ref: string | null;
  size_bytes: number;
  content_hash: string;
  backend: string;
  status: string;
  cfpipe_version: string | null;
  created_at: string;
}

export interface StorageObjectsResult {
  objects: StorageObjectRow[];
  total: number;
  error?: string;
}

/** Sort keys accepted by get_admin_storage_objects — mirror the RPC whitelist. */
export const STORAGE_OBJECT_SORT_KEYS = [
  'created_at', 'size_bytes', 'product_type', 'storage_key', 'observation', 'field', 'status',
] as const;

// Backed by the get_admin_storage_objects RPC: whitelisted server-side sort +
// windowed total in one scan (the previous count:'exact' ran a second full
// COUNT over the registry — its largest-table hot path).
export async function getStorageObjects(params?: {
  observation?: string;
  field?: string;
  productType?: string;
  status?: string;
  backend?: string;
  sortColumn?: string;
  sortDirection?: 'asc' | 'desc';
  page?: number;      // 1-based
  pageSize?: number;
}): Promise<StorageObjectsResult> {
  try {
    const supabase = await requireAdmin();

    const { data, error } = await supabase.rpc('get_admin_storage_objects', {
      p_product_type: params?.productType ?? null,
      p_status: params?.status ?? null,
      p_field: params?.field ?? null,
      p_observation: params?.observation ?? null,
      p_backend: params?.backend ?? null,
      p_sort_column: params?.sortColumn ?? 'created_at',
      p_sort_direction: params?.sortDirection ?? 'desc',
      p_page: params?.page ?? 1,
      p_page_size: params?.pageSize ?? 50,
    });

    if (error) return { objects: [], total: 0, error: error.message };
    const rows = (data ?? []) as (StorageObjectRow & { total_count: number })[];
    const total = rows[0]?.total_count ?? 0;
    return {
      objects: rows.map(({ total_count: _t, ...row }) => row as StorageObjectRow),
      total,
    };
  } catch (err) {
    return {
      objects: [],
      total: 0,
      error: err instanceof Error ? err.message : 'Failed to load storage objects',
    };
  }
}

export interface StorageFacets {
  productTypes: string[];
  statuses: string[];
  backends: string[];
  fields: string[];
  observations: string[];
  error?: string;
}

// Distinct facet values for the filter dropdowns (get_admin_storage_facets —
// grouped scans server-side, replacing the hardcoded 5-of-22 product list).
export async function getStorageFacets(): Promise<StorageFacets> {
  const empty: StorageFacets = {
    productTypes: [], statuses: [], backends: [], fields: [], observations: [],
  };
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase.rpc('get_admin_storage_facets');
    if (error) return { ...empty, error: error.message };
    const rows = (data ?? []) as { kind: string; value: string }[];
    const pick = (kind: string) => rows.filter((r) => r.kind === kind).map((r) => r.value);
    return {
      productTypes: pick('product_type'),
      statuses: pick('status'),
      backends: pick('backend'),
      fields: pick('field'),
      observations: pick('observation'),
    };
  } catch (err) {
    return { ...empty, error: err instanceof Error ? err.message : 'Failed to load facets' };
  }
}

export interface StorageBudget {
  total_bytes: number;
  cap_bytes: number;
  pct_used: number;
  by_product_type: Record<string, number> | null;
  by_backend: Record<string, number> | null;
  error?: string;
}

export async function getStorageBudget(): Promise<StorageBudget | { error: string }> {
  try {
    const supabase = await requireAdmin();
    const { data, error } = await supabase.rpc('get_storage_budget');
    if (error) return { error: error.message };
    return data as unknown as StorageBudget;
  } catch (err) {
    return { error: err instanceof Error ? err.message : 'Failed to load budget' };
  }
}
