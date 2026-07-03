'use client';

import React, { Suspense, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { Card } from '@/components/ui/Card';
import { Loader2, Database, HardDrive, Download } from 'lucide-react';
import { AdminTable } from '@/components/admin/AdminTable';
import { AdminFilterBar, type FacetOption } from '@/components/admin/AdminFilterBar';
import { StorageObjectDrawer } from '@/components/admin/StorageObjectDrawer';
import { flatFilterCodec, useTableUrlState, type SortState } from '@/lib/hooks/useTableUrlState';
import { useAdminTableQuery } from '@/lib/hooks/useAdminTableQuery';
import {
  getStorageObjects, getStorageBudget, getStorageFacets,
  presignStorageObjectDownload,
  type StorageObjectRow, type StorageBudget,
} from '@/lib/actions/storage-registry';
import { STORAGE_OBJECT_SORT_KEYS } from '@/lib/admin/sort-keys';

async function downloadObject(id: number) {
  const res = await presignStorageObjectDownload(id);
  const dl = res[id];
  if (dl?.url) window.location.assign(dl.url);
  else alert(dl?.error ?? 'Failed to presign download');
}

function fmtBytes(n: number): string {
  let v = Number(n);
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB', 'PB']) {
    if (Math.abs(v) < 1024 || u === 'PB') return u === 'B' ? `${v} B` : `${v.toFixed(1)} ${u}`;
    v /= 1024;
  }
  return `${v} B`;
}

