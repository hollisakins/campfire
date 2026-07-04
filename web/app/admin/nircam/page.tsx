'use client';

import React, { Suspense, useMemo, useState } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Card } from '@/components/ui/Card';
import { Loader2, ChevronRight, Copy, Check } from 'lucide-react';
import { AdminTable } from '@/components/admin/AdminTable';
import { AdminFilterBar } from '@/components/admin/AdminFilterBar';
import { flatFilterCodec, useTableUrlState, type SortState } from '@/lib/hooks/useTableUrlState';
import { useAdminTableQuery } from '@/lib/hooks/useAdminTableQuery';
import {
  getNircamExposures,
  getReductionProgress,
  getExposureFilterOptions,
  getExcludedExposures,
  type ReductionProgress,
  type ExcludedExposure,
} from '@/lib/actions/nircam-exposures';
import { EXPOSURE_SORT_KEYS } from '@/lib/admin/sort-keys';
import type { NircamExposure } from '@/lib/types';
import {
  stageBadgeClasses,
  stageBarClasses,
  NIRCAM_STAGES,
  STAGE_COLUMN_KEYS,
} from '@/lib/nircam-stages';
import { buildExposureNavQuery } from '@/lib/nircam-exposure-nav';

// ---------------------------------------------------------------------------
// Status badge helpers
// ---------------------------------------------------------------------------

function ReviewBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    pending: 'bg-yellow-100 dark:bg-yellow-900 text-yellow-800 dark:text-yellow-300',
    approved: 'bg-green-100 dark:bg-green-900 text-green-800 dark:text-green-300',
    excluded: 'bg-red-100 dark:bg-red-900 text-red-800 dark:text-red-300',
  };
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${colors[status] || 'bg-surface-2 text-text-primary'}`}>
      {status}
    </span>
  );
}

function ActionBadge({ status, label }: { status: string; label: string }) {
  if (status === 'none') return <span className="text-xs text-text-secondary">&mdash;</span>;
  const colors: Record<string, string> = {
    needed: 'bg-orange-100 dark:bg-orange-900 text-orange-800 dark:text-orange-300',
    done: 'bg-blue-100 dark:bg-blue-900 text-blue-800 dark:text-blue-300',
  };
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${colors[status] || ''}`}>
      {label}: {status}
    </span>
  );
}

