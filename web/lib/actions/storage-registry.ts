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

export async function getStorageObjects(params?: {
  observation?: string;
  productType?: string;
  status?: string;
  page?: number;
  pageSize?: number;
}): Promise<StorageObjectsResult> {
  try {
    const supabase = await requireAdmin();
    const page = Math.max(0, params?.page ?? 0);
    const pageSize = Math.max(1, params?.pageSize ?? 100);
    const from = page * pageSize;
    const to = from + pageSize - 1;

    let query = supabase
      .from('storage_objects')
      .select(
        'id, storage_key, product_type, instrument, observation, field, exposure_ref, ' +
        'size_bytes, content_hash, backend, status, cfpipe_version, created_at',
        { count: 'exact' },
      )
      .order('created_at', { ascending: false })
      .range(from, to);

    if (params?.observation) query = query.eq('observation', params.observation);
    if (params?.productType) query = query.eq('product_type', params.productType);
    if (params?.status) query = query.eq('status', params.status);

    const { data, count, error } = await query;
    if (error) return { objects: [], total: 0, error: error.message };
    return { objects: (data as unknown as StorageObjectRow[]) || [], total: count ?? 0 };
  } catch (err) {
    return {
      objects: [],
      total: 0,
      error: err instanceof Error ? err.message : 'Failed to load storage objects',
    };
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
