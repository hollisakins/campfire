'use client';

import React from 'react';
import Link from 'next/link';
import { Camera, GitBranch, History, Rocket, Telescope } from 'lucide-react';
import type {
  DeploymentRow,
  DeploymentsResult,
  DeployEventsResult,
} from '@/lib/actions/deployments';
import { Panel, PanelEmpty, PanelError, SkeletonRows, ViewAllLink } from './primitives';
import { fmtAgo, fmtCount, fmtWhen } from '@/lib/admin/format';

const RELEASE_RE = /^\d+\.\d+\.\d+$/;

const ACTION_CLASS: Record<string, string> = {
  publish: 'text-success',
  recover: 'text-success',
  revoke: 'text-danger',
  delete: 'text-danger',
};

function ScopeCell({ observation, field }: { observation: string | null; field: string | null }) {
  const Icon = field ? Camera : Telescope;
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-xs text-text-primary min-w-0">
      <Icon className="w-3 h-3 text-text-tertiary shrink-0" />
      <span className="truncate">{observation ?? field ?? '—'}</span>
    </span>
  );
}

function deploymentItems(d: DeploymentRow): string {
  if (d.n_spectra != null) return `${fmtCount(d.n_spectra)} spectra`;
  if (d.n_targets != null) return `${fmtCount(d.n_targets)} ${d.field ? 'exposures' : 'targets'}`;
  return 'Count unavailable';
}

const STATUS_CLASS: Record<string, string> = {
  published: 'text-text-tertiary',
  draft: 'bg-primary/10 text-primary-text',
  revoked: 'bg-surface-2 text-text-tertiary',
};

