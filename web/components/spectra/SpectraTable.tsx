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
  VisibilityState,
} from '@tanstack/react-table';
import { ArrowUpDown, ArrowUp, ArrowDown, ScanEye, HelpCircle } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import { SpectrumTarget, QUALITY_LABELS } from '@/lib/types';
import { TileThumbnail } from './TileThumbnail';
import { SpectrumThumbnailInline } from './SpectrumThumbnailInline';
import { SpectraTableRow } from './SpectraTableRow';
import { StalenessBadge } from './StalenessBadge';
import { DQ_FLAGS, decodeBitmask } from '@/lib/flags';
import type { SortColumn, SortDirection, ViewMode } from '@/lib/actions/spectra-types';
import { defaultSortColumn } from '@/lib/actions/spectra-types';
import { Card } from '@/components/ui/Card';
import { TablePagination } from '@/components/ui/TablePagination';
import { formatDistance } from '@/lib/utils/coordinate-parser';
import { setNavCache } from '@/lib/navigation-cache';
import {
  ColumnVisibilityDropdown,
  useColumnVisibility,
  type ColumnDefinition,
} from '@/components/ui/ColumnVisibilityDropdown';
import { DownloadDropdown } from './DownloadTableButtons';
import type { AdvancedFilterOptions } from './SpectraFilterBar';

// Column visibility configuration — spectra mode (per-grating rows).
// Only per-spectrum info belongs here; inherited object fields (redshift,
// redshift_quality) live in objects mode. Spectrum thumbnail is rendered
// last to avoid squeezing textual columns when the table is narrow.
const SPECTRA_MODE_COLUMNS: ColumnDefinition[] = [
  { id: 'rgb_thumbnail', label: 'RGB Image', defaultVisible: true },
  { id: 'spectrum_id', label: 'Spectrum ID', alwaysVisible: true },
  { id: 'field', label: 'Field', defaultVisible: true },
  { id: 'ra', label: 'RA', defaultVisible: true },
  { id: 'dec', label: 'Dec', defaultVisible: true },
  { id: 'distance', label: 'Distance', defaultVisible: true },
  { id: 'program', label: 'Program', defaultVisible: true },
  { id: 'grating', label: 'Grating', defaultVisible: true },
  { id: 'redshift_auto', label: 'Redshift (auto)', defaultVisible: true },
  { id: 'signal_to_noise', label: 'S/N', defaultVisible: true },
  { id: 'exposure_time', label: 'Exp. Time', defaultVisible: true },
  { id: 'dq_flags', label: 'DQ', defaultVisible: true },
  { id: 'observation', label: 'Observation', defaultVisible: false },
  { id: 'spectrum_thumbnail', label: 'Spectrum', defaultVisible: true },
];

// Map TanStack Table column IDs to server column names — spectra mode
const SPECTRA_COLUMN_TO_SERVER: Record<string, SortColumn> = {
  'spectrum_id': 'spectrum_id',
  'field': 'field',
  'observation': 'observation',
  'program': 'program_slug',
  'ra': 'ra',
  'dec': 'dec',
  'redshift': 'redshift',
  'redshift_quality': 'redshift_quality',
  'redshift_auto': 'redshift_auto',
  'signal_to_noise': 'signal_to_noise',
  'exposure_time': 'exposure_time',
  'grating': 'grating',
  'distance': 'distance',
};

// Column visibility configuration — objects mode (unique sky positions).
const OBJECTS_COLUMNS: ColumnDefinition[] = [
  { id: 'target_id', label: 'Object ID', alwaysVisible: true },
  { id: 'staleness', label: 'Review', defaultVisible: true },
  { id: 'field', label: 'Field', defaultVisible: true },
  { id: 'ra', label: 'RA', defaultVisible: true },
  { id: 'dec', label: 'Dec', defaultVisible: true },
  { id: 'distance', label: 'Distance', defaultVisible: true },
  { id: 'redshift', label: 'Redshift', alwaysVisible: true },
  { id: 'redshift_quality', label: 'Quality', alwaysVisible: true },
  { id: 'photo_z', label: 'Photo-z', defaultVisible: false },
  { id: 'n_targets', label: '# Observations', defaultVisible: true },
  { id: 'n_spectra', label: '# Spectra', defaultVisible: false },
  { id: 'obj_programs', label: 'Programs', defaultVisible: true },
  { id: 'obj_gratings', label: 'Gratings', defaultVisible: true },
  { id: 'obj_lists', label: 'Tags', defaultVisible: false },
  { id: 'max_snr', label: 'Max S/N', defaultVisible: true },
  { id: 'max_exposure_time', label: 'Max Exp. Time', defaultVisible: false },
];

