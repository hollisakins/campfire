'use client';

import React, { memo } from 'react';
import { flexRender, Row, Cell } from '@tanstack/react-table';
import type { SpectrumTarget } from '@/lib/types';

interface SpectraTableRowProps {
  row: Row<SpectrumTarget>;
  visibleColumnIds: string; // Comma-separated list of visible column IDs for memo comparison
}

/**
 * Memoized table row component to prevent unnecessary re-renders.
 * Re-renders when the underlying data or visible columns change.
 */
const SpectraTableRowComponent: React.FC<SpectraTableRowProps> = ({ row }) => {
  return (
    <tr className="hover:bg-card-hover dark:hover:bg-slate-700 transition-colors">
      {row.getVisibleCells().map((cell: Cell<SpectrumTarget, unknown>) => (
        <td
          key={cell.id}
          className="px-4 py-3 whitespace-nowrap"
          style={{ width: `${cell.column.getSize()}px` }}
        >
          {flexRender(cell.column.columnDef.cell, cell.getContext())}
        </td>
      ))}
    </tr>
  );
};

// Custom comparison function: re-render if row data or visible columns change
export const SpectraTableRow = memo(SpectraTableRowComponent, (prevProps, nextProps) => {
  return (
    prevProps.row.original.id === nextProps.row.original.id &&
    prevProps.visibleColumnIds === nextProps.visibleColumnIds
  );
});

SpectraTableRow.displayName = 'SpectraTableRow';
