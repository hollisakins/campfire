'use client';

import React from 'react';
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react';
import type { SortDirection } from '@/lib/hooks/useTableUrlState';

// Extracted from components/spectra/SpectraTable.tsx's SortableHeader, but
// driven by explicit props instead of TanStack column state — admin tables are
// always server-sorted, so the URL-state hook owns the sort.

interface SortableHeaderProps {
  sorted: false | SortDirection;
  onToggle: () => void;
  children: React.ReactNode;
  className?: string;
}

export const SortableHeader: React.FC<SortableHeaderProps> = ({
  sorted,
  onToggle,
  children,
  className = '',
}) => (
  <button onClick={onToggle} className={`flex items-center gap-1 group ${className}`}>
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
