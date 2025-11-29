'use client';

import React from 'react';
import { X, Search, Loader2 } from 'lucide-react';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';
import { CoordinateSearchChip, CoordinateSearchValue } from '@/components/ui/CoordinateSearchChip';
import {
  REDSHIFT_QUALITY,
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
} from '@/lib/flags';
import type { Program } from '@/lib/types';

// Extended filter options for advanced filtering
export interface AdvancedFilterOptions {
  // Existing filters
  programs: number[];
  fields: string[];
  gratings: string[];
  redshift_quality: number[];
  // New filters
  coordinate_search: CoordinateSearchValue | null;
  redshift_min: number | null;
  redshift_max: number | null;
  spectral_features: number[];
  object_flags: number[];
  dq_flags: number[];
  inspected_only: boolean | null;
  search: string;
}

export const DEFAULT_FILTERS: AdvancedFilterOptions = {
  programs: [],
  fields: [],
  gratings: [],
  redshift_quality: [],
  coordinate_search: null,
  redshift_min: null,
  redshift_max: null,
  spectral_features: [],
  object_flags: [],
  dq_flags: [],
  inspected_only: null,
  search: '',
};

interface SpectraFilterBarProps {
  filters: AdvancedFilterOptions;
  onFiltersChange: (filters: AdvancedFilterOptions) => void;
  availablePrograms: Program[];
  availableFields: string[];
  className?: string;
  isSearchDebouncing?: boolean;
}

const GRATINGS = ['PRISM', 'G140M', 'G235M', 'G395M'];

const INSPECTION_OPTIONS: FilterOption[] = [
  { value: 'inspected', label: 'Inspected only' },
  { value: 'not_inspected', label: 'Not inspected only' },
];