export function RecentDeploymentsPanel({
  deployments, loading, onRetry,
}: {
  deployments: DeploymentsResult | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  return (
    <Panel
      icon={Rocket}
      title="Recent deployments"
      right={<ViewAllLink href="/admin/deployments" />}
    >
      {loading ? (
        <SkeletonRows n={5} />
      ) : deployments?.error ? (
        <PanelError message={deployments.error} onRetry={onRetry} />
      ) : (deployments?.deployments.length ?? 0) === 0 ? (
        <PanelEmpty text="No deployments yet." />
      ) : (
        <div className="divide-y divide-border">
          {deployments!.deployments.map((deployment) => (
            <Link
              key={deployment.id}
              href="/admin/deployments"
              className="grid grid-cols-[minmax(120px,1fr)_auto_auto_auto] md:grid-cols-[minmax(120px,1fr)_auto_auto_auto_auto] items-center gap-3 px-3 py-2.5 hover:bg-card-hover"
            >
              <ScopeCell observation={deployment.observation} field={deployment.field} />
              <span className="text-xs text-text-secondary whitespace-nowrap">
                {deploymentItems(deployment)}
              </span>
              <span className="hidden md:inline font-mono text-[10px] text-text-tertiary whitespace-nowrap">
                {deployment.cfpipe_version ? `cfpipe ${deployment.cfpipe_version}` : 'version —'}
              </span>
              <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${STATUS_CLASS[deployment.status] ?? 'bg-surface-2 text-text-secondary'}`}>
                {deployment.status}
              </span>
              <span
                className="text-xs text-text-tertiary tabular-nums whitespace-nowrap"
                title={fmtWhen(deployment.deployed_at)}
              >
                {fmtAgo(deployment.deployed_at)}
              </span>
            </Link>
          ))}
        </div>
      )}
    </Panel>
  );
}

export function DraftDeploymentsPanel({
  drafts, loading, onRetry,
}: {
  drafts: DeploymentsResult | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  return (
    <Panel
      icon={GitBranch}
      title="Draft deployments"
      right={<ViewAllLink href="/admin/deployments?status=draft" label="Review all" />}
    >
      {loading ? (
        <SkeletonRows n={4} />
      ) : drafts?.error ? (
        <PanelError message={drafts.error} onRetry={onRetry} />
      ) : (drafts?.deployments.length ?? 0) === 0 ? (
        <PanelEmpty text="No deployments are waiting in draft." />
      ) : (
        <div className="divide-y divide-border">
          {drafts!.deployments.map((d) => {
            const isRelease = !d.cfpipe_version || RELEASE_RE.test(d.cfpipe_version);
            return (
              <Link
                key={d.id}
                href="/admin/deployments?status=draft"
                className="flex items-center gap-3 px-3 py-2.5 hover:bg-card-hover"
              >
                <ScopeCell observation={d.observation} field={d.field} />
                <span className="text-xs text-text-secondary whitespace-nowrap">
                  {deploymentItems(d)}
                </span>
                {d.cfpipe_version && (
                  <span
                    className={`font-mono text-[10px] truncate ${isRelease ? 'text-text-tertiary' : 'text-warning'}`}
                    title={isRelease ? undefined : 'Non-release pipeline version'}
                  >
                    {d.cfpipe_version}
                  </span>
                )}
                <span
                  className="text-xs text-text-tertiary tabular-nums ml-auto whitespace-nowrap"
                  title={fmtWhen(d.deployed_at)}
                >
                  drafted {fmtAgo(d.deployed_at)}
                </span>
                <span className="text-xs text-primary-text whitespace-nowrap">Review →</span>
              </Link>
            );
          })}
          {drafts!.total > drafts!.deployments.length && (
            <Link
              href="/admin/deployments?status=draft"
              className="block px-3 py-2 text-xs text-text-secondary hover:bg-card-hover"
            >
              +{drafts!.total - drafts!.deployments.length} more drafts
            </Link>
          )}
        </div>
      )}
      <div className="border-t border-border px-3 py-2 text-[11px] text-text-tertiary">
        Open a draft in the deployments view before making a publication decision.
      </div>
    </Panel>
  );
}

export function RecentDeployEventsPanel({
  events, loading, onRetry,
}: {
  events: DeployEventsResult | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  return (
    <Panel
      icon={History}
      title="Recent deploy activity"
      right={<ViewAllLink href="/admin/deployments" />}
    >
      {loading ? (
        <SkeletonRows n={5} />
      ) : events?.error ? (
        <PanelError message={events.error} onRetry={onRetry} />
      ) : (events?.events.length ?? 0) === 0 ? (
        <PanelEmpty text="No deploy activity yet." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-text-tertiary text-left border-b border-border">
              <tr>
                <th className="px-3 py-1.5 font-medium">When</th>
                <th className="px-3 py-1.5 font-medium">Action</th>
                <th className="px-3 py-1.5 font-medium">Scope</th>
                <th className="px-3 py-1.5 font-medium text-right">Δ</th>
                <th className="px-3 py-1.5 font-medium">Status</th>
                <th className="px-3 py-1.5 font-medium text-right">Actor</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {events!.events.map((event) => (
                <tr key={event.id} className="hover:bg-card-hover/50">
                  <td
                    className="px-3 py-1.5 text-text-secondary whitespace-nowrap tabular-nums"
                    title={fmtWhen(event.occurred_at)}
                  >
                    {fmtAgo(event.occurred_at)}
                  </td>
                  <td className={`px-3 py-1.5 font-medium whitespace-nowrap ${ACTION_CLASS[event.action] ?? 'text-text-primary'}`}>
                    {event.action}
                  </td>
                  <td className="px-3 py-1.5 max-w-44">
                    <ScopeCell observation={event.observation} field={event.field} />
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-text-secondary">
                    {event.affected_count != null ? fmtCount(event.affected_count) : '—'}
                  </td>
                  <td className="px-3 py-1.5 text-text-secondary whitespace-nowrap">
                    {event.status_to ?? '—'}
                  </td>
                  <td className="px-3 py-1.5 text-right text-text-secondary truncate max-w-28">
                    {event.actor_name ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
