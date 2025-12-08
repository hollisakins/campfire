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
import { ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react';
import { SpectrumObject, QUALITY_LABELS } from '@/lib/types';
import { RGBThumbnail } from './RGBThumbnail';
import { SpectrumThumbnail } from './SpectrumThumbnail';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import { Card } from '@/components/ui/Card';
import { TablePagination } from '@/components/ui/TablePagination';
import { formatDistance } from '@/lib/utils/coordinate-parser';

// Map TanStack Table column IDs to server column names
const COLUMN_TO_SERVER_NAME: Record<string, SortColumn> = {
  'object_id': 'object_id',
  'field': 'field',
  'observation': 'observation',
  'ra': 'ra',
  'dec': 'dec',
  'redshift': 'redshift',
  'redshift_quality': 'redshift_quality',
  'max_snr': 'max_snr',
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
  // Loading and error states
  loading?: boolean;
  error?: string | null;
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

// Skeleton row component for loading state
const TableSkeletonRow: React.FC<{ columns: ColumnDef<SpectrumObject>[] }> = ({ columns }) => (
  <tr className="animate-pulse">
    {columns.map((col, i) => (
      <td
        key={i}
        className="px-4 py-3 whitespace-nowrap"
        style={{ width: `${col.minSize || 150}px` }}
      >
        <div className="h-4 bg-gray-200 rounded w-full"></div>
      </td>
    ))}
  </tr>
);

// Skeleton component showing multiple loading rows
const TableSkeleton: React.FC<{ rows: number; columns: ColumnDef<SpectrumObject>[] }> = ({ rows, columns }) => (
  <>
    {Array.from({ length: rows }).map((_, i) => (
      <TableSkeletonRow key={i} columns={columns} />
    ))}
  </>
);

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
  loading = false,
  error = null,
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
    minSize: 100,
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
        id: 'rgb_thumbnail',
        size: 56,
        minSize: 56,
        maxSize: 56,
        header: () => <span className="sr-only">Image</span>,
        cell: ({ row }) => (
          <RGBThumbnail objectId={row.original.object_id} size={48} />
        ),
        enableSorting: false,
      },
      {
        accessorKey: 'object_id',
        minSize: 260,
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
        minSize: 80,
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
        minSize: 110,
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
        minSize: 110,
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
        minSize: 90,
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
        minSize: 90,
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
        id: 'spectrum_thumbnail',
        minSize: 130,
        header: () => <span>Thumbnail</span>,
        cell: ({ row }) => (
          <SpectrumThumbnail objectId={row.original.object_id} width={120} height={40} />
        ),
        enableSorting: false,
      },
      {
        id: 'num_gratings',
        minSize: 80,
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
        minSize: 90,
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
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-card border-b border-border">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    className="px-4 py-3 text-left text-xs font-medium text-text-secondary uppercase tracking-wider"
                    style={{ width: `${header.getSize()}px` }}
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
            {loading ? (
              // Loading state: show skeleton rows
              <TableSkeleton
                rows={isFullDataset ? Math.min(pageSize, 10) : pageSize}
                columns={columns}
              />
            ) : error ? (
              // Error state: show error message
              <tr>
                <td colSpan={hasCoordinateSearch ? 11 : 10} className="px-4 py-16 text-center">
                  <div className="bg-red-50 border border-red-200 rounded-lg p-4 inline-block">
                    <p className="text-red-800">{error}</p>
                  </div>
                </td>
              </tr>
            ) : spectra.length === 0 ? (
              // Empty state: show message
              <tr>
                <td colSpan={hasCoordinateSearch ? 11 : 10} className="px-4 py-12 text-center text-text-secondary">
                  No results found.
                  <p className="text-sm mt-2">
                    If you&apos;re looking for proprietary data, you may need to enter an access code on your profile page.
                  </p>
                </td>
              </tr>
            ) : (
              // Data rows
              table.getRowModel().rows.map((row) => (
                <tr
                  key={row.id}
                  className="hover:bg-card-hover transition-colors"
                >
                  {row.getVisibleCells().map((cell) => (
                    <td
                      key={cell.id}
                      className="px-4 py-3 whitespace-nowrap"
                      style={{ width: `${cell.column.getSize()}px` }}
                    >
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Always show pagination footer */}
      <div className="border-t border-border">
        <TablePagination
          pageIndex={table.getState().pagination.pageIndex}
          pageSize={table.getState().pagination.pageSize}
          totalRows={total}
          loading={loading}
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
    </Card>
  );
};
