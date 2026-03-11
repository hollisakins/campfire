'use client';

import React, { useState, useMemo, useCallback, useEffect } from 'react';
import { useSearchParams, useRouter, usePathname } from 'next/navigation';
import type { MapLayer, MapMarker } from '@/lib/actions/map';
import { MapViewerWrapper } from '@/components/map/MapViewerWrapper';
import { AdvancedFiltersPanel } from '@/components/spectra/AdvancedFiltersPanel';
import type { AdvancedFilterOptions } from '@/components/spectra/SpectraFilterBar';
import { parseFiltersFromURL, filtersToURLParams } from '@/lib/utils/url-params';
import { useDebouncedValue } from '@/lib/hooks/useDebouncedValue';
import { useFilterOptionsQuery } from '@/lib/hooks/useFilterOptionsQuery';
import { useFilteredObjectIds } from '@/lib/hooks/useFilteredObjectIds';

interface MapPageContentProps {
  layers: MapLayer[];
  initialField?: string;
  initialFilter?: string;
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  highlightObjectId?: string;
}

export function MapPageContent({
  layers,
  initialField,
  initialFilter,
  initialCenter,
  initialZoom,
  highlightObjectId,
}: MapPageContentProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Parse filter state from URL
  const initialFilters = useMemo(() => parseFiltersFromURL(searchParams), [searchParams]);

  // Filter state
  const [filters, setFilters] = useState<AdvancedFilterOptions>(initialFilters);
  const [panelOpen, setPanelOpen] = useState(false);
  const [fieldObservations, setFieldObservations] = useState<string[]>([]);

  // Debounce filters for queries
  const { debouncedValue: debouncedFilters } = useDebouncedValue(filters, 300);

  // Check if any filters are active
  const hasActiveFilters = useMemo(() => {
    return (
      filters.programs.length > 0 ||
      filters.fields.length > 0 ||
      filters.observations.length > 0 ||
      filters.redshift_quality.length > 0 ||
      filters.redshift_min !== null ||
      filters.redshift_max !== null ||
      (filters.gratings?.length ?? 0) > 0 ||
      filters.coordinate_search !== null ||
      filters.max_snr_min !== null ||
      filters.max_snr_max !== null ||
      filters.max_exposure_time_min !== null ||
      filters.max_exposure_time_max !== null ||
      (filters.spectral_features?.length ?? 0) > 0 ||
      (filters.object_flags?.length ?? 0) > 0 ||
      (filters.dq_flags?.length ?? 0) > 0 ||
      filters.search.length > 0
    );
  }, [filters]);

  // Fetch filter options (programs, fields)
  const { data: filterOptionsResult } = useFilterOptionsQuery(true);
  const availablePrograms = filterOptionsResult?.programs ?? [];

  // Fetch filtered object IDs when filters are active
  const { data: filteredResult } = useFilteredObjectIds(debouncedFilters, hasActiveFilters);

  // Build the ID set and marker filter function
  const filteredIdSet = useMemo(() => {
    if (!hasActiveFilters || !filteredResult?.objectIds) return null;
    return new Set(filteredResult.objectIds);
  }, [hasActiveFilters, filteredResult]);

  const markerFilter = useMemo(() => {
    if (!filteredIdSet) return undefined;
    return (marker: MapMarker) => filteredIdSet.has(marker.object_id);
  }, [filteredIdSet]);

  // Handle filter changes
  const handleFilterChange = useCallback((newFilters: AdvancedFilterOptions) => {
    setFilters(newFilters);
  }, []);

  // Track selected field and its observations (derived from loaded markers)
  const handleFieldChange = useCallback((_field: string, observations: string[]) => {
    setFieldObservations(observations);
  }, []);

  // Sync filter state to URL (preserving map-specific params).
  // Only updates the URL when filter params actually change — map params
  // (field, ra, dec, z, etc.) are managed by MapViewer separately.
  useEffect(() => {
    const filterParams = filtersToURLParams(debouncedFilters);
    const currentUrl = new URL(window.location.href);

    // Extract current filter params from URL (everything that ISN'T a map param)
    const mapParamKeys = new Set(['field', 'filter', 'ra', 'dec', 'z', 'zoom', 'highlight']);
    const currentFilterEntries: [string, string][] = [];
    for (const [key, val] of currentUrl.searchParams) {
      if (!mapParamKeys.has(key)) currentFilterEntries.push([key, val]);
    }

    // Compare only the filter portion — sort both to avoid ordering differences
    const sortEntries = (entries: Iterable<[string, string]>) =>
      [...entries].sort(([a], [b]) => a.localeCompare(b)).map(([k, v]) => `${k}=${v}`).join('&');
    const newFilterStr = sortEntries(filterParams);
    const currentFilterStr = sortEntries(currentFilterEntries);

    if (newFilterStr !== currentFilterStr) {
      // Rebuild the full URL: preserve existing map params, replace filter params
      const newSearch = new URLSearchParams();
      for (const [key, val] of currentUrl.searchParams) {
        if (mapParamKeys.has(key)) newSearch.set(key, val);
      }
      for (const [key, val] of filterParams) {
        newSearch.set(key, val);
      }
      const newSearchStr = newSearch.toString();
      router.replace(`${pathname}${newSearchStr ? `?${newSearchStr}` : ''}`, { scroll: false });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedFilters, pathname, router]);

  return (
    <div className="h-[calc(100vh-72px)] relative">
      <MapViewerWrapper
        layers={layers}
        initialField={initialField}
        initialFilter={initialFilter}
        initialCenter={initialCenter}
        initialZoom={initialZoom}
        highlightObjectId={highlightObjectId}
        markerFilter={markerFilter}
        filteredIdSet={filteredIdSet}
        onOpenFilters={() => setPanelOpen(true)}
        hasActiveFilters={hasActiveFilters}
        onFieldChange={handleFieldChange}
      />
      <AdvancedFiltersPanel
        isOpen={panelOpen}
        onClose={() => setPanelOpen(false)}
        filters={filters}
        onFiltersChange={handleFilterChange}
        showBasicFilters={true}
        availablePrograms={availablePrograms}
        availableObservations={fieldObservations}
      />
    </div>
  );
}