// Map TanStack Table column IDs to server column names — objects mode.
const OBJECTS_COLUMN_TO_SERVER: Record<string, SortColumn> = {
  'target_id': 'object_id',
  'field': 'field',
  'ra': 'ra',
  'dec': 'dec',
  'redshift': 'redshift',
  'redshift_quality': 'redshift_quality',
  'n_targets': 'n_targets',
  'n_spectra': 'n_spectra',
  'max_snr': 'max_snr',
  'max_exposure_time': 'max_exposure_time',
  'photo_z': 'photo_z',
  'distance': 'distance',
};

interface SpectraTableProps {
  spectra: SpectrumTarget[];
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
  // View mode
  viewMode?: ViewMode;
  onViewModeChange?: (mode: ViewMode) => void;
  // Coordinate search (to show distance column)
  hasCoordinateSearch?: boolean;
  // Current filter params to preserve in detail page links
  currentFilterParams?: URLSearchParams;
  // Loading and error states
  loading?: boolean;
  error?: string | null;
  // Download props
  filters?: AdvancedFilterOptions;
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
        <ArrowUpDown className="w-3.5 h-3.5 text-text-secondary dark:text-slate-400 opacity-0 group-hover:opacity-100 transition-opacity" />
      )}
    </button>
  );
};

// Skeleton row component for loading state
const TableSkeletonRow: React.FC<{ columns: ColumnDef<SpectrumTarget>[] }> = ({ columns }) => (
  <tr className="animate-pulse">
    {columns.map((col, i) => (
      <td
        key={i}
        className="px-4 py-3 whitespace-nowrap"
        style={{ width: `${col.minSize || 150}px` }}
      >
        <div className="h-4 bg-gray-200 dark:bg-slate-700 rounded w-full"></div>
      </td>
    ))}
  </tr>
);

// Skeleton component showing multiple loading rows
const TableSkeleton: React.FC<{ rows: number; columns: ColumnDef<SpectrumTarget>[] }> = ({ rows, columns }) => (
  <>
    {Array.from({ length: rows }).map((_, i) => (
      <TableSkeletonRow key={i} columns={columns} />
    ))}
  </>
);

