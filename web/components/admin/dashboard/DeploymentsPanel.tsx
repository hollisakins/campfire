'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { GitBranch, Camera, Telescope } from 'lucide-react';
import {
  setDeploymentLifecycle,
  type DeploymentRow,
  type DeploymentsResult,
  type DeployEventsResult,
} from '@/lib/actions/deployments';
import { Panel, PanelEmpty, PanelError, SkeletonRows, ViewAllLink } from './primitives';
import { fmtAgo, fmtWhen, fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// Deployments: the drafts queue (with the one mutation the dashboard allows —
// publish, behind a two-step inline confirm, never window.confirm/alert) above
// the merged deploy-events log. Events get the header row and the
// affected-count / status columns the old dashboard dropped.
// ---------------------------------------------------------------------------

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

function DraftRow({ d }: { d: DeploymentRow }) {
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const publish = useMutation({
    mutationFn: () => setDeploymentLifecycle(d.id, 'publish'),
    onSuccess: (res) => {
      if (res.success) {
        queryClient.invalidateQueries({ queryKey: ['admin-dash'] });
      }
    },
  });
  const failed = publish.data && !publish.data.success ? publish.data.error : publish.error?.message;
  const items = d.n_spectra != null
    ? `${fmtCount(d.n_spectra)} spectra`
    : d.n_targets != null
      ? `${fmtCount(d.n_targets)} ${d.field ? 'exposures' : 'targets'}`
      : '—';

  return (
    <div className="px-3 py-1.5">
      <div className="flex items-center gap-3">
        <ScopeCell observation={d.observation} field={d.field} />
        <span className="text-xs text-text-secondary whitespace-nowrap">{items}</span>
        {d.cfpipe_version && (
          <span
            className={`font-mono text-[10px] ${RELEASE_RE.test(d.cfpipe_version) ? 'text-text-tertiary' : 'text-warning'}`}
            title={RELEASE_RE.test(d.cfpipe_version) ? undefined : 'Non-release pipeline version'}
          >
            {d.cfpipe_version}
          </span>
        )}
        <span
          className="text-xs text-text-tertiary tabular-nums ml-auto whitespace-nowrap"
          title={fmtWhen(d.deployed_at)}
        >
          in draft {fmtAgo(d.deployed_at)}
        </span>
        {confirming ? (
          <span className="flex items-center gap-2 shrink-0">
            <button
              onClick={() => publish.mutate()}
              disabled={publish.isPending}
              className="text-xs px-2 py-0.5 rounded bg-primary text-on-primary hover:bg-primary-hover disabled:opacity-50"
            >
              {publish.isPending ? 'Publishing…' : 'Confirm publish'}
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="text-xs text-text-secondary hover:text-text-primary"
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            onClick={() => setConfirming(true)}
            className="text-xs px-2 py-0.5 rounded border border-border text-text-secondary hover:bg-card-hover hover:text-text-primary shrink-0"
          >
            Publish
          </button>
        )}
      </div>
      {failed && <p className="text-xs text-danger mt-1">{failed}</p>}
    </div>
  );
}

export function DeploymentsPanel({
  drafts, events, loading, onRetry,
}: {
  drafts: DeploymentsResult | undefined;
  events: DeployEventsResult | undefined;
  loading: boolean;
  onRetry: () => void;
}) {
  return (
    <Panel
      icon={GitBranch}
      title="Deployments"
      right={<ViewAllLink href="/admin/deployments" />}
    >
      {loading ? (
        <SkeletonRows n={8} />
      ) : (
        <>
          {drafts?.error ? (
            <PanelError message={drafts.error} onRetry={onRetry} />
          ) : (drafts?.deployments.length ?? 0) > 0 ? (
            <div className="divide-y divide-border border-b border-border bg-warning/5">
              {drafts!.deployments.map((d) => <DraftRow key={d.id} d={d} />)}
              {drafts!.total > drafts!.deployments.length && (
                <Link
                  href="/admin/deployments?status=draft"
                  className="block px-3 py-1.5 text-xs text-text-secondary hover:bg-card-hover"
                >
                  +{drafts!.total - drafts!.deployments.length} more drafts
                </Link>
              )}
            </div>
          ) : null}

          {events?.error ? (
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
                  {events!.events.map((e) => (
                    <tr key={e.id} className="hover:bg-card-hover/50">
                      <td
                        className="px-3 py-1.5 text-text-secondary whitespace-nowrap tabular-nums"
                        title={fmtWhen(e.occurred_at)}
                      >
                        {fmtAgo(e.occurred_at)}
                      </td>
                      <td className={`px-3 py-1.5 font-medium whitespace-nowrap ${ACTION_CLASS[e.action] ?? 'text-text-primary'}`}>
                        {e.action}
                      </td>
                      <td className="px-3 py-1.5 max-w-[180px]">
                        <ScopeCell observation={e.observation} field={e.field} />
                      </td>
                      <td className="px-3 py-1.5 text-right tabular-nums text-text-secondary">
                        {e.affected_count != null ? fmtCount(e.affected_count) : '—'}
                      </td>
                      <td className="px-3 py-1.5 text-text-secondary whitespace-nowrap">
                        {e.status_to ?? '—'}
                      </td>
                      <td className="px-3 py-1.5 text-right text-text-secondary truncate max-w-[120px]">
                        {e.actor_name ?? '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