function StageBadge({ stage }: { stage: string }) {
  return (
    <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full font-mono ${stageBadgeClasses(stage)}`}>
      {stage}
    </span>
  );
}

// Horizontal stacked-bar showing distribution of exposures across pipeline
// stages within a (field, filter) group. Segments are colored by phase
// bucket; native title-tooltips show the exact step name and count.
function StageDistributionBar({ progress }: { progress: ReductionProgress }) {
  const total = progress.total || 1;
  const segments = STAGE_COLUMN_KEYS
    .map(({ stage, key }) => ({
      stage,
      count: (progress[key] as number) || 0,
    }))
    .filter(s => s.count > 0);

  if (segments.length === 0) {
    return <div className="h-3 bg-surface-2 rounded" />;
  }

  return (
    <div className="flex h-3 rounded overflow-hidden bg-surface-2">
      {segments.map(({ stage, count }) => (
        <div
          key={stage}
          className={stageBarClasses(stage)}
          style={{ width: `${(count / total) * 100}%` }}
          title={`${stage}: ${count}`}
        />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Progress table
// ---------------------------------------------------------------------------

function ProgressTable({ progress }: { progress: ReductionProgress[] }) {
  if (progress.length === 0) {
    return (
      <p className="text-sm text-text-secondary py-4">
        No reduction data available. Deploy exposures with <code className="text-xs bg-card dark:bg-card-hover px-1 py-0.5 rounded">campfire deploy nircam</code>.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-border">
          <tr>
            <th className="px-3 py-2 text-left text-xs font-medium text-text-secondary uppercase">Field</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-text-secondary uppercase">Filter</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary uppercase">Total</th>
            <th className="px-3 py-2 text-left text-xs font-medium text-text-secondary uppercase w-1/3">Stage distribution</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary uppercase">Pending</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary uppercase">Masking</th>
            <th className="px-3 py-2 text-right text-xs font-medium text-text-secondary uppercase">Correction</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {progress.map((row) => (
            <tr key={`${row.field}-${row.filter}`} className="hover:bg-card-hover">
              <td className="px-3 py-2 font-medium text-text-primary">{row.field}</td>
              <td className="px-3 py-2 text-text-primary">{row.filter}</td>
              <td className="px-3 py-2 text-right text-text-primary">{row.total}</td>
              <td className="px-3 py-2">
                <StageDistributionBar progress={row} />
              </td>
              <td className="px-3 py-2 text-right">
                {row.pending_review > 0 ? (
                  <Link
                    href={`/admin/nircam?field=${encodeURIComponent(row.field)}&filter=${encodeURIComponent(row.filter)}&review=pending`}
                    className="text-yellow-600 dark:text-yellow-400 font-medium hover:underline"
                  >
                    {row.pending_review}
                  </Link>
                ) : (
                  <span className="text-text-secondary">0</span>
                )}
              </td>
              <td className="px-3 py-2 text-right">
                {row.needs_masking > 0 ? (
                  <Link
                    href={`/admin/nircam?field=${encodeURIComponent(row.field)}&filter=${encodeURIComponent(row.filter)}&masking=needed`}
                    className="text-orange-600 dark:text-orange-400 font-medium hover:underline"
                  >
                    {row.needs_masking}
                  </Link>
                ) : (
                  <span className="text-text-secondary">0</span>
                )}
              </td>
              <td className="px-3 py-2 text-right">
                {row.needs_correction > 0 ? (
                  <Link
                    href={`/admin/nircam?field=${encodeURIComponent(row.field)}&filter=${encodeURIComponent(row.filter)}&correction=needed`}
                    className="text-orange-600 dark:text-orange-400 font-medium hover:underline"
                  >
                    {row.needs_correction}
                  </Link>
                ) : (
                  <span className="text-text-secondary">0</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Excluded exposures panel — copy-paste source for fields.toml skip=[]
// ---------------------------------------------------------------------------

function ExcludedPanel({ excluded }: { excluded: ExcludedExposure[] }) {
  const [copied, setCopied] = useState<string | null>(null);

  // Group by (field, filter); within each group emit the TOML-fragment line list.
  const groups = React.useMemo(() => {
    const m = new Map<string, ExcludedExposure[]>();
    for (const e of excluded) {
      const k = `${e.field} / ${e.filter}`;
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(e);
    }
    return Array.from(m.entries());
  }, [excluded]);

  if (excluded.length === 0) return null;

  const copy = (key: string, text: string) => {
    navigator.clipboard.writeText(text);
    setCopied(key);
    setTimeout(() => setCopied(null), 1500);
  };

  return (
    <Card className="mb-6 overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-baseline justify-between bg-surface-2">
        <h2 className="text-sm font-medium text-text-primary uppercase tracking-wider">
          Excluded — copy into <code className="font-mono text-xs">fields.toml</code> <code className="font-mono text-xs">skip = […]</code>
        </h2>
        <span className="text-xs text-text-secondary">{excluded.length} total</span>
      </div>
      <div className="divide-y divide-border">
        {groups.map(([heading, rows]) => {
          const tomlBlock = rows.map(r => `    "${r.filename}",`).join('\n');
          return (
            <div key={heading} className="p-4">
              <div className="flex items-baseline justify-between mb-2">
                <h3 className="text-xs font-medium text-text-secondary">
                  {heading} <span className="text-text-secondary">({rows.length})</span>
                </h3>
                <button
                  onClick={() => copy(heading, tomlBlock)}
                  className="text-xs text-primary hover:underline inline-flex items-center gap-1"
                >
                  {copied === heading ? (
                    <><Check className="w-3 h-3" /> Copied</>
                  ) : (
                    <><Copy className="w-3 h-3" /> Copy</>
                  )}
                </button>
              </div>
              <pre className="text-xs font-mono bg-surface-2 p-2 rounded overflow-x-auto text-text-primary">{tomlBlock}</pre>
              {rows.some(r => r.notes) && (
                <ul className="mt-2 text-xs text-text-secondary space-y-0.5">
                  {rows.filter(r => r.notes).map(r => (
                    <li key={r.filename}>
                      <span className="font-mono">{r.filename}</span> — {r.notes}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// URL-state config (module-level: codec/whitelist must be stable references).
// The same param names are what the detail page parses for prev/next nav —
// see lib/nircam-exposure-nav.ts.
// ---------------------------------------------------------------------------

const FILTER_KEYS = ['field', 'filter', 'detector', 'review', 'stage', 'masking', 'correction'] as const;
const codec = flatFilterCodec(FILTER_KEYS);
const DEFAULT_SORT: SortState = { column: 'filename', direction: 'asc' };

const REVIEW_OPTIONS = [
  { value: 'pending', label: 'Pending' },
  { value: 'approved', label: 'Approved' },
  { value: 'excluded', label: 'Excluded' },
];
const ACTION_STATE_OPTIONS = [
  { value: 'needed', label: 'Needed' },
  { value: 'done', label: 'Done' },
  { value: 'none', label: 'None' },
];

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

function AdminNircamPageInner() {
  const state = useTableUrlState({
    codec,
    sortWhitelist: EXPOSURE_SORT_KEYS,
    defaultSort: DEFAULT_SORT,
  });

  const exposures = useAdminTableQuery<NircamExposure>({
    scope: 'admin-nircam-exposures',
    filters: state.debouncedFilters,
    sort: state.sort,
    page: state.page,
    pageSize: state.pageSize,
    fetchPage: async (page) => {
      const f = state.debouncedFilters;
      const res = await getNircamExposures({
        field: f.field || undefined,
        filter: f.filter || undefined,
        detector: f.detector || undefined,
        reviewStatus: f.review || undefined,
        stage: f.stage || undefined,
        masking: f.masking || undefined,
        correction: f.correction || undefined,
        sortColumn: state.sort.column,
        sortDirection: state.sort.direction,
        page,
        pageSize: state.pageSize,
      });
      return { rows: res.exposures, total: res.total, error: res.error };
    },
  });

  const { data: progressResult } = useQuery({
    queryKey: ['admin-nircam-progress'],
    queryFn: getReductionProgress,
    staleTime: 30_000,
  });
  const { data: excludedResult } = useQuery({
    queryKey: ['admin-nircam-excluded'],
    queryFn: getExcludedExposures,
    staleTime: 30_000,
  });
  const { data: facets } = useQuery({
    queryKey: ['admin-nircam-facets'],
    queryFn: getExposureFilterOptions,
    staleTime: 5 * 60_000,
  });

  // Row links carry the current filter+sort state so the detail page derives
  // prev/next from the same set (see lib/nircam-exposure-nav.ts).
  const navQuery = useMemo(
    () => buildExposureNavQuery(state.filters, state.sort, DEFAULT_SORT),
    [state.filters, state.sort],
  );
  const detailHref = (id: number) => `/admin/nircam/${id}${navQuery ? `?${navQuery}` : ''}`;

  const columns = useMemo<ColumnDef<NircamExposure, unknown>[]>(() => [
    {
      id: 'filename',
      header: 'Filename',
      cell: ({ row }) => (
        <Link
          href={detailHref(row.original.id)}
          className="text-sm font-mono text-primary hover:underline"
        >
          {row.original.filename}
        </Link>
      ),
      meta: { sortKey: 'filename' },
    },
    {
      id: 'field',
      header: 'Field',
      cell: ({ row }) => <span className="text-sm text-text-primary">{row.original.field}</span>,
      meta: { sortKey: 'field' },
    },
    {
      id: 'filter',
      header: 'Filter',
      cell: ({ row }) => <span className="text-sm text-text-primary">{row.original.filter}</span>,
      meta: { sortKey: 'filter' },
    },
    {
      id: 'detector',
      header: 'Detector',
      cell: ({ row }) => <span className="text-sm text-text-secondary">{row.original.detector}</span>,
      meta: { sortKey: 'detector' },
    },
    {
      id: 'stage',
      header: 'Stage',
      cell: ({ row }) => <StageBadge stage={row.original.stage} />,
      meta: { sortKey: 'stage' },
    },
    {
      id: 'review',
      header: 'Review',
      cell: ({ row }) => <ReviewBadge status={row.original.review_status} />,
      meta: { sortKey: 'review_status' },
    },
    {
      id: 'masking',
      header: 'Masking',
      cell: ({ row }) => <ActionBadge status={row.original.masking} label="mask" />,
    },
    {
      id: 'correction',
      header: 'Correction',
      cell: ({ row }) => <ActionBadge status={row.original.correction} label="corr" />,
    },
    {
      id: 'open',
      header: '',
      cell: ({ row }) => (
        <Link href={detailHref(row.original.id)}>
          <ChevronRight className="w-4 h-4 text-text-secondary" />
        </Link>
      ),
      meta: { align: 'right' },
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ], [navQuery]);

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-semibold text-text-primary">NIRCam Reductions</h1>
      </div>

      {progressResult?.error && (
        <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 mb-6">
          <p className="text-red-800 dark:text-red-400">{progressResult.error}</p>
        </div>
      )}

      {/* Progress summary */}
      <Card className="mb-6 overflow-hidden">
        <div className="px-4 py-3 border-b border-border bg-surface-2">
          <h2 className="text-sm font-medium text-text-primary uppercase tracking-wider">
            Reduction Progress
          </h2>
        </div>
        <ProgressTable progress={progressResult?.progress ?? []} />
      </Card>

      {/* Excluded exposures (copy-paste source for fields.toml skip=[]) */}
      <ExcludedPanel excluded={excludedResult?.excluded ?? []} />

      <AdminFilterBar
        facets={[
          {
            kind: 'select', key: 'field', label: 'Field',
            options: (facets?.fields ?? []).map((f) => ({ value: f, label: f })),
          },
          {
            kind: 'select', key: 'filter', label: 'Filter',
            options: (facets?.filters ?? []).map((f) => ({ value: f, label: f })),
          },
          {
            kind: 'select', key: 'detector', label: 'Detector',
            options: (facets?.detectors ?? []).map((d) => ({ value: d, label: d })),
          },
          {
            kind: 'select', key: 'stage', label: 'Stage',
            options: NIRCAM_STAGES.map((s) => ({ value: s, label: s })),
          },
          { kind: 'select', key: 'review', label: 'Review', options: REVIEW_OPTIONS },
          { kind: 'select', key: 'masking', label: 'Masking', options: ACTION_STATE_OPTIONS },
          { kind: 'select', key: 'correction', label: 'Correction', options: ACTION_STATE_OPTIONS },
        ]}
        values={state.filters}
        onChange={(key, value) => state.setFilters({ ...state.filters, [key]: value })}
        onReset={state.resetFilters}
      />

      <AdminTable
        columns={columns}
        data={exposures.rows}
        total={exposures.total}
        page={state.page}
        pageSize={state.pageSize}
        sort={state.sort}
        loading={exposures.isInitialLoading}
        fetching={exposures.isFetching || state.isDebouncing}
        error={exposures.error}
        emptyTitle="No exposures found."
        onSortChange={state.setSort}
        onPageChange={state.setPage}
        onPageSizeChange={state.setPageSize}
        getRowKey={(e) => e.id}
        pageSizeOptions={[25, 50, 100, 200]}
      />
    </div>
  );
}

export default function AdminNircamPage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      }
    >
      <AdminNircamPageInner />
    </Suspense>
  );
}
