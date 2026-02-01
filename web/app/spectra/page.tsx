'use client';

import React, { useState, useEffect, useMemo, Suspense, useTransition } from 'react';
import Link from 'next/link';
import { useSearchParams, useRouter, usePathname } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { SpectraTable } from '@/components/spectra/SpectraTable';
import { SpectraFilterBar, AdvancedFilterOptions } from '@/components/spectra/SpectraFilterBar';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import { LogIn, Loader2, Info, KeyRound } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
  parseFiltersFromURL,
  parsePaginationFromURL,
  parseSortingFromURL,
  filtersToURLParams,
} from '@/lib/utils/url-params';
import { useDebouncedValue } from '@/lib/hooks/useDebouncedValue';
import { useSpectraQuery } from '@/lib/hooks/useSpectraQuery';
import { useFilterOptionsQuery } from '@/lib/hooks/useFilterOptionsQuery';

// Inner component that uses useSearchParams (must be wrapped in Suspense)
function SpectraPageContent() {
  const { user, loading: authLoading, needsAccessCode } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // useTransition for non-blocking state updates
  const [isPending, startTransition] = useTransition();

  // Parse filters, pagination, and sorting from URL on initial load
  const initialFilters = useMemo(() => parseFiltersFromURL(searchParams), [searchParams]);
  const initialPagination = useMemo(() => parsePaginationFromURL(searchParams), [searchParams]);
  const initialSorting = useMemo(() => parseSortingFromURL(searchParams), [searchParams]);

  // UI state (kept local)
  const [filters, setFilters] = useState<AdvancedFilterOptions>(initialFilters);
  const [page, setPage] = useState(initialPagination.page);
  const [pageSize, setPageSize] = useState(initialPagination.pageSize);
  const [sortColumn, setSortColumn] = useState<SortColumn>(initialSorting.sortColumn);
  const [sortDirection, setSortDirection] = useState<SortDirection>(initialSorting.sortDirection);
  const [isFullDataset, setIsFullDataset] = useState(false);

  // Debounce filters to avoid excessive database queries
  const { debouncedValue: debouncedFilters, isDebouncing } = useDebouncedValue(filters, 300);

  // Always use normal pagination for queries - server-side sorting is fast
  // The isFullDataset flag only determines client vs server sorting AFTER data arrives

  // TanStack Query for spectra data
  const {
    data: spectraResult,
    isLoading,
    isFetching,
    error: queryError,
  } = useSpectraQuery({
    filters: debouncedFilters,
    page,
    pageSize,
    sortColumn,
    sortDirection,
    enabled: !authLoading && !!user,
  });

  // TanStack Query for filter options (programs, fields, observations)
  const { data: filterOptionsResult } = useFilterOptionsQuery(!authLoading && !!user);

  // Derive values from query results
  const spectra = spectraResult?.spectra ?? [];
  const totalCount = spectraResult?.total ?? 0;
  const totalPages = spectraResult?.totalPages ?? 0;
  const error = queryError ? 'Failed to fetch data' : spectraResult?.error ?? null;

  // Derive available filter options
  const availablePrograms = filterOptionsResult?.programs ?? [];
  const availableFields = filterOptionsResult?.fields ?? [];
  const availableObservations = filterOptionsResult?.observations ?? [];

  // Update isFullDataset when query results change
  useEffect(() => {
    if (spectraResult) {
      setIsFullDataset(spectraResult.isComplete);
    }
  }, [spectraResult]);

  // Show skeletons whenever fetching (including on filter changes)
  // useTransition keeps inputs responsive during state updates
  const loading = isFetching;

  // Update URL when debounced filters, pagination, or sorting change
  // Using debouncedFilters prevents URL updates on every keystroke, improving performance
  useEffect(() => {
    const params = filtersToURLParams(debouncedFilters, page, pageSize, sortColumn, sortDirection);
    const newSearch = params.toString();
    const currentSearch = searchParams.toString();

    if (newSearch !== currentSearch) {
      router.replace(`${pathname}${newSearch ? `?${newSearch}` : ''}`, { scroll: false });
    }
  }, [debouncedFilters, page, pageSize, sortColumn, sortDirection, pathname, router, searchParams]);

  // Handle filter changes with useTransition for non-blocking updates
  const handleFilterChange = (newFilters: AdvancedFilterOptions) => {
    startTransition(() => {
      setFilters(newFilters);
      setPage(1);
      // Don't reset isFullDataset - let the query response determine this
    });
  };

  const handlePageChange = (newPage: number) => {
    setPage(newPage);
  };

  const handlePageSizeChange = (newPageSize: number) => {
    setPageSize(newPageSize);
    // Reset to page 1 when page size changes
    setPage(1);
  };

  // Handle sort changes from table
  // In full dataset mode, table handles sorting internally (client-side, no refetch needed)
  // In paginated mode, we need to refetch with new sort params
  const handleSortChange = (column: SortColumn, direction: SortDirection) => {
    if (!isFullDataset) {
      // Reset to page 1 when sort changes in server-side mode
      setPage(1);
    }
    // Always update URL state for bookmarkability
    // In full dataset mode, the query key changes but TanStack Query will use cached data
    // since the filter params haven't changed, only sort params
    setSortColumn(column);
    setSortDirection(direction);
  };

  // Show login prompt if not authenticated
  if (!authLoading && !user) {
    return (
      <div className="container mx-auto px-4 py-8">
        <Breadcrumbs
          items={[
            { label: 'CAMPFIRE', href: '/' },
            { label: 'NIRSpec' },
          ]}
          className="mb-6"
        />

        <div className="flex flex-col items-center justify-center py-16 text-center">
          <div className="w-16 h-16 bg-card dark:bg-slate-800 rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary dark:text-slate-400" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary dark:text-slate-100 mb-2">
            Sign in to view spectra
          </h2>
          <p className="text-text-secondary dark:text-slate-400 mb-6 max-w-md">
            Access to NIRSpec spectra requires authentication. Please sign in with your
            CAMPFIRE account to browse the catalog.
          </p>
          <Link
            href="/login"
            className="inline-flex items-center gap-2 px-6 py-3 bg-primary text-white rounded-lg hover:bg-primary-hover transition-colors"
          >
            <LogIn className="w-5 h-5" />
            Sign In
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="container mx-auto px-4 py-8">
      {/* Breadcrumbs */}
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRSpec' },
        ]}
        className="mb-6"
      />

      {/* Page Header */}
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-text-primary dark:text-slate-100 mb-2">NIRSpec Spectra</h1>
        <p className="text-text-secondary dark:text-slate-400">
          Browse and filter the CAMPFIRE spectroscopic catalog
        </p>
      </div>

      {/* Access Code Banner for users without proprietary access */}
      {!authLoading && user && needsAccessCode && (
        <div className="mb-6 p-4 bg-blue-50 dark:bg-blue-950 border border-blue-200 dark:border-blue-900 rounded-lg flex items-start gap-3">
          <Info className="w-5 h-5 text-blue-600 dark:text-blue-400 flex-shrink-0 mt-0.5" />
          <div className="flex-1">
            <p className="text-sm text-blue-900 dark:text-blue-200">
              <strong>You&apos;re viewing public programs only.</strong> To access proprietary programs, redeem an access code.
            </p>
          </div>
          <Link
            href="/profile#access-code"
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white text-sm rounded-md hover:bg-blue-700 transition-colors whitespace-nowrap"
          >
            <KeyRound className="w-4 h-4" />
            Enter Code
          </Link>
        </div>
      )}

      {/* Filter Bar */}
      <div className="mb-6">
        <SpectraFilterBar
          filters={filters}
          onFiltersChange={handleFilterChange}
          availablePrograms={availablePrograms}
          availableFields={availableFields}
          availableObservations={availableObservations}
          isSearchDebouncing={isDebouncing}
        />
      </div>

      {/* Results Container - maintains height during loading to prevent scrollbar flicker */}
      <div className="min-h-[600px]">
        {/* Table - always shown (handles loading/error states internally) */}
        <SpectraTable
          spectra={spectra}
          total={totalCount}
          page={page}
          pageSize={pageSize}
          totalPages={totalPages}
          onPageChange={handlePageChange}
          onPageSizeChange={handlePageSizeChange}
          isFullDataset={isFullDataset}
          sortColumn={sortColumn}
          sortDirection={sortDirection}
          onSortChange={handleSortChange}
          hasCoordinateSearch={filters.coordinate_search !== null}
          currentFilterParams={filtersToURLParams(filters, page, pageSize, sortColumn, sortDirection)}
          loading={loading}
          error={error}
          filters={filters}
        />
      </div>
    </div>
  );
}

// Loading fallback for Suspense
function SpectraPageLoading() {
  return (
    <div className="container mx-auto px-4 py-8">
      <Breadcrumbs
        items={[
          { label: 'CAMPFIRE', href: '/' },
          { label: 'NIRSpec' },
        ]}
        className="mb-6"
      />
      <div className="flex items-center justify-center py-16">
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
        <span className="ml-3 text-text-secondary dark:text-slate-400">Loading...</span>
      </div>
    </div>
  );
}

// Default export wrapped in Suspense for useSearchParams
export default function SpectraPage() {
  return (
    <Suspense fallback={<SpectraPageLoading />}>
      <SpectraPageContent />
    </Suspense>
  );
}
