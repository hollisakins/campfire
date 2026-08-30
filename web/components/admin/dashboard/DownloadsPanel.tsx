'use client';

import React from 'react';
import Link from 'next/link';
import { Download } from 'lucide-react';
import type { DownloadStats } from '@/lib/actions/admin-dashboard';
import { Panel, PanelError, SkeletonRows, Sparkline, StatCell, ViewAllLink } from './primitives';
import { fmtCount } from '@/lib/admin/format';

// ---------------------------------------------------------------------------
// Downloads (30d): totals, the per-day series get_download_stats has always
// returned and nothing rendered, the by-type split (including fits_sync — CLI
// pulls — which the downloads page's label map omits), and top targets.
// ---------------------------------------------------------------------------

export const DOWNLOAD_TYPE_LABELS: Record<string, string> = {
  fits_single: 'single',
  fits_object: 'object',
  fits_batch: 'batch',
  fits_zip: 'zip',
  csv: 'csv',
  sed_plot: 'sed',
  fits_sync: 'cli sync',
};

export function DownloadsPanel({
  stats, days, loading, error, onRetry,
}: {
  stats: DownloadStats | null;
  days: number;
  loading: boolean;
  error?: string;
  onRetry: () => void;
}) {
  return (
    <Panel
      icon={Download}
      title={`Downloads · ${days}d`}
      right={<ViewAllLink href="/admin/downloads" />}
    >
      {loading ? (
        <SkeletonRows n={6} />
      ) : error || !stats ? (
        <PanelError message={error} onRetry={onRetry} />
      ) : stats.total_downloads === 0 ? (
        <p className="p-3 text-xs text-text-tertiary">No downloads in {days} days.</p>
      ) : (
        <div className="p-3 space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <StatCell value={fmtCount(stats.total_downloads)} label="Downloads" href="/admin/downloads" />
            <StatCell value={fmtCount(stats.unique_users)} label="Users" href="/admin/downloads" />
            <StatCell value={fmtCount(stats.total_files)} label="Files" href="/admin/downloads" />
          </div>

          <Sparkline series={stats.downloads_by_day ?? []} days={days} />

          {stats.by_type && Object.keys(stats.by_type).length > 0 && (
            <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-text-secondary">
              {Object.entries(stats.by_type)
                .sort((a, b) => b[1] - a[1])
                .map(([type, n]) => (
                  <span key={type} className="whitespace-nowrap">
                    {DOWNLOAD_TYPE_LABELS[type] ?? type}{' '}
                    <span className="tabular-nums text-text-primary">{fmtCount(n)}</span>
                  </span>
                ))}
            </div>
          )}

          {(stats.most_downloaded_targets?.length ?? 0) > 0 && (
            <div className="space-y-0.5">
              <div className="text-[10px] uppercase tracking-wider text-text-tertiary">
                Top targets
              </div>
              {stats.most_downloaded_targets!.slice(0, 3).map((t) => (
                <Link
                  key={t.target_id}
                  href={`/nirspec/targets/${encodeURIComponent(t.target_id)}`}
                  className="flex justify-between text-[11px] hover:bg-card-hover -mx-1 px-1 rounded"
                >
                  <span className="font-mono text-text-secondary truncate">{t.target_id}</span>
                  <span className="tabular-nums text-text-primary shrink-0">
                    {fmtCount(t.download_count)}
                  </span>
                </Link>
              ))}
            </div>
          )}
        </div>
      )}
    </Panel>
  );
}
