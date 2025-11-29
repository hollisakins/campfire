'use client';

import React, { useMemo, useState, useEffect, useRef } from 'react';
import Link from 'next/link';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  flexRender,
  SortingState,
  ColumnDef,
} from '@tanstack/react-table';
import { ArrowUpDown, ArrowUp, ArrowDown, Eye, EyeOff, Server } from 'lucide-react';
import { SpectrumObject, QUALITY_LABELS } from '@/lib/types';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import { Card } from '@/components/ui/Card';
import { TablePagination } from '@/components/ui/TablePagination';
import { formatDistance } from '@/lib/utils/coordinate-parser';

// Map TanStack Table column IDs to server column names
const COLUMN_TO_SERVER_NAME: Record<string, SortColumn> = {
  'object_id': 'object_id',
  'field': 'field',
  'ra': 'ra',
  'dec': 'dec',
  'redshift': 'redshift',
  'redshift_quality': 'redshift_quality',
};

interface SpectraTableProps {
  spectra: SpectrumObject[];
  // Pagination props
  total: number;
  page: number;
  pageSize: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  // Adaptive sorting props
  isFullDataset: boolean; // true = client-side sort, false = server-side sort
  sortColumn: SortColumn;
  sortDirection: SortDirection;
  onSortChange: (column: SortColumn, direction: SortDirection) => void;
  // Coordinate search (to show distance column)
  hasCoordinateSearch?: boolean;
  // Current filter params to preserve in detail page links
  currentFilterParams?: URLSearchParams;
}

// Helper to get quality label and color
const getQualityInfo = (quality: number) => {
  const def = QUALITY_LABELS.find((q) => q.value === quality);
  return {
    label: def?.label || 'Unknown',
    short: def?.short_label || '?',
    icon: def?.icon || '',
    color: def?.color || '#e0e0e0',
  };
};

// Column header component with sort indicator
const SortableHeader: React.FC<{
  column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void };
  children: React.ReactNode;
  className?: string;
}> = ({ column, children, className = '' }) => {
  const sorted = column.getIsSorted();

  return (
    <button
      onClick={() => column.toggleSorting(sorted === 'asc')}
      className={`flex items-center gap-1 group ${className}`}
    >
      <span>{children}</span>
      {sorted === 'asc' ? (
        <ArrowUp className="w-3.5 h-3.5 text-primary" />
      ) : sorted === 'desc' ? (
        <ArrowDown className="w-3.5 h-3.5 text-primary" />
      ) : (
        <ArrowUpDown className="w-3.5 h-3.5 text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity" />
      )}
    </button>
  );
};

