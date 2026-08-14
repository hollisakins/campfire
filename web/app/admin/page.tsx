'use client';

import React from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import { Card } from '@/components/ui/Card';
import {
  Camera, Database, GitBranch, HardDrive, LayoutDashboard, Telescope, History,
} from 'lucide-react';
import { getDeployments, getDeployEvents } from '@/lib/actions/deployments';
import { getReductionProgress } from '@/lib/actions/nircam-exposures';
import { getStorageBudget, type StorageBudget } from '@/lib/actions/storage-registry';

// ---------------------------------------------------------------------------
// The admin landing page (admin audit 2026-07-03, §3.A): stat tiles and
// "needs attention" queues over already-computable aggregates, each
// deep-linking into the relevant section with pre-applied URL filters (the
// framework-migrated pages parse them). Replaces the old /admin → /admin/codes
// redirect.
// ---------------------------------------------------------------------------

function fmtBytes(n: number): string {
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB', 'PB']) {
    if (Math.abs(v) < 1024 || u === 'PB') return u === 'B' ? `${v} B` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v} B`;
}

function fmtWhen(ts: string): string {
  let iso = ts;
  if (iso.includes(' ') && !iso.includes('T')) iso = iso.replace(' ', 'T');
  if (iso.endsWith('+00')) iso = iso + ':00';
  else if (!iso.endsWith('Z') && !iso.includes('+')) iso = iso + 'Z';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function StatTile({
  href, icon: Icon, label, value, accent, sub,
}: {
  href: string;
  icon: React.ElementType;
  label: string;
  value: React.ReactNode;
  accent?: boolean;
  sub?: string;
}) {
  return (
    <Link href={href} className="block group">
      <Card className="p-4 h-full transition-colors group-hover:bg-card-hover">
        <div className="flex items-center gap-2 text-text-secondary text-sm mb-1.5">
          <Icon className="w-4 h-4" />
          {label}
        </div>
        <div className={`text-2xl font-semibold tabular-nums ${accent ? 'text-yellow-600 dark:text-yellow-400' : 'text-text-primary'}`}>
          {value}
        </div>
        {sub && <div className="text-xs text-text-secondary mt-1">{sub}</div>}
      </Card>
    </Link>
  );
}

export default function AdminDashboardPage() {
  const { data: progressResult } = useQuery({
    queryKey: ['admin-nircam-progress'],
    queryFn: getReductionProgress,
    staleTime: 30_000,
  });
  const { data: drafts } = useQuery({
    queryKey: ['admin-dashboard-drafts'],
    queryFn: () => getDeployments({ status: 'draft', pageSize: 1 }),
    staleTime: 30_000,
  });
  const { data: budget } = useQuery({
    queryKey: ['admin-storage-budget'],
    queryFn: getStorageBudget,
    staleTime: 60_000,
  });
  const { data: recentEvents } = useQuery({
    queryKey: ['admin-dashboard-events'],
    queryFn: () => getDeployEvents({ pageSize: 5 }),
    staleTime: 30_000,
  });
  const { data: pendingRequests } = useQuery({
    queryKey: ['admin-dashboard-inspection-requests'],
    queryFn: async () => {
      const res = await fetch('/api/admin/inspection-requests?status=pending');
      if (!res.ok) return { requests: [] as unknown[] };
      return (await res.json()) as { requests: unknown[] };
    },
    staleTime: 30_000,
  });

  const progress = progressResult?.progress ?? [];
  const pendingReview = progress.reduce((s, r) => s + (r.pending_review ?? 0), 0);
  const needsCorrection = progress.reduce((s, r) => s + (r.needs_correction ?? 0), 0);
  // The view is detector-grain; roll up to field/filter for the queue table.
  // Accumulate every row (so Total covers fully-reviewed detectors too), then
  // keep only groups that still have pending work.
  const attentionMap = new Map<string, { field: string; filter: string; pending: number; total: number }>();
  for (const r of progress) {
    const key = `${r.field}|${r.filter}`;
    const acc = attentionMap.get(key) ?? { field: r.field, filter: r.filter, pending: 0, total: 0 };
    acc.pending += r.pending_review ?? 0;
    acc.total += r.total ?? 0;
    attentionMap.set(key, acc);
  }
  const attention = Array.from(attentionMap.values()).filter((r) => r.pending > 0);

  const budgetOk = budget && 'total_bytes' in budget;
  const b = budgetOk ? (budget as StorageBudget) : null;

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <LayoutDashboard className="w-6 h-6 text-primary" />
        <h1 className="text-2xl font-semibold text-text-primary">Dashboard</h1>
      </div>
      <p className="text-text-secondary text-sm mb-6">
        Reduction-loop status at a glance. Every tile links into the relevant section with
        the matching filter applied.
      </p>

      <div className="grid grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
        <StatTile
          href="/admin/nircam?review=pending"
          icon={Camera}
          label="Exposures pending review"
          value={pendingReview}
          accent={pendingReview > 0}
        />
        <StatTile
          href="/admin/nircam?correction=needed"
          icon={Camera}
          label="Needs correction"
          value={needsCorrection}
          accent={needsCorrection > 0}
        />
        <StatTile
          href="/admin/deployments?status=draft"
          icon={GitBranch}
          label="Draft deployments"
          value={drafts?.total ?? '—'}
          accent={(drafts?.total ?? 0) > 0}
        />
        <StatTile
          href="/admin/inspection-requests"
          icon={Telescope}
          label="Pending access requests"
          value={pendingRequests?.requests?.length ?? '—'}
          accent={(pendingRequests?.requests?.length ?? 0) > 0}
        />
        <StatTile
          href="/admin/intermediate-products"
          icon={HardDrive}
          label="Storage used"
          value={b ? fmtBytes(b.total_bytes) : '—'}
          sub={b ? `of ${fmtBytes(b.cap_bytes)} (${b.pct_used}%)` : undefined}
        />
      </div>

      {attention.length > 0 && (
        <Card className="mb-8 overflow-hidden">
          <div className="px-4 py-3 border-b border-border bg-surface-2 flex items-center gap-2">
            <Camera className="w-4 h-4 text-text-secondary" />
            <h2 className="text-sm font-medium text-text-primary uppercase tracking-wider">
              Needs attention — pending review by field / filter
            </h2>
          </div>
          <table className="w-full text-sm">
            <thead className="text-text-secondary text-left border-b border-border">
              <tr>
                <th className="px-4 py-2 font-medium">Field</th>
                <th className="px-4 py-2 font-medium">Filter</th>
                <th className="px-4 py-2 font-medium text-right">Pending</th>
                <th className="px-4 py-2 font-medium text-right">Total</th>
              </tr>
            </thead>
            <tbody>
              {attention.map((r) => (
                <tr key={`${r.field}-${r.filter}`} className="border-t border-border hover:bg-card-hover/50">
                  <td className="px-4 py-2 font-medium text-text-primary">{r.field}</td>
                  <td className="px-4 py-2 text-text-primary">{r.filter}</td>
                  <td className="px-4 py-2 text-right">
                    <Link
                      href={`/admin/nircam?field=${encodeURIComponent(r.field)}&filter=${encodeURIComponent(r.filter)}&review=pending`}
                      className="text-yellow-600 dark:text-yellow-400 font-medium hover:underline"
                    >
                      {r.pending}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-right text-text-secondary tabular-nums">{r.total}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <Card className="overflow-hidden">
        <div className="px-4 py-3 border-b border-border bg-surface-2 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <History className="w-4 h-4 text-text-secondary" />
            <h2 className="text-sm font-medium text-text-primary uppercase tracking-wider">
              Recent deploy activity
            </h2>
          </div>
          <Link href="/admin/deployments" className="text-xs text-primary hover:underline">
            View all
          </Link>
        </div>
        {(recentEvents?.events?.length ?? 0) === 0 ? (
          <p className="p-4 text-sm text-text-secondary">No deploy events yet.</p>
        ) : (
          <table className="w-full text-sm">
            <tbody>
              {recentEvents!.events.map((e) => (
                <tr key={e.id} className="border-t border-border first:border-t-0">
                  <td className="px-4 py-2 text-text-secondary whitespace-nowrap w-36">
                    {fmtWhen(e.occurred_at)}
                  </td>
                  <td className="px-4 py-2 font-medium text-text-primary w-24">{e.action}</td>
                  <td className="px-4 py-2 font-mono text-xs">
                    {e.observation ?? e.field ?? '—'}
                  </td>
                  <td className="px-4 py-2 text-text-secondary text-right">
                    {e.actor_name ?? '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <div className="mt-8 grid grid-cols-2 gap-4">
        <Link href="/admin/intermediate-products" className="block group">
          <Card className="p-4 transition-colors group-hover:bg-card-hover flex items-center gap-3">
            <Database className="w-5 h-5 text-text-secondary" />
            <div>
              <div className="text-sm font-medium text-text-primary">Storage registry</div>
              <div className="text-xs text-text-secondary">Browse every registered object in the bucket</div>
            </div>
          </Card>
        </Link>
        <Link href="/admin/nircam" className="block group">
          <Card className="p-4 transition-colors group-hover:bg-card-hover flex items-center gap-3">
            <Camera className="w-5 h-5 text-text-secondary" />
            <div>
              <div className="text-sm font-medium text-text-primary">NIRCam reductions</div>
              <div className="text-xs text-text-secondary">Per-field/filter progress + exposure review</div>
            </div>
          </Card>
        </Link>
      </div>
    </div>
  );
}
