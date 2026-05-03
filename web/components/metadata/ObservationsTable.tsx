'use client';

import React, { useMemo, useState } from 'react';
import Link from 'next/link';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  flexRender,
  type SortingState,
  type ColumnDef,
} from '@tanstack/react-table';
import { ArrowUpDown, ArrowUp, ArrowDown, Telescope } from 'lucide-react';
import { TablePagination } from '@/components/ui/TablePagination';
import {
  ColumnVisibilityDropdown,
  useColumnVisibility,
  type ColumnDefinition,
} from '@/components/ui/ColumnVisibilityDropdown';
import { EmptyState } from '@/components/ui/EmptyState';
import { ProvenanceCell } from './ProvenanceCell';
import type { ObservationOverview } from '@/lib/actions/programs';

const COLUMN_DEFS: ColumnDefinition[] = [
  { id: 'observation', label: 'Observation', alwaysVisible: true },
  { id: 'program', label: 'Program', defaultVisible: true },
  { id: 'field', label: 'Field', defaultVisible: true },
  { id: 'cycle', label: 'Cycle', defaultVisible: false },
  { id: 'gratings', label: 'Gratings', defaultVisible: true },
  { id: 'pointing_count', label: 'Pointings', defaultVisible: true },
  { id: 'target_count', label: 'Targets', defaultVisible: true },
  { id: 'spectrum_count', label: 'Spectra', defaultVisible: true },
  { id: 'total_size_bytes', label: 'Size', defaultVisible: true },
  { id: 'reduction', label: 'Reduction', defaultVisible: true },
];

