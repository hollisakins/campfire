'use client';

import React, { useState, useEffect, useCallback, useMemo, Suspense, useRef } from 'react';
import Link from 'next/link';
import { useSearchParams, useRouter, usePathname } from 'next/navigation';
import { Breadcrumbs } from '@/components/ui/Breadcrumbs';
import { SpectraTable } from '@/components/spectra/SpectraTable';
import { SpectraFilterBar, AdvancedFilterOptions } from '@/components/spectra/SpectraFilterBar';
import { DownloadTableButtons } from '@/components/spectra/DownloadTableButtons';
import { getSpectra, getFilterOptions } from '@/lib/actions/spectra';
import type { SortColumn, SortDirection } from '@/lib/actions/spectra-types';
import type { SpectrumObject, Program } from '@/lib/types';
import { LogIn, Loader2 } from 'lucide-react';
import { useAuth } from '@/lib/contexts/AuthContext';
import {
  parseFiltersFromURL,
  parsePaginationFromURL,
  parseSortingFromURL,
  filtersToURLParams,
} from '@/lib/utils/url-params';
import { useDebouncedValue } from '@/lib/hooks/useDebouncedValue';

// Threshold for adaptive sorting: if total results <= this, use client-side sorting
const FULL_DATASET_THRESHOLD = 5000;

