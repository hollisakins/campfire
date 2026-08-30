'use server';

import { createClient } from '@/lib/supabase/server';

// ---------------------------------------------------------------------------
// Admin dashboard actions (2026-08 control-center redesign).
//
// Thin wrappers over the three consolidated dashboard RPCs plus two existing
// RPCs the panel never called (get_lifecycle_status, get_database_overview
// with drafts included) and a server-side path for get_download_stats (the
// downloads page reaches it through the browser client; the dashboard keeps
// one data-access pattern). Every action reports failure in-band via `error`
// so the page can render an explicit unavailable state — never a zero.
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

// --------------------------------------------------------------------------
// get_admin_dashboard_summary
// --------------------------------------------------------------------------

export interface DashboardScope {
  kind: 'observation' | 'field';
  name: string;
  program: string | null;
  created_at: string | null;
  last_deploy_at: string | null;
  last_deploy_status: string | null;
  config_never_pushed: boolean;
  retired: boolean;
}

export interface DashboardSignup {
  user_id: string;
  username: string;
  full_name: string;
  created_at: string;
  can_inspect: boolean;
  is_admin: boolean;
  is_group_account: boolean;
  n_programs: number;
}

export interface DashboardSummary {
  deployments: {
    drafts: number;
    oldest_draft_at: string | null;
    deploys_7d: number;
    unreleased_published: number;
    missing_provenance: number;
    distinct_crds_contexts: number;
    latest_deploy_at: string | null;
  };
  review: {
    nircam_total: number;
    nircam_pending: number;
    nircam_done: number;
    nircam_needs_correction: number;
    rate_total: number;
    rate_pending: number;
    rate_done: number;
    nods_total: number;
    nods_pending: number;
    nods_done: number;
    published_fields_with_pending: number;
    published_obs_with_pending: number;
  };
  storage: {
    reclaimable_bytes: number;
    reclaimable_count: number;
    provisional_hashes: number;
    pushed_undeployed_14d: number;
    registered_7d: number;
    bytes_added_7d: number;
  };
  access: {
    pending_requests: number;
    oldest_request_at: string | null;
    open_invites: number;
    stale_invites: number;
    active_share_links: number;
    links_exposing_drafts: number;
    active_codes: number;
    codes_all_programs: number;
    codes_expiring_soon: number;
  };
  users: {
    total: number;
    admins: number;
    inspectors: number;
    group_accounts: number;
    signups_30d: number;
    unprovisioned_30d: number;
    recent_signups: DashboardSignup[];
  };
  objects: {
    inactive: number;
    stale: number;
    uninspected_published: number;
  };
  activity: {
    inspections_7d: number;
    comments_7d: number;
    active_inspectors_7d: number;
  };
  scopes: {
    config_never_pushed: number;
    retired_with_live_deployment: number;
    never_deployed: number;
    new_scopes: DashboardScope[];
  };
}

export interface DashboardSummaryResult {
  summary: DashboardSummary | null;
  error?: string;
}

export async function getDashboardSummary(): Promise<DashboardSummaryResult> {
  try {
    const { supabase } = await requireAdmin();
    const { data, error } = await supabase.rpc('get_admin_dashboard_summary');
    if (error) return { summary: null, error: error.message };
    return { summary: data as unknown as DashboardSummary };
  } catch (err) {
    return {
      summary: null,
      error: err instanceof Error ? err.message : 'Failed to load dashboard summary',
    };
  }
}

// --------------------------------------------------------------------------
// get_admin_review_queues
// --------------------------------------------------------------------------

export interface NircamQueueScope {
  field: string;
  filter: string;
  total: number;
  pending: number;
  done: number;
  masked: number;
  needs_correction: number;
}

export interface RateQueueScope {
  observation: string;
  total: number;
  pending: number;
  done: number;
  masked: number;
}

export interface NodsQueueScope {
  observation: string;
  total: number;
  pending: number;
  done: number;
  sources: number;
}

export interface ObjectsQueueScope {
  field: string;
  published: number;
  uninspected: number;
  stale: number;
  inactive: number;
}

export interface ReviewQueues {
  nircam: {
    total: number; pending: number; done: number; needs_correction: number;
    top: NircamQueueScope[];
  };
  rate: { total: number; pending: number; done: number; top: RateQueueScope[] };
  nods: { total: number; pending: number; done: number; top: NodsQueueScope[] };
  objects: {
    published: number; uninspected: number; stale: number; inactive: number;
    top: ObjectsQueueScope[];
  };
}

