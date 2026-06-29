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

export interface DeploymentRow {
  id: number;
  observation: string;
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

// ---------------------------------------------------------------------------
// Read: deployments (the lifecycle units)
// ---------------------------------------------------------------------------

export async function getDeployments(params?: {
  status?: DeployStatus;
  observation?: string;
  page?: number;
  pageSize?: number;
}): Promise<DeploymentsResult> {
  try {
    const { supabase } = await requireAdmin();
    const page = Math.max(0, params?.page ?? 0);
    const pageSize = Math.max(1, params?.pageSize ?? 50);
    const from = page * pageSize;
    const to = from + pageSize - 1;

    let query = supabase
      .from('deployments')
      .select(
        'id, observation, status, n_targets, n_spectra, cfpipe_version, deployed_at, published_at, revoked_at',
        { count: 'exact' },
      )
      .order('id', { ascending: false })
      .range(from, to);

    if (params?.status) query = query.eq('status', params.status);
    if (params?.observation) query = query.eq('observation', params.observation);

    const { data, count, error } = await query;
    if (error) return { deployments: [], total: 0, error: error.message };
    return { deployments: (data as DeploymentRow[]) || [], total: count ?? 0 };
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
  deployment_id: number | null;
  status_to: string | null;
  affected_count: number | null;
  occurred_at: string;
  actor: string | null;
  actor_name?: string | null;
}

export interface DeployEventsResult {
  events: DeployEventRow[];
  total: number;
  error?: string;
}

export async function getDeployEvents(params?: {
  action?: string;
  observation?: string;
  page?: number;
  pageSize?: number;
}): Promise<DeployEventsResult> {
  try {
    const { supabase } = await requireAdmin();
    const page = Math.max(0, params?.page ?? 0);
    const pageSize = Math.max(1, params?.pageSize ?? 50);
    const from = page * pageSize;
    const to = from + pageSize - 1;

    let query = supabase
      .from('deploy_events')
      .select(
        'id, action, observation, deployment_id, status_to, affected_count, occurred_at, actor',
        { count: 'exact' },
      )
      .order('occurred_at', { ascending: false })
      .range(from, to);

    if (params?.action) query = query.eq('action', params.action);
    if (params?.observation) query = query.eq('observation', params.observation);

    const { data, count, error } = await query;
    if (error) return { events: [], total: 0, error: error.message };

    const events = (data as DeployEventRow[]) || [];
    // Resolve actor uuids -> usernames in one batch (no FK to embed on).
    const actorIds = Array.from(new Set(events.map((e) => e.actor).filter(Boolean))) as string[];
    if (actorIds.length) {
      const { data: profiles } = await supabase
        .from('user_profiles')
        .select('user_id, username, full_name')
        .in('user_id', actorIds);
      const nameById = new Map(
        (profiles || []).map((p) => [p.user_id, p.full_name || p.username]),
      );
      for (const e of events) {
        if (e.actor) e.actor_name = nameById.get(e.actor) ?? null;
      }
    }
    return { events, total: count ?? 0 };
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
): Promise<{ success: boolean; updated?: number; error?: string }> {
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
    const updated = (data?.spectra?.updated as number) ?? 0;
    return { success: true, updated };
  } catch (err) {
    return {
      success: false,
      error: err instanceof Error ? err.message : 'Lifecycle transition failed',
    };
  }
}
