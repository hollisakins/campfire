'use client';

import React from 'react';
import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
} from '@tanstack/react-table';
import { Card } from '@/components/ui/Card';
import { EmptyState } from '@/components/ui/EmptyState';
import { ErrorState } from '@/components/ui/ErrorState';
import { TablePagination } from '@/components/ui/TablePagination';
import { SortableHeader } from './SortableHeader';
import type { SortDirection, SortState } from '@/lib/hooks/useTableUrlState';

// ---------------------------------------------------------------------------
// The shared admin list shell: a server-driven TanStack table (manual sorting
// + pagination, always) with sortable headers, skeleton loading rows,
// empty/error states, and the standard TablePagination footer. Column defs,
// filter bars, and row actions stay per-page; sorting is wired through
// column.meta.sortKey → onSortChange, using the same sort-key whitelist the
// page passes to useTableUrlState and the backing RPC validates.
// ---------------------------------------------------------------------------

export interface AdminColumnMeta {
  /** Server sort key for this column (must be in the page's sort whitelist). */
  sortKey?: string;
  /** Right-align the column (counts, sizes). */
  align?: 'right';
  headerClassName?: string;
  cellClassName?: string;
}

export interface AdminTableProps<TRow> {
  columns: ColumnDef<TRow, unknown>[];
  data: TRow[];
  total: number;
  /** 1-based */
  page: number;
  pageSize: number;
  sort: SortState;
  /** Initial load — renders skeleton rows. */
  loading: boolean;
  /** Any in-flight fetch — spinner in the pagination footer. */
  fetching?: boolean;
  error?: string | null;
  emptyTitle?: string;
  emptyDescription?: string;
  onSortChange: (column: string, direction: SortDirection) => void;
  onPageChange: (page: number) => void;
  onPageSizeChange: (n: number) => void;
  getRowKey: (row: TRow) => string | number;
  onRowClick?: (row: TRow) => void;
  pageSizeOptions?: number[];
}

function metaOf<TRow>(col: ColumnDef<TRow, unknown>): AdminColumnMeta {
  return (col.meta as AdminColumnMeta | undefined) ?? {};
}

export function AdminTable<TRow>({
  columns,
  data,
  total,
  page,
  pageSize,
  sort,
  loading,
  fetching = false,
  error = null,
  emptyTitle = 'No results',
  emptyDescription,
  onSortChange,
  onPageChange,
  onPageSizeChange,
  getRowKey,
  onRowClick,
  pageSizeOptions,
}: AdminTableProps<TRow>) {
  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
    manualSorting: true,
    manualPagination: true,
  });

  if (error) {
    return <ErrorState message={error} />;
  }

  if (!loading && total === 0) {
    return <EmptyState title={emptyTitle} description={emptyDescription} />;
  }

  return (
    <Card className="overflow-hidden p-0">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-card-hover text-text-secondary text-left">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((header) => {
                  const meta = metaOf(header.column.columnDef as ColumnDef<TRow, unknown>);
                  const align = meta.align === 'right' ? 'text-right' : '';
                  const content = flexRender(
                    header.column.columnDef.header,
                    header.getContext(),
                  );
                  return (
                    <th
                      key={header.id}
                      className={`px-4 py-2 font-medium whitespace-nowrap ${align} ${meta.headerClassName ?? ''}`}
                    >
                      {meta.sortKey ? (
                        <SortableHeader
                          sorted={sort.column === meta.sortKey ? sort.direction : false}
                          onToggle={() =>
                            onSortChange(
                              meta.sortKey!,
                              sort.column === meta.sortKey && sort.direction === 'asc'
                                ? 'desc'
                                : sort.column === meta.sortKey
                                  ? 'asc'
                                  : 'asc',
                            )
                          }
                          className={meta.align === 'right' ? 'ml-auto' : ''}
                        >
                          {content}
                        </SortableHeader>
                      ) : (
                        content
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: Math.min(pageSize, 10) }).map((_, i) => (
                  <tr key={`skeleton-${i}`} className="animate-pulse border-t border-border">
                    {columns.map((_col, j) => (
                      <td key={j} className="px-4 py-3">
                        <div className="h-4 bg-card-hover rounded w-full" />
                      </td>
                    ))}
                  </tr>
                ))
              : table.getRowModel().rows.map((row) => (
                  <tr
                    key={getRowKey(row.original)}
                    className={`border-t border-border hover:bg-card-hover/50 ${
                      onRowClick ? 'cursor-pointer' : ''
                    }`}
                    onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  >
                    {row.getVisibleCells().map((cell) => {
                      const meta = metaOf(cell.column.columnDef as ColumnDef<TRow, unknown>);
                      const align = meta.align === 'right' ? 'text-right' : '';
                      return (
                        <td key={cell.id} className={`px-4 py-2 ${align} ${meta.cellClassName ?? ''}`}>
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      );
                    })}
                  </tr>
                ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-border">
        <TablePagination
          pageIndex={page - 1}
          pageSize={pageSize}
          totalRows={total}
          onPageChange={(idx) => onPageChange(idx + 1)}
          onPageSizeChange={onPageSizeChange}
          pageSizeOptions={pageSizeOptions}
          loading={fetching}
        />
      </div>
    </Card>
  );
}
