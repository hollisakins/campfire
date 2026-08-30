'use client';

import React from 'react';
import Link from 'next/link';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Card } from '@/components/ui/Card';
import {
  KeyRound, LayoutDashboard, Link2, RotateCw, UserPlus, Users,
} from 'lucide-react';
import { getDeployments, getDeployEvents } from '@/lib/actions/deployments';
import { getStorageBudget, type StorageBudget } from '@/lib/actions/storage-registry';
import {
  getDashboardSummary, getReviewQueues, getRecentAdminActivity,
  getLifecycleStatus, getArchiveOverview, getAdminDownloadStats,
} from '@/lib/actions/admin-dashboard';
import { evaluateAttention } from '@/lib/admin/attention';
import { fmtBytes, fmtCount, fmtWhen } from '@/lib/admin/format';
import { AttentionRail } from '@/components/admin/dashboard/AttentionRail';
import { DeploymentsPanel } from '@/components/admin/dashboard/DeploymentsPanel';
import { ReviewBoard } from '@/components/admin/dashboard/ReviewBoard';
import { StoragePanel } from '@/components/admin/dashboard/StoragePanel';
import { ScopesPanel } from '@/components/admin/dashboard/ScopesPanel';
import { HealthPanel } from '@/components/admin/dashboard/HealthPanel';
import { PeoplePanel } from '@/components/admin/dashboard/PeoplePanel';
import { DownloadsPanel } from '@/components/admin/dashboard/DownloadsPanel';
import { ActivityPanel } from '@/components/admin/dashboard/ActivityPanel';

// ---------------------------------------------------------------------------
// The admin control center (2026-08 redesign). Reads top to bottom as the
// data lifecycle: attention → deploy → review → storage/scopes/health →
// people/usage → archive scale. Every number is a deep link with its filter
// pre-applied; no card ever renders a zero for an unresolved query.
// ---------------------------------------------------------------------------

const DOWNLOAD_DAYS = 30;

const QUICK_ACTIONS = [
  { href: '/admin/users?new=invite', icon: UserPlus, label: 'Invite user' },
  { href: '/admin/users?new=group', icon: Users, label: 'Group account' },
  { href: '/admin/codes?new=1', icon: KeyRound, label: 'Access code' },
  { href: '/admin/share-links?new=1', icon: Link2, label: 'Share link' },
];