export const SpectraTable: React.FC<SpectraTableProps> = ({
  spectra,
  total,
  page,
  pageSize,
  totalPages,
  onPageChange,
  onPageSizeChange,
  isFullDataset,
  sortColumn,
  sortDirection,
  onSortChange,
  hasCoordinateSearch = false,
  currentFilterParams,
}) => {
  // Internal sorting state for client-side mode
  // Initialize from props, then manage independently
  // When coordinate search is active, default to sorting by distance
  const [internalSorting, setInternalSorting] = useState<SortingState>(() => {
    if (hasCoordinateSearch) {
      return [{ id: 'distance', desc: false }]; // Sort by distance ascending (nearest first)
    }
    return sortColumn ? [{ id: sortColumn, desc: sortDirection === 'desc' }] : [];
  });

  // Track previous hasCoordinateSearch to detect when it becomes active
  const prevHasCoordinateSearch = useRef(hasCoordinateSearch);

  // Sync internal state when switching from server-side to client-side mode
  // or when props change in server-side mode
  // Also reset to distance sorting when coordinate search becomes active
  useEffect(() => {
    // Reset to distance sort when coordinate search becomes active
    if (hasCoordinateSearch && !prevHasCoordinateSearch.current) {
      setInternalSorting([{ id: 'distance', desc: false }]);
      prevHasCoordinateSearch.current = hasCoordinateSearch;
      return;
    }

    prevHasCoordinateSearch.current = hasCoordinateSearch;

    // In server-side mode, sync from props
    if (!isFullDataset) {
      setInternalSorting(sortColumn ? [{ id: sortColumn, desc: sortDirection === 'desc' }] : []);
    }
  }, [isFullDataset, sortColumn, sortDirection, hasCoordinateSearch]);

  // Use internal state for client-side, props for server-side
  const effectiveSorting = isFullDataset ? internalSorting :
    (sortColumn ? [{ id: sortColumn, desc: sortDirection === 'desc' }] : []);

  // Handle sorting change
  const handleSortingChange = (updater: SortingState | ((old: SortingState) => SortingState)) => {
    const newSorting = typeof updater === 'function' ? updater(effectiveSorting) : updater;

    if (isFullDataset) {
      // Client-side mode: update internal state only
      setInternalSorting(newSorting);
      // Still notify parent for URL sync (but parent will skip refetch)
      if (newSorting.length > 0) {
        const columnId = newSorting[0].id;
        const serverColumn = COLUMN_TO_SERVER_NAME[columnId];
        if (serverColumn) {
          onSortChange(serverColumn, newSorting[0].desc ? 'desc' : 'asc');
        }
      }
    } else {
      // Server-side mode: notify parent to trigger refetch
      if (newSorting.length > 0) {
        const columnId = newSorting[0].id;
        const serverColumn = COLUMN_TO_SERVER_NAME[columnId];
        if (serverColumn) {
          onSortChange(serverColumn, newSorting[0].desc ? 'desc' : 'asc');
        }
      } else {
        onSortChange('object_id', 'asc');
      }
    }
  };

  // Distance column definition (conditional)
  const distanceColumn: ColumnDef<SpectrumObject> = {
    accessorKey: 'distance',
    header: ({ column }) => (
      <SortableHeader column={column}>Distance</SortableHeader>
    ),
    cell: ({ row }) => (
      <span className="text-sm font-mono text-text-primary">
        {row.original.distance != null ? formatDistance(row.original.distance) : 'N/A'}
      </span>
    ),
    sortingFn: (rowA, rowB) => {
      const a = rowA.original.distance ?? Infinity;
      const b = rowB.original.distance ?? Infinity;
      return a - b;
    },
  };

  // Define columns
  const columns = useMemo<ColumnDef<SpectrumObject>[]>(
    () => [
      {
        accessorKey: 'object_id',
        header: ({ column }) => (
          <SortableHeader column={column}>Object ID</SortableHeader>
        ),
        cell: ({ row }) => (
          <Link
            href={`/spectra/${encodeURIComponent(row.original.object_id)}${currentFilterParams ? `?${currentFilterParams.toString()}` : ''}`}
            className="text-sm font-mono text-primary hover:underline"
          >
            {row.original.object_id}
          </Link>
        ),
        sortingFn: 'alphanumeric',
      },
      {
        accessorKey: 'field',
        header: ({ column }) => (
          <SortableHeader column={column}>Field</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm text-text-primary uppercase">{row.original.field}</span>
        ),
        sortingFn: 'alphanumeric',
      },
      {
        accessorKey: 'ra',
        header: ({ column }) => (
          <SortableHeader column={column}>RA</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary">
            {row.original.ra.toFixed(6)}
          </span>
        ),
        sortingFn: 'basic',
      },
      {
        accessorKey: 'dec',
        header: ({ column }) => (
          <SortableHeader column={column}>Dec</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary">
            {row.original.dec.toFixed(6)}
          </span>
        ),
        sortingFn: 'basic',
      },
      // Distance column (only shown when coordinate search is active)
      ...(hasCoordinateSearch ? [distanceColumn] : []),
      {
        accessorKey: 'redshift',
        header: ({ column }) => (
          <SortableHeader column={column}>Redshift</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary">
            {row.original.redshift !== null ? row.original.redshift.toFixed(4) : 'N/A'}
          </span>
        ),
        sortingFn: (rowA, rowB) => {
          const a = rowA.original.redshift ?? -1;
          const b = rowB.original.redshift ?? -1;
          return a - b;
        },
      },
      {
        accessorKey: 'redshift_quality',
        header: ({ column }) => (
          <SortableHeader column={column}>Quality</SortableHeader>
        ),
        cell: ({ row }) => {
          const quality = getQualityInfo(row.original.redshift_quality);
          return (
            <div className="flex items-center gap-1.5">
              <span>{quality.icon}</span>
              <span className="text-sm text-text-primary">{quality.short}</span>
            </div>
          );
        },
        sortingFn: 'basic',
      },
      {
        id: 'num_gratings',
        accessorFn: (row) => row.num_gratings || row.spectra.length,
        header: ({ column }) => (
          <SortableHeader column={column} className="justify-center">
            Gratings
          </SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm text-text-primary text-center block">
            {row.original.num_gratings || row.original.spectra.length}
          </span>
        ),
        sortingFn: 'basic',
      },
      {
        id: 'max_snr',
        accessorFn: (row) => row.max_snr ?? 0,
        header: ({ column }) => (
          <SortableHeader column={column}>Max S/N</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary">
            {row.original.max_snr ? row.original.max_snr.toFixed(1) : 'N/A'}
          </span>
        ),
        sortingFn: 'basic',
      },
      {
        id: 'inspected',
        accessorFn: (row) => row.redshift_quality > 0,
        header: ({ column }) => (
          <SortableHeader column={column} className="justify-center">
            Inspected
          </SortableHeader>
        ),
        cell: ({ row }) => {
          const isInspected = row.original.redshift_quality > 0;
          return (
            <div className="flex justify-center">
              {isInspected ? (
                <Eye className="w-4 h-4 text-green-600" />
              ) : (
                <EyeOff className="w-4 h-4 text-text-secondary opacity-50" />
              )}
            </div>
          );
        },
        sortingFn: 'basic',
      },
    ],
    [hasCoordinateSearch, currentFilterParams]
  );

  // Internal pagination state for client-side mode
  const [internalPagination, setInternalPagination] = useState({
    pageIndex: 0,
    pageSize: 25,
  });

  const table = useReactTable({
    data: spectra,
    columns,
    state: {
      sorting: effectiveSorting,
      pagination: isFullDataset ? internalPagination : {
        pageIndex: page - 1, // TanStack uses 0-based index
        pageSize,
      },
    },
    // Adaptive pagination: client-side for full dataset, server-side otherwise
    manualPagination: !isFullDataset,
    // Adaptive sorting: client-side for full dataset, server-side otherwise
    manualSorting: !isFullDataset,
    pageCount: isFullDataset ? undefined : totalPages,
    onSortingChange: handleSortingChange,
    onPaginationChange: isFullDataset ? setInternalPagination : undefined,
    getCoreRowModel: getCoreRowModel(),
    // Only use client-side sorting/pagination when we have the full dataset
    getSortedRowModel: isFullDataset ? getSortedRowModel() : undefined,
    getPaginationRowModel: isFullDataset ? getPaginationRowModel() : undefined,
  });

  return (
    <Card className="overflow-hidden">
      {/* Server-side mode indicator */}
      {!isFullDataset && (
        <div className="flex items-center gap-2 text-xs text-text-secondary px-4 py-2 bg-amber-50 border-b border-amber-200">
          <Server className="w-3.5 h-3.5" />
          <span>
            Large dataset ({total.toLocaleString()} results) — sorting and pagination are server-side
          </span>
        </div>
      )}
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-card border-b border-border">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    className="px-4 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider"
                  >
                    {header.isPlaceholder
                      ? null
                      : flexRender(header.column.columnDef.header, header.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody className="bg-white divide-y divide-border">
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className="hover:bg-card-hover transition-colors"
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-3 whitespace-nowrap">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {spectra.length === 0 ? (
        <div className="text-center py-12 text-text-secondary">
          No spectra found matching the current filters.
        </div>
      ) : (
        <div className="border-t border-border">
          <TablePagination
            pageIndex={table.getState().pagination.pageIndex}
            pageSize={table.getState().pagination.pageSize}
            totalRows={total}
            onPageChange={(pageIndex) => {
              if (isFullDataset) {
                // Client-side pagination - update internal state
                setInternalPagination(prev => ({ ...prev, pageIndex }));
              } else {
                // Server-side pagination - notify parent
                onPageChange(pageIndex + 1);
              }
            }}
            onPageSizeChange={(newPageSize) => {
              if (isFullDataset) {
                // Client-side pagination - update internal state
                setInternalPagination({ pageIndex: 0, pageSize: newPageSize });
              } else {
                // Server-side pagination - notify parent
                onPageSizeChange(newPageSize);
              }
            }}
          />
        </div>
      )}
    </Card>
  );
};
