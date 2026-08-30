'use client';

import React from 'react';
import Link from 'next/link';
import { MessageSquare } from 'lucide-react';
import type { DashboardSummary, RecentActivityRow } from '@/lib/actions/admin-dashboard';
import { formatActivityField, formatFieldName } from '@/lib/types';
import { Panel, PanelEmpty, PanelError, SkeletonRows, ViewAllLink } from './primitives';
import { fmtAgo, fmtWhen, fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// Inspection & comment activity. Subject links resolve BY KIND — object rows
// to /nirspec/objects/<id>, legacy target rows to the /nirspec/targets/<id>
// redirect shim, spectrum rows as plain text (no route resolves
// "target/GRATING") — fixing the old feed's universal-404 links.
// ---------------------------------------------------------------------------

function subjectHref(row: RecentActivityRow): string | null {
  if (!row.display_id) return null;
  if (row.subject_kind === 'object') return `/nirspec/objects/${encodeURIComponent(row.display_id)}`;
  if (row.subject_kind === 'target') return `/nirspec/targets/${encodeURIComponent(row.display_id)}`;
  return null;
}

export function ActivityPanel({
  rows, summary, loading, error, onRetry,
}: {
  rows: RecentActivityRow[] | null;
  summary: DashboardSummary | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  const counters = summary?.activity;
  return (
    <Panel icon={MessageSquare} title="Inspection activity" right={<ViewAllLink href="/admin/activity" />}>
      {loading ? (
        <SkeletonRows n={7} />
      ) : error || !rows ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <>
          {counters && (
            <div className="px-3 py-2 border-b border-border flex gap-4 text-[11px] text-text-secondary">
              <span>
                <span className="tabular-nums text-text-primary font-medium">{fmtCount(counters.inspections_7d)}</span>
                {' '}inspections 7d
              </span>
              <span>
                <span className="tabular-nums text-text-primary font-medium">{fmtCount(counters.comments_7d)}</span>
                {' '}comments 7d
              </span>
              <span>
                <span className="tabular-nums text-text-primary font-medium">{fmtCount(counters.active_inspectors_7d)}</span>
                {' '}inspectors
              </span>
            </div>
          )}
          {rows.length === 0 ? (
            <PanelEmpty text="No recent activity." />
          ) : (
            <div className="divide-y divide-border">
              {rows.map((row) => {
                const href = subjectHref(row);
                const subject = href ? (
                  <Link href={href} className="font-mono text-[11px] text-primary-text hover:underline truncate">
                    {row.display_id}
                  </Link>
                ) : (
                  <span className="font-mono text-[11px] text-text-secondary truncate">
                    {row.display_id || '—'}
                  </span>
                );
                return (
                  <div key={row.id} className="px-3 py-1.5 text-xs">
                    <div className="flex items-center gap-2">
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
                        <span className="text-text-secondary truncate italic">
                          “{row.content}”
                        </span>
                      )}
                      <span className="ml-auto flex items-center gap-2 shrink-0">
                        {subject}
                        <span className="text-[11px] text-text-tertiary tabular-nums" title={fmtWhen(row.ts)}>
                          {fmtAgo(row.ts)}
                        </span>
                      </span>
                    </div>
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