export const SpectraFilterBar: React.FC<SpectraFilterBarProps> = ({
  filters,
  onFiltersChange,
  availablePrograms,
  availableFields,
  className = '',
  isSearchDebouncing = false,
}) => {
  const updateFilter = <K extends keyof AdvancedFilterOptions>(
    key: K,
    value: AdvancedFilterOptions[K]
  ) => {
    onFiltersChange({ ...filters, [key]: value });
  };

  // Convert programs to filter options
  const programOptions: FilterOption[] = availablePrograms.map((p) => ({
    value: p.program_id,
    label: p.program_name || `Program ${p.program_id}`,
  }));

  // Convert fields to filter options
  const fieldOptions: FilterOption[] = availableFields.map((f) => ({
    value: f,
    label: f,
  }));

  // Convert gratings to filter options
  const gratingOptions: FilterOption[] = GRATINGS.map((g) => ({
    value: g,
    label: g,
  }));

  // Convert quality options
  const qualityOptions: FilterOption[] = REDSHIFT_QUALITY.map((q) => ({
    value: q.value,
    label: q.label,
    icon: q.icon,
    color: q.color,
  }));

  // Convert spectral features
  const spectralFeatureOptions: FilterOption[] = SPECTRAL_FEATURES.map((f) => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  // Convert object flags
  const objectFlagOptions: FilterOption[] = OBJECT_FLAGS.map((f) => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  // Convert DQ flags
  const dqFlagOptions: FilterOption[] = DQ_FLAGS.map((f) => ({
    value: f.value,
    label: f.label,
    icon: f.icon,
    color: f.color,
  }));

  // Check if any filters are active
  const hasActiveFilters =
    filters.programs.length > 0 ||
    filters.fields.length > 0 ||
    filters.gratings.length > 0 ||
    filters.redshift_quality.length > 0 ||
    filters.redshift_min !== null ||
    filters.redshift_max !== null ||
    filters.spectral_features.length > 0 ||
    filters.object_flags.length > 0 ||
    filters.dq_flags.length > 0 ||
    filters.inspected_only !== null ||
    filters.search.length > 0;

  const handleClearAll = () => {
    onFiltersChange(DEFAULT_FILTERS);
  };

  // Handle inspection filter (single select with special values)
  const handleInspectionChange = (selected: (string | number)[]) => {
    if (selected.length === 0) {
      updateFilter('inspected_only', null);
    } else {
      updateFilter('inspected_only', selected[0] === 'inspected');
    }
  };

  const inspectionSelected: (string | number)[] =
    filters.inspected_only === null
      ? []
      : filters.inspected_only
        ? ['inspected']
        : ['not_inspected'];

  return (
    <div className={`space-y-3 ${className}`}>
      {/* Search bar */}
      <div className="relative max-w-md">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary" />
        <input
          type="text"
          value={filters.search}
          onChange={(e) => updateFilter('search', e.target.value)}
          placeholder="Search by Object ID..."
          className="w-full pl-10 pr-10 py-2 text-sm border border-border rounded-lg bg-background text-text-primary placeholder:text-text-secondary focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
        />
        {/* Show loading spinner when debouncing, or clear button when there's text */}
        {isSearchDebouncing ? (
          <div className="absolute right-3 top-1/2 -translate-y-1/2 text-primary">
            <Loader2 className="w-4 h-4 animate-spin" />
          </div>
        ) : filters.search ? (
          <button
            onClick={() => updateFilter('search', '')}
            className="absolute right-3 top-1/2 -translate-y-1/2 text-text-secondary hover:text-text-primary"
          >
            <X className="w-4 h-4" />
          </button>
        ) : null}
      </div>

      {/* Filter chips */}
      <div className="flex flex-wrap items-center gap-2">
        {/* Coordinate search filter */}
        <CoordinateSearchChip
          value={filters.coordinate_search}
          onChange={(value) => updateFilter('coordinate_search', value)}
        />

        {/* Program filter */}
        <FilterChip
          label="Program"
          options={programOptions}
          selected={filters.programs}
          onChange={(selected) => updateFilter('programs', selected as number[])}
        />

        {/* Field filter */}
        <FilterChip
          label="Field"
          options={fieldOptions}
          selected={filters.fields}
          onChange={(selected) => updateFilter('fields', selected as string[])}
        />

        {/* Grating filter */}
        <FilterChip
          label="Grating"
          options={gratingOptions}
          selected={filters.gratings}
          onChange={(selected) => updateFilter('gratings', selected as string[])}
        />

        {/* Divider */}
        <div className="h-6 w-px bg-border mx-1" />

        {/* Redshift range filter */}
        <RangeFilterChip
          label="Redshift"
          min={filters.redshift_min}
          max={filters.redshift_max}
          onChange={(min, max) => {
            onFiltersChange({ ...filters, redshift_min: min, redshift_max: max });
          }}
          minBound={0}
          maxBound={15}
          step={0.1}
          precision={2}
        />

        {/* Quality filter */}
        <FilterChip
          label="Quality"
          options={qualityOptions}
          selected={filters.redshift_quality}
          onChange={(selected) => updateFilter('redshift_quality', selected as number[])}
        />

        {/* Inspection status filter */}
        <FilterChip
          label="Inspected"
          options={INSPECTION_OPTIONS}
          selected={inspectionSelected}
          onChange={handleInspectionChange}
          multiSelect={false}
        />

        {/* Divider */}
        <div className="h-6 w-px bg-border mx-1" />

        {/* Spectral features filter */}
        <FilterChip
          label="Features"
          options={spectralFeatureOptions}
          selected={filters.spectral_features}
          onChange={(selected) => updateFilter('spectral_features', selected as number[])}
        />

        {/* Object flags filter */}
        <FilterChip
          label="Object Type"
          options={objectFlagOptions}
          selected={filters.object_flags}
          onChange={(selected) => updateFilter('object_flags', selected as number[])}
        />

        {/* DQ flags filter */}
        <FilterChip
          label="Data Quality"
          options={dqFlagOptions}
          selected={filters.dq_flags}
          onChange={(selected) => updateFilter('dq_flags', selected as number[])}
        />

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
