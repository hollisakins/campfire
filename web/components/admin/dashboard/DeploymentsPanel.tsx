'use client';

import React from 'react';
import Link from 'next/link';
import { Camera, History, Rocket, Telescope } from 'lucide-react';
import type { DeploymentRow, DeployEventRow } from '@/lib/actions/deployments';
import { fmtAgo, fmtCount, fmtWhen } from '@/lib/admin/format';
import { deploymentItems, isReleaseVersion } from '@/lib/admin/dashboard';
import { Panel, PanelEmpty, PanelError, SkeletonRows, ViewAllLink } from './primitives';

const ACTION_CLASS: Record<string, string> = {
  publish: 'text-success',
  recover: 'text-success',
  publish_dr: 'text-success',
  revoke: 'text-danger',
  delete: 'text-danger',
  withdraw_dr: 'text-danger',
};

const STATUS_CLASS: Record<string, string> = {
  published: 'text-text-tertiary',
  draft: 'bg-primary/10 text-primary-text',
  revoked: 'bg-surface-2 text-text-tertiary',
};

export function ScopeCell({ observation, field }: { observation: string | null; field: string | null }) {
  const Icon = field ? Camera : Telescope;
  return (
    <span className="inline-flex items-center gap-1.5 font-mono text-xs text-text-primary min-w-0">
      <Icon className="w-3 h-3 text-text-tertiary shrink-0" />
      <span className="truncate">{observation ?? field ?? '—'}</span>
    </span>
  );
}

function VersionCell({ version }: { version: string | null }) {
  if (!version) return null;
  const release = isReleaseVersion(version);
  return (
    <span
      className={`font-mono text-[10px] whitespace-nowrap ${release ? 'text-text-tertiary' : 'text-warning'}`}
      title={release ? undefined : 'Unreleased cfpipe build'}
    >
      {version}
    </span>
  );
}

export function RecentDeploymentsPanel({
  deployments, drafts, loading, error, onRetry,
}: {
  deployments: { rows: DeploymentRow[]; total: number } | null;
  drafts: number | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel
      icon={Rocket}
      title="Deployments"
      right={
        <>
          {drafts != null && drafts > 0 && (
            <Link href="/admin/deployments?status=draft" className="text-warning hover:underline whitespace-nowrap">
              {fmtCount(drafts)} draft{drafts === 1 ? '' : 's'}
            </Link>
          )}
          <ViewAllLink href="/admin/deployments" />
        </>
      }
    >
      {loading ? (
        <SkeletonRows n={5} />
      ) : error || !deployments ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : deployments.rows.length === 0 ? (
        <PanelEmpty text="No deployments" />
      ) : (
        <div className="divide-y divide-border">
          {deployments.rows.map((d) => {
            const items = deploymentItems(d);
            return (
              <Link
                key={d.id}
                href={`/admin/deployments?status=${d.status}`}
                className="grid grid-cols-[minmax(120px,1fr)_auto_auto] md:grid-cols-[minmax(140px,1fr)_auto_auto_auto_auto] items-center gap-3 px-3 py-2 hover:bg-card-hover"
              >
                <ScopeCell observation={d.observation} field={d.field} />
                <span className="hidden md:inline text-xs text-text-secondary whitespace-nowrap tabular-nums">
                  {items ? `${fmtCount(items.count)} ${items.unit}` : '—'}
                </span>
                <span className="hidden md:inline"><VersionCell version={d.cfpipe_version} /></span>
                <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${STATUS_CLASS[d.status] ?? 'bg-surface-2 text-text-secondary'}`}>
                  {d.status}
                </span>
                <span className="text-xs text-text-tertiary tabular-nums whitespace-nowrap text-right" title={fmtWhen(d.deployed_at)}>
                  {fmtAgo(d.deployed_at)}
                </span>
              </Link>
            );
          })}
        </div>
      )}
    </Panel>
  );
}

export function DeployEventsPanel({
  events, loading, error, onRetry,
}: {
  events: DeployEventRow[] | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel icon={History} title="Deploy events" right={<ViewAllLink href="/admin/deployments" />}>
      {loading ? (
        <SkeletonRows n={5} />
      ) : error || !events ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : events.length === 0 ? (
        <PanelEmpty text="No deploy events" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs">
            <thead className="text-text-tertiary text-left border-b border-border">
              <tr>
                <th className="px-3 py-1.5 font-medium">When</th>
                <th className="px-3 py-1.5 font-medium">Action</th>
                <th className="px-3 py-1.5 font-medium">Scope</th>
                <th className="px-3 py-1.5 font-medium text-right">Items</th>
                <th className="px-3 py-1.5 font-medium text-right">Actor</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {events.map((e) => (
                <tr key={e.id} className="hover:bg-card-hover/50">
                  <td className="px-3 py-1.5 text-text-secondary whitespace-nowrap tabular-nums" title={fmtWhen(e.occurred_at)}>
                    {fmtAgo(e.occurred_at)}
                  </td>
                  <td className={`px-3 py-1.5 font-medium whitespace-nowrap ${ACTION_CLASS[e.action] ?? 'text-text-primary'}`}>
                    {e.action}
                  </td>
                  <td className="px-3 py-1.5 max-w-44">
                    <ScopeCell observation={e.observation} field={e.field} />
                  </td>
                  <td className="px-3 py-1.5 text-right tabular-nums text-text-secondary">
                    {e.affected_count != null ? fmtCount(e.affected_count) : '—'}
                  </td>
                  <td className="px-3 py-1.5 text-right text-text-secondary truncate max-w-28">
                    {e.actor_name ?? '—'}
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
