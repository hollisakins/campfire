'use client';

import React, { useState } from 'react';
import Link from 'next/link';
import { ListChecks } from 'lucide-react';
import type { ReviewQueues } from '@/lib/actions/admin-dashboard';
import { Panel, PanelError, SkeletonRows, Meter } from './primitives';
import { fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// Review board: the four review grains — NIRCam exposures, NIRSpec rate,
// NIRSpec nods, and objects (the science-side inspection backlog) — as four
// tabs over one repeated table shape (scope / coverage meter / pending), so
// the eye learns a single pattern. The two NIRSpec loops and the objects tier
// get dashboard standing for the first time.
// ---------------------------------------------------------------------------

type TabKey = 'nircam' | 'rate' | 'nods' | 'objects';

function Coverage({ done, total }: { done: number; total: number }) {
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  return (
    <div className="flex items-center gap-2 min-w-0">
      <Meter
        segments={[{ value: pct, className: pct >= 90 ? 'bg-success' : 'bg-warning' }]}
        totalOverride={100}
        height={4}
        className="w-14 shrink-0"
      />
      <span className="text-[11px] tabular-nums text-text-secondary">{pct}%</span>
    </div>
  );
}

function QueueTable({
  rows, empty,
}: {
  rows: {
    key: string;
    scope: React.ReactNode;
    coverage?: { done: number; total: number };
    pending: number;
    href?: string;
    extra?: React.ReactNode;
  }[];
  empty: string;
}) {
  if (rows.length === 0) {
    return <p className="px-3 py-2 text-xs text-text-tertiary">{empty}</p>;
  }
  return (
    <div className="divide-y divide-border">
      {rows.map((r) => {
        const inner = (
          <>
            <span className="font-mono text-xs text-text-primary truncate">{r.scope}</span>
            {r.extra}
            <span className="ml-auto flex items-center gap-3 shrink-0">
              {r.coverage && <Coverage done={r.coverage.done} total={r.coverage.total} />}
              <span className="text-xs font-semibold tabular-nums text-warning w-12 text-right">
                {fmtCount(r.pending)}
              </span>
            </span>
          </>
        );
        const cls = 'flex items-center gap-2 px-3 h-8';
        return r.href ? (
          <Link key={r.key} href={r.href} className={`${cls} hover:bg-card-hover`}>{inner}</Link>
        ) : (
          <div key={r.key} className={cls}>{inner}</div>
        );
      })}
    </div>
  );
}

export function ReviewBoard({
  queues, loading, error, onRetry,
}: {
  queues: ReviewQueues | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  const [tab, setTab] = useState<TabKey>('nircam');

  const tabs: { key: TabKey; label: string; pending: number }[] = queues
    ? [
        { key: 'nircam', label: 'NIRCAM', pending: queues.nircam.pending },
        { key: 'rate', label: 'RATE', pending: queues.rate.pending },
        { key: 'nods', label: 'NODS', pending: queues.nods.pending },
        { key: 'objects', label: 'OBJECTS', pending: queues.objects.uninspected },
      ]
    : [];

  return (
    <Panel
      id="review-board"
      icon={ListChecks}
      title="Review queues"
      right={
        queues && (
          <div className="flex items-center gap-1">
            {tabs.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={`px-1.5 py-0.5 rounded text-[10px] font-medium tracking-wide tabular-nums ${
                  tab === t.key
                    ? 'bg-primary text-on-primary'
                    : 'text-text-secondary hover:text-text-primary'
                }`}
              >
                {t.label}
                {t.pending > 0 && <span className="ml-1 opacity-80">{fmtCount(t.pending)}</span>}
              </button>
            ))}
          </div>
        )
      }
    >
      {loading ? (
        <SkeletonRows n={6} />
      ) : error || !queues ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <>
          {tab === 'nircam' && (
            <>
              <div className="px-3 py-2 flex items-center gap-3 text-xs text-text-secondary border-b border-border">
                <Coverage done={queues.nircam.done} total={queues.nircam.total} />
                <span>{fmtCount(queues.nircam.total)} exposures</span>
                {queues.nircam.needs_correction > 0 && (
                  <Link href="/admin/nircam?correction=needed" className="text-danger hover:underline">
                    {fmtCount(queues.nircam.needs_correction)} need correction
                  </Link>
                )}
                <Link href="/admin/nircam" className="ml-auto text-primary-text hover:underline">
                  All →
                </Link>
              </div>
              <QueueTable
                empty="Fully triaged."
                rows={queues.nircam.top.map((s) => ({
                  key: `${s.field}/${s.filter}`,
                  scope: `${s.field} / ${s.filter}`,
                  coverage: { done: s.done, total: s.total },
                  pending: s.pending,
                  href: `/admin/nircam?field=${encodeURIComponent(s.field)}&filter=${encodeURIComponent(s.filter)}&review=pending`,
                }))}
              />
            </>
          )}
          {tab === 'rate' && (
            <>
              <div className="px-3 py-2 flex items-center gap-3 text-xs text-text-secondary border-b border-border">
                <Coverage done={queues.rate.done} total={queues.rate.total} />
                <span>{fmtCount(queues.rate.total)} rate files</span>
                <Link href="/admin/nirspec/rate" className="ml-auto text-primary-text hover:underline">
                  All →
                </Link>
              </div>
              <QueueTable
                empty="Fully triaged."
                rows={queues.rate.top.map((s) => ({
                  key: s.observation,
                  scope: s.observation,
                  coverage: { done: s.done, total: s.total },
                  pending: s.pending,
                  href: `/admin/nirspec/rate?observation=${encodeURIComponent(s.observation)}&review=pending`,
                }))}
              />
            </>
          )}
          {tab === 'nods' && (
            <>
              <div className="px-3 py-2 flex items-center gap-3 text-xs text-text-secondary border-b border-border">
                <Coverage done={queues.nods.done} total={queues.nods.total} />
                <span>{fmtCount(queues.nods.total)} spectrum exposures</span>
                <Link href="/admin/nirspec/nods" className="ml-auto text-primary-text hover:underline">
                  All →
                </Link>
              </div>
              <QueueTable
                empty="Fully triaged."
                rows={queues.nods.top.map((s) => ({
                  key: s.observation,
                  scope: s.observation,
                  coverage: { done: s.done, total: s.total },
                  pending: s.pending,
                  extra: (
                    <span className="text-[11px] text-text-tertiary shrink-0">
                      {fmtCount(s.sources)} src
                    </span>
                  ),
                  href: '/admin/nirspec/nods',
                }))}
              />
            </>
          )}
          {tab === 'objects' && (
            <>
              <div className="px-3 py-2 flex items-center gap-3 text-xs text-text-secondary border-b border-border">
                <span>{fmtCount(queues.objects.published)} published objects</span>
                {queues.objects.stale > 0 && (
                  <span className="text-warning">{fmtCount(queues.objects.stale)} stale</span>
                )}
                {queues.objects.inactive > 0 && (
                  <span className="text-text-tertiary">{fmtCount(queues.objects.inactive)} inactive</span>
                )}
                <span className="ml-auto text-[10px] uppercase tracking-wider text-text-tertiary">
                  uninspected
                </span>
              </div>
              <QueueTable
                empty="Every published object is inspected."
                rows={queues.objects.top.map((s) => ({
                  key: s.field,
                  scope: s.field,
                  coverage: {
                    done: s.published - s.uninspected,
                    total: s.published,
                  },
                  pending: s.uninspected,
                }))}
              />
            </>
          )}
        </>
      )}
    </Panel>
  );
}