function fmtDate(ts: string): string {
  let iso = ts;
  if (iso.includes(' ') && !iso.includes('T')) iso = iso.replace(' ', 'T');
  if (iso.endsWith('+00')) iso = iso + ':00';
  else if (!iso.endsWith('Z') && !iso.includes('+')) iso = iso + 'Z';
  const d = new Date(iso);
  return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function statusBadge(status: string) {
  const map: Record<string, string> = {
    active: 'bg-green-100 text-green-800 dark:bg-green-950 dark:text-green-300',
    superseded: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
    revoked: 'bg-red-100 text-red-800 dark:bg-red-950 dark:text-red-300',
  };
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${map[status] ?? ''}`}>
      {status}
    </span>
  );
}

// URL-state config (module-level: codec/whitelist must be stable references).
// status defaults to 'active' so the initial view hides superseded/revoked
// tombstones, matching the page's pre-framework behavior.
const FILTER_KEYS = ['product', 'status', 'backend', 'field', 'obs', 'key'] as const;
const codec = flatFilterCodec(FILTER_KEYS, { status: 'active' });
const DEFAULT_SORT: SortState = { column: 'created_at', direction: 'desc' };

const STATUS_OPTIONS = [
  { value: 'active', label: 'Active' },
  { value: 'superseded', label: 'Superseded' },
  { value: 'revoked', label: 'Revoked' },
];

const toOptions = (values: string[]): FacetOption[] =>
  values.map((v) => ({ value: v, label: v }));

const columns: ColumnDef<StorageObjectRow, unknown>[] = [
  {
    id: 'storage_key',
    header: 'Key',
    cell: ({ row }) => (
      <span
        className="font-mono text-xs text-text-primary block truncate max-w-[32rem]"
        title={row.original.storage_key}
      >
        {row.original.storage_key}
      </span>
    ),
    meta: { sortKey: 'storage_key' },
  },
  {
    id: 'product_type',
    header: 'Product',
    cell: ({ row }) => <span className="text-text-secondary">{row.original.product_type}</span>,
    meta: { sortKey: 'product_type' },
  },
  {
    id: 'scope',
    header: 'Scope',
    cell: ({ row }) => (
      <span className="font-mono text-xs">
        {row.original.observation ?? row.original.field ?? '—'}
      </span>
    ),
  },
  {
    id: 'size_bytes',
    header: 'Size',
    cell: ({ row }) => <span className="tabular-nums">{fmtBytes(row.original.size_bytes)}</span>,
    meta: { sortKey: 'size_bytes', align: 'right' },
  },
  {
    id: 'backend',
    header: 'Backend',
    cell: ({ row }) => (
      <span className="uppercase text-xs text-text-secondary">{row.original.backend}</span>
    ),
  },
  {
    id: 'status',
    header: 'Status',
    cell: ({ row }) => statusBadge(row.original.status),
    meta: { sortKey: 'status' },
  },
  {
    id: 'created_at',
    header: 'Created',
    cell: ({ row }) => (
      <span className="text-text-secondary whitespace-nowrap">{fmtDate(row.original.created_at)}</span>
    ),
    meta: { sortKey: 'created_at' },
  },
  {
    id: 'download',
    header: '',
    cell: ({ row }) => (
      <button
        onClick={(e) => { e.stopPropagation(); void downloadObject(row.original.id); }}
        className="text-text-secondary hover:text-primary transition-colors"
        title="Download"
      >
        <Download className="w-4 h-4" />
      </button>
    ),
    meta: { align: 'right' },
  },
];

function StoragePageInner() {
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const state = useTableUrlState({
    codec,
    sortWhitelist: STORAGE_OBJECT_SORT_KEYS,
    defaultSort: DEFAULT_SORT,
  });

  const objects = useAdminTableQuery<StorageObjectRow>({
    scope: 'admin-storage-objects',
    filters: state.debouncedFilters,
    sort: state.sort,
    page: state.page,
    pageSize: state.pageSize,
    fetchPage: async (page) => {
      const f = state.debouncedFilters;
      const res = await getStorageObjects({
        productType: f.product || undefined,
        status: f.status || undefined,
        backend: f.backend || undefined,
        field: f.field || undefined,
        observation: f.obs || undefined,
        search: f.key || undefined,
        sortColumn: state.sort.column,
        sortDirection: state.sort.direction,
        page,
        pageSize: state.pageSize,
      });
      return { rows: res.objects, total: res.total, error: res.error };
    },
  });

  const { data: budget } = useQuery({
    queryKey: ['admin-storage-budget'],
    queryFn: getStorageBudget,
    staleTime: 60_000,
  });

  const { data: facets } = useQuery({
    queryKey: ['admin-storage-facets'],
    queryFn: getStorageFacets,
    staleTime: 5 * 60_000,
  });

  const facetDescriptors = useMemo(() => [
    { kind: 'search' as const, key: 'key', placeholder: 'Search key…' },
    { kind: 'pills' as const, key: 'status', options: STATUS_OPTIONS },
    {
      kind: 'select' as const, key: 'product', label: 'Product',
      options: toOptions(facets?.productTypes ?? []),
    },
    {
      kind: 'select' as const, key: 'backend', label: 'Backend',
      options: toOptions(facets?.backends ?? []),
    },
    {
      kind: 'select' as const, key: 'field', label: 'Field',
      options: toOptions(facets?.fields ?? []),
    },
    {
      kind: 'select' as const, key: 'obs', label: 'Observation',
      options: toOptions(facets?.observations ?? []),
    },
  ], [facets]);

  const budgetOk = budget && !('error' in budget && budget.error) && 'total_bytes' in budget;

  return (
    <div>
      <div className="flex items-center gap-2 mb-2">
        <Database className="w-6 h-6 text-primary" />
        <h1 className="text-2xl font-semibold text-text-primary">Storage</h1>
      </div>
      <p className="text-text-secondary text-sm mb-6">
        Every registered object in cloud storage — canonical intermediates and final products
        each deploy uploads. This is the registry (&ldquo;what&rsquo;s in the bucket&rdquo;) view; the
        draft → published lifecycle lives on the <span className="font-medium">Deployments</span> page.
      </p>

      {budgetOk && (
        <Card className="mb-6 p-4">
          <div className="flex items-center gap-3 text-sm">
            <HardDrive className="w-5 h-5 text-text-secondary" />
            <span className="text-text-primary font-medium">
              {fmtBytes((budget as StorageBudget).total_bytes)}
            </span>
            <span className="text-text-secondary">
              of {fmtBytes((budget as StorageBudget).cap_bytes)} ({(budget as StorageBudget).pct_used}%)
            </span>
          </div>
        </Card>
      )}

      <AdminFilterBar
        facets={facetDescriptors}
        values={state.filters}
        onChange={(key, value) => state.setFilters({ ...state.filters, [key]: value })}
        onReset={state.resetFilters}
      />

      <AdminTable
        columns={columns}
        data={objects.rows}
        total={objects.total}
        page={state.page}
        pageSize={state.pageSize}
        sort={state.sort}
        loading={objects.isInitialLoading}
        fetching={objects.isFetching || state.isDebouncing}
        error={objects.error}
        emptyTitle="No storage objects match this filter."
        onSortChange={state.setSort}
        onPageChange={state.setPage}
        onPageSizeChange={state.setPageSize}
        getRowKey={(o) => o.id}
        onRowClick={(o) => setSelectedId(o.id)}
      />

      {selectedId != null && (
        <StorageObjectDrawer id={selectedId} onClose={() => setSelectedId(null)} />
      )}
    </div>
  );
}

export default function StoragePage() {
  return (
    <Suspense
      fallback={
        <div className="p-8 flex items-center justify-center text-text-secondary">
          <Loader2 className="w-5 h-5 animate-spin mr-2" /> Loading…
        </div>
      }
    >
      <StoragePageInner />
    </Suspense>
  );
}