// Inner component that uses useSearchParams (must be wrapped in Suspense)
function SpectraPageContent() {
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Parse filters, pagination, and sorting from URL on initial load
  const initialFilters = useMemo(() => parseFiltersFromURL(searchParams), [searchParams]);
  const initialPagination = useMemo(() => parsePaginationFromURL(searchParams), [searchParams]);
  const initialSorting = useMemo(() => parseSortingFromURL(searchParams), [searchParams]);

  const [filters, setFilters] = useState<AdvancedFilterOptions>(initialFilters);
  const [page, setPage] = useState(initialPagination.page);
  const [pageSize, setPageSize] = useState(initialPagination.pageSize);
  const [sortColumn, setSortColumn] = useState<SortColumn>(initialSorting.sortColumn);
  const [sortDirection, setSortDirection] = useState<SortDirection>(initialSorting.sortDirection);
  const [spectra, setSpectra] = useState<SpectrumObject[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [totalPages, setTotalPages] = useState(0);
  const [isFullDataset, setIsFullDataset] = useState(true); // true if all data loaded (client-side sort)
  const [availablePrograms, setAvailablePrograms] = useState<Program[]>([]);
  const [availableFields, setAvailableFields] = useState<string[]>([]);
  const [availableObservations, setAvailableObservations] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Ref to skip fetch when sort changes in full dataset mode (client-side sorting)
  const skipNextFetchRef = useRef(false);

  // Track the filters for the currently in-flight request
  const currentFetchFiltersRef = useRef<AdvancedFilterOptions | null>(null);

  // Debounce ALL filters to avoid excessive database queries and race conditions
  // URL updates immediately for bookmarking, but database query waits 300ms
  const { debouncedValue: debouncedFilters, isDebouncing } = useDebouncedValue(
    filters,
    300
  );

  // Update URL when filters, pagination, or sorting change
  useEffect(() => {
    const params = filtersToURLParams(filters, page, pageSize, sortColumn, sortDirection);
    const newSearch = params.toString();
    const currentSearch = searchParams.toString();

    if (newSearch !== currentSearch) {
      router.replace(`${pathname}${newSearch ? `?${newSearch}` : ''}`, { scroll: false });
    }
  }, [filters, page, pageSize, sortColumn, sortDirection, pathname, router, searchParams]);

  // Fetch data function with adaptive sorting strategy
  const fetchData = useCallback(async () => {
    if (authLoading) return;

    // Skip fetch if this was triggered by a client-side sort change
    if (skipNextFetchRef.current) {
      skipNextFetchRef.current = false;
      return;
    }

    // Store reference to the filters we're fetching for
    const fetchingFilters = debouncedFilters;
    currentFetchFiltersRef.current = fetchingFilters;

    setLoading(true);
    setError(null);

    try {
      // Convert debounced filters for server action
      const serverFilters = {
        programs: debouncedFilters.programs,
        fields: debouncedFilters.fields,
        gratings: debouncedFilters.gratings,
        observations: debouncedFilters.observations,
        redshift_quality: debouncedFilters.redshift_quality,
        coordinate_search: debouncedFilters.coordinate_search,
        redshift_min: debouncedFilters.redshift_min,
        redshift_max: debouncedFilters.redshift_max,
        max_snr_min: debouncedFilters.max_snr_min,
        max_snr_max: debouncedFilters.max_snr_max,
        spectral_features: debouncedFilters.spectral_features,
        object_flags: debouncedFilters.object_flags,
        dq_flags: debouncedFilters.dq_flags,
        inspected_only: debouncedFilters.inspected_only,
        search: debouncedFilters.search,
      };

      // Adaptive fetch strategy:
      // - If we expect a small dataset, try to fetch all (up to FULL_DATASET_THRESHOLD)
      // - If dataset is large, use server-side pagination and sorting
      const effectivePageSize = isFullDataset ? FULL_DATASET_THRESHOLD : pageSize;
      const effectivePage = isFullDataset ? 1 : page;

      const result = await getSpectra(
        serverFilters,
        effectivePage,
        effectivePageSize,
        sortColumn,
        sortDirection
      );

      // Only update state if this is still the current request
      if (currentFetchFiltersRef.current !== fetchingFilters) {
        return; // Stale request, ignore results
      }

      if (result.error) {
        setError(result.error);
      } else {
        setSpectra(result.spectra);
        setTotalCount(result.total);

        // Determine if we have the complete dataset
        const hasCompleteData = result.isComplete;
        setIsFullDataset(hasCompleteData);

        // Set pagination values based on mode
        if (hasCompleteData) {
          // Client-side mode: all data loaded, pagination handled by table
          setTotalPages(1);
        } else {
          // Server-side mode: use server pagination
          setTotalPages(result.totalPages);
        }
      }

      // Check again before fetching filter options
      if (currentFetchFiltersRef.current !== fetchingFilters) {
        return;
      }

      // Fetch filter options
      const filterOptions = await getFilterOptions();

      // Final check before updating filter options
      if (currentFetchFiltersRef.current !== fetchingFilters) {
        return;
      }

      if (!filterOptions.error) {
        setAvailablePrograms(filterOptions.programs);
        setAvailableFields(filterOptions.fields);
        setAvailableObservations(filterOptions.observations);
      }
    } catch (err) {
      // Only set error if this is still the current request
      if (currentFetchFiltersRef.current === fetchingFilters) {
        setError('Failed to fetch data');
        console.error(err);
      }
    } finally {
      // Only clear loading if this is still the current request
      if (currentFetchFiltersRef.current === fetchingFilters) {
        setLoading(false);
      }
    }
  }, [
    debouncedFilters,
    page,
    pageSize,
    sortColumn,
    sortDirection,
    isFullDataset,
    authLoading
  ]);

  // Fetch data when filters change or user logs in
  useEffect(() => {
    fetchData();
  }, [fetchData, user]);

  const handleFilterChange = (newFilters: AdvancedFilterOptions) => {
    setFilters(newFilters);
    // Reset to page 1 and assume full dataset mode when filters change
    setPage(1);
    setIsFullDataset(true);
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
  // In full dataset mode, table handles sorting internally (no refetch needed)
  // In paginated mode, we need to refetch with new sort params
  const handleSortChange = (column: SortColumn, direction: SortDirection) => {
    // In full dataset mode, skip the fetch - table handles sorting client-side
    if (isFullDataset) {
      skipNextFetchRef.current = true;
    } else {
      // Reset to page 1 when sort changes in server-side mode
      setPage(1);
    }
    // Always update URL state for bookmarkability
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
          <div className="w-16 h-16 bg-card rounded-full flex items-center justify-center mb-6">
            <LogIn className="w-8 h-8 text-text-secondary" />
          </div>
          <h2 className="text-2xl font-semibold text-text-primary mb-2">
            Sign in to view spectra
          </h2>
          <p className="text-text-secondary mb-6 max-w-md">
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
        <h1 className="text-2xl font-bold text-text-primary mb-2">NIRSpec Spectra</h1>
        <p className="text-text-secondary">
          Browse and filter the CAMPFIRE spectroscopic catalog
        </p>
      </div>

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
        {/* Loading State */}
        {loading && (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="w-8 h-8 animate-spin text-primary" />
            <span className="ml-3 text-text-secondary">Loading objects...</span>
          </div>
        )}

        {/* Error State */}
        {error && !loading && (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 mb-4">
            <p className="text-red-800">{error}</p>
          </div>
        )}

        {/* Results */}
        {!loading && !error && (
          <>
            {/* Download Buttons */}
            {spectra.length > 0 && (
              <DownloadTableButtons
                totalCount={totalCount}
                filters={filters}
                sortColumn={sortColumn}
                sortDirection={sortDirection}
              />
            )}

            {/* Empty State */}
            {spectra.length === 0 ? (
              <div className="text-center py-16 bg-card border border-border rounded-lg">
                <p className="text-text-secondary">
                  No results found.
                </p>
                <p className="text-text-secondary text-sm mt-2">
                  If you&apos;re looking for proprietary data, you may need to enter an access code on your profile page.
                </p>
              </div>
            ) : (
              <SpectraTable
                spectra={spectra}
                total={totalCount}
                page={isFullDataset ? 1 : page}
                pageSize={isFullDataset ? spectra.length : pageSize}
                totalPages={isFullDataset ? 1 : totalPages}
                onPageChange={handlePageChange}
                onPageSizeChange={handlePageSizeChange}
                isFullDataset={isFullDataset}
                sortColumn={sortColumn}
                sortDirection={sortDirection}
                onSortChange={handleSortChange}
                hasCoordinateSearch={filters.coordinate_search !== null}
                currentFilterParams={filtersToURLParams(filters, page, pageSize, sortColumn, sortDirection)}
              />
            )}
          </>
        )}
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
        <span className="ml-3 text-text-secondary">Loading...</span>
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
