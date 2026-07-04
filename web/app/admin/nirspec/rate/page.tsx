'use client';

import React, { Suspense, useMemo } from 'react';
import Link from 'next/link';
import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Loader2, ChevronRight } from 'lucide-react';
import { AdminTable } from '@/components/admin/AdminTable';
import { AdminFilterBar } from '@/components/admin/AdminFilterBar';
import { flatFilterCodec, useTableUrlState, type SortState } from '@/lib/hooks/useTableUrlState';
import { useAdminTableQuery } from '@/lib/hooks/useAdminTableQuery';
import {
  getNirspecRateExposures,
  getNirspecRateFilterOptions,
} from '@/lib/actions/nirspec-rate';
import { NIRSPEC_RATE_SORT_KEYS } from '@/lib/admin/sort-keys';
import type { NirspecRateExposure } from '@/lib/types';
import { buildRateNavQuery } from '@/lib/nirspec-rate-nav';

// ---------------------------------------------------------------------------
// Status badge helpers (mirror the NIRCam list)
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

// ---------------------------------------------------------------------------
// URL-state config (module-level: codec/whitelist must be stable references).
// The same param names are what the detail page parses for prev/next nav —
// see lib/nirspec-rate-nav.ts.
// ---------------------------------------------------------------------------

const FILTER_KEYS = ['observation', 'detector', 'review', 'masking', 'grating'] as const;
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
const DETECTOR_OPTIONS = [
  { value: 'nrs1', label: 'nrs1' },
  { value: 'nrs2', label: 'nrs2' },
];

function AdminNirspecRatePageInner() {
  const state = useTableUrlState({
    codec,
    sortWhitelist: NIRSPEC_RATE_SORT_KEYS,
    defaultSort: DEFAULT_SORT,
  });

  const exposures = useAdminTableQuery<NirspecRateExposure>({
    scope: 'admin-nirspec-rate',
    filters: state.debouncedFilters,
    sort: state.sort,
    page: state.page,
    pageSize: state.pageSize,
    fetchPage: async (page) => {
      const f = state.debouncedFilters;
      const res = await getNirspecRateExposures({
        observation: f.observation || undefined,
        detector: f.detector || undefined,
        reviewStatus: f.review || undefined,
        masking: f.masking || undefined,
        grating: f.grating || undefined,
        sortColumn: state.sort.column,
        sortDirection: state.sort.direction,
        page,
        pageSize: state.pageSize,
      });
      return { rows: res.rows, total: res.total, error: res.error };
    },
  });

  const { data: facets } = useQuery({
    queryKey: ['admin-nirspec-rate-facets'],
    queryFn: getNirspecRateFilterOptions,
    staleTime: 5 * 60_000,
  });

  // Row links carry the current filter+sort state so the detail page derives
  // prev/next from the same set (see lib/nirspec-rate-nav.ts).
  const navQuery = useMemo(
    () => buildRateNavQuery(state.filters, state.sort, DEFAULT_SORT),
    [state.filters, state.sort],
  );
  const detailHref = (id: number) => `/admin/nirspec/rate/${id}${navQuery ? `?${navQuery}` : ''}`;

  const columns = useMemo<ColumnDef<NirspecRateExposure, unknown>[]>(() => [
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
      id: 'observation',
      header: 'Observation',
      cell: ({ row }) => <span className="text-sm text-text-primary">{row.original.observation}</span>,
      meta: { sortKey: 'observation' },
    },
    {
      id: 'exposure_root',
      header: 'Exposure',
      cell: ({ row }) => <span className="text-sm font-mono text-text-secondary">{row.original.exposure_root}</span>,
      meta: { sortKey: 'exposure_root' },
    },
    {
      id: 'detector',
      header: 'Detector',
      cell: ({ row }) => <span className="text-sm text-text-secondary">{row.original.detector}</span>,
      meta: { sortKey: 'detector' },
    },
    {
      id: 'grating',
      header: 'Grating',
      cell: ({ row }) => <span className="text-sm text-text-secondary">{row.original.grating || '—'}</span>,
      meta: { sortKey: 'grating' },
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
        <h1 className="text-2xl font-semibold text-text-primary">NIRSpec Rate Masks</h1>
      </div>

      <p className="text-text-secondary text-sm mb-6">
        Detector rate files (<span className="font-mono">nrs1</span>/<span className="font-mono">nrs2</span>),
        source-independent. Draw <span className="font-mono">DO_NOT_USE</span> polygons on persistence
        trails / MSA shorts; <span className="font-mono">campfire deploy nirspec pull-rate-masks</span> materializes
        them before stage&nbsp;2.
      </p>

      <AdminFilterBar
        facets={[
          {
            kind: 'select', key: 'observation', label: 'Observation',
            options: (facets?.observations ?? []).map((o) => ({ value: o, label: o })),
          },
          { kind: 'select', key: 'detector', label: 'Detector', options: DETECTOR_OPTIONS },
          {
            kind: 'select', key: 'grating', label: 'Grating',
            options: (facets?.gratings ?? []).map((g) => ({ value: g, label: g })),
          },
          { kind: 'select', key: 'review', label: 'Review', options: REVIEW_OPTIONS },
          { kind: 'select', key: 'masking', label: 'Masking', options: ACTION_STATE_OPTIONS },
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
        emptyTitle="No rate files found."
        onSortChange={state.setSort}
        onPageChange={state.setPage}
        onPageSizeChange={state.setPageSize}
        getRowKey={(e) => e.id}
        pageSizeOptions={[25, 50, 100, 200]}
      />
    </div>
  );
}

export default function AdminNirspecRatePage() {
  return (
    <Suspense
      fallback={
        <div className="flex items-center justify-center py-16">
          <Loader2 className="w-8 h-8 animate-spin text-primary" />
        </div>
      }
    >
      <AdminNirspecRatePageInner />
    </Suspense>
  );
}