export default function AdminDashboardPage() {
  const queryClient = useQueryClient();

  const summaryQ = useQuery({
    queryKey: ['admin-dash', 'summary'],
    queryFn: getDashboardSummary,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
  });
  const queuesQ = useQuery({
    queryKey: ['admin-dash', 'queues'],
    queryFn: getReviewQueues,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
  });
  const draftsQ = useQuery({
    queryKey: ['admin-dash', 'drafts'],
    queryFn: () => getDeployments({
      status: 'draft', pageSize: 4, sortColumn: 'deployed_at', sortDirection: 'asc',
    }),
    staleTime: 60_000,
  });
  const eventsQ = useQuery({
    queryKey: ['admin-dash', 'events'],
    queryFn: () => getDeployEvents({ pageSize: 8 }),
    staleTime: 60_000,
  });
  const budgetQ = useQuery({
    queryKey: ['admin-storage-budget'],
    queryFn: getStorageBudget,
    staleTime: 5 * 60_000,
  });
  const downloadsQ = useQuery({
    queryKey: ['admin-dash', 'downloads', DOWNLOAD_DAYS],
    queryFn: () => getAdminDownloadStats(DOWNLOAD_DAYS),
    staleTime: 5 * 60_000,
  });
  const activityQ = useQuery({
    queryKey: ['admin-dash', 'activity'],
    queryFn: () => getRecentAdminActivity(8),
    staleTime: 60_000,
  });
  const overviewQ = useQuery({
    queryKey: ['admin-dash', 'overview'],
    queryFn: getArchiveOverview,
    staleTime: 10 * 60_000,
  });
  const lifecycleQ = useQuery({
    queryKey: ['admin-lifecycle'],
    queryFn: getLifecycleStatus,
    staleTime: Infinity,
  });

  const summary = summaryQ.data?.summary ?? null;
  const summaryError = summaryQ.data?.error ?? (summaryQ.error ? String(summaryQ.error) : undefined);
  const budget: StorageBudget | null =
    budgetQ.data && 'total_bytes' in budgetQ.data ? budgetQ.data : null;
  const budgetError =
    budgetQ.data && !('total_bytes' in budgetQ.data)
      ? budgetQ.data.error
      : budgetQ.error ? String(budgetQ.error) : undefined;

  const anyFetching =
    summaryQ.isFetching || queuesQ.isFetching || draftsQ.isFetching ||
    eventsQ.isFetching || budgetQ.isFetching;

  const refreshAll = () => {
    queryClient.invalidateQueries({ queryKey: ['admin-dash'] });
    queryClient.invalidateQueries({ queryKey: ['admin-storage-budget'] });
  };

  // Computed status line — never a static sentence, never an all-clear while
  // the checks are unresolved.
  let statusLine: React.ReactNode;
  if (summaryQ.isPending) {
    statusLine = <span className="text-text-tertiary">Checking…</span>;
  } else if (summaryError || !summary) {
    statusLine = <span className="text-warning">Status unavailable</span>;
  } else {
    const { firing } = evaluateAttention({ summary, budget });
    const updated = new Date(summaryQ.dataUpdatedAt).toLocaleTimeString(undefined, {
      hour: '2-digit', minute: '2-digit',
    });
    statusLine = (
      <span className="text-text-secondary">
        {firing.length === 0 ? 'All clear' : `${firing.length} item${firing.length === 1 ? '' : 's'} need attention`}
        {summary.deployments.drafts > 0 && ` · ${summary.deployments.drafts} draft${summary.deployments.drafts === 1 ? '' : 's'} waiting`}
        {` · updated ${updated}`}
      </span>
    );
  }

  const overview = overviewQ.data?.overview ?? null;

  return (
    <div className="space-y-4">
      {/* Command bar */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <LayoutDashboard className="w-5 h-5 text-primary" />
            <h1 className="text-xl font-semibold text-text-primary">Dashboard</h1>
          </div>
          <p className="text-xs mt-0.5">{statusLine}</p>
        </div>
        <div className="ml-auto flex items-center gap-1.5 flex-wrap">
          {QUICK_ACTIONS.map(({ href, icon: Icon, label }) => (
            <Link
              key={href}
              href={href}
              className="inline-flex items-center gap-1.5 h-7 px-2 rounded-lg border border-border text-xs text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors"
            >
              <Icon className="w-3.5 h-3.5" />
              {label}
            </Link>
          ))}
          <button
            onClick={refreshAll}
            title="Refresh"
            className="inline-flex items-center justify-center h-7 w-7 rounded-lg border border-border text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors"
          >
            <RotateCw className={`w-3.5 h-3.5 ${anyFetching ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>

      {/* Attention rail */}
      <AttentionRail
        summary={summary}
        budget={budget}
        loading={summaryQ.isPending}
        error={summaryError}
        onRetry={() => summaryQ.refetch()}
        updatedAt={summaryQ.dataUpdatedAt || undefined}
      />

      {/* Deploy band */}
      <div className="grid grid-cols-12 gap-4">
        <div className="col-span-12 xl:col-span-7">
          <DeploymentsPanel
            drafts={draftsQ.data}
            events={eventsQ.data}
            loading={draftsQ.isPending || eventsQ.isPending}
            onRetry={() => { draftsQ.refetch(); eventsQ.refetch(); }}
          />
        </div>
        <div className="col-span-12 xl:col-span-5">
          <ReviewBoard
            queues={queuesQ.data?.queues ?? null}
            loading={queuesQ.isPending}
            error={queuesQ.data?.error}
            onRetry={() => queuesQ.refetch()}
          />
        </div>

        {/* Storage / scopes / health band */}
        <div className="col-span-12 lg:col-span-6 xl:col-span-5">
          <StoragePanel
            budget={budget}
            loading={budgetQ.isPending}
            error={budgetError}
            onRetry={() => budgetQ.refetch()}
          />
        </div>
        <div className="col-span-12 lg:col-span-6 xl:col-span-4">
          <ScopesPanel
            summary={summary}
            loading={summaryQ.isPending}
            error={summaryError}
            onRetry={() => summaryQ.refetch()}
          />
        </div>
        <div className="col-span-12 lg:col-span-6 xl:col-span-3">
          <HealthPanel
            summary={summary}
            lifecycle={lifecycleQ.data?.status ?? null}
            loading={summaryQ.isPending}
            error={summaryError}
            onRetry={() => summaryQ.refetch()}
          />
        </div>

        {/* People / usage band */}
        <div className="col-span-12 lg:col-span-6 xl:col-span-4">
          <PeoplePanel
            summary={summary}
            loading={summaryQ.isPending}
            error={summaryError}
            onRetry={() => summaryQ.refetch()}
          />
        </div>
        <div className="col-span-12 lg:col-span-6 xl:col-span-4">
          <DownloadsPanel
            stats={downloadsQ.data?.stats ?? null}
            days={DOWNLOAD_DAYS}
            loading={downloadsQ.isPending}
            error={downloadsQ.data?.error}
            onRetry={() => downloadsQ.refetch()}
          />
        </div>
        <div className="col-span-12 xl:col-span-4">
          <ActivityPanel
            rows={activityQ.data?.rows ?? null}
            summary={summary}
            loading={activityQ.isPending}
            error={activityQ.data?.error}
            onRetry={() => activityQ.refetch()}
          />
        </div>
      </div>

      {/* Archive scale footer */}
      <Card className="overflow-hidden">
        {overviewQ.isPending ? (
          <div className="p-3"><div className="h-4 bg-surface-2 rounded animate-pulse" /></div>
        ) : !overview ? (
          <p className="p-3 text-xs text-text-tertiary">
            Archive overview unavailable{overviewQ.data?.error ? ` — ${overviewQ.data.error}` : ''}
          </p>
        ) : (
          <div className="px-3 py-2 flex flex-wrap items-center gap-x-6 gap-y-1">
            {([
              ['Programs', overview.n_programs, '/admin/programs'],
              ['Observations', overview.n_observations, null],
              ['Pointings', overview.n_pointings, null],
              ['Targets', overview.n_targets, null],
              ['Spectra', overview.n_spectra, null],
            ] as const).map(([label, value, href]) => {
              const cell = (
                <span key={label} className="text-xs whitespace-nowrap">
                  <span className="tabular-nums font-medium text-text-primary">{fmtCount(value)}</span>
                  <span className="text-text-tertiary ml-1.5 uppercase text-[10px] tracking-wider">{label}</span>
                </span>
              );
              return href ? (
                <Link key={label} href={href} className="hover:opacity-80">{cell}</Link>
              ) : cell;
            })}
            <span className="text-xs whitespace-nowrap">
              <span className="tabular-nums font-medium text-text-primary">{fmtBytes(overview.total_size_bytes)}</span>
              <span className="text-text-tertiary ml-1.5 uppercase text-[10px] tracking-wider">Spectra size</span>
            </span>
            <span className="ml-auto text-[11px] text-text-tertiary whitespace-nowrap">
              Latest deploy {fmtWhen(overview.latest_deployed_at)}
              {overview.latest_cfpipe_version && (
                <span className="font-mono ml-1.5">cfpipe {overview.latest_cfpipe_version}</span>
              )}
            </span>
          </div>
        )}
      </Card>
    </div>
  );
}
