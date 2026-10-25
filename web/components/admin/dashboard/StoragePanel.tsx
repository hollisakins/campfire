'use client';

import React from 'react';
import { HardDrive } from 'lucide-react';
import type { StorageBudget } from '@/lib/actions/storage-registry';
import { fmtBytes, fmtCount } from '@/lib/admin/format';
import { budgetPercent, budgetSegments } from '@/lib/admin/dashboard';
import { Meter, Panel, PanelError, SkeletonRows, ViewAllLink } from './primitives';

export function StoragePanel({
  budget, loading, error, onRetry,
}: {
  budget: StorageBudget | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  if (loading) {
    return <Panel icon={HardDrive} title="Storage"><SkeletonRows n={5} /></Panel>;
  }
  if (error || !budget) {
    return (
      <Panel icon={HardDrive} title="Storage" right={<ViewAllLink href="/admin/intermediate-products" />}>
        <PanelError message={error} onRetry={onRetry} />
      </Panel>
    );
  }

  const segments = budgetSegments(budget);
  const pct = budgetPercent(budget);
  const collected = budget.by_class?.collected;

  return (
    <Panel icon={HardDrive} title="Storage" right={<ViewAllLink href="/admin/intermediate-products" />}>
      <div className="px-3 pt-3 pb-2">
        <div className="flex items-baseline justify-between gap-3 mb-2">
          <span className="text-lg font-semibold tabular-nums text-text-primary">{fmtBytes(budget.total_bytes)}</span>
          <span className={`text-xs tabular-nums ${pct >= 90 ? 'text-danger' : pct >= 75 ? 'text-warning' : 'text-text-secondary'}`}>
            {pct.toFixed(1)}% of {fmtBytes(budget.cap_bytes)}
          </span>
        </div>
        <Meter
          segments={segments.map((s) => ({ value: s.bytes, className: s.className, title: `${s.label}: ${fmtBytes(s.bytes)}` }))}
          totalOverride={budget.cap_bytes}
          height={8}
        />
      </div>
      <div className="divide-y divide-border border-t border-border text-xs">
        {segments.map((s) => (
          <div key={s.key} className="flex items-center gap-2 px-3 py-1.5">
            <span className={`w-2 h-2 rounded-sm ${s.className} shrink-0`} />
            <span className="text-text-secondary">{s.label}</span>
            {s.count != null && <span className="text-text-tertiary tabular-nums">{fmtCount(s.count)}</span>}
            <span className="ml-auto tabular-nums text-text-primary">{fmtBytes(s.bytes)}</span>
          </div>
        ))}
        {collected && collected.count > 0 && (
          <div className="flex items-center gap-2 px-3 py-1.5">
            <span className="w-2 h-2 rounded-sm border border-border shrink-0" />
            <span className="text-text-secondary">Collected</span>
            <span className="text-text-tertiary tabular-nums">{fmtCount(collected.count)}</span>
            <span className="ml-auto tabular-nums text-text-tertiary" title="Size before collection">
              {fmtBytes(collected.logical_bytes ?? 0)} freed
            </span>
          </div>
        )}
      </div>
    </Panel>
  );
}
