'use client';

import React from 'react';
import Link from 'next/link';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { LayoutDashboard, RotateCw } from 'lucide-react';
import { Card } from '@/components/ui/Card';
import { fetchJson } from '@/lib/fetch-json';
import type { AdminDashboardResponse } from '@/app/api/admin/dashboard/route';
import { fmtAgo, fmtBytes, fmtCount, fmtWhen } from '@/lib/admin/format';
import { InProgressStrip } from '@/components/admin/dashboard/InProgressStrip';
import { DeployEventsPanel, RecentDeploymentsPanel } from '@/components/admin/dashboard/DeploymentsPanel';
import { ReviewPanel } from '@/components/admin/dashboard/ReviewPanel';
import { AccessPanel } from '@/components/admin/dashboard/AccessPanel';
import { StoragePanel } from '@/components/admin/dashboard/StoragePanel';
import { ReleasesPanel } from '@/components/admin/dashboard/ReleasesPanel';
import { ActivityPanel } from '@/components/admin/dashboard/ActivityPanel';

// ---------------------------------------------------------------------------
// The admin landing page: an operator overview with a fixed order.
//   1. what is running now (deploy leases, releases mid-lifecycle),
//   2. what was deployed recently,
//   3. the review backlogs and the access queue,
//   4. storage at rest and the data releases,
//   5. the audit trail, with the archive's scale and provenance as a footer.
// One GET carries every panel (/api/admin/dashboard); each panel owns its
// loading / unavailable / empty state, so a failed aggregate never reads as 0.
// ---------------------------------------------------------------------------

const QUERY_KEY = ['admin-dashboard'] as const;