function formatBytes(bytes: number): string {
  if (bytes === 0) return '—';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${sizes[i]}`;
}

function formatNumber(n: number): string {
  return n.toLocaleString();
}

interface ObservationsTableProps {
  observations: ObservationOverview[];
  rightToolbar?: React.ReactNode;
}

export const ObservationsTable: React.FC<ObservationsTableProps> = ({
  observations,
  rightToolbar,
}) => {
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'reduction', desc: true },
  ]);
  const [visibility, setVisibility] = useColumnVisibility(
    COLUMN_DEFS,
    'campfire-metadata-obs-columns'
  );

  const columns = useMemo<ColumnDef<ObservationOverview>[]>(
    () => [
      {
        id: 'observation',
        accessorKey: 'observation',
        header: 'Observation',
        enableSorting: true,
        cell: ({ row }) => (
          <Link
            href={`/nirspec/metadata/programs/${row.original.program_slug}#${row.original.observation}`}
            className="font-mono text-sm text-text-primary dark:text-slate-100 hover:text-primary"
          >
            {row.original.observation}
          </Link>
        ),
      },
      {
        id: 'program',
        accessorFn: (r) => r.program_name || r.program_slug,
        header: 'Program',
        enableSorting: true,
        cell: ({ row }) => (
          <Link
            href={`/nirspec/metadata/programs/${row.original.program_slug}`}
            className="text-sm text-text-secondary dark:text-slate-400 hover:text-primary"
          >
            {row.original.program_name || row.original.program_slug}
          </Link>
        ),
      },
      {
        id: 'field',
        accessorKey: 'field',
        header: 'Field',
        enableSorting: true,
        cell: ({ getValue }) => (
          <span className="text-sm text-text-secondary dark:text-slate-400">
            {String(getValue() || '—')}
          </span>
        ),
      },
      {
        id: 'cycle',
        accessorKey: 'cycle',
        header: 'Cycle',
        enableSorting: true,
        cell: ({ getValue }) => {
          const c = getValue();
          return (
            <span className="text-sm text-text-secondary dark:text-slate-400">
              {c == null ? '—' : `Cycle ${c}`}
            </span>
          );
        },
      },
      {
        id: 'gratings',
        accessorFn: (r) => r.gratings.join(','),
        header: 'Gratings',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-xs font-mono text-text-secondary dark:text-slate-400">
            {row.original.gratings.length === 0 ? '—' : row.original.gratings.join(', ')}
          </span>
        ),
      },
      {
        id: 'pointing_count',
        accessorKey: 'pointing_count',
        header: 'Pointings',
        enableSorting: true,
        cell: ({ getValue }) => (
          <span className="text-sm tabular-nums text-text-primary dark:text-slate-100">
            {formatNumber(Number(getValue()) || 0)}
          </span>
        ),
      },
      {
        id: 'target_count',
        accessorKey: 'target_count',
        header: 'Targets',
        enableSorting: true,
        cell: ({ getValue }) => (
          <span className="text-sm tabular-nums text-text-primary dark:text-slate-100">
            {formatNumber(Number(getValue()) || 0)}
          </span>
        ),
      },
      {
        id: 'spectrum_count',
        accessorKey: 'spectrum_count',
        header: 'Spectra',
        enableSorting: true,
        cell: ({ getValue }) => (
          <span className="text-sm tabular-nums text-text-primary dark:text-slate-100">
            {formatNumber(Number(getValue()) || 0)}
          </span>
        ),
      },
      {
        id: 'total_size_bytes',
        accessorKey: 'total_size_bytes',
        header: 'Size',
        enableSorting: true,
        cell: ({ getValue }) => (
          <span className="text-sm tabular-nums text-text-secondary dark:text-slate-400">
            {formatBytes(Number(getValue()) || 0)}
          </span>
        ),
      },
      {
        id: 'reduction',
        // Sort by reduced_at; null timestamps sort last
        accessorFn: (r) => (r.reduced_at ? new Date(r.reduced_at).getTime() : 0),
        header: 'Reduction',
        enableSorting: true,
        cell: ({ row }) => <ProvenanceCell provenance={row.original} />,
      },
    ],
    []
  );

  const table = useReactTable({
    data: observations,
    columns,
    state: {
      sorting,
      columnVisibility: visibility,
    },
    onSortingChange: setSorting,
    onColumnVisibilityChange: (updater) =>
      setVisibility(typeof updater === 'function' ? updater(visibility) : updater),
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: {
      pagination: { pageSize: 50 },
    },
  });

  if (observations.length === 0) {
    return (
      <EmptyState
        icon={Telescope}
        title="No observations match these filters"
        description="Try clearing some filters or broadening your search."
      />
    );
  }

  return (
    <div className="bg-card dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg">
      <div className="flex items-center justify-between px-3 py-2 border-b border-border dark:border-slate-700">
        <div className="text-sm text-text-secondary dark:text-slate-400">
          {observations.length.toLocaleString()} observation
          {observations.length === 1 ? '' : 's'}
        </div>
        <div className="flex items-center gap-2">
          {rightToolbar}
          <ColumnVisibilityDropdown
            columns={COLUMN_DEFS}
            visibility={visibility}
            onChange={setVisibility}
          />
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-card-hover dark:bg-slate-700/40 sticky top-0">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id}>
                {hg.headers.map((h) => {
                  const sortable = h.column.getCanSort();
                  const sorted = h.column.getIsSorted();
                  return (
                    <th
                      key={h.id}
                      className="px-3 py-2 text-left text-[11px] uppercase tracking-wider font-semibold text-text-secondary dark:text-slate-400 border-b border-border dark:border-slate-700 whitespace-nowrap"
                    >
                      {sortable ? (
                        <button
                          onClick={h.column.getToggleSortingHandler()}
                          className="inline-flex items-center gap-1 hover:text-text-primary dark:hover:text-slate-200"
                        >
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          {sorted === 'asc' ? (
                            <ArrowUp className="w-3 h-3" />
                          ) : sorted === 'desc' ? (
                            <ArrowDown className="w-3 h-3" />
                          ) : (
                            <ArrowUpDown className="w-3 h-3 opacity-50" />
                          )}
                        </button>
                      ) : (
                        flexRender(h.column.columnDef.header, h.getContext())
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className="border-b border-border dark:border-slate-700 last:border-b-0 hover:bg-card-hover dark:hover:bg-slate-700/40 transition-colors"
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-3 py-2 align-middle">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <TablePagination
        pageIndex={table.getState().pagination.pageIndex}
        pageSize={table.getState().pagination.pageSize}
        totalRows={observations.length}
        onPageChange={(p) => table.setPageIndex(p)}
        onPageSizeChange={(s) => table.setPageSize(s)}
      />
    </div>
  );
};
