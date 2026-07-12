'use client';

import React, { useMemo, useState } from 'react';
import Link from 'next/link';
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
import { ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react';
import type { NircamProductRow } from '@/lib/types';
import type { NircamFilterOptions } from './NircamFilterBar';
import { Card } from '@/components/ui/Card';
import { TablePagination } from '@/components/ui/TablePagination';
import { ThumbnailPopup } from './ThumbnailPopup';
import { mosaicBase } from '@/lib/nircam-product-keys';
import {
  generateNircamMosaicDownloadUrls,
  generateNircamExpmapDownloadUrls,
} from '@/lib/actions/download';

interface NircamTableProps {
  /** Field-scoped product rows: mosaics + expmaps (extension 'exp'). */
  products: NircamProductRow[];
  filters: NircamFilterOptions;
  /** mosaic base key -> presigned thumbnail URL (sci rows). */
  thumbnails: Record<string, string>;
  /** mosaic base key -> presigned large quick-look URL (popup). */
  quicklooks: Record<string, string>;
  /** Filters covered by a FitsGL dataset — enables the per-row map View. */
  viewableFilters: Set<string>;
  onSelectionChange?: (selected: NircamProductRow[]) => void;
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
        <ArrowUpDown className="w-3.5 h-3.5 text-text-secondary opacity-0 group-hover:opacity-100 transition-opacity" />
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

// Extension sort priority (exp = the folded-in exposure maps, sorted last).
const EXT_ORDER = ['sci', 'err', 'wht', 'rms', 'srcmask', 'exp'];
const extRank = (ext: string): number => {
  const i = EXT_ORDER.indexOf(ext.toLowerCase());
  return i === -1 ? EXT_ORDER.length - 1 : i; // unknowns before 'exp'
};

// Alphanumeric tile sort (A1, A2, A10, B1 …); null (exp rows) sorts last.
const compareTiles = (a: string | null, b: string | null): number => {
  if (a === b) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  const aMatch = a.match(/^([A-Z]+)(\d+)$/);
  const bMatch = b.match(/^([A-Z]+)(\d+)$/);
  if (aMatch && bMatch) {
    const [, aLetter, aNumber] = aMatch;
    const [, bLetter, bNumber] = bMatch;
    if (aLetter !== bLetter) return aLetter.localeCompare(bLetter);
    return parseInt(aNumber, 10) - parseInt(bNumber, 10);
  }
  return a.localeCompare(b);
};


// Per-row FITS download: authorizes + presigns the key server-side (routed by
// product kind), then navigates the browser to the credential-free proxy URL.
// Text-glyph action per the design (↓ FITS), not an icon button.
const DownloadCell: React.FC<{ row: NircamProductRow }> = ({ row }) => {
  const [busy, setBusy] = useState(false);

  const handleDownload = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const { urls } =
        row.kind === 'expmap'
          ? await generateNircamExpmapDownloadUrls([row.file_path])
          : await generateNircamMosaicDownloadUrls([row.file_path]);
      const proxyUrl = urls[row.file_path];
      if (proxyUrl) {
        const link = document.createElement('a');
        link.href = proxyUrl;
        link.download = row.file_path.split('/').pop() || row.file_path;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
      }
    } catch (err) {
      console.error('Failed to start NIRCam FITS download:', err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={handleDownload}
      disabled={busy}
      title="Download FITS"
      className="group/act inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium text-text-secondary hover:text-text-primary hover:bg-surface-2 disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
    >
      <span className="font-mono text-text-tertiary group-hover/act:text-primary">↓</span>
      <span>{busy ? 'Preparing…' : 'FITS'}</span>
    </button>
  );
};

export const NircamTable: React.FC<NircamTableProps> = ({
  products,
  filters,
  thumbnails,
  quicklooks,
  viewableFilters,
  onSelectionChange,
}) => {
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'filter', desc: false },
  ]);
  const [pagination, setPagination] = useState({
    pageIndex: 0,
    pageSize: 25,
  });
  const [popup, setPopup] = useState<{ url: string; title: string } | null>(null);

  // Apply the facet selections. Facets are strict: an exp row (no tile/scale/
  // epoch axis) drops out when one of those facets is active.
  const filteredProducts = useMemo(() => {
    return products.filter((p) => {
      if (filters.filters.length > 0 && !filters.filters.includes(p.filter)) {
        return false;
      }
      if (filters.tiles.length > 0 && (p.tile === null || !filters.tiles.includes(p.tile))) {
        return false;
      }
      if (
        filters.pixel_scales.length > 0 &&
        (p.pixel_scale === null || !filters.pixel_scales.includes(p.pixel_scale))
      ) {
        return false;
      }
      if (filters.extensions.length > 0 && !filters.extensions.includes(p.extension)) {
        return false;
      }
      if (filters.epochs.length > 0 && (p.epoch === undefined || !filters.epochs.includes(p.epoch))) {
        return false;
      }
      return true;
    });
  }, [products, filters]);

  // Notify parent of selection changes
  React.useEffect(() => {
    if (onSelectionChange) {
      onSelectionChange(filteredProducts);
    }
  }, [filteredProducts, onSelectionChange]);

  // Epoch is an axis only when the field actually has named epochs — mirror
  // the filter bar, which hides its Epoch facet in the same case.
  const hasNamedEpochs = useMemo(
    () => products.some((p) => (p.epoch ?? '') !== ''),
    [products],
  );

  // Define columns
  const columns = useMemo<ColumnDef<NircamProductRow>[]>(
    () => [
      {
        id: 'thumb',
        header: () => null,
        enableSorting: false,
        cell: ({ row }) => {
          const p = row.original;
          if (p.extension !== 'sci') return null;
          const base = mosaicBase(p);
          const url = base ? thumbnails[base] : undefined;
          if (!url) return null;
          // Popup shows the large quick-look when deployed; the thumbnail is
          // the fallback for fields reduced before the pair existed.
          const popupUrl = (base ? quicklooks[base] : undefined) ?? url;
          return (
            <button
              type="button"
              onClick={() =>
                setPopup({
                  url: popupUrl,
                  title: `${p.filter.toUpperCase()} · ${p.tile ?? ''} · ${p.extension}`,
                })
              }
              className="block w-8 h-8 rounded-md border border-border-strong overflow-hidden cursor-zoom-in bg-[#0d0b12]"
              title="Enlarge thumbnail"
            >
              {/* Presigned cross-origin PNG; plain <img> (see NircamFieldCard). */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={url}
                alt=""
                className="w-full h-full object-cover"
                loading="lazy"
                onError={(e) => {
                  (e.currentTarget.parentElement as HTMLElement).style.display = 'none';
                }}
              />
            </button>
          );
        },
      },
      {
        accessorKey: 'filter',
        header: ({ column }) => (
          <SortableHeader column={column}>Filter</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm font-mono text-text-primary uppercase">
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
        cell: ({ row }) =>
          row.original.tile === null ? (
            <span className="text-sm font-mono text-text-tertiary">—</span>
          ) : (
            <span className="text-sm font-mono text-text-primary">
              {row.original.tile}
            </span>
          ),
        sortingFn: (rowA, rowB) => compareTiles(rowA.original.tile, rowB.original.tile),
      },
      {
        accessorKey: 'pixel_scale',
        header: ({ column }) => (
          <SortableHeader column={column}>Scale</SortableHeader>
        ),
        cell: ({ row }) =>
          row.original.pixel_scale === null ? (
            <span className="text-sm font-mono text-text-tertiary">—</span>
          ) : (
            <span className="text-sm font-mono text-text-primary">
              {row.original.pixel_scale}
            </span>
          ),
        sortingFn: 'alphanumeric',
      },
      {
        accessorKey: 'extension',
        header: ({ column }) => (
          <SortableHeader column={column}>Ext</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="inline-flex items-center rounded border border-border px-1.5 py-0.5 text-[11px] font-mono uppercase bg-surface-2 text-text-secondary">
            {row.original.extension}
          </span>
        ),
        sortingFn: (rowA, rowB) =>
          extRank(rowA.original.extension) - extRank(rowB.original.extension) ||
          rowA.original.extension.localeCompare(rowB.original.extension),
      },
      ...(hasNamedEpochs
        ? [{
            accessorKey: 'epoch',
            header: ({ column }) => (
              <SortableHeader column={column}>Epoch</SortableHeader>
            ),
            cell: ({ row }) => {
              const { kind, epoch } = row.original;
              if (kind === 'expmap') {
                return <span className="text-sm text-text-tertiary">—</span>;
              }
              return (epoch ?? '') === '' ? (
                <span className="text-sm text-text-secondary">Full field</span>
              ) : (
                <span className="inline-flex items-center rounded border border-border px-1.5 py-0.5 text-xs font-mono font-medium bg-surface-2 text-text-primary">
                  {epoch}
                </span>
              );
            },
            sortingFn: (rowA, rowB) =>
              (rowA.original.epoch ?? '').localeCompare(rowB.original.epoch ?? ''),
          } satisfies ColumnDef<NircamProductRow>]
        : []),
      {
        accessorKey: 'file_size',
        header: ({ column }) => (
          <SortableHeader column={column}>Size (compressed)</SortableHeader>
        ),
        cell: ({ row }) => {
          const { file_size, file_size_stored } = row.original;
          return (
            <span
              className="text-sm text-text-secondary"
              title={
                file_size_stored != null
                  ? `${formatFileSize(file_size)} uncompressed · ${formatFileSize(file_size_stored)} stored gzipped`
                  : undefined
              }
            >
              {formatFileSize(file_size)}
              {file_size_stored != null && (
                <span className="text-text-tertiary"> ({formatFileSize(file_size_stored)})</span>
              )}
            </span>
          );
        },
        sortingFn: 'basic',
      },
      {
        id: 'actions',
        header: () => <span className="block text-right">Actions</span>,
        enableSorting: false,
        cell: ({ row }) => {
          const p = row.original;
          const canView = p.extension === 'sci' && viewableFilters.has(p.filter);
          return (
            <div className="flex items-center justify-end gap-1">
              {canView && (
                <Link
                  href={`/map?field=${encodeURIComponent(p.field)}&filter=${encodeURIComponent(p.filter)}`}
                  title={`Open ${p.filter.toUpperCase()} in the map`}
                  className="group/act inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium text-text-secondary hover:text-text-primary hover:bg-surface-2 transition-colors"
                >
                  <span className="font-mono text-text-tertiary group-hover/act:text-primary">↗</span>
                  <span>View</span>
                </Link>
              )}
              <DownloadCell row={p} />
            </div>
          );
        },
      },
    ],
    [thumbnails, quicklooks, viewableFilters, hasNamedEpochs]
  );

  const table = useReactTable({
    data: filteredProducts,
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
          <thead className="bg-table-header border-b border-border">
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
          <tbody className="bg-card divide-y divide-border">
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                className="hover:bg-card-hover transition-colors"
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id} className="px-4 py-2.5 whitespace-nowrap">
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {filteredProducts.length === 0 ? (
        <div className="text-center py-12 text-text-secondary">
          No products found matching the current filters.
        </div>
      ) : (
        <div className="border-t border-border">
          <TablePagination
            pageIndex={table.getState().pagination.pageIndex}
            pageSize={table.getState().pagination.pageSize}
            totalRows={filteredProducts.length}
            onPageChange={(pageIndex) => {
              setPagination((prev) => ({ ...prev, pageIndex }));
            }}
            onPageSizeChange={(pageSize) => {
              setPagination({ pageIndex: 0, pageSize });
            }}
          />
        </div>
      )}

      <ThumbnailPopup
        url={popup?.url ?? null}
        title={popup?.title ?? ''}
        onClose={() => setPopup(null)}
      />
    </Card>
  );
};
