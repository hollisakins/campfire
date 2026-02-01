'use client';

import React, { useState, useEffect, useRef } from 'react';
import { X, Search, ChevronDown } from 'lucide-react';
import { FilterChip, FilterOption } from '@/components/ui/FilterChip';
import { FilterChipWithMode, type FilterMode } from '@/components/ui/FilterChipWithMode';
import { RangeFilterChip } from '@/components/ui/RangeFilterChip';
import { CoordinateSearchChip, CoordinateSearchValue } from '@/components/ui/CoordinateSearchChip';
import {
  REDSHIFT_QUALITY,
  SPECTRAL_FEATURES,
  OBJECT_FLAGS,
  DQ_FLAGS,
} from '@/lib/flags';
import type { Program } from '@/lib/types';

// Re-export FilterMode for use by other modules
export type { FilterMode } from '@/components/ui/FilterChipWithMode';

// Search scope type for the search bar
export type SearchScope = 'object_id' | 'my_comments' | 'all_comments';

// Extended filter options for advanced filtering
export interface AdvancedFilterOptions {
  // Existing filters
  programs: number[];
  fields: string[];
  gratings: string[];
  observations: string[];
  redshift_quality: number[];
  // New filters
  coordinate_search: CoordinateSearchValue | null;
  redshift_min: number | null;
  redshift_max: number | null;
  max_snr_min: number | null;
  max_snr_max: number | null;
  spectral_features: number[];
  object_flags: number[];
  dq_flags: number[];
  inspected_only: boolean | null;
  search: string;
  search_scope: SearchScope;
  // Filter modes (any/all/none)
  gratings_mode: FilterMode;
  spectral_features_mode: FilterMode;
  object_flags_mode: FilterMode;
  dq_flags_mode: FilterMode;
}

export const DEFAULT_FILTERS: AdvancedFilterOptions = {
  programs: [],
  fields: [],
  gratings: [],
  observations: [],
  redshift_quality: [],
  coordinate_search: null,
  redshift_min: null,
  redshift_max: null,
  max_snr_min: null,
  max_snr_max: null,
  spectral_features: [],
  object_flags: [],
  dq_flags: [],
  inspected_only: null,
  search: '',
  search_scope: 'object_id',
  // Default modes (any = match any selected option)
  gratings_mode: 'any',
  spectral_features_mode: 'any',
  object_flags_mode: 'any',
  dq_flags_mode: 'any',
};

// Search scope options with labels and placeholders
const SEARCH_SCOPE_OPTIONS: { value: SearchScope; label: string; placeholder: string }[] = [
  { value: 'object_id', label: 'Object ID', placeholder: 'Search by Object ID...' },
  { value: 'my_comments', label: 'My Comments', placeholder: 'Search my comments...' },
  { value: 'all_comments', label: 'All Comments', placeholder: 'Search all comments...' },
];

