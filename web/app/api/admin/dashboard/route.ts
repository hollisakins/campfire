import { NextResponse } from 'next/server';
import { getRequestPrincipal } from '@/lib/auth/identity';
import { listReleases } from '@/lib/server/dr-releases';
import type { DrReleaseRow } from '@/lib/server/dr-exports';
import type { DeploymentRow, DeployEventRow } from '@/lib/actions/deployments';
import type { StorageBudget } from '@/lib/actions/storage-registry';

/**
 * GET /api/admin/dashboard — everything the /admin landing page renders, in
 * one response (decision D-C, #506: reads are GET routes, not actions).
 *
 * Seven reads run in parallel under the admin's cookie client; each lands in
 * its own field, and a read that fails leaves its field null with the message
 * in `errors`, so one slow or broken aggregate degrades one panel instead of
 * the page. The RPCs are admin-gated server-side on top of the check here.
 */

export const dynamic = 'force-dynamic';

const NO_STORE = { 'Cache-Control': 'private, no-store' };

export interface DashboardLease {
  scope_type: 'observation' | 'field';
  scope_key: string;
  since: string;
  heartbeat_at: string | null;
  host: string | null;
  actor_name: string | null;
  /** Heartbeat older than deploy_lease_ttl(): a crashed deploy. */
  stale: boolean;
}

export interface AdminDashboardSummary {
  generated_at: string;
  leases: DashboardLease[];
  deployments: {
    drafts: number;
    deployed_7d: number;
    unreleased_published: number;
    missing_provenance: number;
    crds_contexts: number;
    latest_deployed_at: string | null;
  };
  review: {
    nircam: {
      total: number;
      pending: number;
      done: number;
      needs_correction: number;
      top: { field: string; filter: string; pending: number }[];
    };
    objects: {
      published: number;
      uninspected: number;
      needs_review: number;
      top: { field: string; uninspected: number }[];
    };
  };
  access: { pending_requests: number; oldest_request_at: string | null };
  activity: { inspections_7d: number; inspectors_7d: number; comments_7d: number };
  sync_snapshot: {
    status: 'building' | 'ready' | 'failed';
    started_at: string;
    completed_at: string | null;
    error: string | null;
  } | null;
}

export interface RecentActivityRow {
  id: string;
  type: 'comment' | 'inspection';
  subject_kind: 'target' | 'object' | 'spectrum' | null;
  display_id: string;
  user_id: string | null;
  user_full_name: string | null;
  ts: string;
  content: string | null;
  field_name: string | null;
  old_value: number | null;
  new_value: number | null;
}

export interface ArchiveOverview {
  n_programs: number;
  n_observations: number;
  n_pointings: number;
  n_targets: number;
  n_spectra: number;
  total_size_bytes: number;
  latest_deployed_at: string | null;
  latest_cfpipe_version: string | null;
}

export type AdminDashboardSection =
  | 'summary' | 'deployments' | 'events' | 'budget' | 'releases' | 'overview' | 'activity';

export interface AdminDashboardResponse {
  summary: AdminDashboardSummary | null;
  deployments: { rows: DeploymentRow[]; total: number } | null;
  events: DeployEventRow[] | null;
  budget: StorageBudget | null;
  releases: DrReleaseRow[] | null;
  overview: ArchiveOverview | null;
  activity: RecentActivityRow[] | null;
  errors: Partial<Record<AdminDashboardSection, string>>;
}

const DEPLOYMENTS_SHOWN = 6;
const EVENTS_SHOWN = 6;
const ACTIVITY_SHOWN = 7;

function rpcError(error: { message: string } | null): never | void {
  if (error) throw new Error(error.message);
}

export async function GET() {
  const principal = await getRequestPrincipal();
  if (!principal) return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
  if (!principal.access.isAdmin) return NextResponse.json({ error: 'Admin access required' }, { status: 403 });
  const { supabase } = principal;

  const reads: { [K in AdminDashboardSection]: Promise<AdminDashboardResponse[K]> } = {
    summary: (async () => {
      const { data, error } = await supabase.rpc('get_admin_dashboard');
      rpcError(error);
      return data as unknown as AdminDashboardSummary;
    })(),
    deployments: (async () => {
      const { data, error } = await supabase.rpc('get_admin_deployments', {
        p_status: null, p_instrument: null,
        p_sort_column: 'deployed_at', p_sort_direction: 'desc',
        p_page: 1, p_page_size: DEPLOYMENTS_SHOWN,
      });
      rpcError(error);
      const rows = (data ?? []) as (DeploymentRow & { total_count: number })[];
      return {
        rows: rows.map(({ total_count: _t, ...row }) => row as DeploymentRow),
        total: rows[0]?.total_count ?? 0,
      };
    })(),
    events: (async () => {
      const { data, error } = await supabase.rpc('get_admin_deploy_events', {
        p_action: null, p_observation: null, p_field: null,
        p_sort_column: 'occurred_at', p_sort_direction: 'desc',
        p_page: 1, p_page_size: EVENTS_SHOWN,
      });
      rpcError(error);
      const rows = (data ?? []) as (DeployEventRow & { total_count: number })[];
      return rows.map(({ total_count: _t, ...row }) => row as DeployEventRow);
    })(),
    budget: (async () => {
      const { data, error } = await supabase.rpc('get_storage_budget');
      rpcError(error);
      return data as unknown as StorageBudget;
    })(),
    // The admin's cookie client: the dr_releases policy shows admins every state.
    releases: listReleases(supabase),
    overview: (async () => {
      const { data, error } = await supabase.rpc('get_database_overview', { p_include_unpublished: true });
      rpcError(error);
      const row = ((data ?? []) as Record<string, unknown>[])[0];
      if (!row) throw new Error('Empty overview');
      return {
        n_programs: Number(row.n_programs) || 0,
        n_observations: Number(row.n_observations) || 0,
        n_pointings: Number(row.n_pointings) || 0,
        n_targets: Number(row.n_targets) || 0,
        n_spectra: Number(row.n_spectra) || 0,
        total_size_bytes: Number(row.total_size_bytes) || 0,
        latest_deployed_at: (row.latest_deployed_at as string | null) ?? null,
        latest_cfpipe_version: (row.latest_cfpipe_version as string | null) ?? null,
      };
    })(),
    activity: (async () => {
      const { data, error } = await supabase.rpc('get_admin_recent_activity', { p_limit: ACTIVITY_SHOWN });
      rpcError(error);
      return (data ?? []) as RecentActivityRow[];
    })(),
  };

  const sections = Object.keys(reads) as AdminDashboardSection[];
  const settled = await Promise.allSettled(sections.map((s) => reads[s]));

  const body: AdminDashboardResponse = {
    summary: null, deployments: null, events: null, budget: null,
    releases: null, overview: null, activity: null, errors: {},
  };
  sections.forEach((section, i) => {
    const result = settled[i];
    if (result.status === 'fulfilled') {
      (body as unknown as Record<string, unknown>)[section] = result.value;
    } else {
      body.errors[section] = result.reason instanceof Error ? result.reason.message : String(result.reason);
    }
  });

  return NextResponse.json(body, { headers: NO_STORE });
}
