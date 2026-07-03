'use server';

import { createClient } from '@/lib/supabase/server';

// ---------------------------------------------------------------------------
// Intermediate-product lifecycle admin actions (epic #210, B3 / #219).
//
// The admin surface over B2's lifecycle: browse deployments and their status,
// publish / revoke / recover them (via the SECURITY DEFINER set_deployment_status
// RPC), and read the deploy_events audit log. All gated through requireAdmin();
// the RPCs are independently admin/service_role-gated server-side.
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
  return { supabase, userId: user.id };
}

export type DeployStatus = 'draft' | 'published' | 'revoked';

// Scope: exactly one of observation (NIRSpec) / field (NIRCam) is set,
// mirroring the deployments_scope_check constraint. For NIRCam rows,
// n_targets carries the exposure count (no n_spectra).
export interface DeploymentRow {
  id: number;
  observation: string | null;
  field: string | null;
  status: DeployStatus;
  n_targets: number | null;
  n_spectra: number | null;
  cfpipe_version: string | null;
  deployed_at: string | null;
  published_at: string | null;
  revoked_at: string | null;
}

export interface DeploymentsResult {
  deployments: DeploymentRow[];
  total: number;
  error?: string;
}

// Sort-key whitelists live in @/lib/admin/sort-keys — a 'use server' module
// may only export async functions.

// ---------------------------------------------------------------------------
// Read: deployments (the lifecycle units)
// ---------------------------------------------------------------------------
// Backed by the get_admin_deployments RPC: whitelisted server-side sort +
// windowed total in one scan (no PostgREST count:'exact' second query).

export async function getDeployments(params?: {
  status?: DeployStatus;
  instrument?: 'nirspec' | 'nircam';
  sortColumn?: string;
  sortDirection?: 'asc' | 'desc';
  page?: number;      // 1-based
  pageSize?: number;
}): Promise<DeploymentsResult> {
  try {
    const { supabase } = await requireAdmin();

    const { data, error } = await supabase.rpc('get_admin_deployments', {
      p_status: params?.status ?? null,
      p_instrument: params?.instrument ?? null,
      p_sort_column: params?.sortColumn ?? 'deployed_at',
      p_sort_direction: params?.sortDirection ?? 'desc',
      p_page: params?.page ?? 1,
      p_page_size: params?.pageSize ?? 50,
    });

    if (error) return { deployments: [], total: 0, error: error.message };
    const rows = (data ?? []) as (DeploymentRow & { total_count: number })[];
    const total = rows[0]?.total_count ?? 0;
    return {
      deployments: rows.map(({ total_count: _t, ...row }) => row as DeploymentRow),
      total,
    };
  } catch (err) {
    return {
      deployments: [],
      total: 0,
      error: err instanceof Error ? err.message : 'Failed to load deployments',
    };
  }
}

// ---------------------------------------------------------------------------
// Read: deploy_events audit log
// ---------------------------------------------------------------------------

export interface DeployEventRow {
  id: string;
  action: string;
  observation: string | null;
  // NIRCam scope. First-class deploy_events.field column (Phase 3), surfaced by
  // the get_admin_deploy_events RPC; NULL for NIRSpec (observation) events.
  field: string | null;
  deployment_id: number | null;
  status_to: string | null;
  affected_count: number | null;
  occurred_at: string;
  actor: string | null;
  actor_name: string | null;
}

export interface DeployEventsResult {
  events: DeployEventRow[];
  total: number;
  error?: string;
}

// Backed by get_admin_deploy_events: the NIRCam scope extraction and the
// actor-name join happen in SQL (one scan), replacing two extra round-trips.
export async function getDeployEvents(params?: {
  action?: string;
  observation?: string;
  field?: string;
  sortColumn?: string;
  sortDirection?: 'asc' | 'desc';
  page?: number;      // 1-based
  pageSize?: number;
}): Promise<DeployEventsResult> {
  try {
    const { supabase } = await requireAdmin();

    const { data, error } = await supabase.rpc('get_admin_deploy_events', {
      p_action: params?.action ?? null,
      p_observation: params?.observation ?? null,
      p_field: params?.field ?? null,
      p_sort_column: params?.sortColumn ?? 'occurred_at',
      p_sort_direction: params?.sortDirection ?? 'desc',
      p_page: params?.page ?? 1,
      p_page_size: params?.pageSize ?? 50,
    });

    if (error) return { events: [], total: 0, error: error.message };
    const rows = (data ?? []) as (DeployEventRow & { total_count: number })[];
    const total = rows[0]?.total_count ?? 0;
    return {
      events: rows.map(({ total_count: _t, ...row }) => row as DeployEventRow),
      total,
    };
  } catch (err) {
    return {
      events: [],
      total: 0,
      error: err instanceof Error ? err.message : 'Failed to load audit log',
    };
  }
}

// ---------------------------------------------------------------------------
// Mutate: publish / revoke / recover a deployment
// ---------------------------------------------------------------------------

export type LifecycleAction = 'publish' | 'revoke' | 'recover';

const ACTION_TO_STATUS: Record<LifecycleAction, DeployStatus> = {
  publish: 'published',
  revoke: 'revoked',
  recover: 'published',
};

export async function setDeploymentLifecycle(
  deploymentId: number,
  action: LifecycleAction,
): Promise<{
  success: boolean;
  updated?: number;
  updatedKind?: 'spectra' | 'images';
  error?: string;
}> {
  try {
    const { supabase, userId } = await requireAdmin();
    const toStatus = ACTION_TO_STATUS[action];
    if (!toStatus) return { success: false, error: `Unknown action: ${action}` };

    // set_deployment_status flips the deployment + its spectra + recomputes
    // has_published_spectrum + writes audit rows, atomically server-side.
    const { data, error } = await supabase.rpc('set_deployment_status', {
      p_deployment_id: deploymentId,
      p_to: toStatus,
      p_actor: userId,
    });

    if (error) return { success: false, error: error.message };
    // The RPC's result shape is instrument-dependent: NIRSpec deployments
    // report {spectra: {updated}}, NIRCam deployments {nircam_images: {updated}}.
    const spectraUpdated = data?.spectra?.updated as number | undefined;
    const imagesUpdated = data?.nircam_images?.updated as number | undefined;
    const updated = spectraUpdated ?? imagesUpdated ?? 0;
    const updatedKind = spectraUpdated != null ? 'spectra' as const : 'images' as const;
    return { success: true, updated, updatedKind };
  } catch (err) {
    return {
      success: false,
      error: err instanceof Error ? err.message : 'Lifecycle transition failed',
    };
  }
}
