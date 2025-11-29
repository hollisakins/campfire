'use client';

import React from 'react';
import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';

interface TablePaginationProps {
  pageIndex: number;
  pageSize: number;
  totalRows: number;
  onPageChange: (pageIndex: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  pageSizeOptions?: number[];
  className?: string;
}

export const TablePagination: React.FC<TablePaginationProps> = ({
  pageIndex,
  pageSize,
  totalRows,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [10, 25, 50, 100],
  className = '',
}) => {
  const pageCount = Math.ceil(totalRows / pageSize);
  const canPreviousPage = pageIndex > 0;
  const canNextPage = pageIndex < pageCount - 1;

  const startRow = pageIndex * pageSize + 1;
  const endRow = Math.min((pageIndex + 1) * pageSize, totalRows);

  const handlePageSizeChange = (e: React.ChangeEvent<HTMLSelectElement>) => {
    const newSize = Number(e.target.value);
    onPageSizeChange(newSize);
    // Reset to first page when changing page size
    onPageChange(0);
  };

  return (
    <div className={`flex items-center justify-between gap-4 py-3 px-4 ${className}`}>
      {/* Left side: Page size selector */}
      <div className="flex items-center gap-2 text-sm text-text-secondary">
        <span>Show</span>
        <select
          value={pageSize}
          onChange={handlePageSizeChange}
          className="px-2 py-1 border border-border rounded-md bg-background text-text-primary text-sm focus:outline-none focus:ring-2 focus:ring-primary"
        >
          {pageSizeOptions.map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </select>
        <span>per page</span>
      </div>

      {/* Center: Row count info */}
      <div className="text-sm text-text-secondary">
        {totalRows === 0 ? (
          <span>No results</span>
        ) : (
          <span>
            Showing <span className="font-medium text-text-primary">{startRow.toLocaleString()}</span>
            {' - '}
            <span className="font-medium text-text-primary">{endRow.toLocaleString()}</span>
            {' of '}
            <span className="font-medium text-text-primary">{totalRows.toLocaleString()}</span>
          </span>
        )}
      </div>

      {/* Right side: Navigation buttons */}
      <div className="flex items-center gap-1">
        {/* First page */}
        <button
          onClick={() => onPageChange(0)}
          disabled={!canPreviousPage}
          className={`
            p-1.5 rounded-md transition-colors
            ${canPreviousPage
              ? 'hover:bg-card text-text-secondary hover:text-text-primary'
              : 'text-border cursor-not-allowed'
            }
          `}
          title="First page"
        >
          <ChevronsLeft className="w-4 h-4" />
        </button>

        {/* Previous page */}
        <button
          onClick={() => onPageChange(pageIndex - 1)}
          disabled={!canPreviousPage}
          className={`
            p-1.5 rounded-md transition-colors
            ${canPreviousPage
              ? 'hover:bg-card text-text-secondary hover:text-text-primary'
              : 'text-border cursor-not-allowed'
            }
          `}
          title="Previous page"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>

        {/* Page indicator */}
        <div className="flex items-center gap-1 px-2">
          <span className="text-sm text-text-secondary">Page</span>
          <input
            type="number"
            value={pageIndex + 1}
            onChange={(e) => {
              const page = e.target.value ? Number(e.target.value) - 1 : 0;
              const clampedPage = Math.max(0, Math.min(page, pageCount - 1));
              onPageChange(clampedPage);
            }}
            min={1}
            max={pageCount || 1}
            className="w-12 px-2 py-1 text-sm text-center border border-border rounded-md bg-background text-text-primary focus:outline-none focus:ring-2 focus:ring-primary"
          />
          <span className="text-sm text-text-secondary">of {pageCount || 1}</span>
        </div>

        {/* Next page */}
        <button
          onClick={() => onPageChange(pageIndex + 1)}
          disabled={!canNextPage}
          className={`
            p-1.5 rounded-md transition-colors
            ${canNextPage
              ? 'hover:bg-card text-text-secondary hover:text-text-primary'
              : 'text-border cursor-not-allowed'
            }
          `}
          title="Next page"
        >
          <ChevronRight className="w-4 h-4" />
        </button>

        {/* Last page */}
        <button
          onClick={() => onPageChange(pageCount - 1)}
          disabled={!canNextPage}
          className={`
            p-1.5 rounded-md transition-colors
            ${canNextPage
              ? 'hover:bg-card text-text-secondary hover:text-text-primary'
              : 'text-border cursor-not-allowed'
            }
          `}
          title="Last page"
        >
          <ChevronsRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
};
