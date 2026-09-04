'use client';

import React, { useState, useMemo, useCallback, useEffect } from 'react';
import { useSearchParams, usePathname } from 'next/navigation';
import type { MapLayer, MapObjectMarker, FitsglDataset } from '@/lib/actions/map';
import { MapViewerWrapper } from '@/components/map/MapViewerWrapper';
import { AdvancedFiltersPanel } from '@/components/spectra/AdvancedFiltersPanel';
import type { FilterOptions } from '@/lib/actions/filter-params';
import { parseFiltersFromURL, filtersToURLParams } from '@/lib/utils/url-params';
import { useDebouncedValue } from '@/lib/hooks/useDebouncedValue';
import { useFilterOptionsQuery } from '@/lib/hooks/useFilterOptionsQuery';
import { useAuth } from '@/lib/contexts/AuthContext';
import { useFilteredObjectIds } from '@/lib/hooks/useFilteredObjectIds';
import { useFieldObjectMarkers } from '@/lib/hooks/useFieldObjectMarkers';

interface MapPageContentProps {
  layers: MapLayer[];
  fitsglDatasets: FitsglDataset[];
  initialField?: string;
  initialFilter?: string;
  initialCenter?: { ra: number; dec: number };
  initialZoom?: number;
  highlightObjectId?: string;
}

export function MapPageContent({
  layers,
  fitsglDatasets,
  initialField,
  initialFilter,
  initialCenter,
  initialZoom,
  highlightObjectId,
}: MapPageContentProps) {
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Parse filter state from URL
  const initialFilters = useMemo(() => parseFiltersFromURL(searchParams), [searchParams]);

  // Filter state
  const [filters, setFilters] = useState<FilterOptions>(initialFilters);
  const [panelOpen, setPanelOpen] = useState(false);
  const [currentField, setCurrentField] = useState<string | undefined>(initialField);

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
      (filters.list_ids?.length ?? 0) > 0 ||
      (filters.dq_flags?.length ?? 0) > 0 ||
      filters.search.length > 0
    );
  }, [filters]);

  // Start the marker query with the page, not with the lazily loaded map
  // chunk (perf T1-6 / #502): same TanStack key as MapViewer's own call, so
  // the two dedupe and the map finds the markers warm. The field guess
  // mirrors MapViewer's default (URL field, else the first field by name).
  const guessedField = useMemo(() => {
    if (currentField) return currentField;
    const set = new Set<string>();
    for (const l of layers) set.add(l.field);
    for (const d of fitsglDatasets) if (d.kind === 'field') set.add(d.field);
    return [...set].sort()[0];
  }, [currentField, layers, fitsglDatasets]);
  useFieldObjectMarkers(guessedField);

  // Fetch filter options (programs, fields) — once auth has resolved to a
  // user; the route answers 401 to anonymous callers.
  const { user, loading: authLoading } = useAuth();
  const { data: filterOptionsResult } = useFilterOptionsQuery(!authLoading && !!user);
  const availablePrograms = filterOptionsResult?.programs ?? [];

  // Scope filter query to the current map field so the RPC only returns
  // objects visible on this field (avoids fetching IDs across all fields).
  const queryFilters = useMemo(() => {
    if (!currentField || debouncedFilters.fields.length > 0) return debouncedFilters;
    return { ...debouncedFilters, fields: [currentField] };
  }, [debouncedFilters, currentField]);

  // Fetch filtered object IDs when filters are active
  const { data: filteredResult } = useFilteredObjectIds(queryFilters, hasActiveFilters);

  // Build the ID set and marker filter function
  const filteredIdSet = useMemo(() => {
    if (!hasActiveFilters || !filteredResult?.objectIds) return null;
    return new Set(filteredResult.objectIds);
  }, [hasActiveFilters, filteredResult]);

  const markerFilter = useMemo(() => {
    if (!filteredIdSet) return undefined;
    return (marker: MapObjectMarker) => filteredIdSet.has(marker.object_id);
  }, [filteredIdSet]);

  // Handle filter changes
  const handleFilterChange = useCallback((newFilters: FilterOptions) => {
    setFilters(newFilters);
  }, []);

  // Track selected field
  const handleFieldChange = useCallback((field: string) => {
    setCurrentField(field);
  }, []);

  // Sync filter state to URL (preserving map-specific params).
  // Uses history.replaceState (not router.replace) to avoid triggering a
  // Next.js soft navigation on this force-dynamic page, which would
  // re-execute the server component and could race with in-flight filter
  // queries. This matches how MapViewer syncs map params (zoom, pan).
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
      const url = new URL(window.location.href);
      // Clear non-map params
      for (const key of [...url.searchParams.keys()]) {
        if (!mapParamKeys.has(key)) url.searchParams.delete(key);
      }
      // Add filter params
      for (const [key, val] of filterParams) {
        url.searchParams.set(key, val);
      }
      window.history.replaceState(null, '', url.toString());
    }
  }, [debouncedFilters, pathname]);

  return (
    <div className="h-[calc(100vh-72px)] relative">
      <MapViewerWrapper
        layers={layers}
        fitsglDatasets={fitsglDatasets}
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
        availableObservations={[]}
      />
    </div>
  );
}
