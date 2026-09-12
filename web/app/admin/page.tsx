'use client';

import React from 'react';
import Link from 'next/link';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { LayoutDashboard, RotateCw } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { getDeployments, getDeployEvents } from '@/lib/actions/deployments';
import {
  getArchiveOverview,
  getDashboardSummary,
  getRecentAdminActivity,
  getReviewQueues,
} from '@/lib/actions/admin-dashboard';
import { fmtBytes, fmtCount, fmtWhen } from '@/lib/admin/format';
import { DecisionPanel } from '@/components/admin/dashboard/DecisionPanel';
import { ReductionWorkPanel } from '@/components/admin/dashboard/ReductionWorkPanel';
import {
  RecentDeploymentsPanel,
  RecentDeployEventsPanel,
} from '@/components/admin/dashboard/DeploymentsPanel';
import { OperationalContextPanel } from '@/components/admin/dashboard/OperationalContextPanel';
import { ActivityPanel } from '@/components/admin/dashboard/ActivityPanel';

// The admin landing page is an operator overview, not an inventory of every
// available aggregate. Its hierarchy is deliberately stable:
//   1. what was deployed recently,
//   2. inspection and review state by workflow,
//   3. administrative decisions and operational context,
//   4. audit history and quiet archive scale.

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
  const deploymentsQ = useQuery({
    queryKey: ['admin-dash', 'deployments'],
    queryFn: () => getDeployments({
      pageSize: 6, sortColumn: 'deployed_at', sortDirection: 'desc',
    }),
    staleTime: 60_000,
  });
  const eventsQ = useQuery({
    queryKey: ['admin-dash', 'events'],
    queryFn: () => getDeployEvents({ pageSize: 5 }),
    staleTime: 60_000,
  });
  const activityQ = useQuery({
    queryKey: ['admin-dash', 'activity'],
    queryFn: () => getRecentAdminActivity(5),
    staleTime: 60_000,
  });
  const overviewQ = useQuery({
    queryKey: ['admin-dash', 'overview'],
    queryFn: getArchiveOverview,
    staleTime: 10 * 60_000,
  });

  const summary = summaryQ.data?.summary ?? null;
  const summaryError = summaryQ.data?.error ?? (summaryQ.error ? String(summaryQ.error) : undefined);
  const overview = overviewQ.data?.overview ?? null;

  const anyFetching =
    summaryQ.isFetching || queuesQ.isFetching || deploymentsQ.isFetching ||
    eventsQ.isFetching || activityQ.isFetching || overviewQ.isFetching;

  const refreshAll = () => {
    queryClient.invalidateQueries({ queryKey: ['admin-dash'] });
  };

  let statusLine: React.ReactNode;
  if (summaryQ.isPending) {
    statusLine = <span className="text-text-tertiary">Loading current state…</span>;
  } else if (summaryError || !summary) {
    statusLine = <span className="text-warning">Portal status unavailable</span>;
  } else {
    const updated = new Date(summaryQ.dataUpdatedAt).toLocaleTimeString(undefined, {
      hour: '2-digit', minute: '2-digit',
    });
    statusLine = (
      <span className="text-text-secondary">
        {`${fmtCount(summary.deployments.deploys_7d)} deployment${summary.deployments.deploys_7d === 1 ? '' : 's'} in the past 7 days`}
        {` · updated ${updated}`}
      </span>
    );
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start gap-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <LayoutDashboard className="w-5 h-5 text-primary" />
            <h1 className="text-xl font-semibold text-text-primary">Admin overview</h1>
          </div>
          <p className="text-xs mt-0.5">{statusLine}</p>
        </div>
        <button
          onClick={refreshAll}
          title="Refresh dashboard data"
          className="ml-auto inline-flex items-center justify-center h-8 w-8 rounded-lg border border-border text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors"
        >
          <RotateCw className={`w-3.5 h-3.5 ${anyFetching ? 'animate-spin' : ''}`} />
        </button>
      </div>

      <RecentDeploymentsPanel
        deployments={deploymentsQ.data}
        loading={deploymentsQ.isPending}
        onRetry={() => deploymentsQ.refetch()}
      />

      <ReductionWorkPanel
        queues={queuesQ.data?.queues ?? null}
        loading={queuesQ.isPending}
        error={queuesQ.data?.error}
        onRetry={() => queuesQ.refetch()}
      />

      <div className="grid grid-cols-12 gap-4 items-start">
        <div className="col-span-12 xl:col-span-5">
          <DecisionPanel
            summary={summary}
            loading={summaryQ.isPending}
            error={summaryError}
            onRetry={() => summaryQ.refetch()}
          />
        </div>
        <div className="col-span-12 xl:col-span-7">
          <OperationalContextPanel
            summary={summary}
            overview={overview}
            loading={summaryQ.isPending || overviewQ.isPending}
            error={summaryError}
            onRetry={() => {
              summaryQ.refetch();
              overviewQ.refetch();
            }}
          />
        </div>

        <div className="col-span-12 xl:col-span-7">
          <RecentDeployEventsPanel
            events={eventsQ.data}
            loading={eventsQ.isPending}
            onRetry={() => eventsQ.refetch()}
          />
        </div>
        <div className="col-span-12 xl:col-span-5">
          <ActivityPanel
            rows={activityQ.data?.rows ?? null}
            summary={summary}
            loading={activityQ.isPending}
            error={activityQ.data?.error}
            onRetry={() => activityQ.refetch()}
          />
        </div>
      </div>

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