export default function AdminDashboardPage() {
  const queryClient = useQueryClient();
  const q = useQuery({
    queryKey: QUERY_KEY,
    queryFn: ({ signal }) => fetchJson<AdminDashboardResponse>('/api/admin/dashboard', { signal }),
    staleTime: 30_000,
    refetchOnWindowFocus: true,
    // Poll while a deploy or a release job is running so the strip tracks it.
    refetchInterval: (query) => {
      const d = query.state.data;
      const busy = (d?.summary?.leases.length ?? 0) > 0
        || (d?.releases ?? []).some((r) => ['freezing', 'exporting', 'copying'].includes(r.state));
      return busy ? 20_000 : false;
    },
  });

  const data = q.data;
  const loading = q.isPending;
  // A transport-level failure (401, network) blanks every panel with the same message.
  const transportError = q.error ? String(q.error instanceof Error ? q.error.message : q.error) : undefined;
  const err = (section: keyof AdminDashboardResponse['errors']) => transportError ?? data?.errors[section];
  const retry = () => queryClient.invalidateQueries({ queryKey: QUERY_KEY });

  const summary = data?.summary ?? null;
  const overview = data?.overview ?? null;
  const updated = q.dataUpdatedAt
    ? new Date(q.dataUpdatedAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <LayoutDashboard className="w-5 h-5 text-primary" />
        <h1 className="text-xl font-semibold text-text-primary">Admin</h1>
        <span className="ml-auto text-xs text-text-tertiary tabular-nums">
          {updated ? `Updated ${updated}` : ''}
        </span>
        <button
          onClick={retry}
          title="Refresh"
          aria-label="Refresh"
          className="inline-flex items-center justify-center h-8 w-8 rounded-lg border border-border text-text-secondary hover:bg-card-hover hover:text-text-primary transition-colors"
        >
          <RotateCw className={`w-3.5 h-3.5 ${q.isFetching ? 'animate-spin' : ''}`} />
        </button>
      </div>

      {summary && (
        <InProgressStrip leases={summary.leases} releases={data?.releases ?? []} />
      )}

      <RecentDeploymentsPanel
        deployments={data?.deployments ?? null}
        drafts={summary?.deployments.drafts ?? null}
        loading={loading}
        error={err('deployments')}
        onRetry={retry}
      />

      <div className="grid grid-cols-12 gap-4 items-start">
        <div className="col-span-12 xl:col-span-8">
          <ReviewPanel review={summary?.review ?? null} loading={loading} error={err('summary')} onRetry={retry} />
        </div>
        <div className="col-span-12 xl:col-span-4">
          <AccessPanel access={summary?.access ?? null} loading={loading} error={err('summary')} onRetry={retry} />
        </div>

        <div className="col-span-12 xl:col-span-4">
          <StoragePanel budget={data?.budget ?? null} loading={loading} error={err('budget')} onRetry={retry} />
        </div>
        <div className="col-span-12 xl:col-span-8">
          <ReleasesPanel releases={data?.releases ?? null} loading={loading} error={err('releases')} onRetry={retry} />
        </div>

        <div className="col-span-12 xl:col-span-7">
          <DeployEventsPanel events={data?.events ?? null} loading={loading} error={err('events')} onRetry={retry} />
        </div>
        <div className="col-span-12 xl:col-span-5">
          <ActivityPanel
            rows={data?.activity ?? null}
            counters={summary?.activity ?? null}
            loading={loading}
            error={err('activity')}
            onRetry={retry}
          />
        </div>
      </div>

      <Card className="overflow-hidden">
        {loading ? (
          <div className="p-3"><div className="h-4 bg-surface-2 rounded animate-pulse" /></div>
        ) : (
          <div className="px-3 py-2 flex flex-wrap items-center gap-x-5 gap-y-1 text-xs">
            {overview ? (
              <>
                {([
                  ['Programs', overview.n_programs, '/admin/programs'],
                  ['Observations', overview.n_observations, null],
                  ['Pointings', overview.n_pointings, null],
                  ['Targets', overview.n_targets, null],
                  ['Spectra', overview.n_spectra, null],
                ] as const).map(([label, value, href]) => {
                  const cell = (
                    <span className="whitespace-nowrap">
                      <span className="tabular-nums font-medium text-text-primary">{fmtCount(value)}</span>
                      <span className="text-text-tertiary ml-1.5 uppercase text-[10px] tracking-wider">{label}</span>
                    </span>
                  );
                  return href
                    ? <Link key={label} href={href} className="hover:opacity-80">{cell}</Link>
                    : <React.Fragment key={label}>{cell}</React.Fragment>;
                })}
                <span className="whitespace-nowrap">
                  <span className="tabular-nums font-medium text-text-primary">{fmtBytes(overview.total_size_bytes)}</span>
                  <span className="text-text-tertiary ml-1.5 uppercase text-[10px] tracking-wider">Spectra</span>
                </span>
              </>
            ) : (
              <span className="text-text-tertiary">Archive overview unavailable</span>
            )}

            <span className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-text-tertiary">
              {overview?.latest_cfpipe_version && (
                <span className="font-mono">cfpipe {overview.latest_cfpipe_version}</span>
              )}
              {summary && (
                <>
                  <Link href="/admin/deployments" className={`hover:underline ${summary.deployments.missing_provenance > 0 ? 'text-warning' : ''}`}>
                    {summary.deployments.missing_provenance > 0
                      ? `${fmtCount(summary.deployments.missing_provenance)} without provenance`
                      : 'Provenance complete'}
                  </Link>
                  {summary.deployments.unreleased_published > 0 && (
                    <Link href="/admin/deployments" className="text-warning hover:underline">
                      {fmtCount(summary.deployments.unreleased_published)} on unreleased builds
                    </Link>
                  )}
                  <span>{fmtCount(summary.deployments.crds_contexts)} CRDS context{summary.deployments.crds_contexts === 1 ? '' : 's'}</span>
                  <span
                    className={summary.sync_snapshot?.status === 'failed' ? 'text-danger' : ''}
                    title={summary.sync_snapshot ? fmtWhen(summary.sync_snapshot.completed_at ?? summary.sync_snapshot.started_at) : undefined}
                  >
                    {summary.sync_snapshot
                      ? `Sync snapshot ${summary.sync_snapshot.status} · ${fmtAgo(summary.sync_snapshot.completed_at ?? summary.sync_snapshot.started_at)}`
                      : 'No sync snapshot'}
                  </span>
                </>
              )}
            </span>
          </div>
        )}
      </Card>
    </div>
  );
}
