'use client';

import React from 'react';
import { X } from 'lucide-react';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';

export interface NircamFilterOptions {
  fields: string[];
  tiles: string[];
  filters: string[];
  pixel_scales: string[];
  extensions: string[];
  epochs: string[];
}

export const DEFAULT_NIRCAM_FILTERS: NircamFilterOptions = {
  fields: [],
  tiles: [],
  filters: [],
  pixel_scales: [],
  extensions: [],
  epochs: [],
};

// Label for a raw epoch value: '' (the full-field mosaic) reads as "Full field".
const epochLabel = (e: string) => (e === '' ? 'Full field' : e);

interface NircamFilterBarProps {
  filterState: NircamFilterOptions;
  onFiltersChange: (filters: NircamFilterOptions) => void;
  availableFields: string[];
  availableTiles: string[];
  availableFilters: string[];
  availablePixelScales: string[];
  availableExtensions: string[];
  availableEpochs: string[];
  className?: string;
}

export const NircamFilterBar: React.FC<NircamFilterBarProps> = ({
  filterState,
  onFiltersChange,
  availableFields,
  availableTiles,
  availableFilters,
  availablePixelScales,
  availableExtensions,
  availableEpochs,
  className = '',
}) => {
  const updateFilter = <K extends keyof NircamFilterOptions>(
    key: K,
    value: NircamFilterOptions[K]
  ) => {
    onFiltersChange({ ...filterState, [key]: value });
  };

  // Convert to filter options
  const fieldOptions: FilterOption[] = availableFields.map((f) => ({
    value: f,
    label: f.toUpperCase(),
  }));

  const tileOptions: FilterOption[] = availableTiles.map((t) => ({
    value: t,
    label: t,
  }));

  const filterOptions: FilterOption[] = availableFilters.map((f) => ({
    value: f,
    label: f.toUpperCase(),
  }));

  const pixelScaleOptions: FilterOption[] = availablePixelScales.map((p) => ({
    value: p,
    label: p,
  }));

  const extensionOptions: FilterOption[] = availableExtensions.map((e) => ({
    value: e,
    label: e.toUpperCase(),
  }));

  const epochOptions: FilterOption[] = availableEpochs.map((e) => ({
    value: e,
    label: epochLabel(e),
  }));

  // Only surface the Epoch facet when the data actually has a named epoch
  // (something other than the full-field ''); otherwise it's noise.
  const hasEpochFacet = availableEpochs.some((e) => e !== '');

  // Check if any filters are active
  const hasActiveFilters =
    filterState.fields.length > 0 ||
    filterState.tiles.length > 0 ||
    filterState.filters.length > 0 ||
    filterState.pixel_scales.length > 0 ||
    filterState.extensions.length > 0 ||
    filterState.epochs.length > 0;

  const handleClearAll = () => {
    onFiltersChange(DEFAULT_NIRCAM_FILTERS);
  };

  return (
    <div className={`space-y-3 ${className}`}>
      {/* Filter chips */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Field filter */}
        <FilterChip
          label="Field"
          options={fieldOptions}
          selected={filterState.fields}
          onChange={(selected) => updateFilter('fields', selected as string[])}
        />

        {/* Filter (wavelength) filter */}
        <FilterChip
          label="Filter"
          options={filterOptions}
          selected={filterState.filters}
          onChange={(selected) => updateFilter('filters', selected as string[])}
        />

        {/* Tile filter */}
        <FilterChip
          label="Tile"
          options={tileOptions}
          selected={filterState.tiles}
          onChange={(selected) => updateFilter('tiles', selected as string[])}
        />

        {/* Divider */}
        <div className="h-6 w-px bg-border mx-1" />

        {/* Pixel scale filter */}
        <FilterChip
          label="Pixel Scale"
          options={pixelScaleOptions}
          selected={filterState.pixel_scales}
          onChange={(selected) => updateFilter('pixel_scales', selected as string[])}
        />

        {/* Extension filter */}
        <FilterChip
          label="Extension"
          options={extensionOptions}
          selected={filterState.extensions}
          onChange={(selected) => updateFilter('extensions', selected as string[])}
        />

        {/* Epoch filter (only when named epochs exist) */}
        {hasEpochFacet && (
          <FilterChip
            label="Epoch"
            options={epochOptions}
            selected={filterState.epochs}
            onChange={(selected) => updateFilter('epochs', selected as string[])}
          />
        )}

        {/* Clear all button */}
        {hasActiveFilters && (
          <>
            <div className="h-6 w-px bg-border mx-1" />
            <button
              onClick={handleClearAll}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary hover:text-text-primary transition-colors"
            >
              <X className="w-3.5 h-3.5" />
              Clear all
            </button>
          </>
        )}
      </div>
    </div>
  );
};
