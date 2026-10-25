'use client';

import React from 'react';
import Link from 'next/link';
import { Camera, ListChecks, Telescope } from 'lucide-react';
import type { AdminDashboardSummary } from '@/app/api/admin/dashboard/route';
import { fmtCount } from '@/lib/admin/format';
import { CountLink, Meter, Panel, PanelError, SkeletonRows } from './primitives';

interface ScopeChip {
  label: string;
  count: number;
  href: string;
}

function WorkflowRow({
  icon: Icon, label, done, total, primary, secondary, chips,
}: {
  icon: React.ElementType;
  label: string;
  done: number;
  total: number;
  primary: { value: number; label: string; href: string };
  secondary?: { value: number; label: string; href: string };
  chips: ScopeChip[];
}) {
  return (
    <div className="px-3 py-3 grid gap-3 md:grid-cols-[minmax(170px,1fr)_minmax(180px,1.4fr)_minmax(200px,1.4fr)] md:items-center">
      <div className="flex items-center gap-2.5 min-w-0">
        <Icon className="w-4 h-4 text-text-tertiary shrink-0" />
        <div className="min-w-0">
          <div className="text-sm font-medium text-text-primary">{label}</div>
          <div className="text-[11px] text-text-tertiary tabular-nums">
            {fmtCount(done)} / {fmtCount(total)} inspected
          </div>
        </div>
      </div>

      <div className="min-w-0">
        <Meter
          segments={[{ value: done, className: 'bg-success' }]}
          totalOverride={total}
          height={5}
          className="mb-2"
        />
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          <CountLink value={primary.value} label={primary.label} href={primary.href} warn />
          {secondary && <CountLink value={secondary.value} label={secondary.label} href={secondary.href} warn />}
        </div>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {chips.length === 0 ? (
          <span className="text-xs text-text-tertiary">Clear</span>
        ) : chips.map((chip) => (
          <Link
            key={chip.href}
            href={chip.href}
            className="inline-flex items-center gap-1.5 rounded-md bg-surface-2 px-2 py-1 text-[11px] text-text-secondary hover:text-text-primary"
          >
            <span className="font-mono truncate max-w-44">{chip.label}</span>
            <span className="tabular-nums text-warning">{fmtCount(chip.count)}</span>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function ReviewPanel({
  review, loading, error, onRetry,
}: {
  review: AdminDashboardSummary['review'] | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel icon={ListChecks} title="Review">
      {loading ? (
        <SkeletonRows n={5} />
      ) : error || !review ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : (
        <div className="divide-y divide-border">
          <WorkflowRow
            icon={Camera}
            label="NIRCam exposures"
            done={review.nircam.done}
            total={review.nircam.total}
            primary={{ value: review.nircam.pending, label: 'pending', href: '/admin/nircam?review=pending' }}
            secondary={{ value: review.nircam.needs_correction, label: 'need correction', href: '/admin/nircam?correction=needed' }}
            chips={review.nircam.top.map((s) => ({
              label: `${s.field} / ${s.filter}`,
              count: s.pending,
              href: `/admin/nircam?field=${encodeURIComponent(s.field)}&filter=${encodeURIComponent(s.filter)}&review=pending`,
            }))}
          />
          <WorkflowRow
            icon={Telescope}
            label="NIRSpec objects"
            done={Math.max(0, review.objects.published - review.objects.uninspected)}
            total={review.objects.published}
            primary={{ value: review.objects.uninspected, label: 'uninspected', href: '/nirspec?quality=0' }}
            secondary={{ value: review.objects.needs_review, label: 'need review', href: '/nirspec?needs_review=true' }}
            chips={review.objects.top.map((s) => ({
              label: s.field,
              count: s.uninspected,
              href: `/nirspec?quality=0&fields=${encodeURIComponent(s.field)}`,
            }))}
          />
        </div>
      )}
    </Panel>
  );
}
