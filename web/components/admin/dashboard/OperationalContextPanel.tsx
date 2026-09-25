'use client';

import React from 'react';
import Link from 'next/link';
import { Gauge } from 'lucide-react';
import type { ArchiveOverview, DashboardSummary } from '@/lib/actions/admin-dashboard';
import { fmtCount } from '@/lib/admin/format';
import { Panel, PanelError, SkeletonRows } from './primitives';

function ContextRow({
  label, value, detail, href, tone = 'neutral',
}: {
  label: string;
  value: React.ReactNode;
  detail?: React.ReactNode;
  href?: string;
  tone?: 'neutral' | 'warning';
}) {
  const body = (
    <div className="flex items-start justify-between gap-4 px-3 py-2.5">
      <div>
        <div className="text-xs text-text-secondary">{label}</div>
        {detail && <div className="text-[11px] text-text-tertiary mt-0.5">{detail}</div>}
      </div>
      <div className={`text-sm text-right tabular-nums ${tone === 'warning' ? 'text-warning' : 'text-text-primary'}`}>
        {value}
      </div>
    </div>
  );
  return href ? <Link href={href} className="block hover:bg-card-hover">{body}</Link> : body;
}

export function OperationalContextPanel({
  summary,
  overview,
  loading,
  error,
  onRetry,
}: {
  summary: DashboardSummary | null;
  overview: ArchiveOverview | null;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  if (loading) {
    return <Panel icon={Gauge} title="Operational context"><SkeletonRows n={6} /></Panel>;
  }
  if (error || !summary) {
    return (
      <Panel icon={Gauge} title="Operational context">
        <PanelError message={error} onRetry={onRetry} />
      </Panel>
    );
  }

  const provenanceIssues = summary.deployments.missing_provenance;
  return (
    <Panel icon={Gauge} title="Operational context">
      <div className="divide-y divide-border">
        <ContextRow
          label="Deployment provenance"
          value={provenanceIssues > 0 ? `${fmtCount(provenanceIssues)} missing` : 'Complete'}
          detail={overview?.latest_cfpipe_version
            ? <span className="font-mono">Latest: cfpipe {overview.latest_cfpipe_version}</span>
            : `${fmtCount(summary.deployments.unreleased_published)} deployments use non-release versions`}
          href="/admin/deployments"
          tone={provenanceIssues > 0 ? 'warning' : 'neutral'}
        />
        <ContextRow
          label="Calibration contexts"
          value={fmtCount(summary.deployments.distinct_crds_contexts)}
          detail="Across current deployments"
          href="/admin/deployments"
        />
      </div>
      <div className="border-t border-border px-3 py-2 text-[11px] text-text-tertiary">
        Actual backend usage and stale file/deployment checks are not yet exposed to this page.
      </div>
    </Panel>
  );
}