export interface ReviewQueuesResult {
  queues: ReviewQueues | null;
  error?: string;
}

export async function getReviewQueues(): Promise<ReviewQueuesResult> {
  try {
    const { supabase } = await requireAdmin();
    const { data, error } = await supabase.rpc('get_admin_review_queues');
    if (error) return { queues: null, error: error.message };
    return { queues: data as unknown as ReviewQueues };
  } catch (err) {
    return {
      queues: null,
      error: err instanceof Error ? err.message : 'Failed to load review queues',
    };
  }
}

// --------------------------------------------------------------------------
// get_admin_recent_activity
// --------------------------------------------------------------------------

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

export interface RecentActivityResult {
  rows: RecentActivityRow[] | null;
  error?: string;
}

export async function getRecentAdminActivity(limit = 8): Promise<RecentActivityResult> {
  try {
    const { supabase } = await requireAdmin();
    const { data, error } = await supabase.rpc('get_admin_recent_activity', {
      p_limit: limit,
    });
    if (error) return { rows: null, error: error.message };
    return { rows: (data ?? []) as RecentActivityRow[] };
  } catch (err) {
    return {
      rows: null,
      error: err instanceof Error ? err.message : 'Failed to load activity',
    };
  }
}

// --------------------------------------------------------------------------
// get_lifecycle_status — existing RPC, never called by the web before.
// Effectively constant per deploy; cache for the session.
// --------------------------------------------------------------------------

export interface LifecycleStatus {
  enabled: boolean;
  version: number;
  checks: Record<string, boolean>;
}

export interface LifecycleStatusResult {
  status: LifecycleStatus | null;
  error?: string;
}

export async function getLifecycleStatus(): Promise<LifecycleStatusResult> {
  try {
    const { supabase } = await requireAdmin();
    const { data, error } = await supabase.rpc('get_lifecycle_status');
    if (error) return { status: null, error: error.message };
    return { status: data as unknown as LifecycleStatus };
  } catch (err) {
    return {
      status: null,
      error: err instanceof Error ? err.message : 'Failed to load lifecycle status',
    };
  }
}

// --------------------------------------------------------------------------
// get_database_overview with drafts included — the admin footer wants the
// whole archive, not just the published slice the public wrapper requests.
// --------------------------------------------------------------------------

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

export interface ArchiveOverviewResult {
  overview: ArchiveOverview | null;
  error?: string;
}

export async function getArchiveOverview(): Promise<ArchiveOverviewResult> {
  try {
    const { supabase } = await requireAdmin();
    const { data, error } = await supabase.rpc('get_database_overview', {
      p_include_unpublished: true,
    });
    if (error) return { overview: null, error: error.message };
    const row = (data ?? [])[0];
    if (!row) return { overview: null, error: 'Empty overview' };
    return {
      overview: {
        n_programs: Number(row.n_programs) || 0,
        n_observations: Number(row.n_observations) || 0,
        n_pointings: Number(row.n_pointings) || 0,
        n_targets: Number(row.n_targets) || 0,
        n_spectra: Number(row.n_spectra) || 0,
        total_size_bytes: Number(row.total_size_bytes) || 0,
        latest_deployed_at: row.latest_deployed_at ?? null,
        latest_cfpipe_version: row.latest_cfpipe_version ?? null,
      },
    };
  } catch (err) {
    return {
      overview: null,
      error: err instanceof Error ? err.message : 'Failed to load archive overview',
    };
  }
}

// --------------------------------------------------------------------------
// get_download_stats — same RPC the downloads page uses, reached through a
// server action so the dashboard keeps one data-access pattern.
// --------------------------------------------------------------------------

export interface DownloadStats {
  total_downloads: number;
  unique_users: number;
  total_files: number;
  total_targets: number;
  by_type: Record<string, number> | null;
  downloads_by_day: { day: string; count: number }[] | null;
  most_downloaded_targets: { target_id: string; download_count: number }[] | null;
}

export interface DownloadStatsResult {
  stats: DownloadStats | null;
  error?: string;
}

export async function getAdminDownloadStats(days = 30): Promise<DownloadStatsResult> {
  try {
    const { supabase } = await requireAdmin();
    const { data, error } = await supabase.rpc('get_download_stats', { p_days: days });
    if (error) return { stats: null, error: error.message };
    return { stats: data as unknown as DownloadStats };
  } catch (err) {
    return {
      stats: null,
      error: err instanceof Error ? err.message : 'Failed to load download stats',
    };
  }
}
