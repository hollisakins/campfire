'use client';

import React, { useMemo, useState } from 'react';
import {
  useReactTable,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  getFilteredRowModel,
  flexRender,
  SortingState,
  ColumnDef,
} from '@tanstack/react-table';
import { ArrowUpDown, ArrowUp, ArrowDown, Download } from 'lucide-react';
import type { NircamImage } from '@/lib/types';
import type { NircamFilterOptions } from './NircamFilterBar';
import { Card } from '@/components/ui/Card';
import { TablePagination } from '@/components/ui/TablePagination';

// CANDIDE server base URL for NIRCam data
const CDN_BASE_URL = 'https://exchg.calet.org/hakins/data/data/nircam';

interface NircamTableProps {
  images: NircamImage[];
  filters: NircamFilterOptions;
  onSelectionChange?: (selectedImages: NircamImage[]) => void;
}

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
        <ArrowUpDown className="w-3.5 h-3.5 text-text-secondary dark:text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity" />
      )}
    </button>
  );
};

// Helper to format file size
const formatFileSize = (bytes: number | undefined): string => {
  if (!bytes) return '-';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
};

// Helper to construct download URL
const getDownloadUrl = (image: NircamImage): string => {
  return `${CDN_BASE_URL}/${image.file_path}`;
};

export const NircamTable: React.FC<NircamTableProps> = ({
  images,
  filters,
  onSelectionChange,
}) => {
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'field', desc: false },
  ]);
  const [pagination, setPagination] = useState({
    pageIndex: 0,
    pageSize: 25,
  });

  // Filter images based on filter state
  const filteredImages = useMemo(() => {
    return images.filter((image) => {
      if (filters.fields.length > 0 && !filters.fields.includes(image.field)) {
        return false;
      }
      if (filters.tiles.length > 0 && !filters.tiles.includes(image.tile)) {
        return false;
      }
      if (filters.filters.length > 0 && !filters.filters.includes(image.filter)) {
        return false;
      }
      if (filters.pixel_scales.length > 0 && !filters.pixel_scales.includes(image.pixel_scale)) {
        return false;
      }
      if (filters.versions.length > 0 && !filters.versions.includes(image.version)) {
        return false;
      }
      if (filters.extensions.length > 0 && !filters.extensions.includes(image.extension)) {
        return false;
      }
      return true;
    });
  }, [images, filters]);

  // Notify parent of selection changes
  React.useEffect(() => {
    if (onSelectionChange) {
      onSelectionChange(filteredImages);
    }
  }, [filteredImages, onSelectionChange]);

  // Define columns
  const columns = useMemo<ColumnDef<NircamImage>[]>(
    () => [
      {
        accessorKey: 'field',
        header: ({ column }) => (
          <SortableHeader column={column}>Field</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-medium text-text-primary dark:text-slate-100 uppercase">
            {row.original.field}
          </span>
        ),
        sortingFn: 'alphanumeric',
      },
      {
        accessorKey: 'filter',
        header: ({ column }) => (
          <SortableHeader column={column}>Filter</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100 uppercase">
            {row.original.filter}
          </span>
        ),
        sortingFn: 'alphanumeric',
      },
      {
        accessorKey: 'tile',
        header: ({ column }) => (
          <SortableHeader column={column}>Tile</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.tile}
          </span>
        ),
        sortingFn: (rowA, rowB) => {
          // Sort tiles alphanumerically (A1, A2, A10, B1, etc.)
          const a = rowA.original.tile;
          const b = rowB.original.tile;
          const aMatch = a.match(/^([A-Z]+)(\d+)$/);
          const bMatch = b.match(/^([A-Z]+)(\d+)$/);

          if (aMatch && bMatch) {
            const [, aLetter, aNumber] = aMatch;
            const [, bLetter, bNumber] = bMatch;

            if (aLetter !== bLetter) {
              return aLetter.localeCompare(bLetter);
            }
            return parseInt(aNumber, 10) - parseInt(bNumber, 10);
          }
          return a.localeCompare(b);
        },
      },
      {
        accessorKey: 'pixel_scale',
        header: ({ column }) => (
          <SortableHeader column={column}>Pixel Scale</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.pixel_scale}
          </span>
        ),
        sortingFn: 'alphanumeric',
      },
      {
        accessorKey: 'version',
        header: ({ column }) => (
          <SortableHeader column={column}>Version</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.version}
          </span>
        ),
        sortingFn: 'alphanumeric',
      },
      {
        accessorKey: 'extension',
        header: ({ column }) => (
          <SortableHeader column={column}>Extension</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100 uppercase">
            {row.original.extension}
          </span>
        ),
        sortingFn: (rowA, rowB) => {
          // Sort extensions by priority: sci > err > rms > srcmask
          const order = ['sci', 'err', 'rms', 'srcmask'];
          const aIdx = order.indexOf(rowA.original.extension.toLowerCase());
          const bIdx = order.indexOf(rowB.original.extension.toLowerCase());
          if (aIdx === -1 && bIdx === -1) {
            return rowA.original.extension.localeCompare(rowB.original.extension);
          }
          if (aIdx === -1) return 1;
          if (bIdx === -1) return -1;
          return aIdx - bIdx;
        },
      },
      {
        accessorKey: 'file_size',
        header: ({ column }) => (
          <SortableHeader column={column}>Size</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm text-text-secondary dark:text-slate-400">
            {formatFileSize(row.original.file_size)}
          </span>
        ),
        sortingFn: 'basic',
      },
      {
        id: 'download',
        header: () => <span>Download</span>,
        cell: ({ row }) => (
          <a
            href={getDownloadUrl(row.original)}
            className="inline-flex items-center gap-1.5 text-sm text-primary hover:text-primary-hover hover:underline"
            download
          >
            <Download className="w-4 h-4" />
            <span>Download</span>
          </a>
        ),
      },
    ],
    []
  );

  const table = useReactTable({
    data: filteredImages,
    columns,
    state: {
      sorting,
      pagination,
    },
    onSortingChange: setSorting,
    onPaginationChange: setPagination,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  return (
    <Card className="overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-card dark:bg-slate-800 border-b border-border dark:border-slate-700">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider"
                  >
                    {header.isPlaceholder
                      ? null
                      : flexRender(header.column.columnDef.header, header.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody className="bg-white dark:bg-slate-800 divide-y divide-border dark:divide-slate-700">
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className="hover:bg-card-hover dark:hover:bg-slate-700 transition-colors"
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

      {filteredImages.length === 0 ? (
        <div className="text-center py-12 text-text-secondary dark:text-slate-400">
          No images found matching the current filters.
        </div>
      ) : (
        <div className="border-t border-border dark:border-slate-700">
          <TablePagination
            pageIndex={table.getState().pagination.pageIndex}
            pageSize={table.getState().pagination.pageSize}
            totalRows={filteredImages.length}
            onPageChange={(pageIndex) => {
              setPagination((prev) => ({ ...prev, pageIndex }));
            }}
            onPageSizeChange={(pageSize) => {
              setPagination({ pageIndex: 0, pageSize });
            }}
          />
        </div>
      )}
    </Card>
  );
};
