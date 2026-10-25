'use client';

import React from 'react';
import Link from 'next/link';
import { MessageSquare } from 'lucide-react';
import type { AdminDashboardSummary, RecentActivityRow } from '@/app/api/admin/dashboard/route';
import { formatActivityField, formatFieldName } from '@/lib/types';
import { fmtAgo, fmtCount, fmtWhen } from '@/lib/admin/format';
import { Panel, PanelEmpty, PanelError, SkeletonRows, ViewAllLink } from './primitives';

// Subject links resolve by kind: object rows to /nirspec/objects/<id>, legacy
// target rows to the /nirspec/targets/<id> shim, spectrum rows as text.
function subjectHref(row: RecentActivityRow): string | null {
  if (!row.display_id) return null;
  if (row.subject_kind === 'object') return `/nirspec/objects/${encodeURIComponent(row.display_id)}`;
  if (row.subject_kind === 'target') return `/nirspec/targets/${encodeURIComponent(row.display_id)}`;
  return null;
}

export function ActivityPanel({
  rows, counters, loading, error, onRetry,
}: {
  rows: RecentActivityRow[] | null;
  counters: AdminDashboardSummary['activity'] | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel icon={MessageSquare} title="Inspection activity" right={<ViewAllLink href="/admin/activity" />}>
      {loading ? (
        <SkeletonRows n={7} />
      ) : error || !rows ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <>
          {counters && (
            <div className="px-3 py-2 border-b border-border flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-text-secondary">
              <span><span className="tabular-nums text-text-primary font-medium">{fmtCount(counters.inspections_7d)}</span> inspections · 7d</span>
              <span><span className="tabular-nums text-text-primary font-medium">{fmtCount(counters.comments_7d)}</span> comments · 7d</span>
              <span><span className="tabular-nums text-text-primary font-medium">{fmtCount(counters.inspectors_7d)}</span> inspectors · 7d</span>
            </div>
          )}
          {rows.length === 0 ? (
            <PanelEmpty text="No recent activity" />
          ) : (
            <div className="divide-y divide-border">
              {rows.map((row) => {
                const href = subjectHref(row);
                const subject = href ? (
                  <Link href={href} className="font-mono text-[11px] text-primary-text hover:underline truncate max-w-[180px]">
                    {row.display_id}
                  </Link>
                ) : (
                  <span className="font-mono text-[11px] text-text-secondary truncate max-w-[180px]">
                    {row.display_id || '—'}
                  </span>
                );
                return (
                  <div key={row.id} className="px-3 py-1.5 text-xs flex items-center gap-2">
                    <span className="text-text-primary truncate shrink-0 max-w-[120px]">
                      {row.user_full_name ?? 'System'}
                    </span>
                    {row.type === 'inspection' ? (
                      <span className="text-text-secondary truncate">
                        {formatFieldName(row.field_name ?? '')}{' '}
                        {formatActivityField(row.field_name ?? '', row.old_value)}
                        {' → '}
                        {formatActivityField(row.field_name ?? '', row.new_value)}
                      </span>
                    ) : (
                      <span className="text-text-secondary truncate italic">“{row.content}”</span>
                    )}
                    <span className="ml-auto flex items-center gap-2 shrink-0">
                      {subject}
                      <span className="text-[11px] text-text-tertiary tabular-nums" title={fmtWhen(row.ts)}>
                        {fmtAgo(row.ts)}
                      </span>
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
