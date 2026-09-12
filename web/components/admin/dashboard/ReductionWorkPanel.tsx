'use client';

import React from 'react';
import Link from 'next/link';
import { Camera, ListChecks } from 'lucide-react';
import type { ReviewQueues } from '@/lib/actions/admin-dashboard';
import { fmtCount } from '@/lib/admin/format';
import { Panel, PanelError, SkeletonRows } from './primitives';

interface BacklogScope {
  label: string;
  pending: number;
  href?: string;
}

function WorkflowRow({
  icon: Icon,
  label,
  purpose,
  done,
  total,
  pending,
  blockers,
  scopes,
  href,
}: {
  icon: React.ElementType;
  label: string;
  purpose: string;
  done: number;
  total: number;
  pending: number;
  blockers?: string;
  scopes: BacklogScope[];
  href: string;
}) {
  return (
    <div className="px-3 py-3">
      <div className="grid gap-3 md:grid-cols-[minmax(190px,1.1fr)_150px_minmax(220px,1.2fr)_auto] md:items-center">
        <div className="flex items-start gap-2.5 min-w-0">
          <Icon className="w-4 h-4 text-text-tertiary mt-0.5 shrink-0" />
          <div className="min-w-0">
            <div className="text-sm font-medium text-text-primary">{label}</div>
            <div className="text-xs text-text-tertiary mt-0.5">{purpose}</div>
          </div>
        </div>

        <div className="flex gap-5 md:block">
          <div className="text-sm tabular-nums text-text-primary">
            <span className="font-semibold">{fmtCount(done)}</span>
            <span className="text-text-tertiary"> / {fmtCount(total)} reviewed</span>
          </div>
          <div className={`text-xs mt-0.5 ${pending > 0 ? 'text-warning' : 'text-success'}`}>
            {pending > 0 ? `${fmtCount(pending)} pending` : 'Complete'}
            {blockers ? ` · ${blockers}` : ''}
          </div>
        </div>

        <div className="min-w-0">
          {scopes.length === 0 ? (
            <span className="text-xs text-text-tertiary">No backlog</span>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {scopes.map((scope) => {
                const body = (
                  <>
                    <span className="font-mono truncate max-w-44">{scope.label}</span>
                    <span className="tabular-nums text-warning">{fmtCount(scope.pending)}</span>
                  </>
                );
                const cls = 'inline-flex items-center gap-1.5 rounded-md bg-surface-2 px-2 py-1 text-[11px] text-text-secondary';
                return scope.href ? (
                  <Link key={`${scope.label}-${scope.href}`} href={scope.href} className={`${cls} hover:text-text-primary`}>
                    {body}
                  </Link>
                ) : (
                  <span key={scope.label} className={cls}>{body}</span>
                );
              })}
            </div>
          )}
        </div>

        <Link href={href} className="text-xs text-primary-text hover:underline whitespace-nowrap">
          Open queue →
        </Link>
      </div>
    </div>
  );
}

export function ReductionWorkPanel({
  queues, loading, error, onRetry,
}: {
  queues: ReviewQueues | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel id="review-work" icon={ListChecks} title="Inspection and review">
      {loading ? (
        <SkeletonRows n={6} />
      ) : error || !queues ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <div className="divide-y divide-border">
          <WorkflowRow
            icon={Camera}
            label="NIRCam exposure review"
            purpose="Inspect inputs before they are combined into mosaics."
            done={queues.nircam.done}
            total={queues.nircam.total}
            pending={queues.nircam.pending}
            blockers={queues.nircam.needs_correction > 0
              ? `${fmtCount(queues.nircam.needs_correction)} need correction`
              : undefined}
            scopes={queues.nircam.top.slice(0, 3).map((scope) => ({
              label: `${scope.field} / ${scope.filter}`,
              pending: scope.pending,
              href: `/admin/nircam?field=${encodeURIComponent(scope.field)}&filter=${encodeURIComponent(scope.filter)}&review=pending`,
            }))}
            href="/admin/nircam?review=pending"
          />
          <WorkflowRow
            icon={ListChecks}
            label="NIRSpec spectrum inspection"
            purpose="Curate spectra in the live database."
            done={Math.max(0, queues.objects.published - queues.objects.uninspected)}
            total={queues.objects.published}
            pending={queues.objects.uninspected}
            blockers={queues.objects.stale > 0
              ? `${fmtCount(queues.objects.stale)} stale after reconciliation`
              : undefined}
            scopes={queues.objects.top.slice(0, 3).map((scope) => ({
              label: scope.field,
              pending: scope.uninspected,
            }))}
            href="/nirspec"
          />
        </div>
      )}
    </Panel>
  );
}
