'use client';

import React from 'react';
import Link from 'next/link';
import { CheckCircle2, UserCheck } from 'lucide-react';
import type { DashboardSummary } from '@/lib/actions/admin-dashboard';
import { buildDecisionItems } from '@/lib/admin/decisions';
import { fmtCount } from '@/lib/admin/format';
import { Panel, PanelError, SkeletonRows } from './primitives';

const TONE = {
  action: {
    icon: UserCheck,
    label: 'Admin action',
    border: 'border-warning/30',
    background: 'bg-warning/5',
    badge: 'text-warning',
  },
} as const;

export function DecisionPanel({
  summary, loading, error, onRetry,
}: {
  summary: DashboardSummary | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  if (loading) {
    return (
      <Panel icon={UserCheck} title="Administrative decisions">
        <SkeletonRows n={2} />
      </Panel>
    );
  }

  if (error || !summary) {
    return (
      <Panel icon={UserCheck} title="Administrative decisions">
        <PanelError message={error} onRetry={onRetry} />
      </Panel>
    );
  }

  const items = buildDecisionItems(summary);

  return (
    <Panel
      icon={UserCheck}
      title="Administrative decisions"
      right={items.length > 0 ? <span>{fmtCount(items.length)} open</span> : undefined}
    >
      {items.length === 0 ? (
        <div className="flex items-start gap-2.5 px-3 py-3">
          <CheckCircle2 className="w-4 h-4 text-success mt-0.5 shrink-0" />
          <div>
            <p className="text-sm text-text-primary">No access or provisioning decisions are waiting.</p>
          </div>
        </div>
      ) : (
        <div className="divide-y divide-border">
          {items.map((item) => {
            const tone = TONE[item.tone];
            const Icon = tone.icon;
            return (
              <Link
                key={item.id}
                href={item.href}
                className={`grid grid-cols-[auto_1fr_auto] gap-3 px-3 py-2.5 hover:bg-card-hover transition-colors border-l-2 ${tone.border} ${tone.background}`}
              >
                <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${tone.badge}`} />
                <span className="min-w-0">
                  <span className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="text-sm font-medium text-text-primary">{item.label}</span>
                    <span className={`text-[10px] uppercase tracking-wider ${tone.badge}`}>
                      {tone.label}
                    </span>
                  </span>
                  <span className="block text-xs text-text-secondary mt-0.5">{item.detail}</span>
                </span>
                <span className="text-lg font-semibold tabular-nums text-text-primary self-center">
                  {fmtCount(item.count)}
                </span>
              </Link>
            );
          })}
        </div>
      )}
    </Panel>
  );
}
