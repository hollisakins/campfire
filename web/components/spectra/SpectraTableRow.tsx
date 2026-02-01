'use client';

import React, { memo } from 'react';
import { flexRender, Row, Cell } from '@tanstack/react-table';
import type { SpectrumObject } from '@/lib/types';

interface SpectraTableRowProps {
  row: Row<SpectrumObject>;
}

/**
 * Memoized table row component to prevent unnecessary re-renders.
 * Only re-renders when the underlying data (object_id) changes.
 */
const SpectraTableRowComponent: React.FC<SpectraTableRowProps> = ({ row }) => {
  return (
    <tr className="hover:bg-card-hover dark:hover:bg-slate-700 transition-colors">
      {row.getVisibleCells().map((cell: Cell<SpectrumObject, unknown>) => (
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

// Custom comparison function: only re-render if the row's data ID changes
// This prevents re-renders during sorting/pagination state changes
export const SpectraTableRow = memo(SpectraTableRowComponent, (prevProps, nextProps) => {
  return prevProps.row.original.id === nextProps.row.original.id;
});

SpectraTableRow.displayName = 'SpectraTableRow';