interface SpectraFilterBarProps {
  filters: AdvancedFilterOptions;
  onFiltersChange: (filters: AdvancedFilterOptions) => void;
  availablePrograms: Program[];
  availableFields: string[];
  availableObservations: string[];
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
  availableObservations,
  className = '',
  isSearchDebouncing = false,
}) => {
  // Local state for search input to keep it responsive during typing
  const [localSearch, setLocalSearch] = useState(filters.search);
  // Local state for scope dropdown
  const [scopeDropdownOpen, setScopeDropdownOpen] = useState(false);
  const scopeDropdownRef = useRef<HTMLDivElement>(null);

  // Track the last value WE sent to the parent to distinguish our echoes from external changes
  const lastSentValueRef = useRef(filters.search);

  // Sync from props ONLY for external changes (e.g., URL navigation), not our own echoes
  useEffect(() => {
    // If this is the value we just sent, it's our own echo - ignore it
    if (filters.search === lastSentValueRef.current) {
      return;
    }
    // External change (e.g., URL navigation) - sync to local state
    setLocalSearch(filters.search);
    lastSentValueRef.current = filters.search;
  }, [filters.search]);

  // Debounce updates to parent - only propagate after 300ms of no typing
  useEffect(() => {
    // Skip if values are already in sync
    if (localSearch === filters.search) return;

    const timer = setTimeout(() => {
      // Update the ref BEFORE calling parent to mark this as "our" change
      lastSentValueRef.current = localSearch;
      onFiltersChange({ ...filters, search: localSearch });
    }, 300);

    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [localSearch]); // Intentionally exclude filters and onFiltersChange to prevent loops

  // Close scope dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (scopeDropdownRef.current && !scopeDropdownRef.current.contains(event.target as Node)) {
        setScopeDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Get current scope option
  const currentScope = SEARCH_SCOPE_OPTIONS.find(s => s.value === filters.search_scope) || SEARCH_SCOPE_OPTIONS[0];

  // Handle scope change
  const handleScopeChange = (scope: SearchScope) => {
    onFiltersChange({ ...filters, search_scope: scope });
    setScopeDropdownOpen(false);
  };

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

  // Convert observations to filter options
  const observationOptions: FilterOption[] = availableObservations.map((o) => ({
    value: o,
    label: o,
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
    filters.observations.length > 0 ||
    filters.redshift_quality.length > 0 ||
    filters.redshift_min !== null ||
    filters.redshift_max !== null ||
    filters.max_snr_min !== null ||
    filters.max_snr_max !== null ||
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
      {/* Search bar with scope dropdown */}
      <div className="relative max-w-md flex" ref={scopeDropdownRef}>
        {/* Scope dropdown button */}
        <div className="relative">
          <button
            onClick={() => setScopeDropdownOpen(!scopeDropdownOpen)}
            className="flex items-center gap-1 px-3 py-2 text-sm border border-r-0 border-border dark:border-slate-700 rounded-l-lg bg-card dark:bg-slate-800 text-text-primary dark:text-slate-100 hover:bg-card-hover dark:hover:bg-slate-700 transition-colors"
          >
            <span className="whitespace-nowrap">{currentScope.label}</span>
            <ChevronDown className={`w-3.5 h-3.5 transition-transform ${scopeDropdownOpen ? 'rotate-180' : ''}`} />
          </button>
          {/* Dropdown menu */}
          {scopeDropdownOpen && (
            <div className="absolute top-full left-0 mt-1 w-40 bg-background dark:bg-slate-800 border border-border dark:border-slate-700 rounded-lg shadow-lg z-50 py-1">
              {SEARCH_SCOPE_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  onClick={() => handleScopeChange(option.value)}
                  className={`w-full text-left px-3 py-2 text-sm hover:bg-card-hover dark:hover:bg-slate-700 transition-colors ${
                    filters.search_scope === option.value
                      ? 'text-primary font-medium'
                      : 'text-text-primary dark:text-slate-100'
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>
          )}
        </div>
        {/* Search input */}
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-text-secondary dark:text-slate-400" />
          <input
            type="text"
            value={localSearch}
            onChange={(e) => setLocalSearch(e.target.value)}
            placeholder={currentScope.placeholder}
            className="w-full pl-10 pr-10 py-2 text-sm border border-border dark:border-slate-700 rounded-r-lg bg-background dark:bg-slate-800 text-text-primary dark:text-slate-100 placeholder:text-text-secondary dark:placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-primary focus:border-transparent"
          />
          {/* Show clear button when there's text */}
          {localSearch && (
            <button
              onClick={() => setLocalSearch('')}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200"
            >
              <X className="w-4 h-4" />
            </button>
          )}
        </div>
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

        {/* Grating filter - with mode toggle (objects can have multiple gratings) */}
        <FilterChipWithMode
          label="Grating"
          options={gratingOptions}
          selected={filters.gratings}
          onChange={(selected) => updateFilter('gratings', selected as string[])}
          mode={filters.gratings_mode}
          onModeChange={(mode) => updateFilter('gratings_mode', mode)}
          showModeToggle={true}
        />

        {/* Observation filter */}
        <FilterChip
          label="Observation"
          options={observationOptions}
          selected={filters.observations}
          onChange={(selected) => updateFilter('observations', selected as string[])}
        />

        {/* Divider */}
        <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />

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

        {/* Max S/N range filter */}
        <RangeFilterChip
          label="Max S/N"
          min={filters.max_snr_min}
          max={filters.max_snr_max}
          onChange={(min, max) => {
            onFiltersChange({ ...filters, max_snr_min: min, max_snr_max: max });
          }}
          minBound={0}
          maxBound={100}
          step={0.1}
          precision={1}
          quickRanges={[
            { label: '>3', min: 3, max: null },
            { label: '>5', min: 5, max: null },
            { label: '>10', min: 10, max: null },
            { label: '>25', min: 25, max: null },
            { label: '>50', min: 50, max: null },
          ]}
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
        <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />

        {/* Spectral features filter - with mode toggle (bitmask) */}
        <FilterChipWithMode
          label="Features"
          options={spectralFeatureOptions}
          selected={filters.spectral_features}
          onChange={(selected) => updateFilter('spectral_features', selected as number[])}
          mode={filters.spectral_features_mode}
          onModeChange={(mode) => updateFilter('spectral_features_mode', mode)}
          showModeToggle={true}
        />

        {/* Object flags filter - with mode toggle (bitmask) */}
        <FilterChipWithMode
          label="Object Type"
          options={objectFlagOptions}
          selected={filters.object_flags}
          onChange={(selected) => updateFilter('object_flags', selected as number[])}
          mode={filters.object_flags_mode}
          onModeChange={(mode) => updateFilter('object_flags_mode', mode)}
          showModeToggle={true}
        />

        {/* DQ flags filter - with mode toggle (bitmask) */}
        <FilterChipWithMode
          label="Data Quality"
          options={dqFlagOptions}
          selected={filters.dq_flags}
          onChange={(selected) => updateFilter('dq_flags', selected as number[])}
          mode={filters.dq_flags_mode}
          onModeChange={(mode) => updateFilter('dq_flags_mode', mode)}
          showModeToggle={true}
        />

        {/* Clear all button */}
        {hasActiveFilters && (
          <>
            <div className="h-6 w-px bg-border dark:bg-slate-700 mx-1" />
            <button
              onClick={handleClearAll}
              className="inline-flex items-center gap-1 px-3 py-1.5 text-sm text-text-secondary dark:text-slate-400 hover:text-text-primary dark:hover:text-slate-200 transition-colors"
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