// Popup tooltip for the view mode toggle
const ViewModeTooltip: React.FC = () => {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  return (
    <div ref={ref} className="relative flex items-center">
      <button
        onClick={() => setOpen(!open)}
        className="text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
        aria-label="View mode help"
      >
        <HelpCircle className="w-3.5 h-3.5" />
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-1.5 z-50 w-64 px-3 py-2 text-xs text-text-secondary dark:text-slate-400 bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg">
          <p><span className="font-medium text-text-primary dark:text-slate-200">Objects</span> = unique sources across programs</p>
          <p><span className="font-medium text-text-primary dark:text-slate-200">Spectra</span> = individual grating exposures</p>
        </div>
      )}
    </div>
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
  viewMode = 'objects',
  onViewModeChange,
  hasCoordinateSearch = false,
  currentFilterParams,
  loading = false,
  error = null,
  filters,
}) => {
  const { user, userProfile } = useAuth();
  const canInspect = !!(user && userProfile?.can_comment);
  const isSpectraMode = viewMode === 'spectra';
  const isObjectsMode = viewMode === 'objects';

  // Column config and server-name mapping depend on view mode (Objects | Spectra).
  const columnConfig = isObjectsMode ? OBJECTS_COLUMNS : SPECTRA_MODE_COLUMNS;
  const COLUMN_TO_SERVER_NAME = isObjectsMode ? OBJECTS_COLUMN_TO_SERVER : SPECTRA_COLUMN_TO_SERVER;

  // Reverse mapping: server column name → TanStack column ID (needed for effectiveSorting)
  const SERVER_TO_COLUMN_NAME = useMemo(() => {
    const reverse: Record<string, string> = {};
    for (const [clientId, serverId] of Object.entries(COLUMN_TO_SERVER_NAME)) {
      reverse[serverId as string] = clientId;
    }
    return reverse;
  }, [COLUMN_TO_SERVER_NAME]);

  // Column visibility state with localStorage persistence (separate keys per mode)
  const visibilityKey = isObjectsMode
    ? 'campfire-spectra-columns-objects'
    : 'campfire-spectra-columns-spectra';
  const [columnVisibility, setColumnVisibility] = useColumnVisibility(
    columnConfig,
    visibilityKey
  );

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
    // Map server column name back to client column ID (e.g. best_redshift → redshift)
    if (!isFullDataset) {
      const clientId = sortColumn ? (SERVER_TO_COLUMN_NAME[sortColumn] || sortColumn) : null;
      setInternalSorting(clientId ? [{ id: clientId, desc: sortDirection === 'desc' }] : []);
    }
  }, [isFullDataset, sortColumn, sortDirection, hasCoordinateSearch, SERVER_TO_COLUMN_NAME]);

  // Use internal state for client-side, props for server-side
  const effectiveSorting = useMemo(() => {
    if (isFullDataset) return internalSorting;
    const clientId = sortColumn ? (SERVER_TO_COLUMN_NAME[sortColumn] || sortColumn) : null;
    return clientId ? [{ id: clientId, desc: sortDirection === 'desc' }] : [];
  }, [isFullDataset, internalSorting, sortColumn, sortDirection, SERVER_TO_COLUMN_NAME]);

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
        onSortChange(defaultSortColumn(viewMode), 'asc');
      }
    }
  };

  // Distance column definition (conditional)
  const distanceColumn: ColumnDef<SpectrumTarget> = {
    accessorKey: 'distance',
    minSize: 100,
    header: ({ column }) => (
      <SortableHeader column={column}>Distance</SortableHeader>
    ),
    cell: ({ row }) => (
      <span className="text-sm font-mono text-text-primary dark:text-slate-100">
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
  const columns = useMemo<ColumnDef<SpectrumTarget>[]>(
    () => [
      {
        id: 'rgb_thumbnail',
        size: 56,
        minSize: 56,
        maxSize: 56,
        header: () => <span className="sr-only">Image</span>,
        cell: ({ row }) => (
          <TileThumbnail targetId={row.original.target_id} size={48} />
        ),
        enableSorting: false,
      },
      // Needs Review staleness column (objects mode only).
      ...(isObjectsMode ? [{
        id: 'staleness',
        minSize: 110,
        header: () => <span className="normal-case">Review</span>,
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <StalenessBadge
            reason={row.original.staleness_reason ?? null}
            lastInspectedAt={row.original.last_inspected_at ?? null}
            lastDataChangeAt={row.original.last_data_change_at ?? null}
            compact
          />
        ),
        enableSorting: false,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: Object ID column (links to object detail page).
      ...(isObjectsMode ? [{
        accessorKey: 'target_id' as const,
        id: 'target_id',
        minSize: 260,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Object ID</SortableHeader>
        ),
        cell: ({ row, table }: { row: { original: SpectrumTarget }; table: ReturnType<typeof useReactTable<SpectrumTarget>> }) => {
          const filterStr = currentFilterParams?.toString() || '';
          return (
            <Link
              href={`/nirspec/objects/${encodeURIComponent(row.original.target_id)}${filterStr ? `?${filterStr}` : ''}`}
              className="text-sm font-mono text-primary hover:underline"
              onClick={() => {
                const rows = table.getRowModel().rows;
                const visibleIds = rows.map(r => r.original.target_id);
                const pageIndex = table.getState().pagination.pageIndex;
                const ps = table.getState().pagination.pageSize;
                setNavCache({
                  ids: visibleIds,
                  filters: filterStr,
                  sort: `${sortColumn}_${sortDirection}`,
                  pageStart: pageIndex * ps,
                  total,
                });
              }}
            >
              {row.original.target_id}
            </Link>
          );
        },
        sortingFn: 'alphanumeric' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      ...(isSpectraMode ? [{
        id: 'spectrum_id',
        minSize: 320,
        accessorFn: (row: SpectrumTarget) => row.spectra[0]?.spectrum_id ?? '',
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Spectrum ID</SortableHeader>
        ),
        cell: ({ row, table }: { row: { original: SpectrumTarget }; table: ReturnType<typeof useReactTable<SpectrumTarget>> }) => {
          const rows = table.getRowModel().rows;
          const visibleIds = [...new Set(rows.map(r => r.original.target_id))];
          const pageIndex = table.getState().pagination.pageIndex;
          const ps = table.getState().pagination.pageSize;
          const filterStr = currentFilterParams?.toString() || '';
          const spectrumId = row.original.spectra[0]?.spectrum_id ?? row.original.target_id;
          const fitsPath = row.original.spectra[0]?.fits_path;
          const parentObjectId = row.original.parent_object_id;

          const linkParams = [filterStr, spectrumId ? `spectrum=${encodeURIComponent(spectrumId)}` : '']
            .filter(Boolean)
            .join('&');
          const href = parentObjectId
            ? `/nirspec/objects/${encodeURIComponent(parentObjectId)}${linkParams ? `?${linkParams}` : ''}`
            : `/nirspec/targets/${encodeURIComponent(row.original.target_id)}${linkParams ? `?${linkParams}` : ''}`;

          return (
            <Link
              href={href}
              onClick={() => setNavCache({
                ids: visibleIds,
                filters: filterStr,
                sort: `${sortColumn}_${sortDirection}`,
                pageStart: pageIndex * ps,
                total,
              })}
              className="text-sm font-mono text-primary hover:underline"
              title={fitsPath}
            >
              {spectrumId}
            </Link>
          );
        },
        sortingFn: 'alphanumeric' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      {
        accessorKey: 'field',
        minSize: 80,
        header: ({ column }) => (
          <SortableHeader column={column}>Field</SortableHeader>
        ),
        cell: ({ row }) => (
          <span className="text-sm text-text-primary dark:text-slate-100 uppercase">{row.original.field}</span>
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
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
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
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.dec.toFixed(6)}
          </span>
        ),
        sortingFn: 'basic',
      },
      // Distance column (only shown when coordinate search is active)
      ...(hasCoordinateSearch ? [distanceColumn] : []),
      // Redshift column: objects mode only. In spectra mode this value is
      // inherited from the parent object (all spectra of the same object share
      // it), so showing it per-row is duplicated; the per-spectrum auto-fit
      // redshift lives in the `redshift_auto` column instead.
      ...(isObjectsMode ? [{
        accessorKey: 'redshift' as const,
        minSize: 90,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Redshift</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.redshift !== null ? row.original.redshift.toFixed(4) : 'N/A'}
          </span>
        ),
        sortingFn: (rowA: { original: SpectrumTarget }, rowB: { original: SpectrumTarget }) => {
          const a = rowA.original.redshift ?? -1;
          const b = rowB.original.redshift ?? -1;
          return a - b;
        },
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Program column (spectra mode only — objects mode uses obj_programs).
      // `normal-case` opts out of the table's uppercase header style since
      // program slugs read awkwardly in all-caps.
      ...(!isObjectsMode ? [{
        id: 'program',
        minSize: 120,
        accessorFn: (row: SpectrumTarget) => row.program_name || row.program_slug,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column} className="normal-case">Program</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const name = row.original.program_name;
          return (
            <span className="text-sm text-text-primary dark:text-slate-100">
              {name || row.original.program_slug}
            </span>
          );
        },
        sortingFn: 'alphanumeric' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Quality column: objects mode only. Spectra inherit redshift_quality
      // from their parent object (we don't inspect spectra individually), so
      // showing it per-row in spectra mode is duplicated and confusing.
      ...(isObjectsMode ? [{
        accessorKey: 'redshift_quality' as const,
        minSize: 90,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Quality</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const quality = getQualityInfo(row.original.redshift_quality);
          return (
            <div className="flex items-center gap-1.5">
              <span>{quality.icon}</span>
              <span className="text-sm text-text-primary dark:text-slate-100">{quality.short}</span>
            </div>
          );
        },
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Spectra mode: per-spectrum redshift_auto column
      ...(isSpectraMode ? [{
        id: 'redshift_auto',
        minSize: 120,
        accessorFn: (row: SpectrumTarget) => row.spectra[0]?.redshift_auto ?? null,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column} className="normal-case">Redshift (auto)</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const z = row.original.spectra[0]?.redshift_auto;
          return (
            <span className="text-sm font-mono text-text-primary dark:text-slate-100">
              {z != null ? z.toFixed(4) : '—'}
            </span>
          );
        },
        sortingFn: (a, b) => ((a.original.spectra[0]?.redshift_auto ?? -1) - (b.original.spectra[0]?.redshift_auto ?? -1)),
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Spectra mode: per-spectrum DQ flags column
      ...(isSpectraMode ? [{
        id: 'dq_flags',
        minSize: 130,
        accessorFn: (row: SpectrumTarget) => row.spectra[0]?.dq_flags ?? 0,
        header: () => <span>DQ</span>,
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const mask = row.original.spectra[0]?.dq_flags ?? 0;
          if (mask === 0) {
            return <span className="text-xs text-text-secondary dark:text-slate-500">—</span>;
          }
          const active = decodeBitmask(mask, DQ_FLAGS);
          const defs = DQ_FLAGS.filter(f => active.includes(f.value));
          return (
            <div className="flex flex-wrap gap-1 max-w-[180px]">
              {defs.map(f => (
                <span
                  key={f.key}
                  className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-medium"
                  style={{ backgroundColor: f.color, color: '#1a1a1a' }}
                  title={f.description}
                >
                  {f.icon} {f.short}
                </span>
              ))}
            </div>
          );
        },
        enableSorting: false,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: n_targets column
      ...(isObjectsMode ? [{
        id: 'n_targets',
        minSize: 90,
        accessorFn: (row: SpectrumTarget) => row.n_targets ?? 0,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column} className="justify-center"># Observations</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm text-text-primary dark:text-slate-100 text-center block">
            {row.original.n_targets ?? 0}
          </span>
        ),
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: n_spectra column
      ...(isObjectsMode ? [{
        id: 'n_spectra',
        minSize: 90,
        accessorFn: (row: SpectrumTarget) => row.n_spectra ?? 0,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column} className="justify-center"># Spectra</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm text-text-primary dark:text-slate-100 text-center block">
            {row.original.n_spectra ?? 0}
          </span>
        ),
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: photo_z column
      ...(isObjectsMode ? [{
        id: 'photo_z',
        minSize: 100,
        accessorFn: (row: SpectrumTarget) => row.photo_z ?? null,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column} className="justify-end">Photo-z</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100 text-right block">
            {row.original.photo_z != null ? row.original.photo_z.toFixed(4) : '—'}
          </span>
        ),
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: programs column
      ...(isObjectsMode ? [{
        id: 'obj_programs',
        minSize: 140,
        header: () => <span className="normal-case">Programs</span>,
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const programs = row.original.programs ?? [];
          return (
            <div className="flex flex-wrap gap-1">
              {programs.map((p) => (
                <span key={p} className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-surface-secondary text-text-secondary">
                  {p}
                </span>
              ))}
            </div>
          );
        },
        enableSorting: false,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: gratings column
      ...(isObjectsMode ? [{
        id: 'obj_gratings',
        minSize: 140,
        header: () => <span className="normal-case">Gratings</span>,
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const gratings = row.original.gratings ?? [];
          return (
            <div className="flex flex-wrap gap-1">
              {gratings.map((g) => (
                <span key={g} className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-surface-secondary text-text-secondary">
                  {g}
                </span>
              ))}
            </div>
          );
        },
        enableSorting: false,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: lists column
      ...(isObjectsMode ? [{
        id: 'obj_lists',
        minSize: 160,
        header: () => <span className="normal-case">Tags</span>,
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const lists = row.original.lists ?? [];
          if (lists.length === 0) return <span className="text-xs text-text-secondary dark:text-slate-500">---</span>;
          return (
            <div className="flex flex-wrap gap-1 max-w-[200px]">
              {lists.map((l) => (
                <Link
                  key={l.id}
                  href={`/nirspec/tags/${l.slug}`}
                  className="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-[10px] font-medium hover:opacity-80 transition-opacity"
                  style={{
                    backgroundColor: l.color ? `${l.color}40` : 'var(--color-surface-secondary)',
                    color: l.color || 'var(--color-text-secondary)',
                  }}
                  onClick={(e) => e.stopPropagation()}
                >
                  {l.icon && <span>{l.icon}</span>}
                  {l.name}
                </Link>
              ))}
            </div>
          );
        },
        enableSorting: false,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: max_snr column
      ...(isSpectraMode ? [] : [{
        id: 'max_snr',
        minSize: 90,
        accessorFn: (row: SpectrumTarget) => row.max_snr ?? 0,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>S/N</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.max_snr ? row.original.max_snr.toFixed(1) : 'N/A'}
          </span>
        ),
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>]),
      // Spectra mode: signal_to_noise column (per-spectrum)
      ...(isSpectraMode ? [{
        id: 'signal_to_noise',
        minSize: 90,
        accessorFn: (row: SpectrumTarget) => row.spectra[0]?.signal_to_noise ?? 0,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>S/N</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const snr = row.original.spectra[0]?.signal_to_noise;
          return (
            <span className="text-sm font-mono text-text-primary dark:text-slate-100">
              {snr != null ? snr.toFixed(1) : 'N/A'}
            </span>
          );
        },
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Objects mode: max_exposure_time column
      ...(isSpectraMode ? [] : [{
        id: 'max_exposure_time',
        minSize: 110,
        accessorFn: (row: SpectrumTarget) => row.max_exposure_time ?? 0,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Exp. Time</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.max_exposure_time ? `${row.original.max_exposure_time.toFixed(0)}s` : 'N/A'}
          </span>
        ),
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>]),
      // Spectra mode: exposure_time column (per-spectrum)
      ...(isSpectraMode ? [{
        id: 'exposure_time',
        minSize: 110,
        accessorFn: (row: SpectrumTarget) => row.spectra[0]?.exposure_time ?? 0,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Exp. Time</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => {
          const expTime = row.original.spectra[0]?.exposure_time;
          return (
            <span className="text-sm font-mono text-text-primary dark:text-slate-100">
              {expTime != null ? `${expTime.toFixed(0)}s` : 'N/A'}
            </span>
          );
        },
        sortingFn: 'basic' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Grating column (spectra mode only) — sits next to Observation since
      // both describe the exposure setup.
      ...(isSpectraMode ? [{
        id: 'grating',
        minSize: 90,
        accessorFn: (row: SpectrumTarget) => row.spectra[0]?.grating ?? '',
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Grating</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.spectra[0]?.grating ?? 'N/A'}
          </span>
        ),
        sortingFn: 'alphanumeric' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Observation column (hidden in objects mode — objects span observations)
      ...(!isObjectsMode ? [{
        accessorKey: 'observation' as const,
        minSize: 150,
        header: ({ column }: { column: { getIsSorted: () => false | 'asc' | 'desc'; toggleSorting: (desc?: boolean) => void } }) => (
          <SortableHeader column={column}>Observation</SortableHeader>
        ),
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <span className="text-sm font-mono text-text-primary dark:text-slate-100">
            {row.original.observation || 'N/A'}
          </span>
        ),
        sortingFn: 'alphanumeric' as const,
      } satisfies ColumnDef<SpectrumTarget>] : []),
      // Spectrum thumbnail (spectra mode only — no per-object thumbnails in
      // objects mode). Rendered last so the wide image cell doesn't squeeze
      // the textual columns when the viewport is narrow.
      ...(!isObjectsMode ? [{
        id: 'spectrum_thumbnail',
        minSize: 130,
        header: () => <span className="normal-case">Spectrum</span>,
        cell: ({ row }: { row: { original: SpectrumTarget } }) => (
          <SpectrumThumbnailInline
            spectra={isSpectraMode ? row.original.spectra.slice(0, 1) : row.original.spectra}
            width={120}
            height={40}
          />
        ),
        enableSorting: false,
      } satisfies ColumnDef<SpectrumTarget>] : []),
    ],
    [hasCoordinateSearch, currentFilterParams, isSpectraMode, isObjectsMode]
  );

  // Convert column visibility state to TanStack Table format
  const tableColumnVisibility = useMemo<VisibilityState>(() => {
    const visibility: VisibilityState = {};
    columnConfig.forEach(col => {
      // Distance column is special - only include in visibility state when coordinate search is active
      // (avoids TanStack Table warning about referencing a non-existent column)
      if (col.id === 'distance') {
        if (hasCoordinateSearch) {
          visibility[col.id] = columnVisibility[col.id] !== false;
        }
      } else {
        visibility[col.id] = columnVisibility[col.id] !== false;
      }
    });
    return visibility;
  }, [columnVisibility, hasCoordinateSearch, columnConfig]);

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
      columnVisibility: tableColumnVisibility,
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

  // Count visible columns for colspan calculations
  const visibleColumnCount = table.getVisibleLeafColumns().length;

  // Get visible column IDs string for row memoization
  const visibleColumnIds = table.getVisibleLeafColumns().map(c => c.id).join(',');

  // Filter columns for visibility dropdown - exclude distance when not in coord search mode
  const availableColumnsForDropdown = useMemo(() => {
    return columnConfig.filter(col => col.id !== 'distance' || hasCoordinateSearch);
  }, [hasCoordinateSearch, columnConfig]);

  return (
    <Card className="overflow-hidden">
      {/* Table header with view mode toggle, column visibility, and download dropdowns */}
      <div className="flex items-center justify-between px-4 py-2 bg-card dark:bg-slate-800 border-b border-border dark:border-slate-700">
        <div className="flex items-center gap-3">
          <span className="text-sm text-text-secondary dark:text-slate-400">
            {loading ? 'Loading...' : `${total.toLocaleString()} ${isObjectsMode ? 'unique objects' : 'spectra'}`}
          </span>
          {/* View mode toggle */}
          {onViewModeChange && (
            <>
              <div className="flex items-center bg-gray-100 dark:bg-slate-700 rounded-md p-0.5">
                {(['objects', 'spectra'] as const).map((mode) => (
                  <button
                    key={mode}
                    onClick={() => onViewModeChange(mode)}
                    className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
                      viewMode === mode
                        ? 'bg-white dark:bg-slate-600 text-text-primary dark:text-slate-100 shadow-sm'
                        : 'text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200'
                    }`}
                  >
                    {mode.charAt(0).toUpperCase() + mode.slice(1)}
                  </button>
                ))}
              </div>
              <ViewModeTooltip />
            </>
          )}
        </div>
        <div className="flex items-center gap-1">
          {canInspect && isObjectsMode && spectra.length > 0 && (
            <Link
              href={`/inspect?start=${encodeURIComponent(spectra[0].target_id)}&${currentFilterParams?.toString() || ''}`}
              onClick={() => {
                const rows = table.getRowModel().rows;
                const visibleIds = rows.map(r => r.original.target_id);
                const pageIndex = table.getState().pagination.pageIndex;
                const ps = table.getState().pagination.pageSize;
                setNavCache({
                  ids: visibleIds,
                  filters: currentFilterParams?.toString() || '',
                  sort: `${sortColumn}_${sortDirection}`,
                  pageStart: pageIndex * ps,
                  total,
                });
              }}
              className="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium rounded-lg bg-primary hover:bg-primary-hover text-white transition-colors"
              title="Streamlined fullscreen view for rapid quality inspection. Auto-filters to uninspected objects and supports keyboard shortcuts for efficient review."
            >
              <ScanEye className="w-3.5 h-3.5" />
              Inspect
            </Link>
          )}
          {filters && (
            <DownloadDropdown
              totalCount={total}
              filters={filters}
              sortColumn={sortColumn}
              sortDirection={sortDirection}
              viewMode={viewMode}
              loading={loading}
            />
          )}
          <ColumnVisibilityDropdown
            columns={availableColumnsForDropdown}
            visibility={columnVisibility}
            onChange={setColumnVisibility}
          />
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full">
          <thead className="bg-card dark:bg-slate-800 border-b border-border dark:border-slate-700">
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    className="px-4 py-3 text-left text-xs font-medium text-text-secondary dark:text-slate-400 uppercase tracking-wider"
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
          <tbody className="bg-white dark:bg-slate-800 divide-y divide-border dark:divide-slate-700">
            {loading ? (
              // Loading state: show skeleton rows matching visible columns
              <TableSkeleton
                rows={isFullDataset ? Math.min(pageSize, 10) : pageSize}
                columns={table.getVisibleLeafColumns().map(c => c.columnDef)}
              />
            ) : error ? (
              // Error state: show error message
              <tr>
                <td colSpan={visibleColumnCount} className="px-4 py-16 text-center">
                  <div className="bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-900 rounded-lg p-4 inline-block">
                    <p className="text-red-800 dark:text-red-400">{error}</p>
                  </div>
                </td>
              </tr>
            ) : spectra.length === 0 ? (
              // Empty state: show message
              <tr>
                <td colSpan={visibleColumnCount} className="px-4 py-12 text-center text-text-secondary dark:text-slate-400">
                  No results found.
                  <p className="text-sm mt-2">
                    If you&apos;re looking for proprietary data, you may need to enter an access code on your profile page.
                  </p>
                </td>
              </tr>
            ) : (
              // Data rows - using memoized row component to prevent unnecessary re-renders
              table.getRowModel().rows.map((row) => (
                <SpectraTableRow
                  key={row.id}
                  row={row}
                  visibleColumnIds={visibleColumnIds}
                />
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Always show pagination footer */}
      <div className="border-t border-border dark:border-slate-700">
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
